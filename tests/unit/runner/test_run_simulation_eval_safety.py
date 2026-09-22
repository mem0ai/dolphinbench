from __future__ import annotations

import shutil
import os
import json
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness import run_simulation as runner


class RunSimulationEvalSafetyTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"DOLPHINBENCH_AGENT_IMAGE": "sha256:" + "0" * 64})
        environment.start()
        self.addCleanup(environment.stop)

    def test_seed_only_is_an_explicit_runner_mode(self) -> None:
        args = runner.build_arg_parser().parse_args([
            "--persona", "morgan",
            "--provider", "mem0",
            "--out", "/tmp/results.json",
            "--seed-only",
        ])
        self.assertTrue(args.seed_only)
        self.assertFalse(args.skip_seeding)

    def test_seed_only_accepts_a_bounded_number_of_new_messages(self) -> None:
        args = runner.build_arg_parser().parse_args([
            "--persona", "morgan",
            "--provider", "hindsight",
            "--out", "/tmp/results.json",
            "--seed-only",
            "--max-new-seed-items", "100",
        ])
        self.assertEqual(100, args.max_new_seed_items)

    def test_bounded_seed_only_resume_delivers_each_message_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sim_path = root / "sim.yaml"
            sim_path.write_text(
                "name: bounded-seed-resume\n"
                "sessions:\n"
                "  - id: seed-1\n"
                "    label: Seed\n"
                "    messages: [one, two, three, four]\n"
                "test_queries:\n"
                "  - id: '001'\n"
            )
            baseline = root / "baseline.json"
            baseline.write_text("{}")
            out_path = root / "results.json"
            persona = SimpleNamespace(
                id="morgan", life_sim=sim_path,
                mock_manifest=root / "manifest.yaml", baseline_state=baseline,
            )
            delivered: list[str] = []

            def fake_run_hermes(_profile, message, **_kwargs):
                delivered.append(message)
                receipt_path = Path(os.environ["DOLPHINBENCH_HINDSIGHT_SEED_RECEIPTS"])
                with receipt_path.open("a") as receipt:
                    receipt.write(json.dumps({
                        "provider": "hindsight",
                        "operation_kind": "automatic_sync_turn",
                        "seed_item_id": os.environ["DOLPHINBENCH_SEED_ITEM_ID"],
                    }) + "\n")
                return SimpleNamespace(
                    ok=True, error=None, status_code=None, retry_after_seconds=None,
                    session_id="seed-session", session_path=None,
                    available_tools=[
                        "mcp_dolphinbench_apps_send_email", "memory", "session_search",
                        "hindsight_recall", "hindsight_reflect", "hindsight_retain",
                    ],
                    tool_calls=[], response_text="seeded", token_usage={}, model=None,
                    stdout="", stderr="",
                )

            common_env = {
                "DOLPHINBENCH_HERMES_PROFILE": "test-hindsight",
                "DOLPHINBENCH_HERMES_TOOLSETS": (
                    "dolphinbench-apps,memory,session_search,hindsight"
                ),
                "DOLPHINBENCH_COST_LEDGER_PATH": str(root / "ledger.jsonl"),
            }
            first = runner.build_arg_parser().parse_args([
                "--persona", "morgan", "--sim", str(sim_path),
                "--provider", "hindsight", "--out", str(out_path),
                "--seed-only", "--max-new-seed-items", "2",
            ])
            second = runner.build_arg_parser().parse_args([
                "--persona", "morgan", "--sim", str(sim_path),
                "--provider", "hindsight", "--out", str(out_path),
                "--seed-only", "--max-new-seed-items", "2", "--resume",
            ])
            with patch.object(runner, "resolve_persona_paths", return_value=persona), \
                    patch.object(runner, "run_hermes", side_effect=fake_run_hermes), \
                    patch.object(runner, "reset_mock_mcp"), \
                    patch.object(runner, "load_test", side_effect=AssertionError("must not load tests")), \
                    patch.object(runner, "load_pricing", return_value={}), \
                    patch.object(runner, "pricing_version", return_value="test"), \
                    patch.object(runner.time, "sleep"), \
                    patch.dict(os.environ, common_env, clear=False):
                self.assertEqual(runner.run_simulation(first), 0)
                partial = json.loads(out_path.read_text())
                self.assertFalse(partial["summary"]["seed_complete"])
                self.assertNotIn("ended", partial)
                self.assertNotIn("phase1_ended_at", partial)
                self.assertIn("last_seed_group_ended_at", partial)

                self.assertEqual(runner.run_simulation(second), 0)

            complete = json.loads(out_path.read_text())
            self.assertEqual(delivered, ["one", "two", "three", "four"])
            self.assertEqual(len(complete["seed_calls"]), 4)
            self.assertTrue(complete["summary"]["seed_complete"])
            self.assertIn("ended", complete)
            self.assertIn("phase1_ended_at", complete)
            self.assertEqual(complete["test_results"], [])

    def test_partial_loader_accepts_only_legacy_incomplete_seed_only_ended_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.json"
            legacy_partial = {
                "simulation": "smoke", "provider": "hindsight", "run_mode": "seed_only",
                "ended": "2026-01-01T00:00:00+00:00",
                "phase1_ended_at": "2026-01-01T00:00:00+00:00",
                "summary": {"seed_complete": False},
                "seed_sessions": [], "seed_calls": [], "test_results": [],
            }
            runner._atomic_write_json(path, legacy_partial)
            loaded = runner._load_partial_results(path, "smoke", "hindsight")
            self.assertNotIn("ended", loaded)
            self.assertNotIn("phase1_ended_at", loaded)
            self.assertEqual(loaded["last_seed_group_ended_at"], legacy_partial["ended"])

            for completed in (
                {**legacy_partial, "summary": {"seed_complete": True}},
                {**legacy_partial, "run_mode": "evaluation"},
            ):
                runner._atomic_write_json(path, completed)
                with self.assertRaisesRegex(RuntimeError, "completed run"):
                    runner._load_partial_results(path, "smoke", "hindsight")

    def test_resume_writes_only_new_progress_and_preserves_recovery_and_failure(self) -> None:
        for old_count, fail_next, recover_receipt in (
            (1, False, False), (60, False, False),
            (60, True, False), (60, False, True),
        ):
            with self.subTest(old_count=old_count, fail=fail_next, recovery=recover_receipt), \
                    tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                sim_path = root / "sim.json"
                sim_path.write_text(json.dumps({
                    "name": "resume-progress",
                    "sessions": [
                        {"id": f"old-{i}", "messages": [f"old message {i}"]}
                        for i in range(old_count)
                    ] + [{"id": "new", "messages": ["new one", "new two"]}],
                    "test_queries": [],
                }))
                baseline = root / "baseline.json"
                baseline.write_text("{}")
                out_path = root / "results.json"
                persona = SimpleNamespace(
                    id="morgan", life_sim=sim_path,
                    mock_manifest=root / "manifest.yaml", baseline_state=baseline,
                )
                delivered = []
                writes = []
                resuming = False

                def fake_run_hermes(_profile, message, **_kwargs):
                    if resuming:
                        # Startup plus a recovered session, never every old session.
                        self.assertEqual(len(writes), 2 if recover_receipt else 1)
                        self.assertEqual(len(writes[0]["seed_calls"]), old_count)
                    delivered.append(message)
                    failed = resuming and fail_next
                    if not failed:
                        receipt = Path(os.environ["DOLPHINBENCH_HINDSIGHT_SEED_RECEIPTS"])
                        with receipt.open("a") as handle:
                            handle.write(json.dumps({
                                "provider": "hindsight",
                                "operation_kind": "automatic_sync_turn",
                                "seed_item_id": os.environ["DOLPHINBENCH_SEED_ITEM_ID"],
                            }) + "\n")
                    return SimpleNamespace(
                        ok=not failed, error="intentional failure" if failed else None,
                        status_code=None, retry_after_seconds=None,
                        session_id="seed-session", session_path=None,
                        available_tools=[
                            "mcp_dolphinbench_apps_send_email", "memory", "session_search",
                            "hindsight_recall", "hindsight_reflect", "hindsight_retain",
                        ],
                        tool_calls=[], response_text="seeded", token_usage={}, model=None,
                        stdout="", stderr="",
                    )

                def record_write(path, payload):
                    if path == out_path:
                        writes.append(copy.deepcopy(payload))
                    original_write(path, payload)

                argv = ["--persona", "morgan", "--sim", str(sim_path),
                        "--provider", "hindsight", "--out", str(out_path),
                        "--seed-only", "--no-capture-memory", "--max-new-seed-items"]
                env = {
                    "DOLPHINBENCH_HERMES_PROFILE": "test-hindsight",
                    "DOLPHINBENCH_HERMES_TOOLSETS": "dolphinbench-apps,memory,session_search,hindsight",
                    "DOLPHINBENCH_COST_LEDGER_PATH": str(root / "ledger.jsonl"),
                }
                original_write = runner._atomic_write_json
                with patch.object(runner, "resolve_persona_paths", return_value=persona), \
                        patch.object(runner, "run_hermes", side_effect=fake_run_hermes), \
                        patch.object(runner, "reset_mock_mcp"), \
                        patch.object(runner, "load_test", side_effect=AssertionError("must not load tests")), \
                        patch.object(runner, "load_pricing", return_value={}), \
                        patch.object(runner, "pricing_version", return_value="test"), \
                        patch.object(runner.time, "sleep"), \
                        patch.dict(os.environ, env, clear=False):
                    self.assertEqual(runner.run_simulation(
                        runner.build_arg_parser().parse_args(argv + [str(old_count)])), 0)
                    if recover_receipt:
                        saved = json.loads(out_path.read_text())
                        saved["seed_calls"].pop()
                        saved["seed_sessions"].pop()
                        original_write(out_path, saved)
                    resuming = True
                    args = runner.build_arg_parser().parse_args(argv + ["1", "--resume"])
                    with patch.object(runner, "_atomic_write_json", side_effect=record_write):
                        if fail_next:
                            with self.assertRaisesRegex(RuntimeError, "seed delivery is ambiguous"):
                                runner.run_simulation(args)
                        else:
                            self.assertEqual(runner.run_simulation(args), 0)
                self.assertEqual(delivered, [f"old message {i}" for i in range(old_count)] + ["new one"])
                saved = json.loads(out_path.read_text())
                self.assertEqual(len(saved["seed_calls"]), old_count + 1)
                self.assertEqual(len(saved["seed_sessions"]), old_count)
                self.assertLessEqual(len(writes), 5)
                if fail_next:
                    self.assertEqual(saved["seed_calls"][-1]["driver_error"], "intentional failure")
                else:
                    self.assertTrue(saved["seed_calls"][-1]["seed_message_delivered"])
                    self.assertFalse(saved["summary"]["seed_complete"])

    def test_seed_only_exits_before_loading_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sim_path = root / "sim.yaml"
            sim_path.write_text(
                "name: seed-only\n"
                "sessions: []\n"
                "test_queries:\n"
                "  - id: '001'\n"
            )
            baseline = root / "baseline.json"
            baseline.write_text("{}")
            out_path = root / "results.json"
            persona = SimpleNamespace(
                id="morgan",
                life_sim=sim_path,
                mock_manifest=root / "manifest.yaml",
                baseline_state=baseline,
            )
            args = SimpleNamespace(
                persona="morgan",
                sim=str(sim_path),
                provider="builtin",
                out=str(out_path),
                verbose=False,
                skip_seeding=False,
                seed_only=True,
                resume=False,
                read_only_test_memory=True,
                isolate_hermes_test_sessions=True,
                only="",
                capture_memory=False,
                model=None,
            )
            with patch.object(runner, "resolve_persona_paths", return_value=persona), \
                    patch.object(runner, "_guard_hermes_profile_tools"), \
                    patch.object(runner, "reset_mock_mcp"), \
                    patch.object(runner, "_copy_hermes_profile"), \
                    patch.object(runner, "load_test", side_effect=AssertionError("must not load tests")), \
                    patch.dict(os.environ, {
                        "DOLPHINBENCH_HERMES_PROFILE": "seed-only-profile",
                        "DOLPHINBENCH_HERMES_TOOLSETS": (
                            "dolphinbench-apps,memory,session_search"
                        ),
                        "DOLPHINBENCH_COST_LEDGER_PATH": str(root / "ledger.jsonl"),
                    }, clear=False):
                self.assertEqual(runner.run_simulation(args), 0)
            saved = json.loads(out_path.read_text())
            self.assertEqual(saved["run_mode"], "seed_only")
            self.assertEqual(saved["test_results"], [])
            self.assertNotIn("phase2_started_at", saved)

    def test_test_narrative_time_requires_per_test_anchor(self) -> None:
        self.assertEqual(
            runner._test_narrative_time({"narrative_anchor_date": "2026-09-14"}),
            "2026-09-14",
        )
        with self.assertRaisesRegex(RuntimeError, "narrative_anchor_date"):
            runner._test_narrative_time({})

    def test_hermes_safety_defaults_and_refusal(self) -> None:
        args = runner.build_arg_parser().parse_args(
            ["--provider", "builtin", "--out", "results.json"]
        )
        self.assertTrue(args.read_only_test_memory)
        self.assertTrue(args.isolate_hermes_test_sessions)
        runner._require_hermes_test_safety("builtin", True, True)
        runner._require_hermes_test_safety("dummy", False, False)
        with self.assertRaisesRegex(SystemExit, "read-only Phase 2 memory"):
            runner._require_hermes_test_safety("builtin", False, True)
        with self.assertRaisesRegex(SystemExit, "fresh copy"):
            runner._require_hermes_test_safety("builtin", True, False)

        with patch.dict(
            os.environ,
            {
                "DOLPHINBENCH_HERMES_TOOLSETS": (
                    "dolphinbench-apps,memory,session_search,unknown-toolset"
                )
            },
            clear=False,
        ):
            runner._guard_hermes_profile_tools("unused", "builtin")
            os.environ.pop("DOLPHINBENCH_AGENT_IMAGE")
            with self.assertRaisesRegex(SystemExit, "isolated Hermes image"):
                runner._guard_hermes_profile_tools("unused", "builtin")

    def test_seed_delivery_prevents_timeout_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "session-1.json"
            session_path.write_text('{"turns": []}')
            delivered_timeout = SimpleNamespace(
                ok=False,
                session_id="session-1",
                session_path=str(session_path),
            )
            missing_trace = SimpleNamespace(
                ok=False,
                session_id="session-2",
                session_path=str(Path(tmp) / "missing.json"),
            )
            not_delivered = SimpleNamespace(
                ok=False,
                session_id=None,
                session_path=None,
            )
            self.assertTrue(runner._seed_message_was_delivered("builtin", delivered_timeout))
            self.assertFalse(runner._seed_message_was_delivered("builtin", missing_trace))
            self.assertFalse(runner._seed_message_was_delivered("builtin", not_delivered))
        self.assertTrue(runner._failure_details("HTTP 429: rate limited").transient)

    def test_transient_failures_use_status_and_retry_after(self) -> None:
        limited = SimpleNamespace(
            error="HTTP 429: rate limited",
            status_code=429,
            retry_after_seconds=17,
        )
        details = runner._failure_details(limited)
        self.assertTrue(details.transient)
        self.assertEqual(details.status_code, 429)
        self.assertEqual(runner._retry_delay(details, 1), 17)
        for status in (408, 409, 500, 502, 503, 504):
            self.assertTrue(
                runner._failure_details(f"HTTP {status}: transient").transient,
                status,
            )
        self.assertTrue(runner._failure_details("connection refused").transient)
        self.assertFalse(runner._failure_details("content filter refusal").transient)

    def test_seed_delivery_states_do_not_replay_uncertain_messages(self) -> None:
        self.assertEqual(
            runner._seed_delivery_state(
                "builtin", SimpleNamespace(ok=False, session_id=None, session_path=None,
                                             error="timeout after 180s"),
            ),
            "ambiguous",
        )
        self.assertEqual(
            runner._seed_delivery_state(
                "builtin", SimpleNamespace(ok=False, session_id=None, session_path=None,
                                             error="HTTP 429: rate limited", status_code=429),
            ),
            "known_not_delivered",
        )

    def test_partial_results_keep_completed_units_and_flag_ambiguous_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial.json"
            payload = {
                "simulation": "smoke", "provider": "builtin", "profile": "p",
                "seed_sessions": [],
                "seed_calls": [
                    {"session_id": "s1", "message_index": 0,
                     "seed_delivery_state": "delivered"},
                    {"session_id": "s1", "message_index": 1,
                     "seed_delivery_state": "ambiguous"},
                ],
                "test_results": [{"test_id": "001"}],
            }
            runner._atomic_write_json(path, payload)
            loaded = runner._load_partial_results(path, "smoke", "builtin")
            self.assertEqual(runner._completed_seed_keys(loaded), {("s1", 0)})
            self.assertEqual(runner._ambiguous_seed_keys(loaded), {("s1", 1)})
            self.assertEqual(runner._completed_test_ids(loaded), {"001"})

    def test_provider_receipt_is_the_only_provider_seed_delivery_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt_path = Path(tmp) / "mem0.jsonl"
            receipt_path.write_text(json.dumps({
                "provider": "mem0",
                "operation_kind": "automatic_sync_turn",
                "seed_item_id": "s1:0",
            }) + "\n")
            expected = {"s1:0": ("s1", 0)}
            status = runner._validate_seed_receipts(
                receipt_path, "mem0", expected, require_all=True,
            )
            self.assertEqual(status["completed_seed_keys"], {("s1", 0)})
            failed_driver = SimpleNamespace(
                ok=False, error="timeout", session_id=None, session_path=None,
            )
            self.assertEqual(
                runner._seed_delivery_state(
                    "mem0", failed_driver, matching_seed_receipt=True,
                ),
                "delivered",
            )
            self.assertEqual(
                runner._seed_delivery_state("mem0", failed_driver), "ambiguous",
            )
            successful_outer_call = SimpleNamespace(
                ok=True, error=None, session_id="session-1", session_path=None,
            )
            self.assertEqual(
                runner._seed_delivery_state("mem0", successful_outer_call),
                "ambiguous",
            )

    def test_receipt_recovers_seed_after_crash_before_results_write(self) -> None:
        expected = {"s1:0": ("s1", 0), "s2:0": ("s2", 0)}
        results = {"seed_calls": [], "seed_sessions": [], "test_results": []}
        runner._receipt_resume_seed_calls(results, expected, {("s1", 0)})
        self.assertEqual(len(results["seed_calls"]), 1)
        recovered = results["seed_calls"][0]
        self.assertEqual(recovered["seed_item_id"], "s1:0")
        self.assertTrue(recovered["recovered_from_seed_receipt"])
        self.assertTrue(recovered["matching_seed_receipt"])
        self.assertIn("cost_unavailable_reason", recovered)
        self.assertEqual(
            runner._receipt_ambiguous_seed_keys(results, {("s1", 0)}), set(),
        )

        results["seed_calls"][0]["seed_delivery_state"] = "ambiguous"
        runner._receipt_resume_seed_calls(results, expected, {("s1", 0)})
        self.assertEqual(results["seed_calls"][0]["seed_delivery_state"], "delivered")
        self.assertTrue(results["seed_calls"][0]["matching_seed_receipt"])

        already_recorded = {
            "seed_calls": [{
                "session_id": "s1",
                "message_index": 0,
                "seed_delivery_state": "delivered",
            }],
        }
        runner._receipt_resume_seed_calls(already_recorded, expected, {("s1", 0)})
        self.assertNotIn(
            "recovered_from_seed_receipt", already_recorded["seed_calls"][0],
        )

    def test_receipts_reject_duplicate_missing_and_unexpected_automatic_items(self) -> None:
        expected = {"s1:0": ("s1", 0), "s2:0": ("s2", 0)}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "honcho.jsonl"

            def write_records(*item_ids: str) -> None:
                path.write_text("".join(json.dumps({
                    "provider": "honcho",
                    "operation_kind": "automatic_sync_turn",
                    "seed_item_id": item_id,
                }) + "\n" for item_id in item_ids))

            write_records("s1:0", "s1:0")
            with self.assertRaisesRegex(RuntimeError, "duplicate automatic honcho"):
                runner._validate_seed_receipts(path, "honcho", expected, require_all=False)

            write_records("s1:0", "other:0")
            with self.assertRaisesRegex(RuntimeError, "unexpected automatic honcho"):
                runner._validate_seed_receipts(path, "honcho", expected, require_all=False)

            write_records("s1:0")
            with self.assertRaisesRegex(RuntimeError, "missing automatic honcho"):
                runner._validate_seed_receipts(path, "honcho", expected, require_all=True)

    def test_receipt_file_allows_resume_before_results_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "run.json"
            receipt_path = runner._seed_receipt_path(out_path, "honcho")
            assert receipt_path is not None
            self.assertFalse(runner._can_resume_without_results(out_path, "honcho"))
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text("old run\n")
            self.assertTrue(
                runner._prepare_seed_receipt_file(receipt_path, resuming=False)
            )
            self.assertEqual(receipt_path.read_text(), "")
            self.assertFalse(out_path.exists())
            self.assertTrue(runner._can_resume_without_results(out_path, "honcho"))
            self.assertFalse(runner._can_resume_without_results(out_path, "builtin"))

            receipt_path.write_text("durable receipt\n")
            self.assertTrue(
                runner._prepare_seed_receipt_file(receipt_path, resuming=True)
            )
            self.assertEqual(receipt_path.read_text(), "durable receipt\n")

    def test_no_results_file_resume_recovers_provider_seed_without_wiping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sim_path = root / "sim.yaml"
            sim_path.write_text(
                "name: receipt-resume\n"
                "sessions:\n"
                "  - id: seed-1\n"
                "    label: Seed\n"
                "    messages: [hello]\n"
                "test_queries: []\n"
            )
            baseline = root / "baseline.json"
            baseline.write_text("{}")
            out_path = root / "resume.json"
            receipt_path = runner._seed_receipt_path(out_path, "mem0")
            assert receipt_path is not None
            receipt_path.write_text(json.dumps({
                "provider": "mem0",
                "operation_kind": "automatic_sync_turn",
                "seed_item_id": "seed-1:0",
            }) + "\n")
            persona = SimpleNamespace(
                id="morgan", life_sim=sim_path,
                mock_manifest=root / "manifest.yaml", baseline_state=baseline,
            )
            args = SimpleNamespace(
                persona="morgan", sim=str(sim_path), provider="mem0", out=str(out_path),
                verbose=False, skip_seeding=False, resume=True, read_only_test_memory=True,
                isolate_hermes_test_sessions=True, only="", capture_memory=False,
                model=None,
            )
            with patch.object(runner, "resolve_persona_paths", return_value=persona), \
                    patch.object(runner, "run_hermes", side_effect=AssertionError("must not seed")), \
                    patch.object(runner, "_wait_for_mem0_event_log", return_value={"completed": 1}), \
                    patch.object(runner, "_copy_hermes_profile"), \
                    patch.object(runner, "_safe_snapshot", return_value={}), \
                    patch.dict(os.environ, {
                        "DOLPHINBENCH_HERMES_PROFILE": "receipt-profile",
                        "DOLPHINBENCH_COST_LEDGER_PATH": str(root / "ledger.jsonl"),
                    }, clear=False):
                self.assertEqual(runner.run_simulation(args), 0)
            recovered = json.loads(out_path.read_text())
            self.assertTrue(recovered["seed_calls"][0]["recovered_from_seed_receipt"])
            self.assertEqual(recovered["seed_receipts"]["automatic_receipt_count"], 1)

    def test_unknown_ledger_model_does_not_become_zero_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.jsonl"
            ledger.write_text(
                '{"ts": 10, "model": "unknown", "input_tokens": 3, "output_tokens": 2}\n'
            )
            result = runner._read_cost_ledger(
                ledger,
                "1970-01-01T00:00:00+00:00",
                None,
                "1970-01-01T00:00:20+00:00",
                {"models": {}},
            )
            self.assertIsNone(result["total_cost"])
            self.assertEqual(result["cost_unavailable_calls"], 1)
            self.assertIsNone(result["by_model"]["unknown"]["cost"])

    def test_transient_test_failure_is_retried_from_clean_profile_and_app_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sim_path = root / "sim.yaml"
            sim_path.write_text("name: retry-test\nsessions: []\ntest_queries:\n  - id: '001'\n")
            baseline = root / "baseline.json"
            baseline.write_text('{"records": ["clean"]}')
            profile_root = root / "profiles"
            (profile_root / "eval").mkdir(parents=True)
            state = root / "state.json"
            log = root / "calls.jsonl"
            tool_config = root / "tools.json"
            persona = SimpleNamespace(
                id="morgan", life_sim=sim_path, mock_manifest=root / "manifest.yaml",
                baseline_state=baseline,
            )
            attempts: list[str] = []

            def fake_run_hermes(*_args, **_kwargs):
                self.assertIn(
                    "memory",
                    os.environ["DOLPHINBENCH_HERMES_TOOLSETS"].split(","),
                )
                attempts.append(log.read_text())
                if len(attempts) == 1:
                    state.write_text('{"records": ["dirty"]}')
                    log.write_text("first attempt")
                    return SimpleNamespace(
                        ok=False, error="HTTP 429: rate limited", status_code=429,
                        retry_after_seconds=0, session_id=None, session_path=None,
                        available_tools=[], tool_calls=[], response_text="", token_usage={}, model=None,
                    )
                self.assertEqual(state.read_text(), baseline.read_text())
                return SimpleNamespace(
                    ok=True, error=None, status_code=None, retry_after_seconds=None,
                    session_id="test-session", session_path=None,
                    available_tools=[
                        "mcp_dolphinbench_apps_lookup", "session_search"
                    ],
                    tool_calls=[], response_text="done", token_usage={}, model=None,
                )

            args = SimpleNamespace(
                persona="morgan", sim=str(sim_path), provider="builtin", out=str(root / "out.json"),
                verbose=False, skip_seeding=True, resume=False, read_only_test_memory=True,
                isolate_hermes_test_sessions=True, only="", capture_memory=False,
                model=None,
            )
            with patch.object(runner, "HERMES_PROFILES_DIR", profile_root), \
                    patch.object(runner, "MOCK_STATE_PATH", state), \
                    patch.object(runner, "MOCK_LOG_PATH", log), \
                    patch.object(runner, "MOCK_TOOL_CONFIG", tool_config), \
                    patch.object(runner, "resolve_persona_paths", return_value=persona), \
                    patch.object(runner, "load_test", return_value={
                        "test": "do it", "narrative_anchor_date": "2026-01-01",
                        "mock_state": {"records": ["clean"]}, "grade": {"type": "regex", "config": {}},
                    }), \
                    patch.object(runner, "run_hermes", side_effect=fake_run_hermes), \
                    patch.object(runner, "grade", return_value={"passed": True, "score": 1.0}), \
                    patch("harness.hermes_driver._is_dolphinbench_mock_tool_name", return_value=True), \
                    patch.object(runner, "reset_mock_mcp"), \
                    patch.object(runner.time, "sleep"), \
                    patch.dict(os.environ, {
                        "DOLPHINBENCH_HERMES_PROFILE": "eval",
                        "DOLPHINBENCH_HERMES_TOOLSETS": (
                            "dolphinbench-apps,memory,session_search"
                        ),
                        "DOLPHINBENCH_COST_LEDGER_PATH": str(root / "ledger.jsonl"),
                    }, clear=False):
                self.assertEqual(runner.run_simulation(args), 0)
            saved = json.loads((root / "out.json").read_text())
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts, ["", ""])
            self.assertEqual(len(saved["test_results"]), 1)
            self.assertEqual(len(saved["test_results"][0]["attempts"]), 2)

    def test_isolation_profile_names_include_absolute_output_identity(self) -> None:
        first = runner._hermes_isolation_base("dolphinbench-builtin", Path("a/results.json"))
        second = runner._hermes_isolation_base("dolphinbench-builtin", Path("b/results.json"))
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(first), 58)
        self.assertLessEqual(len(f"{first}-t001"), 64)

    def test_archives_trace_before_profile_clone_can_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clone = root / "clone"
            clone.mkdir()
            session = clone / "session.json"
            session.write_text('{"turns": []}')
            trace = runner._archive_hermes_session(
                str(session), None, root / "traces", "test_001.json"
            )
            shutil.rmtree(clone)
            self.assertIsNotNone(trace)
            self.assertEqual(Path(trace).read_text(), '{"turns": []}')

    def test_archives_only_one_state_database_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.db"
            connection = sqlite3.connect(state)
            connection.executescript("""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, system_prompt TEXT,
                    system_prompt_hash TEXT, model TEXT
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                    content TEXT, reasoning TEXT, reasoning_details TEXT,
                    tool_calls TEXT
                );
                CREATE TABLE session_model_usage (
                    session_id TEXT, model TEXT, billing_provider TEXT,
                    billing_base_url TEXT, billing_mode TEXT, task TEXT,
                    input_tokens INTEGER
                );
                CREATE TABLE system_prompts (hash TEXT PRIMARY KEY, prompt TEXT);
            """)
            connection.execute(
                "INSERT INTO system_prompts VALUES (?, ?)", ("prompt-1", "system text"),
            )
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                ("keep", "system text", "prompt-1", "model"),
            )
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                ("omit", "x" * 1_000_000, None, "model"),
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (1, "keep", "assistant", "answer", "summary", "details", "[]"),
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (2, "omit", "user", "x" * 1_000_000, None, None, None),
            )
            connection.execute(
                "INSERT INTO session_model_usage VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("keep", "model", "azure", "url", "responses", "agent", 10),
            )
            connection.commit()
            connection.close()

            trace = runner._archive_hermes_session(
                str(state), "keep", root / "traces", "keep.json",
            )

            self.assertIsNotNone(trace)
            payload = json.loads(Path(trace).read_text())
            self.assertEqual(payload["session"]["id"], "keep")
            self.assertEqual(payload["messages"][0]["reasoning"], "summary")
            self.assertEqual(payload["messages"][0]["reasoning_details"], "details")
            self.assertEqual(payload["system_prompt"]["prompt"], "system text")
            self.assertEqual(payload["model_usage"][0]["input_tokens"], 10)
            self.assertNotIn("omit", Path(trace).read_text())
            self.assertLess(Path(trace).stat().st_size, 20_000)

    def test_mem0_barrier_rejects_absent_or_empty_event_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.jsonl"
            with self.assertRaisesRegex(RuntimeError, "event log is missing"):
                runner._wait_for_mem0_event_log(missing)
            empty = Path(tmp) / "empty.jsonl"
            empty.write_text("")
            with self.assertRaisesRegex(RuntimeError, "no completion events"):
                runner._wait_for_mem0_event_log(empty)

    def test_mem0_barrier_rejects_agent_scoped_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_log = Path(tmp) / "events.jsonl"
            event_log.write_text(json.dumps({
                "event_id": "event-1",
                "user_id": "user-1",
                "agent_id": "hermes",
            }) + "\n")
            with self.assertRaisesRegex(RuntimeError, "outside this run's memory scope"):
                runner._wait_for_mem0_event_log(
                    event_log,
                    expected_user_id="user-1",
                )

    def test_honcho_plugin_errors_and_cross_provider_tools_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_root = Path(tmp)
            log = profile_root / "honcho" / "logs" / "agent.log"
            log.parent.mkdir(parents=True)
            log.write_text("ERROR plugins.memory.honcho ingestion failed\n")
            with patch.object(runner, "HERMES_PROFILES_DIR", profile_root):
                with self.assertRaisesRegex(RuntimeError, "Honcho seeding"):
                    runner._require_healthy_honcho("honcho", "seeding")

        contaminated = SimpleNamespace(
            available_tools=[
                "mcp_dolphinbench_apps_send_email",
                "memory",
                "session_search",
                "mem0_search",
            ]
        )
        with self.assertRaisesRegex(SystemExit, "different memory provider"):
            runner._validate_hermes_runtime_tools(
                contaminated, "seed", "seed-1", "builtin"
            )

        unknown = SimpleNamespace(
            available_tools=[
                "mcp_dolphinbench_apps_send_email",
                "memory",
                "session_search",
                "terminal",
            ]
        )
        runner._validate_hermes_runtime_tools(unknown, "seed", "seed-2", "builtin")

        fake_mock_prefix = SimpleNamespace(
            available_tools=[
                "mcp_dolphinbench_apps_not_in_any_manifest",
                "memory",
                "session_search",
            ]
        )
        with self.assertRaisesRegex(SystemExit, "no DolphinBench mock tools"):
            runner._validate_hermes_runtime_tools(
                fake_mock_prefix, "seed", "seed-3", "builtin"
            )

    def test_mem0_test_surface_hides_native_memory_and_provider_writes(self) -> None:
        read_only = SimpleNamespace(
            available_tools=[
                "mcp_dolphinbench_apps_send_email",
                "session_search",
                "search_memories",
            ]
        )
        runner._validate_hermes_runtime_tools(read_only, "test", "106", "mem0")

        writable = SimpleNamespace(
            available_tools=[
                *read_only.available_tools,
                "save_memory",
            ]
        )
        with self.assertRaisesRegex(SystemExit, "memory write tools"):
            runner._validate_hermes_runtime_tools(writable, "test", "106", "mem0")

        native_write = SimpleNamespace(
            available_tools=[*read_only.available_tools, "memory"]
        )
        with self.assertRaisesRegex(SystemExit, "writable native memory tool"):
            runner._validate_hermes_runtime_tools(native_write, "test", "106", "mem0")

    def test_mem0_seed_surface_accepts_all_current_provider_tools(self) -> None:
        seeded = SimpleNamespace(
            available_tools=[
                "mcp_dolphinbench_apps_send_email",
                "memory",
                "session_search",
                "search_memories",
                "save_memory",
                "update_memory",
                "delete_memory",
            ]
        )
        runner._validate_hermes_runtime_tools(seeded, "seed", "107", "mem0")

    def test_mem0_official_search_name_satisfies_runtime_check(self) -> None:
        retired = SimpleNamespace(
            available_tools=[
                "mcp_dolphinbench_apps_send_email",
                "session_search",
                "mem0_search",
            ]
        )
        runner._validate_hermes_runtime_tools(retired, "test", "108", "mem0")

    def test_mem0_official_write_names_are_only_allowed_during_ingestion(self) -> None:
        retired = SimpleNamespace(
            available_tools=[
                "mcp_dolphinbench_apps_send_email",
                "memory",
                "session_search",
                "search_memories",
                "mem0_add",
                "mem0_update",
                "mem0_delete",
            ]
        )
        runner._validate_hermes_runtime_tools(retired, "seed", "109", "mem0")
        retired.available_tools.remove("memory")
        with self.assertRaisesRegex(SystemExit, "memory write tools"):
            runner._validate_hermes_runtime_tools(retired, "test", "109", "mem0")

    def test_supermemory_test_surface_rejects_provider_writes(self) -> None:
        read_only = SimpleNamespace(
            available_tools=[
                "mcp_dolphinbench_apps_send_email",
                "session_search",
                "supermemory_search",
            ]
        )
        runner._validate_hermes_runtime_tools(
            read_only, "test", "106", "supermemory"
        )

        writable = SimpleNamespace(
            available_tools=[
                *read_only.available_tools,
                "supermemory_store",
            ]
        )
        with self.assertRaisesRegex(SystemExit, "memory write tools"):
            runner._validate_hermes_runtime_tools(
                writable, "test", "106", "supermemory"
            )

    def test_honcho_surface_accepts_latest_reasoning_tool(self) -> None:
        latest_honcho = SimpleNamespace(
            available_tools=[
                "mcp_dolphinbench_apps_send_email",
                "session_search",
                "honcho_profile",
                "honcho_search",
                "honcho_context",
                "honcho_reasoning",
            ]
        )
        runner._validate_hermes_runtime_tools(
            latest_honcho, "test", "001", "honcho"
        )

    def test_cost_scope_reports_provider_fees_separately(self) -> None:
        self.assertIn("embeddings", runner._AGENT_INFERENCE_COST_SCOPE)


if __name__ == "__main__":
    unittest.main()
