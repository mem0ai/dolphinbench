"""Offline checks for the information supplied to field grading."""

import json
import unittest
from unittest.mock import patch

from graders import llm_judge
from graders.mechanical import grade_hybrid, grade_tool_trace
from harness import run_simulation
from harness.mining.preview import prod_grade_result


class ActionContextTests(unittest.TestCase):
    def setUp(self):
        self.request = "Text Jamie whether to pick up another coffee. I have had two."
        self.action = {
            "tool": "send_sms",
            "args": {"to": "+14155550125", "body": "Please don't pick up another coffee for me."},
            "result": {"ok": True},
        }
        self.config = {
            "today": "2026-09-14",
            "assertions": [{
                "type": "field_llm_judge", "tool": "send_sms", "path": "args.body",
                "criterion": "The message declines another coffee.",
                "context": "Jamie uses +14155550125.",
            }],
        }

    def test_certification_and_evaluation_send_identical_context(self):
        spec = {"test": self.request, "grade": {"type": "tool_trace", "config": self.config}}
        with patch.object(llm_judge, "_call_judge", return_value={"passed": True, "reason": "Declines."}) as judge:
            evaluation = run_simulation.grade(spec, "done", [self.action], self.request)
            certification = prod_grade_result(spec, {"tool_trace": [self.action], "response_text": "done"})
        self.assertEqual(evaluation, certification)
        self.assertEqual(judge.call_args_list[0], judge.call_args_list[1])
        prompt = judge.call_args_list[0].args[1]
        self.assertIn(self.request, prompt)
        self.assertIn("+14155550125", prompt)
        self.assertIn("2026-09-14", prompt)
        self.assertIn("Jamie uses", prompt)
        self.assertIn('"field_being_checked": "args.body"', prompt)

    def test_hybrid_and_diagnostic_checks_receive_request(self):
        config = {**self.config, "diagnostic_assertions": self.config["assertions"]}
        with patch.object(llm_judge, "judge_field_value", return_value={"ok": True, "reason": "ok"}) as judge:
            grade_hybrid("done", [self.action], {"tool_trace": config}, test_message=self.request)
        self.assertEqual(judge.call_count, 2)
        for call in judge.call_args_list:
            self.assertIn(self.request, call.kwargs["context"])

    def test_each_call_keeps_its_own_arguments_and_rejected_verdict(self):
        other = {"tool": "send_sms", "args": {"to": "another-person", "body": "Please buy coffee."}}
        with patch.object(llm_judge, "judge_field_value", return_value={"ok": False, "reason": "Wrong action."}) as judge:
            result = grade_tool_trace([self.action, other], self.config, self.request)
        self.assertFalse(result["passed"])
        for call, action in zip(judge.call_args_list, [self.action, other]):
            context = json.loads(call.kwargs["context"].split("Action context (JSON):\n")[1])
            self.assertEqual(context["arguments"], action["args"])
            self.assertEqual(call.args[0], action["args"]["body"])
            self.assertNotIn("result", context)

    def test_other_field_types_keep_complete_values(self):
        for tool, field, value in [
            ("send_email", "body", "Morgan will review the draft."),
            ("send_discord_dm", "message", "Leave feedback in Figma comments."),
            ("create_calendar_event", "attendees", ["Sofia", "Devon", "Anna", "Sarah Kim", "Jake", "Leo"]),
        ]:
            with self.subTest(field=field):
                action = {"tool": tool, "args": {field: value, "to": "recipient"}}
                config = {"assertions": [{"type": "field_llm_judge", "tool": tool,
                                          "path": "args." + field, "criterion": "Required meaning."}]}
                with patch.object(llm_judge, "judge_field_value", return_value={"ok": True}) as judge:
                    grade_tool_trace([action], config, self.request)
                self.assertEqual(judge.call_args.args[0], value)
                context = json.loads(judge.call_args.kwargs["context"].split("Action context (JSON):\n")[1])
                self.assertEqual(context["arguments"], action["args"])

    def test_prompt_does_not_allow_context_to_supply_a_missing_answer(self):
        prompt = llm_judge.FIELD_JUDGE_SYSTEM_PROMPT
        self.assertNotIn("Use only what the VALUE explicitly states", prompt)
        self.assertIn("not evidence that the agent did it", prompt)
        self.assertIn("Do not supply a missing answer", prompt)
        self.assertIn("different action", prompt)


if __name__ == "__main__":
    unittest.main()
