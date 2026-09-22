"""Result aggregation must preserve evidence and disclose incomplete accounting."""

from pathlib import Path
import json
import tempfile
import unittest

from reference import evaluate


class ResultTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = {"jobs": [{
            "id": "fixture", "persona": "fixture", "configuration": "builtin",
            "provider": "builtin", "run_dir": str(self.root),
        }]}
        self.state = {"manifest": "manifest.json", "phases": []}

    def phase(self, payload, status="completed"):
        path = self.root / f"phase-{len(self.state['phases'])}.json"
        path.write_text(json.dumps(payload))
        self.state["phases"].append({"jobs": [{
            "id": "fixture", "status": status, "result_path": str(path),
        }]})
        return str(path)

    def combine(self):
        evaluate._write_combined_results(self.manifest, self.state)
        return json.loads((self.root / "combined_results.json").read_text())

    def test_records_scores_usage_and_evidence_across_disjoint_phases(self):
        rows = [
            {"source_id": "001", "passed": True, "cost_usd": 2.0,
             "latency_seconds": 10.0, "usage": {"input_tokens": 13},
             "trace_path": "001/trace.json", "grading": {"action": True}},
            {"source_id": "002", "passed": False, "cost_usd": 3.0,
             "latency_seconds": 20.0, "error": "missing required action",
             "tool_calls": [{"name": "send_email", "arguments": {"to": "fixture"}}]},
        ]
        ingestion = [{"source_id": "history-1", "cost_usd": 1.0}]
        paths = [self.phase({"seed_calls": ingestion, "test_results": rows[:1]}),
                 self.phase({"test_results": rows[1:]})]
        combined = self.combine()
        self.assertEqual(combined["test_results"], rows)
        self.assertEqual(combined["seed_calls"], ingestion)
        self.assertEqual(combined["phase_result_paths"], paths)
        summary = combined["summary"]
        self.assertEqual((summary["passes"], summary["total"], summary["pass_rate"]), (1, 2, 0.5))
        self.assertEqual(summary["total_cost_usd"], 6.0)
        self.assertEqual(summary["median_latency_seconds"], 15.0)
        self.assertFalse(summary["complete_200_test_run"])

    def test_missing_cost_and_usage_are_not_invented(self):
        for missing in ({}, {"cost_usd": None, "usage": None}):
            with self.subTest(missing=missing):
                self.state["phases"] = []
                row = {"source_id": "001", "passed": False, **missing}
                self.phase({"test_results": [row]})
                combined = self.combine()
                self.assertEqual(combined["test_results"], [row])
                self.assertIsNone(combined["summary"]["total_cost_usd"])
                self.assertEqual(combined["summary"]["cost_unavailable_count"], 1)
                self.assertIsNone(combined["summary"]["median_latency_seconds"])

    def test_failed_phase_keeps_completed_prefix_without_counting_it_twice(self):
        first = {"source_id": "001", "passed": True}
        second = {"source_id": "002", "passed": False, "error": "runtime failed"}
        paths = [self.phase({"test_results": [first]}, status="failed"),
                 self.phase({"test_results": [second]})]
        combined = self.combine()
        self.assertEqual(combined["test_results"], [first, second])
        self.assertEqual(combined["phase_result_paths"], paths)

    def test_duplicate_tests_or_repeated_ingestion_are_rejected(self):
        for field, row in (("test_results", {"source_id": "001", "passed": True}),
                           ("seed_calls", {"source_id": "history-1"})):
            with self.subTest(field=field):
                self.state["phases"] = []
                self.phase({field: [row]})
                self.phase({field: [row]})
                with self.assertRaisesRegex(RuntimeError, "more than once|more than one phase"):
                    self.combine()

    def test_only_a_full_test_set_is_marked_complete(self):
        for count in (0, 199, 200):
            with self.subTest(count=count):
                self.state["phases"] = []
                self.phase({"test_results": [
                    {"source_id": f"{i:03d}", "passed": True, "cost_usd": 0}
                    for i in range(1, count + 1)
                ]})
                summary = self.combine()["summary"]
                self.assertEqual(summary["total"], count)
                self.assertEqual(summary["complete_200_test_run"], count == 200)
