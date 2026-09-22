"""Local tests for the Azure language judge request."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from graders import llm_judge


class TestAzureJudgeConfiguration(unittest.TestCase):
    def test_sends_and_records_sol_medium_reasoning(self) -> None:
        captured: dict = {}

        def fake_urlopen_json(request, timeout, *, model=None):
            captured["budget_model"] = model
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {
                        "content": '{"passed": true, "reasoning": "The value matches."}'
                    },
                }],
                "usage": {},
            }

        config = {
            "backend": "azure",
            "azure_endpoint": "https://judge.example.invalid",
            "azure_api_key": "test-key",
            "azure_deployment": "gpt-5.6-sol",
            "reasoning_effort": "medium",
            "rubric": [{"id": "required_text", "criterion": "Includes the required text."}],
        }
        with patch.object(llm_judge, "_urlopen_json", side_effect=fake_urlopen_json):
            result = llm_judge.grade_llm_judge(
                "Includes the required text.",
                "Write the required text.",
                config,
            )

        self.assertIn("/deployments/gpt-5.6-sol/", captured["url"])
        self.assertEqual(captured["budget_model"], "gpt-5.6-sol")
        self.assertEqual(captured["body"]["reasoning_effort"], "medium")
        self.assertEqual(result["judge_model"], "azure:gpt-5.6-sol")
        self.assertEqual(result["judge_reasoning_effort"], "medium")
        self.assertTrue(result["passed"])


if __name__ == "__main__":
    unittest.main()
