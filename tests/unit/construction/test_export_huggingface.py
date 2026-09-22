import json
import unittest

from construction.export_huggingface import ROOT, build_rows, dataset_card


class HuggingFaceExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = build_rows(ROOT)

    def test_exports_the_complete_release(self):
        self.assertEqual(len(self.rows["messages"]), 13_539)
        self.assertEqual(len(self.rows["facts"]), 1_058)
        self.assertEqual(len(self.rows["tests"]), 600)
        self.assertEqual({row["persona"] for row in self.rows["messages"]}, {"morgan", "alex", "riley"})
        self.assertEqual({row["persona"] for row in self.rows["tests"]}, {"morgan", "alex", "riley"})

    def test_preserves_structured_test_evidence_as_json(self):
        row = next(row for row in self.rows["tests"] if row["persona"] == "morgan" and row["test_id"] == "001")
        self.assertEqual(row["required_fact_ids"], [2])
        self.assertEqual(row["expected_tools"], ["place_order"])
        self.assertEqual(json.loads(row["grading"])["type"], "tool_trace")
        self.assertEqual(json.loads(row["app_state"]), {})
        self.assertEqual(json.loads(row["shared_app_state"])["path"], "state.json")

    def test_card_defines_the_three_loadable_configurations(self):
        card = dataset_card(self.rows)
        self.assertIn("config_name: messages", card)
        self.assertIn("config_name: facts", card)
        self.assertIn("config_name: tests", card)
        self.assertIn('load_dataset("mem0ai/dolphinbench")', card)
        self.assertIn("arxiv:2609.24971", card)


if __name__ == "__main__":
    unittest.main()
