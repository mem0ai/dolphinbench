"""Submission export and verification use recorded evidence, never model calls."""

from __future__ import annotations

import copy
import io
import json
import sqlite3
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from graders import llm_judge
from graders.mechanical import grade_tool_trace
from harness import submission
from harness.submission_capture import Recorder, build_rollout, chat_messages
from harness.export_submission import _read_trace, build_documents, main


USAGE = {"prompt_tokens": 10, "completion_tokens": 2,
         "prompt_tokens_details": {"cached_tokens": 3}, "total_tokens": 12}
SETTINGS = {"model": "fixture-model", "reasoning_effort": "high"}
JUDGE = {"model": "gpt-5.6-sol", "reasoning_effort": "medium"}
RELEASE_PATH = Path(__file__).resolve().parents[1]


def assistant(content):
    return {"role": "assistant", "content": content, "usage": copy.deepcopy(USAGE)}


def fixture():
    releases = {}
    ingestion = {"settings": dict(SETTINGS), "sessions": [], "total_cost_usd": 3}
    tests = {"settings": dict(SETTINGS), "judge_settings": dict(JUDGE), "tests": [], "total_cost_usd": 6}
    for persona in ("morgan", "alex", "riley"):
        history = [{"id": "000001", "narrative_date": "2023-01-01T09:00:00-08:00",
                    "messages": ["My coffee shop is Blue Bottle."]}]
        releases[persona] = {"sessions": history, "tests": []}
        ingestion["sessions"].append({"persona": persona, "session_id": "000001", "duration_ms": 100,
            "messages": [{"role": "user", "content": "[2023-01-01T09:00:00-08:00] My coffee shop is Blue Bottle."},
                         assistant("Noted.")]})
        for i in range(1, 201):
            test_id = f"{i:03d}"
            spec = {"id": test_id, "narrative_anchor_date": "2026-09-14", "test": "Order a small latte.",
                    "grade": {"type": "tool_trace", "config": {"today": "2026-09-14", "assertions": [
                        {"type": "field_equals", "tool": "place_order", "path": "args.restaurant", "value": "Blue Bottle"}]}}}
            releases[persona]["tests"].append(spec)
            call = {"role": "assistant", "tool_calls": [{"id": "order", "name": "place_order",
                    "arguments": {"restaurant": "Blue Bottle", "items": ["small latte"]}}], "usage": dict(USAGE)}
            tests["tests"].append({"persona": persona, "test_id": test_id, "duration_ms": 250,
                "messages": [{"role": "user", "content": spec["test"]}, call,
                             {"role": "tool", "tool_call_id": "order", "content": {"ok": True, "id": "order_1"}},
                             assistant("Ordered.")], "grading": [{"check": 0, "passed": True}]})
    return releases, ingestion, tests


class SubmissionTests(unittest.TestCase):
    def test_phase_costs_are_required_and_not_added_to_token_prices(self):
        releases, ingestion, tests = fixture()
        pricing = {"models": {"fixture-model": {"input_cost_per_token": 1, "output_cost_per_token": 1}}}
        result = submission.validate(ingestion, tests, releases, pricing=pricing)
        self.assertEqual(result["total_cost_usd"], 9)
        self.assertGreater(result["execution"]["estimated_model_cost_usd"], 9)
        for document in (ingestion, tests):
            for value in (None, True, -1, float('nan'), float('inf'), '6', 10 ** 1000):
                original = document["total_cost_usd"]
                document["total_cost_usd"] = value
                with self.assertRaises(submission.SubmissionError):
                    submission.validate(ingestion, tests, releases)
                document["total_cost_usd"] = original
            cost = document.pop("total_cost_usd")
            with self.assertRaises(submission.SubmissionError):
                submission.validate(ingestion, tests, releases)
            document["total_cost_usd"] = cost
        ingestion["total_cost_usd"] = tests["total_cost_usd"] = 1e308
        with self.assertRaises(submission.SubmissionError):
            submission.validate(ingestion, tests, releases)
        ingestion["total_cost_usd"] = tests["total_cost_usd"] = 0
        self.assertEqual(submission.validate(ingestion, tests, releases)["total_cost_usd"], 0)

    def test_structured_app_results_preserve_visible_wrapper_and_match_calls(self):
        row = fixture()[2]["tests"][0]
        row["messages"][2]["content"] = '<untrusted_tool_result>{"result":"wrapped"}</untrusted_tool_result>'
        original = copy.deepcopy(row["messages"])
        call = row["messages"][1]["tool_calls"][0]
        row["app_calls"] = [{"tool": call["name"], "args": call["arguments"], "result": {"ok": True}}]
        self.assertEqual(submission.grading_calls(row, "fixture")[0]["result"], {"ok": True})
        self.assertEqual(row["messages"], original)
        row["app_calls"].append(copy.deepcopy(row["app_calls"][0]))
        with self.assertRaisesRegex(submission.SubmissionError, "no matching"):
            submission.grading_calls(row, "fixture")
        row["app_calls"] = [{"tool": "invented", "args": {}}]
        with self.assertRaisesRegex(submission.SubmissionError, "no matching"):
            submission.grading_calls(row, "fixture")

    def test_response_settings_control_pricing_and_remain_assistant_only(self):
        row = {"messages": [assistant("one"), assistant("two")], "duration_ms": 10}
        row["messages"][1]["settings"] = {"model": "second"}
        pricing = {"models": {"fixture-model": {"input_cost_per_token": 1, "output_cost_per_token": 1},
                              "second": {"input_cost_per_token": 2, "output_cost_per_token": 2}}}
        stats = submission._phase_stats([row], SETTINGS, pricing)
        self.assertEqual(stats["estimated_model_cost_usd"], 36)
        with self.assertRaisesRegex(submission.SubmissionError, "settings belong"):
            submission.check_messages([{"role": "user", "content": "hi", "settings": SETTINGS},
                                       assistant("hello")], "fixture")

    def setUp(self):
        self.releases, self.ingestion, self.tests = fixture()
        self.network = patch("urllib.request.urlopen", side_effect=AssertionError("A local check made a network call"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def validate(self):
        return submission.validate(self.ingestion, self.tests, self.releases)

    def test_complete_round_trip_contains_exactly_two_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submission.zip"
            summary = submission.write_zip(path, self.ingestion, self.tests, self.releases)
            self.assertEqual(summary["passes"], 600)
            self.assertEqual(summary["total_cost_usd"], 9)
            self.assertEqual(summary["ingestion"]["total_cost_usd"], 3)
            self.assertEqual(summary["execution"]["total_cost_usd"], 6)
            self.assertIsNone(summary["execution"]["estimated_model_cost_usd"])
            self.assertEqual(summary["execution"]["tokens"]["input_tokens"], 7 * 1200)
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(set(archive.namelist()), {"ingestion.json", "tests.json"})
            ingestion, tests = submission.read_zip(path)
            self.assertEqual((ingestion, tests), (self.ingestion, self.tests))
            self.assertEqual(submission.validate(ingestion, tests, self.releases), summary)
            with self.assertRaisesRegex(submission.SubmissionError, "already exists"):
                submission.write_zip(path, ingestion, tests, self.releases)

    def test_coverage_and_content_are_required(self):
        for change in ("missing_session", "missing_test", "duplicate", "source", "request", "grading"):
            with self.subTest(change=change):
                releases, ingestion, tests = fixture()
                if change == "missing_session":
                    ingestion["sessions"].pop()
                elif change == "missing_test":
                    tests["tests"].pop()
                elif change == "duplicate":
                    tests["tests"].append(copy.deepcopy(tests["tests"][0]))
                elif change == "source":
                    ingestion["sessions"][0]["messages"][0]["content"] = "Different source."
                elif change == "request":
                    tests["tests"][0]["messages"][0]["content"] = "Different request."
                else:
                    tests["tests"][0]["grading"][0]["passed"] = False
                with self.assertRaises(submission.SubmissionError):
                    submission.validate(ingestion, tests, releases)

    def test_missing_results_usage_and_unapproved_fields_fail(self):
        for change in ("result", "usage", "attempts", "provider", "message_index", "negative", "bool", "nan"):
            with self.subTest(change=change):
                releases, ingestion, tests = fixture()
                row = tests["tests"][0]
                if change == "result":
                    row["messages"].pop(2)
                elif change == "usage":
                    row["messages"][-1].pop("usage")
                elif change == "attempts":
                    row["attempts"] = []
                elif change == "provider":
                    tests["settings"]["provider"] = "unneeded"
                elif change == "message_index":
                    ingestion["sessions"][0]["message_index"] = 0
                else:
                    row["duration_ms"] = {"negative": -1, "bool": True, "nan": float("nan")}[change]
                with self.assertRaises(submission.SubmissionError):
                    submission.validate(ingestion, tests, releases)

    def test_grade_error_leaves_no_output(self):
        self.tests["tests"][0]["grading"][0]["passed"] = False
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submission.zip"
            with self.assertRaises(submission.SubmissionError):
                submission.write_zip(path, self.ingestion, self.tests, self.releases)
            self.assertFalse(path.exists())

    def test_known_credentials_are_not_exported(self):
        secret = "private-credential-do-not-export"
        self.ingestion["sessions"][0]["messages"][-1]["content"] = secret
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"EXAMPLE_API_KEY": secret}):
            path = Path(directory) / "submission.zip"
            with self.assertRaisesRegex(submission.SubmissionError, "configured credential"):
                submission.write_zip(path, self.ingestion, self.tests, self.releases)
            self.assertFalse(path.exists())

    def test_zip_rejects_extra_paths_duplicate_json_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.zip"
            for names in (("../ingestion.json", "tests.json"), ("ingestion.json", "tests.json", "run.json")):
                with zipfile.ZipFile(path, "w") as archive:
                    for name in names:
                        archive.writestr(name, "{}")
                with self.assertRaises(submission.SubmissionError):
                    submission.read_zip(path)
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("ingestion.json", '{"sessions": [], "sessions": []}')
                archive.writestr("tests.json", "{}")
            with self.assertRaisesRegex(submission.SubmissionError, "Duplicate JSON key"):
                submission.read_zip(path)
            with patch.object(submission, "MAX_JSON_BYTES", 1):
                with self.assertRaisesRegex(submission.SubmissionError, "oversized"):
                    submission.read_zip(path)
        with self.assertRaises(submission.SubmissionError):
            submission.load_json('{"duration_ms": NaN}')

    def test_zip_limits_apply_before_directory_parsing_and_decompression(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submission.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for name in submission.FILES:
                    archive.writestr(name, "{}")
            with patch.object(submission, "MAX_ZIP_BYTES", path.stat().st_size - 1), \
                    patch.object(zipfile, "ZipFile", side_effect=AssertionError("Parsed oversized ZIP")):
                with self.assertRaisesRegex(submission.SubmissionError, "ZIP exceeds"):
                    submission.read_zip(path)
            with patch.object(submission, "MAX_ZIP_METADATA_BYTES", 64), \
                    patch.object(zipfile, "ZipInfo", side_effect=AssertionError("Enumerated oversized directory")):
                with self.assertRaisesRegex(submission.SubmissionError, "metadata"):
                    submission.read_zip(path)
            with patch.object(submission, "MAX_TOTAL_JSON_BYTES", 3):
                with self.assertRaisesRegex(submission.SubmissionError, "Combined JSON"):
                    submission.read_zip(path)
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("ingestion.json", '{"padding":"' + " " * 1024 * 1024 + '"}')
                archive.writestr("tests.json", "{}")
            self.assertLess(path.stat().st_size, 2048)
            with patch.object(submission, "MAX_JSON_BYTES", 1024), \
                    patch.object(zipfile.ZipFile, "open", side_effect=AssertionError("Decompressed oversized entry")):
                with self.assertRaisesRegex(submission.SubmissionError, "oversized"):
                    submission.read_zip(path)
            # High compression alone is not a rejection: legitimate JSON repeats.
            self.assertEqual(len(submission.read_zip(path)[0]["padding"]), 1024 * 1024)

    def test_zip_counts_actual_decompressed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submission.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for name in submission.FILES:
                    archive.writestr(name, "{}")
            for limits, content, error in (
                ({"MAX_JSON_BYTES": 64}, b"{}" + b" " * 100, "Decompressed JSON exceeds"),
                ({"MAX_TOTAL_JSON_BYTES": 64}, b"{}" + b" " * 100, "Decompressed JSON exceeds"),
                ({}, b"{} ", "size differs"),
            ):
                with self.subTest(limits=limits), patch.multiple(submission, ZIP_READ_BYTES=16, **limits), \
                        patch.object(zipfile.ZipFile, "open", side_effect=lambda *args: io.BytesIO(content)):
                    with self.assertRaisesRegex(submission.SubmissionError, error):
                        submission.read_zip(path)

    def test_zip_rejects_links_directories_and_unsupported_compression(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submission.zip"
            for mode in (stat.S_IFLNK, stat.S_IFDIR, stat.S_IFIFO, stat.S_IFSOCK):
                with self.subTest(mode=mode):
                    info = zipfile.ZipInfo("ingestion.json")
                    info.create_system = 3
                    info.external_attr = (mode | 0o600) << 16
                    with zipfile.ZipFile(path, "w") as archive:
                        archive.writestr(info, "{}")
                        archive.writestr("tests.json", "{}")
                    with self.assertRaisesRegex(submission.SubmissionError, "regular files"):
                        submission.read_zip(path)
            for compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
                with self.subTest(compression=compression):
                    with zipfile.ZipFile(path, "w", compression=compression) as archive:
                        for name in submission.FILES:
                            archive.writestr(name, "{}")
                    if compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        self.assertEqual(submission.read_zip(path), ({}, {}))
                    else:
                        with self.assertRaisesRegex(submission.SubmissionError, "compression"):
                            submission.read_zip(path)

    def test_zip_rejects_corruption_encryption_and_ambiguous_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submission.zip"
            for name in ("/ingestion.json", "folder/ingestion.json", "..\\ingestion.json", "tests.json",
                         "ingestion.jsonXhidden"):
                warning = self.assertWarns(UserWarning) if name == "tests.json" else nullcontext()
                with self.subTest(name=name), warning:
                    with zipfile.ZipFile(path, "w") as archive:
                        archive.writestr(name, "{}")
                        archive.writestr("tests.json", "{}")
                    if "Xhidden" in name:
                        path.write_bytes(path.read_bytes().replace(b"Xhidden", b"\x00hidden"))
                    with self.assertRaisesRegex(submission.SubmissionError, "at its root"):
                        submission.read_zip(path)
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("ingestion.json", "{}")
                archive.writestr("tests.json", "{}")
            original = path.read_bytes()
            central = original.index(b"PK\x01\x02")
            for corruption in ("encrypted", "crc", "truncated", "filename_encoding"):
                with self.subTest(corruption=corruption):
                    data = bytearray(original)
                    if corruption == "encrypted":
                        data[central + 8] |= 1
                        data[6] |= 1
                    elif corruption == "crc":
                        data[30 + len("ingestion.json")] = ord("!")
                    elif corruption == "filename_encoding":
                        data[central + 9] |= 8
                        data[central + 46] = 255
                    else:
                        data = data[:30]
                    path.write_bytes(data)
                    with self.assertRaises(submission.SubmissionError):
                        submission.read_zip(path)
            self.assertEqual({item.name for item in Path(directory).iterdir()}, {"submission.zip"})

    def test_json_rejects_numeric_overflow_invalid_encoding_and_excessive_nesting(self):
        for content in ('{"value": 1e999}', '{"value": -1e999}', '{"value": Infinity}',
                        '{"value": NaN}', b'{"value": "\xff"}', '[' * 2000 + ']' * 2000,
                        '{"nested": {"value": 1, "value": 2}}', '{"value": ' + '1' * 10000 + '}'):
            with self.subTest(content=repr(content)[:60]):
                with self.assertRaises(submission.SubmissionError):
                    submission.load_json(content)
        self.assertEqual(submission.load_json('{"value": 1.5e2}'), {"value": 150.0})

    def test_only_model_cost_is_reported_and_cache_is_not_double_counted(self):
        pricing = {"models": {"fixture-model": {"input_cost_per_token": 0.01,
                    "output_cost_per_token": 0.02, "cache_read_input_token_cost": 0.001}}}
        summary = submission.validate(self.ingestion, self.tests, self.releases, pricing=pricing)
        self.assertAlmostEqual(summary["ingestion"]["estimated_model_cost_usd"], 3 * (7 * .01 + 3 * .001 + 2 * .02))
        counts = submission.token_counts({"input_tokens": 10, "output_tokens": 5,
            "cache_read_input_tokens": 20, "cache_creation_input_tokens": 30})
        self.assertEqual(sum(counts[k] for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")), 65)
        counts = submission.token_counts({"input_tokens": 10, "output_tokens": 5,
            "input_tokens_details": {"cached_tokens": 4}, "output_tokens_details": {"reasoning_tokens": 2}, "total_tokens": 15})
        self.assertEqual(counts["input_tokens"], 6)
        self.assertEqual(counts["output_tokens"], 5)
        self.assertEqual(counts["reasoning_tokens"], 2)
        for usage in ({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 99},
                      {"prompt_tokens": 1, "completion_tokens": True},
                      {"input_tokens": 1, "output_tokens": 1, "_source": "estimate"}):
            with self.assertRaises(submission.SubmissionError):
                submission.token_counts(usage)

    def record_grade(self, checks, *, version=1, semantic_version=1, answers=None):
        spec = self.releases["morgan"]["tests"][0]
        config = {"assertions": checks, "today": "2026-09-14"}
        if version == 2:
            config.update(check_version=2, semantic_judge_version=semantic_version)
        spec["grade"]["config"] = config
        row = self.tests["tests"][0]
        calls = submission.check_messages(row["messages"], "fixture")
        answers = answers or [{"passed": True, "reason": "The restaurant matches."}]
        iterator = iter(answers)
        def response(*args, **kwargs):
            return {"choices": [{"message": {"role": "assistant", "content": json.dumps(next(iterator))}}],
                    "usage": copy.deepcopy(USAGE)}
        with patch.object(llm_judge, "AZURE_ENDPOINT", "https://judge.invalid"), \
                patch.object(llm_judge, "AZURE_API_KEY", "private-test-key"), \
                patch.object(llm_judge, "_urlopen_json", side_effect=response):
            grade = grade_tool_trace(calls, config, test_message=spec["test"])
        row["grading"] = []
        for i, detail in enumerate(grade["details"]):
            check = {"check": i, "passed": detail["ok"]}
            if detail.get("judge_messages"):
                check["messages"] = detail["judge_messages"]
                self.tests["judge_settings"] = detail["judge_settings"]
            row["grading"].append(check)
        return grade

    def test_semantic_grading_records_and_replays_exact_prompts(self):
        grade = self.record_grade([{"type": "field_llm_judge", "tool": "place_order",
                                  "path": "args.restaurant", "criterion": "Names Blue Bottle."}])
        self.assertTrue(grade["passed"])
        self.assertNotIn("private-test-key", json.dumps(grade))
        self.assertEqual(self.validate()["passes"], 600)
        self.tests["tests"][0]["grading"][0]["messages"][1]["content"] += " Altered prompt."
        with self.assertRaisesRegex(submission.SubmissionError, "matching this action"):
            self.validate()

    def test_judge_response_cannot_override_fixed_model_settings(self):
        self.record_grade([{"type": "field_llm_judge", "tool": "place_order",
                            "path": "args.restaurant", "criterion": "Names Blue Bottle."}])
        messages = self.tests["tests"][0]["grading"][0]["messages"]
        next(message for message in messages if message["role"] == "assistant")["settings"] = {"model": "other"}
        with self.assertRaisesRegex(submission.SubmissionError, "overrides the fixed settings"):
            self.validate()

    def test_dated_agent_message_replays_judge_for_original_published_request(self):
        self.record_grade([{"type": "field_llm_judge", "tool": "place_order",
                            "path": "args.restaurant", "criterion": "Names Blue Bottle."}])
        spec = self.releases["morgan"]["tests"][0]
        self.tests["tests"][0]["messages"][0]["content"] = (
            f"[{spec['narrative_anchor_date']}] {spec['test']}"
        )
        self.assertEqual(self.validate()["passes"], 600)

    def test_missing_judge_evidence_never_makes_a_model_call(self):
        self.record_grade([{"type": "field_llm_judge", "tool": "place_order",
                            "path": "args.restaurant", "criterion": "Names Blue Bottle."}])
        self.tests["tests"][0]["grading"][0].pop("messages")
        with self.assertRaisesRegex(submission.SubmissionError, "Missing judge"):
            self.validate()

    def test_batched_judge_response_is_stored_once_and_preserves_action_binding(self):
        checks = [{"type": "field_llm_judge", "tool": "place_order", "action_id": "order",
                   "check_id": "shop", "path": "args.restaurant", "criterion": "Names Blue Bottle."},
                  {"type": "field_llm_judge", "tool": "place_order", "action_id": "order",
                   "check_id": "drink", "path": "args.items", "criterion": "Orders a small latte."}]
        answer = {"results": [{"check_id": "shop", "passed": True, "reason": "Correct shop."},
                              {"check_id": "drink", "passed": True, "reason": "Correct drink."}]}
        self.record_grade(checks, version=2, semantic_version=2, answers=[answer])
        grading = self.tests["tests"][0]["grading"]
        self.assertIn("messages", grading[0])
        self.assertNotIn("messages", grading[1])
        self.assertEqual(self.validate()["passes"], 600)
        self.tests["tests"][0]["messages"][1]["tool_calls"][0]["arguments"]["items"] = ["large tea"]
        with self.assertRaises(submission.SubmissionError):
            self.validate()


class RecorderTests(unittest.TestCase):
    def test_lazy_tool_expansion_retains_each_response_settings(self):
        first = {"settings": {"model": "fixture", "tools": []},
                 "messages": [{"role": "user", "content": "hello"}], "assistant": assistant("one")}
        second = {"settings": {"model": "fixture", "tools": [{"name": "expanded"}]},
                  "messages": [*first["messages"], first["assistant"]], "assistant": assistant("two")}
        third = {"settings": first["settings"],
                 "messages": [*second["messages"], second["assistant"]], "assistant": assistant("three")}
        result = build_rollout([first, second, third])
        self.assertEqual(result["settings"], first["settings"])
        self.assertEqual(result["messages"][2]["settings"], second["settings"])
        self.assertNotIn("settings", result["messages"][3])
        submission.check_messages(result["messages"], "fixture")

    def test_runner_preserves_cli_interpreter_arguments_and_records_locally(self):
        from harness import hermes_driver
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "hermes"
            binary.write_text("#!/fixture/venv/bin/python\n")
            with patch.object(hermes_driver, "HERMES_BIN", str(binary)), \
                    patch.object(hermes_driver, "hermes_profiles_dir", return_value=root), \
                    patch.dict("os.environ", {"DOLPHINBENCH_CAPTURE_SUBMISSION": "1"}), \
                    patch("reference.execution.hermes_agent.run_cli", side_effect=RuntimeError("fixture stop")) as run:
                hermes_driver.run_hermes("fixture-profile", "Hello.", model="fixture-model", narrative_time="2023-01-01")
            args = run.call_args.args[0]
            self.assertEqual(args[:4], ["/fixture/venv/bin/python", "-m", "harness.hermes_observed_cli", str(binary)])
            self.assertEqual(args[4:], ["-p", "fixture-profile", "chat", "-q", "[2023-01-01] Hello.", "-Q", "--yolo", "-m", "fixture-model"])
            self.assertEqual(run.call_args.kwargs["env"]["DOLPHINBENCH_SUBMISSION_TRACE_DIR"],
                             str(root / "fixture-profile/submission-traces"))
            self.assertFalse((root / "fixture-profile").exists())

    def test_archived_trace_includes_recorded_responses_without_changing_database(self):
        from harness.run_simulation import _archive_hermes_session
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.db"
            with sqlite3.connect(database) as connection:
                connection.executescript("""
                    CREATE TABLE sessions (id TEXT, system_prompt_hash TEXT);
                    CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, content TEXT);
                    CREATE TABLE session_model_usage (session_id TEXT, model TEXT,
                        billing_provider TEXT, billing_base_url TEXT, billing_mode TEXT, task TEXT);
                    INSERT INTO sessions VALUES ('fixture', NULL);
                    INSERT INTO messages VALUES (1, 'fixture', 'user', 'Hello.');
                """)
            original = database.read_bytes()
            event = {"settings": dict(SETTINGS), "messages": [{"role": "user", "content": "Hello."}],
                     "assistant": assistant("Hi.")}
            recorder = Recorder(root / "submission-traces")
            recorder.write("fixture", event)
            path = _archive_hermes_session(str(database), "fixture", root / "traces", "fixture.json")
            trace = json.loads(Path(path).read_text())
            self.assertEqual(trace["submission"], build_rollout([event]))
            self.assertEqual(database.read_bytes(), original)
            recorder.write("fixture", {"error": "fixture failure"})
            path = _archive_hermes_session(str(database), "fixture", root / "traces", "failed.json")
            trace = json.loads(Path(path).read_text())
            self.assertNotIn("submission", trace)
            self.assertIn("fixture failure", trace["submission_error"])

    def event(self):
        return {"settings": dict(SETTINGS), "messages": [{"role": "user", "content": "Order coffee."}],
                "assistant": {"role": "assistant", "tool_calls": [
                    {"id": "one", "name": "order", "arguments": {"item": "latte"}}], "usage": dict(USAGE)}}

    def test_rollout_records_conversation_once_with_usage_on_each_response(self):
        first = self.event()
        prior = copy.deepcopy(first["assistant"])
        prior.pop("usage")
        second = {"settings": dict(SETTINGS), "messages": [*copy.deepcopy(first["messages"]), prior,
                  {"role": "tool", "tool_call_id": "one", "content": {"ok": True}}],
                  "assistant": assistant("Ordered.")}
        result = build_rollout([first, second])
        self.assertEqual(len(result["messages"]), 4)
        self.assertEqual([m["role"] for m in result["messages"]], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(result["messages"][1]["usage"], USAGE)
        submission.check_messages(result["messages"], "recorded")
        second["messages"][0]["content"] = "Rewritten context."
        with self.assertRaisesRegex(submission.SubmissionError, "rewritten"):
            build_rollout([first, second])

    def test_responses_api_messages_and_tool_arguments_are_normalized(self):
        messages = chat_messages([
            {"role": "user", "content": [{"type": "input_text", "text": "Hello"}]},
            {"type": "reasoning", "encrypted_content": "not a visible message"},
            {"type": "function_call", "call_id": "one", "name": "search", "arguments": '{"q":"coffee"}'},
            {"type": "function_call_output", "call_id": "one", "output": "Found it."},
        ], "Shared instructions.")
        self.assertEqual(messages[0], {"role": "system", "content": "Shared instructions."})
        self.assertEqual(messages[1]["content"], "Hello")
        self.assertEqual(messages[2]["tool_calls"][0]["arguments"], {"q": "coffee"})
        self.assertEqual(messages[3]["tool_call_id"], "one")

    def test_hook_recording_does_not_capture_headers_or_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(Path(directory))
            recorder.observe("pre_api_request", session_id="session", api_request_id="request",
                model="fixture-model", provider="private-provider", base_url="https://private.invalid",
                request={"body": {"model": "fixture-model", "reasoning_effort": "high"},
                         "headers": {"Authorization": "private-key"}},
                request_messages=[{"role": "user", "content": "Hello."}])
            recorder.observe("post_api_request", session_id="session", api_request_id="request",
                assistant_message=SimpleNamespace(content="Hi.", tool_calls=[]), usage=dict(USAGE), api_duration=1.5)
            text = (Path(directory) / "session.jsonl").read_text()
            self.assertNotIn("private", text)
            result = build_rollout([json.loads(text)])
            self.assertEqual(result["messages"][-1]["duration_ms"], 1500)
            self.assertEqual(result["messages"][-1]["usage"], USAGE)


class ExporterTests(unittest.TestCase):
    def test_phase_defaults_do_not_flatten_changed_record_settings(self):
        from harness.export_submission import record_settings, _hoist_system_prompt
        original = {"model": "first"}
        incoming = {"model": "second", "tools": []}
        row = {"messages": [{"role": "system", "content": "instructions"}, assistant("one"), assistant("two")]}
        row["messages"][2]["settings"] = {"model": "third"}
        self.assertEqual(record_settings(original, incoming, [row], "fixture"), original)
        self.assertEqual(row["messages"][1]["settings"], incoming)
        self.assertEqual(row["messages"][2]["settings"], {"model": "third"})
        _hoist_system_prompt(original, [row])
        self.assertEqual(row["messages"][0]["role"], "system")

    def test_cli_checks_zip_without_source_records_or_network(self):
        releases, ingestion, tests = fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-fixture.zip"
            submission.write_zip(path, ingestion, tests, releases)
            output = io.StringIO()
            with patch("sys.argv", ["export_submission", "--release", "fixture-release", "--check", str(path)]), \
                    patch("harness.export_submission.read_release", return_value=releases), \
                    patch("harness.export_submission.load_pricing", return_value={}), \
                    patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network call")), \
                    patch("sys.stdout", output):
                self.assertEqual(main(), 0)
            self.assertEqual(json.loads(output.getvalue())["tests"], 600)

    def test_older_aggregate_trace_is_rejected_without_inventing_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "old.json"
            path.write_text(json.dumps({"messages": [{"role": "assistant", "content": "Done."}],
                                        "model_usage": [{"input_tokens": 100, "output_tokens": 10}]}))
            original = path.read_bytes()
            with self.assertRaisesRegex(submission.SubmissionError, "Older aggregate usage"):
                _read_trace({"trace_path": str(path)}, root / "results.json")
            self.assertEqual(path.read_bytes(), original)

    @unittest.skipUnless((RELEASE_PATH / "manifest.json").is_file(), "Released corpus unavailable")
    def test_full_released_history_and_tests_round_trip_with_synthetic_responses(self):
        from graders.judge_recording import replay_judge
        release_path = RELEASE_PATH
        with patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network call")):
            releases = submission.read_release(release_path)
            ingestion = {"settings": dict(SETTINGS), "sessions": [], "total_cost_usd": 3}
            tests = {"settings": dict(SETTINGS), "judge_settings": dict(JUDGE), "tests": [], "total_cost_usd": 6}
            for persona, release in releases.items():
                for session in release["sessions"]:
                    ingestion["sessions"].append({"persona": persona, "session_id": session["id"], "duration_ms": 1,
                        "messages": [{"role": "user", "content": f"[{session['narrative_date']}] {session['messages'][0].strip()}"},
                                     assistant("Synthetic fixture response, not a benchmark execution.")]})
                for spec in release["tests"]:
                    config = {**spec["grade"]["config"], "raise_on_judge_error": True}
                    with replay_judge([], JUDGE):
                        grade = grade_tool_trace([], config, test_message=spec["test"])
                    tests["tests"].append({"persona": persona, "test_id": str(spec["id"]).zfill(3), "duration_ms": 1,
                        "messages": [{"role": "user", "content": spec["test"]},
                                     assistant("Synthetic fixture response, not a benchmark execution.")],
                        "grading": [{"check": i, "passed": row["ok"]} for i, row in enumerate(grade["details"])]})
            self.assertEqual(len(ingestion["sessions"]), 13539)
            self.assertEqual(len(tests["tests"]), 600)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "synthetic-fixture.zip"
                summary = submission.write_zip(path, ingestion, tests, releases)
                reloaded = submission.read_zip(path)
                self.assertEqual(submission.validate(*reloaded, releases), summary)

    def test_latest_execution_and_new_session_ids_without_modifying_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, ingestion, tests = fixture()
            ingestion_paths, test_paths, maps = {}, {}, {}
            originals = {}
            for persona in ("morgan", "alex", "riley"):
                row = next(row for row in ingestion["sessions"] if row["persona"] == persona)
                seed = {"session_id": "old-session", "message_index": 0, "latency_seconds": 10,
                        "submission": {"settings": dict(SETTINGS), "messages": row["messages"]}}
                path = root / f"{persona}-ingestion.json"
                path.write_text(json.dumps({"seed_calls": [{**seed, "latency_seconds": 99}, seed], "total_cost_usd": 1}))
                ingestion_paths[persona] = path
                maps[persona] = [{"session_id": "000001", "original_session_id": "old-session", "original_message_index": 0}]
                results = []
                for test in tests["tests"]:
                    if test["persona"] == persona:
                        results.append({"test_id": test["test_id"], "latency_seconds": 99,
                            "attempts": [{"latency_seconds": 98}, {"latency_seconds": 1}],
                            "submission": {"settings": dict(SETTINGS), "messages": test["messages"]},
                            "grade": {"details": [{"ok": True}]}})
                path = root / f"{persona}-tests.json"
                path.write_text(json.dumps({"test_results": results, "total_cost_usd": 2}))
                test_paths[persona] = path
            originals = {path: path.read_bytes() for path in root.iterdir()}
            exported_ingestion, exported_tests = build_documents(ingestion_paths, test_paths, maps)
            self.assertEqual(exported_ingestion["sessions"][0]["duration_ms"], 10000)
            self.assertEqual(exported_tests["tests"][0]["duration_ms"], 1000)
            self.assertNotIn("message_index", json.dumps(exported_ingestion))
            self.assertNotIn("attempts", json.dumps(exported_tests))
            self.assertEqual(originals, {path: path.read_bytes() for path in root.iterdir()})
            releases, _, _ = fixture()
            self.assertEqual(submission.validate(exported_ingestion, exported_tests, releases)["passes"], 600)


if __name__ == "__main__":
    unittest.main()
