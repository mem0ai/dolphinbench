"""Declared comparisons and complete-action checks, with no provider calls."""

import unittest
from unittest.mock import patch

from graders.mechanical import grade_tool_trace


class ExplicitChecksTests(unittest.TestCase):
    def test_evaluated_trace_wide_checks_include_failed_calls(self):
        success = {"tool": "send_sms", "args": {"to": "Ren"}, "result": {"ok": True}}
        failure = {"tool": "send_sms", "args": {"to": "Other"}, "result": {"ok": False}}
        count = self.check("tool_success_count", action=None, count=1)
        every = self.check("field_equals_all_calls", action=None, path="args.to", value="Ren")
        self.assertTrue(self.grade([success, failure], [count])["passed"])
        self.assertFalse(self.grade([success, failure], [every])["passed"])
        self.assertFalse(self.grade([], [every])["passed"])

    def test_evaluated_result_fields_and_exact_unordered_lists(self):
        call = {"tool": "send_sms", "args": {}, "result": {"recipients": ["B", "A"]}}
        check = self.check("field_list_equals_unordered", path="result.recipients", values=["A", "B"])
        self.assertTrue(self.grade([call], [check])["passed"])
        call["result"]["recipients"].append("A")
        self.assertFalse(self.grade([call], [check])["passed"])
        absent = self.check("field_absent", path="result.error")
        self.assertTrue(self.grade([call], [absent])["passed"])
        call["result"]["error"] = None
        self.assertFalse(self.grade([call], [absent])["passed"])

    def test_evaluated_markdown_bullets_use_visible_words(self):
        check = self.check("field_markdown_bullets", path="args.body",
                           value={"exact_items": 2, "max_words": 2, "only_bullets": True})
        for body, passed in [("- **First** item\n- Second item", True),
                             ("Introduction\n- First item\n- Second item", False),
                             ("- Too many words\n- Second item", False)]:
            self.assertEqual(self.grade([{"tool": "send_sms", "args": {"body": body}}], [check])["passed"], passed)

    def check(self, kind="field_equals", *, name="recipient", action="text", **kwargs):
        return {"type": kind, "tool": "send_sms", "action_id": action,
                "check_id": name, **kwargs}

    def grade(self, calls, checks):
        return grade_tool_trace(calls, {"check_version": 2, "assertions": checks},
                                test_message="Text my partner about Tuesday.")

    def test_same_message_must_have_recipient_and_content(self):
        checks = [self.check(path="args.to", value="Ren"),
                  self.check(name="content", path="args.body", value="Session plan")]
        calls = [{"tool": "send_sms", "args": {"to": "Ren", "body": "Unrelated"}},
                 {"tool": "send_sms", "args": {"to": "Someone else", "body": "Session plan"}}]
        with patch("graders.llm_judge.judge_field_value", side_effect=AssertionError("mechanical model call")):
            self.assertFalse(self.grade(calls, checks)["passed"])
            calls.append({"tool": "send_sms", "args": {"to": "Ren", "body": "Session plan"}})
            result = self.grade(calls, checks)
            self.assertTrue(result["passed"])
            self.assertEqual([row["matched_call_index"] for row in result["details"]], [2, 2])

    def test_no_tool_alias_or_pattern_rewrite(self):
        checks = [self.check("field_regex", path="args.body", pattern="^AB-CD$")]
        with patch("graders.llm_judge.judge_field_value", side_effect=AssertionError("mechanical model call")):
            self.assertFalse(self.grade([{"tool": "send_sms", "args": {"body": "AB CD"}}], checks)["passed"])
            checks = [self.check("tool_called")]
            checks[0]["tool"] = "create_runbook_entry"
            self.assertFalse(self.grade([{"tool": "update_runbook_entry", "args": {}}], checks)["passed"])

    def test_numbers_are_explicit_exact_and_tool_scoped(self):
        check = self.check("field_eq_number", path="args.amount", value=231)
        with patch("graders.llm_judge.judge_field_value", side_effect=AssertionError("mechanical model call")):
            self.assertFalse(self.grade([{"tool": "unrelated", "args": {"amount": 231}}], [check])["passed"])
            self.assertFalse(self.grade([{"tool": "send_sms", "args": {"amount": 231.0000001}}], [check])["passed"])
            self.assertTrue(self.grade([{"tool": "send_sms", "args": {"amount": "231.0"}}], [check])["passed"])
            check = self.check(path="args.id", value="1")
            self.assertFalse(self.grade([{"tool": "send_sms", "args": {"id": "001"}}], [check])["passed"])

    def test_llm_always_uses_generated_rubric(self):
        check = self.check("field_llm_judge", path="args.to", criterion="The text goes to Alex's climbing partner Ren.")
        call = {"tool": "send_sms", "args": {"to": "Ren", "body": "Tuesday?"}}
        with patch("graders.llm_judge.judge_field_value", return_value={"ok": True, "reason": "same contact"}) as judge:
            self.assertTrue(self.grade([call], [check])["passed"])
            self.assertEqual(judge.call_count, 1)
            self.assertEqual(judge.call_args.args, ("Ren", check["criterion"]))
            self.assertIn("Text my partner", judge.call_args.kwargs["context"])
            self.assertIn("Tuesday?", judge.call_args.kwargs["context"])

    def test_model_error_is_not_a_negative_example(self):
        check = self.check("field_llm_judge", path="args.body", criterion="Contains the plan.")
        with patch("graders.llm_judge.judge_field_value", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                self.grade([{"tool": "send_sms", "args": {"body": "Plan"}}], [check])

    def test_two_requested_actions_need_two_calls(self):
        checks = [self.check(path="args.to", value="Ren"),
                  self.check(name="second", action="second_text", path="args.to", value="Ren")]
        call = {"tool": "send_sms", "args": {"to": "Ren"}}
        self.assertFalse(self.grade([call], checks)["passed"])
        self.assertTrue(self.grade([call, call], checks)["passed"])

    def test_rejects_hybrid_and_unknown_checks(self):
        for kind in ("recipient_matches", "email_recipients_received", "tool_called_with_value", "guess"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.grade([], [self.check(kind)])

    def test_timezone_equivalence_is_a_declared_comparison(self):
        check = self.check("datetime_absolute_eq", path="args.start", value="2028-01-01T12:00:00Z")
        self.assertTrue(self.grade([{"tool": "send_sms", "args": {"start": "2028-01-01T07:00:00-05:00"}}], [check])["passed"])
        self.assertFalse(self.grade([{"tool": "send_sms", "args": {"start": "2028-01-01T12:00:00"}}], [check])["passed"])

    def test_missing_empty_zero_and_null_are_distinct(self):
        for expected, actual, passes in [(0, False, False), (0, 0, True), (None, None, True), ("1", 1, False)]:
            with self.subTest(expected=expected, actual=actual):
                check = self.check(path="args.value", value=expected)
                self.assertEqual(self.grade([{"tool": "send_sms", "args": {"value": actual}}], [check])["passed"], passes)
                self.assertFalse(self.grade([{"tool": "send_sms", "args": {}}], [check])["passed"])
        check = self.check("field_absent_or_empty", path="args.value")
        self.assertFalse(self.grade([{"tool": "send_sms", "args": {"value": 0}}], [check])["passed"])
        self.assertTrue(self.grade([{"tool": "send_sms", "args": {}}], [check])["passed"])

    def test_numeric_boundaries_and_lists(self):
        cases = [("field_lte", {"value": 100}, 100, True),
                 ("field_lte", {"value": 100}, 100.01, False),
                 ("field_gte", {"value": 0}, False, False),
                 ("field_gte", {"value": 0}, "NaN", False),
                 ("field_list_includes", {"values": ["Ren", "Jo"]}, ["Jo", "Ren", "Pat"], True),
                 ("field_list_includes", {"values": ["Ren"]}, ["ren"], False)]
        for kind, operands, actual, expected in cases:
            with self.subTest(kind=kind, actual=actual):
                check = self.check(kind, path="args.value", **operands)
                self.assertEqual(self.grade([{"tool": "send_sms", "args": {"value": actual}}], [check])["passed"], expected)

    def test_bad_configuration_raises_instead_of_guessing(self):
        malformed = [self.check(path="args.value", value=1, criterion="One"),
                     self.check("field_llm_judge", path="args.value", criterion="One", value=1),
                     self.check("field_eq_number", path="args.value", value=True),
                     self.check("date_on", path="args.value", value="not a date"),
                     self.check("field_equals", action="", path="args.value", value=1)]
        for check in malformed:
            with self.subTest(check=check), self.assertRaises(ValueError):
                self.grade([], [check])
        check = self.check("tool_called")
        with self.assertRaisesRegex(ValueError, "unique"):
            self.grade([], [check, check])

    def test_matching_does_not_depend_on_greedy_call_order(self):
        checks = [self.check("tool_called", name="any", action="any_text"),
                  self.check(path="args.to", value="Ren")]
        calls = [{"tool": "send_sms", "args": {"to": "Ren"}},
                 {"tool": "send_sms", "args": {"to": "Jo"}}]
        self.assertTrue(self.grade(calls, checks)["passed"])
