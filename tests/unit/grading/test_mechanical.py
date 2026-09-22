"""Unit tests for graders/mechanical.py primitives.

Covers the new named primitives introduced after the B8 (field_contains)
and P2 (datetime_gte) bugs:

  - field_substring        — value as substring of string OR list element
  - field_in_list          — exact element membership in a list
  - time_of_day_gte/lte    — HH:MM comparison ignoring date
  - date_on / date_on_or_{after,before} — date-only comparison
  - datetime_absolute_gte/lte — full ISO datetime comparison
  - weekday_in             — date falls on one of named weekdays

Plus deprecation-warning tests for `field_contains`, `datetime_gte`,
`datetime_lte` (they still grade as before but emit a warning).

Runnable via `python3 tests/graders/test_mechanical.py` (no pytest
required) and also picked up by `pytest tests/graders/`. Repo has no
pytest in requirements.txt — the unittest module is stdlib.
"""

from __future__ import annotations

import sys
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

# Make `graders.mechanical` importable when run as a script from
# anywhere (`python3 tests/graders/test_mechanical.py` from repo root,
# or via pytest from any cwd).
DOLPHINBENCH_ROOT = Path(__file__).resolve().parents[3]
if str(DOLPHINBENCH_ROOT) not in sys.path:
    sys.path.insert(0, str(DOLPHINBENCH_ROOT))

from graders.mechanical import grade_hybrid, grade_tool_trace  # noqa: E402


# =========================================================================
# Helpers — build tool-call traces of the shape the harness produces.
# =========================================================================

def _call(tool: str, **args) -> dict:
    """Build a single tool-call record matching the MCP log shape."""
    return {"tool": tool, "args": dict(args), "result": {"ok": True}}


def _grade(calls: list[dict], assertion: dict,
           required_tool: str | None = None) -> dict:
    """Run a single assertion through grade_tool_trace and return the
    result dict. Filters out the synthetic required_tool check so the
    caller only sees their own assertion's pass/fail in details[0]."""
    cfg: dict = {"assertions": [assertion]}
    if required_tool:
        cfg["required_tool"] = required_tool
    return grade_tool_trace(calls, cfg)


def _passed(calls: list[dict], assertion: dict,
            required_tool: str | None = None) -> bool:
    """Convenience: did the assertion pass? Strips the synthetic
    required_tool check by indexing the assertion's own details entry."""
    res = _grade(calls, assertion, required_tool)
    # When required_tool is set, details[0] is the required_tool check
    # and details[1] is the user assertion. Otherwise details[0] is
    # the user assertion.
    idx = 1 if required_tool else 0
    return res["details"][idx]["ok"]


class TestSavedGradingEvidence(unittest.TestCase):
    def test_assertion_result_keeps_rule_explanation_and_observed_value(self):
        calls = [_call("send_email", to="person@example.com")]
        assertion = {
            "type": "field_equals",
            "tool": "send_email",
            "path": "args.to",
            "value": "person@example.com",
        }

        detail = _grade(calls, assertion)["details"][0]

        self.assertTrue(detail["ok"])
        self.assertEqual(detail["assertion"], assertion)
        self.assertEqual(
            detail["observed_values"],
            [{"tool": "send_email", "value": "person@example.com"}],
        )
        self.assertTrue(detail["reason"])


class TestRecipientAndEmptyFieldChecks(unittest.TestCase):
    def test_literal_recipient_passes_without_an_llm_call(self):
        assertion = {
            "type": "recipient_matches",
            "tool": "send_slack_dm",
            "path": "args.user",
            "value": "Priya Nair",
        }
        with patch("graders.llm_judge.judge_field_value") as judge:
            self.assertTrue(
                _passed([_call("send_slack_dm", user="Priya Nair")], assertion)
            )
        judge.assert_not_called()

    def test_different_recipient_uses_the_alias_judge(self):
        assertion = {
            "type": "recipient_matches",
            "tool": "send_slack_dm",
            "path": "args.user",
            "value": "Priya Nair",
        }
        with patch(
            "graders.llm_judge.judge_field_value",
            return_value={"ok": True, "reason": "same person"},
        ) as judge:
            self.assertTrue(
                _passed([_call("send_slack_dm", user="@priya")], assertion)
            )
        judge.assert_called_once()

    def test_absent_or_empty_field_rejects_an_external_cc(self):
        assertion = {
            "type": "field_absent_or_empty",
            "tool": "send_email",
            "path": "args.cc",
        }
        self.assertTrue(
            _passed([_call("send_email", to="sarah@scaffold.dev")], assertion)
        )
        self.assertFalse(
            _passed(
                [
                    _call(
                        "send_email",
                        to="sarah@scaffold.dev",
                        cc=["greg@acme.com"],
                    )
                ],
                assertion,
            )
        )


# =========================================================================
# field_substring
# =========================================================================

class TestFieldSubstring(unittest.TestCase):
    """Substring search across string OR list-of-strings fields."""

    # --- pass cases ---

    def test_substring_in_string(self):
        calls = [_call("send_email", subject="weekly status update")]
        a = {"type": "field_substring", "path": "args.subject",
             "value": "status"}
        self.assertTrue(_passed(calls, a))

    def test_full_string_match(self):
        calls = [_call("send_email", to="alice@example.com")]
        a = {"type": "field_substring", "path": "args.to",
             "value": "alice@example.com"}
        self.assertTrue(_passed(calls, a))

    def test_substring_in_list_element(self):
        # B8 regression: cc=["sarah.chen@org.io"], value="sarah@" should
        # PASS because "sarah" appears as substring of an element.
        # Old field_contains would have failed (set membership).
        calls = [_call("send_email",
                       cc=["sarah.chen@org.io", "marcus@org.io"])]
        a = {"type": "field_substring", "path": "args.cc",
             "value": "sarah"}
        self.assertTrue(_passed(calls, a))

    def test_match_in_one_of_multiple_calls(self):
        calls = [
            _call("send_email", subject="lunch"),
            _call("send_email", subject="weekly status update"),
        ]
        a = {"type": "field_substring", "path": "args.subject",
             "value": "status"}
        self.assertTrue(_passed(calls, a))

    def test_tool_scoped_match(self):
        calls = [
            _call("create_calendar_event", title="status review"),
            _call("send_email", subject="lunch"),
        ]
        a = {"type": "field_substring", "tool": "create_calendar_event",
             "path": "args.title", "value": "status"}
        self.assertTrue(_passed(calls, a))

    # --- fail cases ---

    def test_substring_not_in_string(self):
        calls = [_call("send_email", subject="weekly status update")]
        a = {"type": "field_substring", "path": "args.subject",
             "value": "metrics"}
        self.assertFalse(_passed(calls, a))

    def test_substring_not_in_any_list_element(self):
        calls = [_call("send_email",
                       cc=["alice@org.io", "bob@org.io"])]
        a = {"type": "field_substring", "path": "args.cc",
             "value": "sarah"}
        self.assertFalse(_passed(calls, a))

    def test_field_missing(self):
        calls = [_call("send_email", subject="hi")]
        a = {"type": "field_substring", "path": "args.body",
             "value": "anything"}
        self.assertFalse(_passed(calls, a))

    def test_field_is_none(self):
        calls = [_call("send_email", subject=None)]
        a = {"type": "field_substring", "path": "args.subject",
             "value": "anything"}
        self.assertFalse(_passed(calls, a))

    def test_field_is_number_not_string_or_list(self):
        # Numeric / bool fields don't substring-match — should fail
        # cleanly, not raise.
        calls = [_call("send_email", priority=1)]
        a = {"type": "field_substring", "path": "args.priority",
             "value": "1"}
        self.assertFalse(_passed(calls, a))

    # --- edge cases ---

    def test_empty_string_value_matches_string(self):
        # Pythonic: "" in "anything" is True. Worth pinning.
        calls = [_call("send_email", subject="hi")]
        a = {"type": "field_substring", "path": "args.subject",
             "value": ""}
        self.assertTrue(_passed(calls, a))

    def test_list_with_non_string_elements(self):
        # Mixed list — should still find the match in the string
        # element without crashing on the int.
        calls = [_call("create_calendar_event",
                       attendees=["sarah.chen@org.io", 42])]
        a = {"type": "field_substring", "path": "args.attendees",
             "value": "sarah"}
        self.assertTrue(_passed(calls, a))


# =========================================================================
# field_in_list
# =========================================================================

class TestFieldInList(unittest.TestCase):
    """Exact element membership in a list field."""

    # --- pass cases ---

    def test_element_present(self):
        calls = [_call("send_email",
                       cc=["alice@org.io", "bob@org.io"])]
        a = {"type": "field_in_list", "path": "args.cc",
             "value": "alice@org.io"}
        self.assertTrue(_passed(calls, a))

    def test_single_element_list(self):
        calls = [_call("create_calendar_event",
                       attendees=["priya.sharma@org.io"])]
        a = {"type": "field_in_list", "path": "args.attendees",
             "value": "priya.sharma@org.io"}
        self.assertTrue(_passed(calls, a))

    def test_match_across_multiple_calls(self):
        calls = [
            _call("send_email", cc=["x@org.io"]),
            _call("send_email", cc=["alice@org.io", "bob@org.io"]),
        ]
        a = {"type": "field_in_list", "path": "args.cc",
             "value": "alice@org.io"}
        self.assertTrue(_passed(calls, a))

    def test_tool_scoped_membership(self):
        calls = [
            _call("send_email", cc=["wrong@org.io"]),
            _call("create_calendar_event",
                  attendees=["alice@org.io"]),
        ]
        a = {"type": "field_in_list", "tool": "create_calendar_event",
             "path": "args.attendees", "value": "alice@org.io"}
        self.assertTrue(_passed(calls, a))

    # --- fail cases ---

    def test_substring_not_element_fails(self):
        # B8 regression: field_in_list should NOT pass on substring
        # matches. value="sarah" against ["sarah.chen@org.io"] FAILS
        # because no element equals "sarah" exactly.
        # Old field_contains would have ALSO failed via set membership
        # here, so this is the inverse safety check: the new
        # field_in_list keeps the strict-membership semantics rubric
        # authors usually want.
        calls = [_call("send_email",
                       cc=["sarah.chen@org.io"])]
        a = {"type": "field_in_list", "path": "args.cc",
             "value": "sarah"}
        self.assertFalse(_passed(calls, a))

    def test_element_absent(self):
        calls = [_call("send_email",
                       cc=["alice@org.io", "bob@org.io"])]
        a = {"type": "field_in_list", "path": "args.cc",
             "value": "carol@org.io"}
        self.assertFalse(_passed(calls, a))

    def test_field_is_string_not_list(self):
        # field_in_list explicitly REQUIRES a list. A bare string
        # field — even one equal to the value — should fail; that's
        # the whole point of disambiguating from field_substring.
        calls = [_call("send_email", to="alice@org.io")]
        a = {"type": "field_in_list", "path": "args.to",
             "value": "alice@org.io"}
        self.assertFalse(_passed(calls, a))

    def test_field_missing(self):
        calls = [_call("send_email", subject="hi")]
        a = {"type": "field_in_list", "path": "args.cc",
             "value": "alice@org.io"}
        self.assertFalse(_passed(calls, a))

    def test_empty_list(self):
        calls = [_call("send_email", cc=[])]
        a = {"type": "field_in_list", "path": "args.cc",
             "value": "alice@org.io"}
        self.assertFalse(_passed(calls, a))

    # --- edge cases ---

    def test_exact_match_with_substring_neighbor(self):
        # value is a true substring of one element AND equal to
        # another. Must pass on the equality, not on the substring.
        calls = [_call("send_email",
                       cc=["sarah", "sarah.chen@org.io"])]
        a = {"type": "field_in_list", "path": "args.cc",
             "value": "sarah"}
        self.assertTrue(_passed(calls, a))


# =========================================================================
# time_of_day_gte / time_of_day_lte
# =========================================================================

class TestTimeOfDay(unittest.TestCase):
    """HH:MM comparison ignoring the date component."""

    # --- pass cases ---

    def test_after_threshold_same_day(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-23T14:00:00-07:00")]
        a = {"type": "time_of_day_gte", "path": "args.start",
             "value": "10:00"}
        self.assertTrue(_passed(calls, a))

    def test_exact_match_gte(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-23T10:00:00-07:00")]
        a = {"type": "time_of_day_gte", "path": "args.start",
             "value": "10:00"}
        self.assertTrue(_passed(calls, a))

    def test_before_threshold_lte(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-23T08:00:00-07:00")]
        a = {"type": "time_of_day_lte", "path": "args.start",
             "value": "10:00"}
        self.assertTrue(_passed(calls, a))

    def test_evening_after_morning_threshold(self):
        # Date is irrelevant — 18:00 on any day is after 10:00.
        calls = [_call("create_calendar_event",
                       start="2026-04-23T18:30:00-07:00")]
        a = {"type": "time_of_day_gte", "path": "args.start",
             "value": "10:00"}
        self.assertTrue(_passed(calls, a))

    def test_match_in_one_of_multiple_calls(self):
        calls = [
            _call("create_calendar_event",
                  start="2026-04-23T08:00:00-07:00"),
            _call("create_calendar_event",
                  start="2026-04-23T14:00:00-07:00"),
        ]
        a = {"type": "time_of_day_gte", "path": "args.start",
             "value": "10:00"}
        self.assertTrue(_passed(calls, a))

    # --- fail cases ---

    def test_p2_regression_morning_fails_after_10am_check(self):
        # P2 bug: rubric used datetime_gte with anchor 2026-04-18T...
        # to mean "after 10am". An 8am booking on 2026-04-23 silently
        # passed because the absolute date was newer. With the named
        # time_of_day_gte primitive the same booking now correctly
        # FAILS the 10:00 threshold.
        calls = [_call("create_calendar_event",
                       start="2026-04-23T08:00:00-07:00")]
        a = {"type": "time_of_day_gte", "path": "args.start",
             "value": "10:00"}
        self.assertFalse(_passed(calls, a))

    def test_lte_fails_when_after_threshold(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-23T11:00:00-07:00")]
        a = {"type": "time_of_day_lte", "path": "args.start",
             "value": "10:00"}
        self.assertFalse(_passed(calls, a))

    def test_minute_precision(self):
        # 09:59 fails 10:00 gte by 1 minute.
        calls = [_call("create_calendar_event",
                       start="2026-04-23T09:59:00-07:00")]
        a = {"type": "time_of_day_gte", "path": "args.start",
             "value": "10:00"}
        self.assertFalse(_passed(calls, a))

    def test_invalid_value_fails(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-23T14:00:00-07:00")]
        a = {"type": "time_of_day_gte", "path": "args.start",
             "value": "not-a-time"}
        self.assertFalse(_passed(calls, a))

    def test_missing_field(self):
        calls = [_call("create_calendar_event")]
        a = {"type": "time_of_day_gte", "path": "args.start",
             "value": "10:00"}
        self.assertFalse(_passed(calls, a))

    # --- edge cases ---

    def test_naive_datetime_no_tz(self):
        # ISO without timezone — wall-clock comparison still works.
        calls = [_call("create_calendar_event",
                       start="2026-04-23T14:00:00")]
        a = {"type": "time_of_day_gte", "path": "args.start",
             "value": "10:00"}
        self.assertTrue(_passed(calls, a))

    def test_tz_local_time_used_not_utc_normalized(self):
        # Field is 09:00 in -08:00 (i.e. 17:00 UTC). The primitive
        # docstring says it uses the wall-clock time encoded in the
        # field, NOT a UTC-normalized comparison. So 09:00 < 10:00
        # local must FAIL even though 17:00 UTC > 10:00 UTC.
        calls = [_call("create_calendar_event",
                       start="2026-04-23T09:00:00-08:00")]
        a = {"type": "time_of_day_gte", "path": "args.start",
             "value": "10:00"}
        self.assertFalse(_passed(calls, a))


# =========================================================================
# date_on / date_on_or_after / date_on_or_before
# =========================================================================

class TestDateComparisons(unittest.TestCase):
    """Date-only comparison ignoring time and timezone."""

    # --- date_on pass ---

    def test_date_on_exact(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-23T14:00:00-07:00")]
        a = {"type": "date_on", "path": "args.start",
             "value": "2026-04-23"}
        self.assertTrue(_passed(calls, a))

    def test_date_on_match_across_calls(self):
        calls = [
            _call("create_calendar_event",
                  start="2026-04-22T14:00:00-07:00"),
            _call("create_calendar_event",
                  start="2026-04-23T14:00:00-07:00"),
        ]
        a = {"type": "date_on", "path": "args.start",
             "value": "2026-04-23"}
        self.assertTrue(_passed(calls, a))

    # --- date_on fail ---

    def test_date_on_wrong_day(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-22T23:59:00-07:00")]
        a = {"type": "date_on", "path": "args.start",
             "value": "2026-04-23"}
        self.assertFalse(_passed(calls, a))

    # --- date_on_or_after pass ---

    def test_date_on_or_after_strictly_later(self):
        calls = [_call("create_calendar_event",
                       start="2026-05-01T09:00:00-07:00")]
        a = {"type": "date_on_or_after", "path": "args.start",
             "value": "2026-04-23"}
        self.assertTrue(_passed(calls, a))

    def test_date_on_or_after_equal(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-23T00:01:00-07:00")]
        a = {"type": "date_on_or_after", "path": "args.start",
             "value": "2026-04-23"}
        self.assertTrue(_passed(calls, a))

    # --- date_on_or_after fail ---

    def test_date_on_or_after_earlier_fails(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-22T23:59:00-07:00")]
        a = {"type": "date_on_or_after", "path": "args.start",
             "value": "2026-04-23"}
        self.assertFalse(_passed(calls, a))

    # --- date_on_or_before ---

    def test_date_on_or_before_pass(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-22T08:00:00-07:00")]
        a = {"type": "date_on_or_before", "path": "args.start",
             "value": "2026-04-23"}
        self.assertTrue(_passed(calls, a))

    def test_date_on_or_before_equal_pass(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-23T23:59:00-07:00")]
        a = {"type": "date_on_or_before", "path": "args.start",
             "value": "2026-04-23"}
        self.assertTrue(_passed(calls, a))

    def test_date_on_or_before_strictly_after_fails(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-24T00:01:00-07:00")]
        a = {"type": "date_on_or_before", "path": "args.start",
             "value": "2026-04-23"}
        self.assertFalse(_passed(calls, a))

    # --- edges ---

    def test_invalid_date_value_fails(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-23T14:00:00-07:00")]
        a = {"type": "date_on", "path": "args.start",
             "value": "not-a-date"}
        self.assertFalse(_passed(calls, a))


# =========================================================================
# datetime_absolute_gte / datetime_absolute_lte
# =========================================================================

class TestDatetimeAbsolute(unittest.TestCase):
    """Full absolute datetime comparison — what the deprecated
    datetime_gte was always doing, but with an unambiguous name."""

    def test_gte_strictly_later(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-23T14:00:00-07:00")]
        a = {"type": "datetime_absolute_gte", "path": "args.start",
             "value": "2026-04-18T12:00:00-07:00"}
        self.assertTrue(_passed(calls, a))

    def test_gte_equal(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-18T12:05:00-07:00")]
        a = {"type": "datetime_absolute_gte", "path": "args.start",
             "value": "2026-04-18T12:05:00-07:00"}
        self.assertTrue(_passed(calls, a))

    def test_gte_earlier_fails(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-17T08:00:00-07:00")]
        a = {"type": "datetime_absolute_gte", "path": "args.start",
             "value": "2026-04-18T12:00:00-07:00"}
        self.assertFalse(_passed(calls, a))

    def test_lte_strictly_earlier(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-17T08:00:00-07:00")]
        a = {"type": "datetime_absolute_lte", "path": "args.start",
             "value": "2026-04-18T12:00:00-07:00"}
        self.assertTrue(_passed(calls, a))

    def test_lte_later_fails(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-23T14:00:00-07:00")]
        a = {"type": "datetime_absolute_lte", "path": "args.start",
             "value": "2026-04-18T12:00:00-07:00"}
        self.assertFalse(_passed(calls, a))

    def test_invalid_iso_fails(self):
        calls = [_call("create_calendar_event", start="not-a-date")]
        a = {"type": "datetime_absolute_gte", "path": "args.start",
             "value": "2026-04-18T12:00:00-07:00"}
        self.assertFalse(_passed(calls, a))


# =========================================================================
# weekday_in
# =========================================================================

class TestWeekdayIn(unittest.TestCase):
    """Verify the field's date falls on one of the named weekdays.

    M5 / T9 use cases: 'must schedule on Friday', 'avoid weekends'."""

    # --- pass ---

    def test_friday_pass(self):
        # 2026-04-24 is a Friday.
        calls = [_call("create_calendar_event",
                       start="2026-04-24T10:00:00-07:00")]
        a = {"type": "weekday_in", "path": "args.start",
             "value": ["Friday"]}
        self.assertTrue(_passed(calls, a))

    def test_weekday_set_pass(self):
        # 2026-04-22 is a Wednesday.
        calls = [_call("create_calendar_event",
                       start="2026-04-22T10:00:00-07:00")]
        a = {"type": "weekday_in", "path": "args.start",
             "value": ["Monday", "Tuesday", "Wednesday",
                       "Thursday", "Friday"]}
        self.assertTrue(_passed(calls, a))

    def test_case_insensitive(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-24T10:00:00-07:00")]
        a = {"type": "weekday_in", "path": "args.start",
             "value": ["friday"]}
        self.assertTrue(_passed(calls, a))

    def test_match_across_calls(self):
        calls = [
            _call("create_calendar_event",
                  start="2026-04-25T10:00:00-07:00"),  # Saturday
            _call("create_calendar_event",
                  start="2026-04-24T10:00:00-07:00"),  # Friday
        ]
        a = {"type": "weekday_in", "path": "args.start",
             "value": ["Friday"]}
        self.assertTrue(_passed(calls, a))

    # --- fail ---

    def test_wrong_weekday(self):
        # 2026-04-23 is a Thursday — not in the asked-for set.
        calls = [_call("create_calendar_event",
                       start="2026-04-23T10:00:00-07:00")]
        a = {"type": "weekday_in", "path": "args.start",
             "value": ["Friday"]}
        self.assertFalse(_passed(calls, a))

    def test_weekend_excluded(self):
        # 2026-04-25 is a Saturday.
        calls = [_call("create_calendar_event",
                       start="2026-04-25T10:00:00-07:00")]
        a = {"type": "weekday_in", "path": "args.start",
             "value": ["Monday", "Tuesday", "Wednesday",
                       "Thursday", "Friday"]}
        self.assertFalse(_passed(calls, a))

    def test_empty_value_fails(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-24T10:00:00-07:00")]
        a = {"type": "weekday_in", "path": "args.start",
             "value": []}
        self.assertFalse(_passed(calls, a))

    def test_invalid_iso_field_fails(self):
        calls = [_call("create_calendar_event", start="not-a-date")]
        a = {"type": "weekday_in", "path": "args.start",
             "value": ["Friday"]}
        self.assertFalse(_passed(calls, a))

    # --- edge ---

    def test_string_value_coerced_to_list(self):
        # Author wrote `value: Friday` (string) instead of `[Friday]`.
        calls = [_call("create_calendar_event",
                       start="2026-04-24T10:00:00-07:00")]
        a = {"type": "weekday_in", "path": "args.start",
             "value": "Friday"}
        self.assertTrue(_passed(calls, a))


# =========================================================================
# field_llm_judge — per-assertion `tool:` filter must scope which calls
# the judge sees. See docs/v1_to_v1_1_flip_audit.md (H8 section) for the
# bug write-up: previously the field_llm_judge branch ignored
# `a["tool"]` and ran the judge against every call in the trace, which
# could mask failures (criterion accidentally satisfied by another
# tool's args) or cause false fails (criterion checked against the
# wrong tool's args).
# =========================================================================

class TestExactToolCallCount(unittest.TestCase):
    """An exact count is available when duplicate actions are wrong."""

    def test_exact_count_passes_for_one_filtered_call(self):
        self.assertTrue(
            _passed(
                [_call("post_pr_comment", body="Looks good")],
                {"type": "tool_call_count", "tool": "post_pr_comment", "count": 1},
            )
        )

    def test_exact_count_rejects_duplicate_side_effects(self):
        self.assertFalse(
            _passed(
                [
                    _call("post_pr_comment", body="Looks good"),
                    _call("post_pr_comment", body="Second comment"),
                ],
                {"type": "tool_call_count", "tool": "post_pr_comment", "count": 1},
            )
        )

    def test_exact_count_ignores_other_tools(self):
        self.assertTrue(
            _passed(
                [
                    _call("post_pr_comment", body="Looks good"),
                    _call("send_email", to="reviewer@example.com"),
                ],
                {"type": "tool_call_count", "tool": "post_pr_comment", "count": 1},
            )
        )


class TestFieldLLMJudgeToolFilter(unittest.TestCase):
    """field_llm_judge must honor the per-assertion `tool:` filter, and
    must remain backward-compatible (judge all calls) when `tool:` is
    absent."""

    def _fake_judge(self, *, pass_when_value_contains: str):
        """Build a stand-in for graders.llm_judge.judge_field_value that
        records every (value, criterion) pair it's asked about and
        returns ok=True only when the value's JSON repr contains the
        marker substring. Lets us assert WHICH tool's args the judge
        was invoked on."""
        seen: list[dict] = []

        def fake(value, criterion, context="", config=None):
            seen.append({"value": value, "criterion": criterion})
            ok = (isinstance(value, str)
                  and pass_when_value_contains in value)
            return {"ok": ok, "reason": "ok" if ok else "no marker"}

        return fake, seen

    def test_tool_filter_scopes_judge_to_named_tool(self):
        # Two calls with the same `notes` path. tool_a's notes lack the
        # marker; tool_b's notes contain it. With `tool: tool_a` the
        # judge must only see tool_a's notes (and therefore fail) —
        # NOT silently pass off tool_b's notes.
        calls = [
            _call("tool_a", notes="meeting recap, action items pending"),
            _call("tool_b", notes="recap mentions Rishi as the intro"),
        ]
        a = {"type": "field_llm_judge", "tool": "tool_a",
             "path": "args.notes",
             "criterion": "Notes mention Rishi"}
        fake, seen = self._fake_judge(pass_when_value_contains="Rishi")
        with patch("graders.llm_judge.judge_field_value", side_effect=fake):
            ok = _passed(calls, a)
        self.assertFalse(
            ok,
            "tool: tool_a must scope the judge to tool_a's args; "
            "the criterion is only satisfied by tool_b and must NOT "
            "leak into the verdict",
        )
        # Belt-and-braces: judge must have been called exactly once,
        # and only on tool_a's notes value.
        self.assertEqual(len(seen), 1,
                         f"expected 1 judge call (tool_a only), got "
                         f"{len(seen)}: {seen}")
        self.assertEqual(seen[0]["value"],
                         "meeting recap, action items pending")

    def test_tool_filter_passes_when_named_tool_satisfies(self):
        # Mirror: with `tool: tool_a`, if tool_a IS the one with the
        # marker, the judge passes (and tool_b is not even consulted).
        calls = [
            _call("tool_a", notes="recap mentions Rishi as the intro"),
            _call("tool_b", notes="meeting recap, action items pending"),
        ]
        a = {"type": "field_llm_judge", "tool": "tool_a",
             "path": "args.notes",
             "criterion": "Notes mention Rishi"}
        fake, seen = self._fake_judge(pass_when_value_contains="Rishi")
        with patch("graders.llm_judge.judge_field_value", side_effect=fake):
            ok = _passed(calls, a)
        self.assertTrue(ok)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["value"],
                         "recap mentions Rishi as the intro")

    def test_no_tool_filter_judges_all_calls_backward_compat(self):
        # Without `tool:`, behavior must match the pre-fix code path:
        # judge runs across every tool call and ANY-pass semantics
        # apply. tool_a fails the marker, tool_b passes it → overall
        # ok=True, with the judge seeing BOTH calls (until the first
        # passing one short-circuits the loop, which is tool_b in this
        # ordering).
        calls = [
            _call("tool_a", notes="meeting recap, action items pending"),
            _call("tool_b", notes="recap mentions Rishi as the intro"),
        ]
        a = {"type": "field_llm_judge",  # no `tool:` field
             "path": "args.notes",
             "criterion": "Notes mention Rishi"}
        fake, seen = self._fake_judge(pass_when_value_contains="Rishi")
        with patch("graders.llm_judge.judge_field_value", side_effect=fake):
            ok = _passed(calls, a)
        self.assertTrue(ok, "without `tool:`, judge must consider all calls")
        # Both calls should have been considered (tool_a failed, then
        # tool_b passed and the loop short-circuits).
        self.assertEqual(len(seen), 2,
                         f"expected judge to walk both calls until first "
                         f"pass, got {len(seen)}: {seen}")
        self.assertEqual(
            [s["value"] for s in seen],
            ["meeting recap, action items pending",
             "recap mentions Rishi as the intro"],
        )

    def test_tool_filter_no_matching_calls_fails_cleanly(self):
        # If `tool:` is set but no call matches that tool, the judge
        # must NOT be invoked and the assertion must fail with the
        # "no tool calls to judge" reason — not silently pass and not
        # accidentally fall back to the unfiltered tool_calls list.
        calls = [
            _call("tool_b", notes="recap mentions Rishi as the intro"),
        ]
        a = {"type": "field_llm_judge", "tool": "tool_a",
             "path": "args.notes",
             "criterion": "Notes mention Rishi"}
        fake, seen = self._fake_judge(pass_when_value_contains="Rishi")
        with patch("graders.llm_judge.judge_field_value", side_effect=fake):
            res = _grade(calls, a)
        detail = res["details"][0]
        self.assertFalse(detail["ok"])
        self.assertEqual(len(seen), 0,
                         "judge must not be invoked when tool: filter "
                         "matches zero calls")
        self.assertIn("no tool calls to judge", detail.get("reason", ""))


# =========================================================================
# Deprecation aliases — still grade as before, but emit warnings
# =========================================================================

class TestDeprecationWarnings(unittest.TestCase):
    """field_contains, datetime_gte, datetime_lte still grade correctly
    (so existing test YAMLs keep passing) but must emit a
    DeprecationWarning so authors are nudged to migrate."""

    def test_field_contains_emits_warning(self):
        calls = [_call("send_email", cc=["alice@org.io"])]
        a = {"type": "field_contains", "path": "args.cc",
             "value": "alice@org.io"}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ok = _passed(calls, a)
        self.assertTrue(ok)
        self.assertTrue(any(issubclass(w.category, DeprecationWarning)
                            and "field_substring" in str(w.message)
                            for w in caught),
                        f"expected DeprecationWarning mentioning "
                        f"field_substring; got {[str(w.message) for w in caught]}")

    def test_datetime_gte_emits_warning(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-23T14:00:00-07:00")]
        a = {"type": "datetime_gte", "path": "args.start",
             "value": "2026-04-18T12:00:00-07:00"}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ok = _passed(calls, a)
        self.assertTrue(ok)
        self.assertTrue(any(issubclass(w.category, DeprecationWarning)
                            and "time_of_day" in str(w.message)
                            for w in caught),
                        f"expected DeprecationWarning mentioning time_of_day; "
                        f"got {[str(w.message) for w in caught]}")

    def test_datetime_lte_emits_warning(self):
        calls = [_call("create_calendar_event",
                       start="2026-04-17T08:00:00-07:00")]
        a = {"type": "datetime_lte", "path": "args.start",
             "value": "2026-04-18T12:00:00-07:00"}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ok = _passed(calls, a)
        self.assertTrue(ok)
        self.assertTrue(any(issubclass(w.category, DeprecationWarning)
                            for w in caught))

    def test_field_contains_preserves_old_behavior(self):
        # Belt-and-braces: the deprecated alias must still grade
        # identically to the pre-bugfix implementation. If we ever
        # drop the alias, this test should be removed too.
        calls = [_call("send_email", cc=["alice@org.io"])]
        a = {"type": "field_contains", "path": "args.cc",
             "value": "alice@org.io"}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            self.assertTrue(_passed(calls, a))


class TestHybridGraderShorthand(unittest.TestCase):
    """Regress the I3/I5/I6 audit bug: a hybrid grader whose only sub-
    block is a top-level ``rubric:`` (no ``llm_judge:``, ``regex:``, or
    ``tool_trace:``) used to return total_checks=0 / details=[]. After
    the fix, the rubric is promoted into an llm_judge sub-grader so the
    rubric actually runs.
    """

    def test_top_level_rubric_promoted_to_llm_judge(self):
        # Patch out the network-using llm_judge so we just verify the
        # promoted config reaches it with the rubric intact.
        captured: dict[str, object] = {}

        def fake_grade_llm_judge(response_text, test_message, cfg, tool_calls=None):
            captured["cfg"] = cfg
            captured["tool_calls"] = tool_calls
            return {
                "grader_type": "llm_judge",
                "passed": True,
                "score": 1.0,
                "details": [{"name": "stub", "ok": True, "reason": "stub"}],
                "total_checks": 1,
                "passed_checks": 1,
            }

        with patch("graders.llm_judge.grade_llm_judge", fake_grade_llm_judge):
            cfg = {"rubric": [{"id": "stub", "criterion": "anything"}]}
            r = grade_hybrid("response", [], cfg, test_message="t")
        self.assertEqual(r["total_checks"], 1)
        self.assertEqual(r["passed_checks"], 1)
        self.assertTrue(r["passed"])
        self.assertEqual(captured["cfg"]["rubric"],
                         [{"id": "stub", "criterion": "anything"}])

    def test_top_level_assertions_promoted_to_tool_trace(self):
        # A top-level assertions list (no `tool_trace:` sub-key) should
        # be promoted into a tool_trace sub-grader instead of being
        # silently dropped.
        calls = [_call("send_slack_message", to="priya")]
        cfg = {
            "assertions": [
                {"type": "tool_called", "tool": "send_slack_message"},
                {"type": "tool_not_called", "tool": "send_discord_message"},
            ],
        }
        r = grade_hybrid("ignored", calls, cfg, test_message="t")
        self.assertEqual(r["total_checks"], 2)
        self.assertEqual(r["passed_checks"], 2)
        self.assertTrue(r["passed"])

    def test_explicit_subblocks_take_precedence(self):
        # If both a top-level `rubric` and an explicit `llm_judge`
        # block are present, the explicit block wins (no double-grade).
        captured: list[dict] = []

        def fake_grade_llm_judge(response_text, test_message, cfg, tool_calls=None):
            captured.append(cfg)
            return {
                "grader_type": "llm_judge",
                "passed": True,
                "score": 1.0,
                "details": [],
                "total_checks": 1,
                "passed_checks": 1,
            }

        with patch("graders.llm_judge.grade_llm_judge", fake_grade_llm_judge):
            cfg = {
                "rubric": [{"id": "shorthand", "criterion": "x"}],
                "llm_judge": {"rubric": [{"id": "explicit", "criterion": "y"}]},
            }
            grade_hybrid("response", [], cfg, test_message="t")
        self.assertEqual(len(captured), 1, "llm_judge invoked exactly once")
        self.assertEqual(captured[0]["rubric"][0]["id"], "explicit")

    def test_hybrid_with_only_top_level_rubric_does_not_return_zero_checks(self):
        # Audit-bug regression: without the fix, a config that ONLY has
        # top-level `rubric:` produced total_checks=0 / details=[]
        # because none of the recognized sub-keys matched.
        def fake_grade_llm_judge(response_text, test_message, cfg, tool_calls=None):
            return {
                "grader_type": "llm_judge",
                "passed": False,
                "score": 0.0,
                "details": [{"name": "x", "ok": False, "reason": "stub"}],
                "total_checks": 1,
                "passed_checks": 0,
            }

        with patch("graders.llm_judge.grade_llm_judge", fake_grade_llm_judge):
            cfg = {"rubric": [{"id": "x", "criterion": "anything"}]}
            r = grade_hybrid("response", [], cfg, test_message="t")
        self.assertGreater(r["total_checks"], 0,
                           "hybrid grader must execute the top-level rubric")
        self.assertNotEqual(r["details"], [],
                            "details must include the rubric sub-result")


if __name__ == "__main__":
    unittest.main(verbosity=2)
