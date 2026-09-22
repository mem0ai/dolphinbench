from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import run_simulation as runner


class BackgroundGradingTests(unittest.TestCase):
    def test_two_saved_outputs_can_be_graded_at_the_same_time(self) -> None:
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()

        def fake_grade(spec, response, tool_calls, query):
            if response == "first":
                first_started.set()
                self.assertTrue(release_first.wait(timeout=2))
            else:
                second_started.set()
            return {"passed": True, "score": 1.0}

        with tempfile.TemporaryDirectory() as raw:
            out_path = Path(raw) / "results.json"
            results = {"test_results": []}
            first = {
                "test_id": "001", "source_id": "001", "query": "first",
                "response": "first", "effective_tool_calls": [],
            }
            second = {
                "test_id": "002", "source_id": "002", "query": "second",
                "response": "second", "effective_tool_calls": [],
            }
            results["test_results"].extend([first, second])
            out_path.write_text(json.dumps(results))

            with patch.object(runner, "grade", side_effect=fake_grade), \
                    patch.dict(runner.os.environ, {"DOLPHINBENCH_GRADING_WORKERS": "2"}):
                grader = runner._BackgroundGrader(results, out_path)
                try:
                    grader.submit(first, {})
                    self.assertTrue(first_started.wait(timeout=1))
                    grader.submit(second, {})
                    self.assertTrue(second_started.wait(timeout=1))
                finally:
                    release_first.set()
                grader.finish()

            saved = json.loads(out_path.read_text())
            self.assertTrue(all(entry["passed"] for entry in saved["test_results"]))
            self.assertTrue(all("grading_status" not in entry for entry in saved["test_results"]))

    def test_saved_ungraded_output_is_graded_without_an_agent_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out_path = Path(raw) / "results.json"
            results = {"test_results": [{
                "test_id": "001", "source_id": "001", "query": "saved query",
                "response": "saved response", "effective_tool_calls": [],
                "grading_status": "pending",
            }]}
            out_path.write_text(json.dumps(results))

            with patch.object(runner, "load_test", return_value={}), \
                    patch.object(runner, "grade", return_value={"passed": False, "score": 0.0}):
                grader = runner._BackgroundGrader(results, out_path)
                grader.submit_saved_outputs()
                grader.finish()

            saved = json.loads(out_path.read_text())
            self.assertFalse(saved["test_results"][0]["passed"])
            self.assertEqual(saved["test_results"][0]["response"], "saved response")


if __name__ == "__main__":
    unittest.main()
