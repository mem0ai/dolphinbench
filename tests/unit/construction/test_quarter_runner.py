from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import yaml

from construction import quarter_runner
from construction.checkpoints import load_checkpoint, save_checkpoint, validated_checkpoint_identity
from construction.quarter_runner import CONFIG_KEYS, _ledger_summary, quarter_windows, run, validate_config


class QuarterRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.review_calls: list[Path] = []

        def review(output_dir: Path, **_: object) -> dict:
            self.review_calls.append(output_dir)
            with (output_dir / "call_ledger.jsonl").open("a") as ledger:
                ledger.write('{"model":"gpt-5.6-sol","cache_hit":false}\n')
            return {
                "status": "passed",
                "validation": {
                    "valid": True,
                    "passed": True,
                    "errors": [],
                    "finding_count": 0,
                },
            }

        self.review_patch = patch(
            "construction.quarter_runner.history_quarter_review.run_review",
            side_effect=review,
        )
        self.review_patch.start()

    def tearDown(self) -> None:
        self.review_patch.stop()

    def state(self) -> dict:
        return {
            "history": [],
            "session_purposes": [],
            "entities": [{"id": "person:morgan", "name": "Morgan"}],
            "facts": [],
            "app_state": {},
            "unfinished_threads": [],
        }

    def save_checkpoint(self, path: Path, end_date: str) -> str:
        save_checkpoint(
            path=path,
            persona="morgan",
            week={"week_id": f"through-{end_date}", "end_date": end_date},
            previous_hash="previous",
            inputs={},
            state=self.state(),
            operation_results=[],
            covered_events=[],
            system_sha256="system",
            identity_version=2,
        )
        return validated_checkpoint_identity(path)

    def fixture(self, root: Path, *, checkpoint_date: str = "2025-03-31") -> dict:
        root.mkdir(parents=True, exist_ok=True)
        files = {
            "spec": root / "spec.yaml",
            "persona_sheet": root / "persona.md",
            "generator_context": root / "context.md",
            "overview": root / "overview.json",
            "quarter_plan": root / "quarter_plan.json",
        }
        files["spec"].write_text("persona: morgan\nnarrative_timezone: America/Los_Angeles\n")
        files["persona_sheet"].write_text("Morgan\n")
        files["generator_context"].write_text("World\n")
        files["overview"].write_text(json.dumps({"overview": {"story": "continue"}}) + "\n")
        files["quarter_plan"].write_text(
            json.dumps({"plan": {"period_start": "2025-04-01", "period_end": "2025-06-30", "events": []}}) + "\n"
        )
        checkpoint = root / "checkpoint"
        self.save_checkpoint(checkpoint, checkpoint_date)
        config = {
            "version": 1,
            "persona": "morgan",
            "quarter_id": "2025_q2",
            "quarter_start": "2025-04-01",
            "quarter_end": "2025-06-30",
            **{key: str(value) for key, value in files.items()},
            "starting_checkpoint": str(checkpoint),
        }
        config_path = root / "config.yaml"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False))
        return {"config": config, "config_path": config_path, "checkpoint": checkpoint}

    def arguments(self, fixture: dict, root: Path) -> argparse.Namespace:
        return argparse.Namespace(
            config=fixture["config_path"],
            out=root / "output",
            tool_python=Path(sys.executable),
            confirm_paid_calls=True,
        )

    def test_window_totals_accepts_one_complete_action_correction(self) -> None:
        totals = quarter_runner._window_totals(
            {
                "sessions": 18,
                "new_facts": 0,
                "app_operations": 12,
                "model_calls": 5,
                "generation_stages": [
                    "story_planning",
                    "occurrence_enrichment",
                    "unexecutable_contact_correction",
                    "corrected_occurrence_enrichment",
                    "session_writing",
                ],
            }
        )

        self.assertEqual(totals["model_calls"], 5)

    def test_window_totals_rejects_incomplete_action_correction(self) -> None:
        with self.assertRaisesRegex(ValueError, "both correction stages"):
            quarter_runner._window_totals(
                {
                    "sessions": 18,
                    "new_facts": 0,
                    "app_operations": 0,
                    "model_calls": 4,
                    "generation_stages": [
                        "story_planning",
                        "occurrence_enrichment",
                        "unexecutable_contact_correction",
                        "session_writing",
                    ],
                }
            )

    def test_stop_after_one_window_writes_paused_resumable_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self.fixture(root)
            args = self.arguments(fixture, root)
            args.stop_after_weeks = 1
            calls: list[argparse.Namespace] = []
            with patch("construction.quarter_runner.build_history_window.run", self.window_stage(calls)):
                manifest = run(args)

            self.assertEqual(len(calls), 1)
            self.assertEqual(manifest["status"], "paused")
            self.assertEqual(manifest["next_unbuilt_date"], "2025-04-08")
            self.assertEqual(
                manifest["next_unbuilt_window"],
                {"start_date": "2025-04-08", "end_date": "2025-04-14"},
            )
            self.assertEqual(len(manifest["windows"]), 1)
            self.assertEqual(manifest["windows"][0]["status"], "passed")
            self.assertEqual(manifest["totals"], {"sessions": 1, "new_facts": 2, "app_operations": 3, "model_calls": 3})
            self.assertFalse((root / "output" / "final_checkpoint").exists())

    def test_stop_after_one_window_resumes_at_next_unbuilt_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self.fixture(root)
            args = self.arguments(fixture, root)
            args.stop_after_weeks = 1
            with patch("construction.quarter_runner.build_history_window.run", self.window_stage([])):
                run(args)

            resumed_calls: list[argparse.Namespace] = []
            with patch("construction.quarter_runner.build_history_window.run", self.window_stage(resumed_calls)):
                run(args)

            self.assertEqual(len(resumed_calls), 1)
            self.assertEqual(
                (resumed_calls[0].start_date, resumed_calls[0].end_date),
                (date(2025, 4, 8), date(2025, 4, 14)),
            )
            self.assertEqual(
                resumed_calls[0].resume_from_checkpoint,
                root / "output" / "windows" / "2025-04-01_to_2025-04-07" / "candidate_checkpoint",
            )
            self.assertEqual(json.loads((root / "output" / "manifest.json").read_text())["status"], "paused")

    def test_story_only_pause_keeps_the_same_week_ready_for_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self.fixture(root)
            args = self.arguments(fixture, root)
            args.stop_after_story = True
            calls: list[argparse.Namespace] = []

            def story_stage(window_args: argparse.Namespace) -> dict:
                calls.append(window_args)
                window_args.out.mkdir(parents=True, exist_ok=True)
                (window_args.out / "story_response.json").write_text(
                    json.dumps({"plan": {"occurrences": [{"contact_id": "one"}]}})
                    + "\n"
                )
                with Path(os.environ["DOLPHINBENCH_CALL_LEDGER"]).open("a") as ledger:
                    ledger.write('{"model":"story","cache_hit":false}\n')
                manifest = {
                    "status": "story_only",
                    "persona": "morgan",
                    "period_start": window_args.start_date.isoformat(),
                    "period_end": window_args.end_date.isoformat(),
                    "starting_checkpoint_sha256": validated_checkpoint_identity(
                        window_args.resume_from_checkpoint
                    ),
                    "sessions": 1,
                    "model_calls": 1,
                }
                (window_args.out / "manifest.json").write_text(
                    json.dumps(manifest) + "\n"
                )
                return manifest

            with patch(
                "construction.quarter_runner.build_history_window.run", story_stage
            ):
                manifest = run(args)

            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0].stop_after_story)
            self.assertEqual(manifest["status"], "paused")
            self.assertEqual(manifest["pause_reason"], "story_review")
            self.assertEqual(manifest["next_unbuilt_date"], "2025-04-01")
            self.assertEqual(
                manifest["next_unbuilt_window"],
                {"start_date": "2025-04-01", "end_date": "2025-04-07"},
            )
            self.assertEqual(manifest["windows"][0]["status"], "story_only")
            self.assertEqual(self.review_calls, [])
            self.assertFalse((root / "output" / "final_checkpoint").exists())

            resumed_args = self.arguments(fixture, root)
            resumed_args.stop_after_weeks = 1
            resumed_calls: list[argparse.Namespace] = []
            with patch(
                "construction.quarter_runner.build_history_window.run",
                self.window_stage(resumed_calls),
            ):
                resumed = run(resumed_args)

            self.assertEqual(len(resumed_calls), 1)
            self.assertFalse(resumed_calls[0].stop_after_story)
            self.assertEqual(resumed_calls[0].start_date, date(2025, 4, 1))
            self.assertEqual(resumed["next_unbuilt_date"], "2025-04-08")

    def window_stage(self, calls: list[argparse.Namespace]):
        def stage(args: argparse.Namespace) -> dict:
            calls.append(args)
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "call_ledger.jsonl").write_text(
                '{"model":"story","cache_hit":false}\n'
                '{"model":"planner","cache_hit":false}\n'
                '{"model":"writer","cache_hit":false}\n'
            )
            _, state = load_checkpoint(args.resume_from_checkpoint)
            candidate = args.out / "candidate_checkpoint"
            save_checkpoint(
                path=candidate,
                persona="morgan",
                week={"week_id": f"{args.start_date}_to_{args.end_date}", "end_date": args.end_date.isoformat()},
                previous_hash=validated_checkpoint_identity(args.resume_from_checkpoint),
                inputs={},
                state=state,
                operation_results=[],
                covered_events=[],
                system_sha256="system",
                identity_version=2,
            )
            manifest = {
                "status": "passed",
                "persona": "morgan",
                "period_start": args.start_date.isoformat(),
                "period_end": args.end_date.isoformat(),
                "starting_checkpoint_sha256": validated_checkpoint_identity(
                    args.resume_from_checkpoint
                ),
                "sessions": 1,
                "new_facts": 2,
                "app_operations": 3,
                "model_calls": 3,
                "candidate_checkpoint_sha256": validated_checkpoint_identity(candidate),
            }
            (args.out / "manifest.json").write_text(json.dumps(manifest) + "\n")
            return manifest
        return stage

    def failed_output_root(self, root: Path, fixture: dict) -> Path:
        output = root / "output"
        output.mkdir()
        (output / "call_ledger.jsonl").write_text("")
        (output / "window_config.yaml").write_text(
            yaml.safe_dump(
                {
                    "persona": "morgan",
                    "quarter_start": "2025-04-01",
                    "quarter_end": "2025-06-30",
                    "spec": str(Path(fixture["config"]["spec"]).resolve()),
                    "persona_sheet": str(Path(fixture["config"]["persona_sheet"]).resolve()),
                    "generator_context": str(Path(fixture["config"]["generator_context"]).resolve()),
                    "overview": str(Path(fixture["config"]["overview"]).resolve()),
                    "quarter_plan": str(Path(fixture["config"]["quarter_plan"]).resolve()),
                },
                sort_keys=False,
            )
        )
        (output / "manifest.json").write_text(json.dumps({"status": "failed"}) + "\n")
        return output

    def test_config_contract_is_exact(self) -> None:
        self.assertEqual(
            CONFIG_KEYS,
            {
                "version", "persona", "quarter_id", "quarter_start", "quarter_end", "spec",
                "persona_sheet", "generator_context", "overview", "starting_checkpoint", "quarter_plan",
            },
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self.fixture(root)
            self.assertEqual(validate_config(fixture["config_path"])["accepted_through"], date(2025, 3, 31))
            fixture["config"]["extra"] = "no"
            fixture["config_path"].write_text(yaml.safe_dump(fixture["config"]))
            with self.assertRaisesRegex(ValueError, "config keys"):
                validate_config(fixture["config_path"])

    def test_config_rejects_a_plan_for_another_quarter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self.fixture(root)
            plan_path = Path(fixture["config"]["quarter_plan"])
            plan_path.write_text(
                json.dumps(
                    {
                        "plan": {
                            "period_start": "2025-07-01",
                            "period_end": "2025-09-30",
                            "events": [],
                        }
                    }
                )
                + "\n"
            )
            with self.assertRaisesRegex(ValueError, "quarter_plan dates"):
                validate_config(fixture["config_path"])

    def test_windows_are_consecutive_seven_day_periods_with_short_final_window(self) -> None:
        self.assertEqual(
            quarter_windows(date(2025, 4, 1), date(2025, 4, 16)),
            [(date(2025, 4, 1), date(2025, 4, 7)), (date(2025, 4, 8), date(2025, 4, 14)), (date(2025, 4, 15), date(2025, 4, 16))],
        )

    def test_runner_chains_checkpoints_uses_three_calls_and_writes_totals(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self.fixture(root)
            args = self.arguments(fixture, root)
            calls: list[argparse.Namespace] = []
            with patch("construction.quarter_runner.build_history_window.run", self.window_stage(calls)):
                manifest = run(args)

            self.assertEqual(len(calls), 13)
            self.assertEqual((calls[0].start_date, calls[0].end_date), (date(2025, 4, 1), date(2025, 4, 7)))
            self.assertEqual((calls[-1].start_date, calls[-1].end_date), (date(2025, 6, 24), date(2025, 6, 30)))
            for previous, current in zip(calls, calls[1:]):
                self.assertEqual(current.resume_from_checkpoint, previous.out / "candidate_checkpoint")
            self.assertEqual(
                manifest["model_calls"],
                {
                    "expected": 40,
                    "accepted_stage_calls": 40,
                    "paid_calls": 40,
                    "cache_hits": 0,
                    "ledger_entries": 40,
                    "actual": 40,
                },
            )
            self.assertEqual(manifest["totals"], {"sessions": 13, "new_facts": 26, "app_operations": 39, "model_calls": 40})
            self.assertEqual(len(manifest["windows"]), 13)
            self.assertEqual(validated_checkpoint_identity(root / "output" / "final_checkpoint"), manifest["final_checkpoint_identity"])
            window_config = yaml.safe_load((root / "output" / "window_config.yaml").read_text())
            self.assertEqual(set(window_config), {"persona", "quarter_start", "quarter_end", "spec", "persona_sheet", "generator_context", "overview", "quarter_plan"})
            self.assertEqual((root / "output" / "call_ledger.jsonl").read_text().count("\n"), 40)
            self.assertEqual(self.review_calls, [root / "output"])

    def test_failing_quarter_review_blocks_final_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self.fixture(root)
            self.review_patch.stop()
            self.review_patch = patch(
                "construction.quarter_runner.history_quarter_review.run_review",
                return_value={
                    "status": "failed",
                    "validation": {
                        "valid": True,
                        "passed": False,
                        "errors": [],
                        "finding_count": 1,
                    },
                },
            )
            self.review_patch.start()
            with patch("construction.quarter_runner.build_history_window.run", self.window_stage([])):
                with self.assertRaisesRegex(ValueError, "found 1 concrete defect"):
                    run(self.arguments(fixture, root))
            self.assertFalse((root / "output" / "final_checkpoint").exists())
            manifest = json.loads((root / "output" / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["stage"], "history_quarter_review")
            self.assertEqual(manifest["totals"]["model_calls"], 40)

    def test_runner_starts_after_an_existing_accepted_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self.fixture(root, checkpoint_date="2025-04-07")
            calls: list[argparse.Namespace] = []
            with patch("construction.quarter_runner.build_history_window.run", self.window_stage(calls)):
                manifest = run(self.arguments(fixture, root))
            self.assertEqual((calls[0].start_date, calls[0].end_date), (date(2025, 4, 8), date(2025, 4, 14)))
            self.assertEqual(manifest["model_calls"]["actual"], 37)

    def test_runner_stops_at_first_failed_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self.fixture(root)
            calls: list[argparse.Namespace] = []
            success = self.window_stage(calls)

            def fail_second(args: argparse.Namespace) -> dict:
                if len(calls) == 1:
                    calls.append(args)
                    args.out.mkdir(parents=True)
                    return {"status": "failed", "model_calls": 2}
                return success(args)

            with patch("construction.quarter_runner.build_history_window.run", fail_second):
                with self.assertRaisesRegex(ValueError, "history window did not pass"):
                    run(self.arguments(fixture, root))
            self.assertEqual(len(calls), 2)
            failed = json.loads((root / "output" / "manifest.json").read_text())
            self.assertEqual(failed["windows"][-1]["status"], "failed")
            self.assertEqual(failed["windows"][-1]["start_date"], "2025-04-08")

    def test_stop_after_all_weeks_does_not_run_quarter_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self.fixture(root)
            args = self.arguments(fixture, root)
            args.stop_after_weeks = 13
            with patch("construction.quarter_runner.build_history_window.run", self.window_stage([])):
                manifest = run(args)
            self.assertEqual(self.review_calls, [])
            self.assertEqual(manifest["status"], "paused")
            self.assertIsNone(manifest["next_unbuilt_date"])
            self.assertFalse((root / "output" / "final_checkpoint").exists())

    def test_failed_manifest_counts_calls_recorded_before_the_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self.fixture(root)

            def fail_after_calls(args: argparse.Namespace) -> dict:
                args.out.mkdir(parents=True, exist_ok=True)
                (args.out / "call_ledger.jsonl").write_text(
                    '{"model":"planner","cache_hit":false}\n'
                    '{"model":"writer","cache_hit":true}\n'
                )
                raise KeyError("reason")

            with patch(
                "construction.quarter_runner.build_history_window.run",
                fail_after_calls,
            ):
                with self.assertRaisesRegex(KeyError, "reason"):
                    run(self.arguments(fixture, root))

            failed = json.loads((root / "output" / "manifest.json").read_text())
            self.assertEqual(
                failed["model_calls"],
                {
                    "expected": 40,
                    "accepted_stage_calls": 0,
                    "paid_calls": 1,
                    "cache_hits": 1,
                    "ledger_entries": 2,
                    "actual": 1,
                },
            )
            self.assertEqual(failed["totals"]["model_calls"], 0)
            self.assertEqual(
                (root / "output" / "call_ledger.jsonl").read_text().count("\n"),
                2,
            )

    def test_runner_resumes_passed_prefix_without_duplicate_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self.fixture(root)
            args = self.arguments(fixture, root)
            first_calls: list[argparse.Namespace] = []
            success = self.window_stage(first_calls)

            def fail_second(window_args: argparse.Namespace) -> dict:
                if len(first_calls) == 1:
                    first_calls.append(window_args)
                    window_args.out.mkdir(parents=True, exist_ok=True)
                    return {"status": "failed", "model_calls": 2}
                return success(window_args)

            with patch("construction.quarter_runner.build_history_window.run", fail_second):
                with self.assertRaisesRegex(ValueError, "history window did not pass"):
                    run(args)

            second_calls: list[argparse.Namespace] = []
            with patch(
                "construction.quarter_runner.build_history_window.run",
                self.window_stage(second_calls),
            ):
                manifest = run(args)

            self.assertEqual(len(second_calls), 12)
            self.assertEqual(second_calls[0].start_date, date(2025, 4, 8))
            self.assertEqual(
                second_calls[0].resume_from_checkpoint,
                root / "output" / "windows" / "2025-04-01_to_2025-04-07" / "candidate_checkpoint",
            )
            self.assertEqual(
                manifest["model_calls"],
                {
                    "expected": 40,
                    "accepted_stage_calls": 40,
                    "paid_calls": 40,
                    "cache_hits": 0,
                    "ledger_entries": 40,
                    "actual": 40,
                },
            )
            self.assertEqual((root / "output" / "call_ledger.jsonl").read_text().count("\n"), 40)

    def test_ledger_summary_counts_paid_and_cached_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = Path(temp) / "call_ledger.jsonl"
            ledger.write_text(
                '{"cache_hit":false}\n{"cache_hit":true}\n{"cache_hit":false}\n'
            )
            self.assertEqual(
                _ledger_summary(ledger),
                {"paid_calls": 2, "cache_hits": 1, "ledger_entries": 3},
            )

    def test_ledger_summary_rejects_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = Path(temp) / "call_ledger.jsonl"
            ledger.write_text('{"cache_hit":false}\nnot-json\n')
            with self.assertRaisesRegex(ValueError, "row 2 is not valid JSON"):
                _ledger_summary(ledger)

    def test_ledger_summary_rejects_invalid_or_missing_cache_hit(self) -> None:
        for row in ('{"cache_hit":"false"}', '{}'):
            with self.subTest(row=row), tempfile.TemporaryDirectory() as temp:
                ledger = Path(temp) / "call_ledger.jsonl"
                ledger.write_text(row + "\n")
                with self.assertRaisesRegex(ValueError, "cache_hit must be a boolean"):
                    _ledger_summary(ledger)

    def test_existing_output_rejects_mismatched_stored_window_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self.fixture(root)
            output = self.failed_output_root(root, fixture)
            window_config = yaml.safe_load((output / "window_config.yaml").read_text())
            window_config["quarter_plan"] = "/different/quarter_plan.json"
            (output / "window_config.yaml").write_text(yaml.safe_dump(window_config))

            with self.assertRaisesRegex(ValueError, "window_config.yaml does not match"):
                run(self.arguments(fixture, root))

    def test_completed_existing_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self.fixture(root)
            with patch(
                "construction.quarter_runner.build_history_window.run",
                self.window_stage([]),
            ):
                run(self.arguments(fixture, root))

            with self.assertRaisesRegex(ValueError, "already completed"):
                run(self.arguments(fixture, root))

    def test_corrupt_passed_checkpoint_chain_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self.fixture(root)
            with patch(
                "construction.quarter_runner.build_history_window.run",
                self.window_stage([]),
            ):
                run(self.arguments(fixture, root))

            output = root / "output"
            (output / "final_checkpoint").rename(output / "removed_final_checkpoint")
            root_manifest = json.loads((output / "manifest.json").read_text())
            root_manifest["status"] = "failed"
            (output / "manifest.json").write_text(json.dumps(root_manifest) + "\n")
            candidate_facts = (
                output
                / "windows"
                / "2025-04-01_to_2025-04-07"
                / "candidate_checkpoint"
                / "facts.yaml"
            )
            candidate_facts.write_text(candidate_facts.read_text() + "\n")

            with self.assertRaisesRegex(ValueError, "payload hashes do not match"):
                run(self.arguments(fixture, root))

    def test_completed_quarter_makes_no_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self.fixture(root, checkpoint_date="2025-06-30")
            calls: list[argparse.Namespace] = []
            with patch("construction.quarter_runner.build_history_window.run", self.window_stage(calls)):
                manifest = run(self.arguments(fixture, root))
            self.assertEqual(calls, [])
            self.assertEqual(
                manifest["model_calls"],
                {
                    "expected": 1,
                    "accepted_stage_calls": 1,
                    "paid_calls": 1,
                    "cache_hits": 0,
                    "ledger_entries": 1,
                    "actual": 1,
                },
            )

    def test_runner_has_no_old_plan_or_schedule_dependency(self) -> None:
        source = inspect.getsource(quarter_runner)
        self.assertNotIn("plan_quarter_events", source)
        self.assertNotIn("plan_quarter_schedule", source)


if __name__ == "__main__":
    unittest.main()
