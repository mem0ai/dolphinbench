import importlib.util
import unittest
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "public_run_records",
    Path(__file__).resolve().parents[1] / "scripts" / "build_public_run_records.py",
)
records = importlib.util.module_from_spec(spec)
spec.loader.exec_module(records)


class ToolEvidenceTests(unittest.TestCase):
    def test_keeps_fields_used_by_grading_and_repeated_output(self):
        arguments = {"id": "pr-184", "status": "open"}
        result = {
            "id": "pr-184",
            "status": "open",
            "pr": {"id": "pr-184", "updated_at": "2026-09-22", "source": "review"},
        }
        actual = records.clean_app_calls({"effective_tool_calls": [
            {"tool": "get_pr", "args": arguments, "result": result},
        ]})
        self.assertEqual(actual, [{"tool": "get_pr", "input": arguments, "output": result}])

    def test_keeps_empty_tool_results(self):
        for result in (None, False, 0, "", [], {}):
            with self.subTest(result=result):
                actual = records.clean_app_calls({"effective_tool_calls": [
                    {"tool": "search", "args": {}, "result": result},
                ]})
                self.assertIn("output", actual[0])
                self.assertEqual(actual[0]["output"], result)

    def test_keeps_memory_result_fields(self):
        result = {"id": "memory-1", "source": "session-2", "score": 0.9}
        actual = records.clean_memory_calls([
            {"kind": "result", "name": "memory_search", "content": result},
        ])
        self.assertEqual(actual[0]["output"], result)


if __name__ == "__main__":
    unittest.main()
