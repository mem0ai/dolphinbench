"""Plain-English instructions for the canonical DolphinBench test-authoring calls."""

from pathlib import Path

from authoring.models import MAX_PROPOSAL_COUNT


def _prompt_text(name: str) -> str:
    return (Path(__file__).parent / "prompt_text" / name).read_text(encoding="utf-8").strip()


IDEA_PLANNER_SYSTEM = _prompt_text("planner_v4.md")
COMPLETE_TEST_WRITER_SYSTEM = _prompt_text("writer_v4.md")
TEST_REVIEW_SYSTEM = _prompt_text("reviewer_v4.md")


REQUEST_QUALITY_REVIEW_RULES = """Review the work and wording before checking coverage. You are writing for the person deciding whether this test should be kept. Use plain language, name the concrete problem, and distinguish what the supplied material shows from your judgment.

In audit.request_quality, state who is asking and what useful result they need in reader_and_goal. Assess work_is_plausible: does that person have a credible reason to ask for this work now? Assess scope_is_necessary: does each requested part serve that goal, rather than reproduce more available facts? Assess request_is_natural: does the actual request sound like that person asking their assistant for help, rather than an evaluator listing expected answers? Explain each assessment using the actual situation and words, not labels such as "natural" or "covered" alone. Approval of a plan and a passing certification gate do not establish these qualities.

For each grading_assertions row, fill result_quality. Explain which necessary result the check measures and why another check does not already prove it. Mark it false for an optional or duplicate check. Do not infer necessity merely from the presence of a detail in a source or plan. Do not reduce the number of checks by merging independently necessary results.

Each assessment has passes, explanation, and issue_indexes. For a passing assessment, use an empty issue_indexes list. For a failing assessment, reference the zero-based issues that identify the offending requirement or wording, its owner, and the literal correction. Use planning when the approved work is implausible or unnecessarily broad; it needs a newly approved idea, not a writer silently removing work. Use user_request for wording introduced by the writer. Use grading_checks for an optional or duplicate check introduced during grading. An unnecessary approved result is still a planning error. Quote the offending request wording or cite the exact source passage in the linked issue as required by this review. Reject the test when any assessment fails.

Judge fit to the person and work, not length, vocabulary, or a fixed number of actions. Specialist language, required content in a document or email, remembered tool arguments, and genuinely multi-part tasks are valid. Do not apply word limits, jargon bans, or blanket bans on documents, facts, or multiple requirements. An investor email can need remembered funding context even though the tool stays the same. Standalone recall is not new work. The request and readable state must still hide the answer, and newer explicit instructions still take precedence over older facts."""


TEST_REQUIREMENTS_RULES = """Use these same requirements when writing or reviewing a test.

The approved work defines what the user wants done. The exact earlier messages define the remembered facts and their dates. The tool contracts define what can be executed. Do not let one of these substitute for another. Preserve the approved work in the final request; reject an unsupported approved requirement instead of inventing evidence or silently changing it.

Require realistic present-day work, not standalone recall. Earlier messages must supply a necessary observable part of that work that the request and readable starting state do not reveal. The tool name may stay the same: necessary remembered content, recipients, options, and other arguments all count. A failed request does not make its fact globally unusable. Respect later information and newer explicit user instructions.

Grade every distinct remembered result the work needs, prove every requested final external or state-changing action occurred, and identify its target when needed to prove the remembered result reached the right destination. Do not grade other request-supplied details or ordinary supporting reads. Do not grade a result twice, including separately forbidding its opposite when one check already proves the required result. A large fact is not a checklist of everything an answer must reproduce.

Accept normal equivalent completions. Reject missing, incomplete, contradictory, or negated required meaning. Preserve necessary conditions, timing, scope, and exceptions; recognizing a related topic is not enough. Do not infer extra restrictions on recipients, call counts, or wording. Use exact comparisons only when that literal value is required. Judge recipient delivery from actual recipient fields, not body mentions, and do not force a recipient into To when CC would complete the approved work.

For new check_version 2 tests, use each check's declared method and action_id. A complete action satisfies its target and content requirements on the same tool call. Judge semantic checks by their generated criteria; mechanical comparisons do not switch to semantic judging after failure. Explain temporal applicability from stated dates, conditions, and replacements, not source age alone.

Naming the subject of a question does not reveal its answer. For example, 'HSA eligibility' identifies a topic; it does not say whether the person is eligible. A writer's list of hidden strings is a clue to inspect, not independent proof of answer leakage. Compare the actual request and readable starting records with what the source establishes."""


def test_idea_planner_system(count: int) -> str:
    """Return the first-call instructions for ``count`` proposals."""
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or not 1 <= count <= MAX_PROPOSAL_COUNT
    ):
        raise ValueError(
            f"proposal count must be an integer from 1 through {MAX_PROPOSAL_COUNT}"
        )
    return _prompt_text("planner.md")


# Existing callers prepare the default ten-proposal batch.
TEST_IDEA_PLANNER_SYSTEM = test_idea_planner_system(10)


def evidence_planner_system(count: int) -> str:
    """Return the source-grounded second-call instructions for ``count`` proposals."""
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or not 1 <= count <= MAX_PROPOSAL_COUNT
    ):
        raise ValueError(
            f"proposal count must be an integer from 1 through {MAX_PROPOSAL_COUNT}"
        )
    return _prompt_text("evidence_checker.md")


EVIDENCE_PLANNER_SYSTEM = evidence_planner_system(10)


DESIGN_SYSTEM = """Turn one proposed test into an executable test, or reject it with one exact reason.

The input contains:
- planned_task: the new situation, the work the user wants done, the selected fact IDs, the expected tools, the result history should produce, what an assistant without history could do, and the planner's exact source evidence for every fact-backed result;
- selected_fact_evidence: the official fact text and the complete earlier user messages that support each selected fact;
- current_facts_that_may_change_the_meaning: other facts connected to a selected fact through explicit replacement links;
- tools: complete tool names, arguments, read behavior, write behavior, and limits;
- current_readable_app_state: the records those tools can currently read;
- evaluation_date: the date of the new request.

Your job has three parts.

First, read the complete earlier user messages. State exactly what they establish and whether that information still applies on the evaluation date. Do not make a one-time instruction into a permanent rule. Respect later facts that replace or narrow it. Reject the proposal if the source messages do not support the planned use.

Verify the planner's exact source evidence and its explanation for why the listed tools can complete the work. The source passage must support the stated result, and the result must still apply on the evaluation date. Reject the proposal when the approved evidence does not support the planned use.

The user's request must ask for work that naturally requires the remembered information. Write the requested work as a normal instruction: state its purpose, audience, and broad subjects. Do not list every remembered detail separately. Do not place a requirement only in the expected result when the request does not naturally require it.

Do not turn a narrow restriction into a broader prohibition. Require only what the source messages actually establish.

An earlier request shows what the user wanted done. It does not, by itself, prove that the assistant completed the action or that the action did not occur. If the supplied messages do not report an outcome, do not require an answer claiming that the past action happened, did not happen, or has no record. Test the information those messages actually establish. Explicit negative facts are valid when the source explicitly states them, and a newer explicit request takes precedence over older information.

Second, verify that the proposed work is realistic and executable with the supplied tools. Every non-memory input needed for the work must come from the future request, an existing readable record, or one neutral new record. A neutral record must not contain the remembered answer. For list-backed app state, use an empty record_key and put the record identifier in record.id. For dictionary-backed app state, provide record_key. Reject the proposal if a required recipient, address, record, date, identifier, or tool capability is unavailable.

When the task sends something to a person, the present situation must include the exact email address, phone number, handle, or record ID unless remembering that identifier is itself what the test measures. Do not create a test that can fail merely because an assistant without history cannot locate the recipient.

Third, prepare these four fields for the separate request writer.
- present_situation: concrete facts about the current situation that the user can say directly.
- requested_work: the concrete work the user is asking the assistant to do.
- missing_information: describe the missing kind of information the assistant needs to work out from earlier messages, without writing its answer value.
- information_the_request_must_not_reveal: state the date, label, recipient, decision, or factual claim that the request must leave unstated, again without writing its answer value.

The first two fields may contain only present-day information and the requested work. The last two fields name missing semantic details, not their answer values. Do not state, hint at, or point indirectly to the remembered answer. Do not use phrases such as "my usual", "my normal", "my preferred", "as we discussed", "use what I told you", "already established", "from the context available to you", or "based on the background context" as a substitute for the missing answer. Ordinary uses of words such as "normal", "previous", and "earlier" are allowed when they identify real present-day work rather than unseen history. Preserve the planned action. Do not turn drafting into sending, creating into updating, reviewing into posting, or one tool operation into another.

Keeping the answer out of the user's request means the user does not state the answer. It does not mean the request tells the assistant not to provide it. Never instruct the assistant to omit required content, leave a required value, owner, or boundary blank, use TBD, or wait for later confirmation when the planned result requires that content. The omitted fact must be determined by the assistant and included in the completed work.

Return the smallest list of results whose correct value, content, or choice depends on the earlier messages. For each result, list the selected earlier-message fact IDs that supply the needed information. Every result must have at least one fact ID, and every selected fact ID must appear in at least one result. Do not include an action or detail merely because the current request or current app state requires it. Put every tool needed to complete the request, including supporting reads and final actions, in expected_tool_calls. Do not write grading checks, answer-bearing values, expected history results, or a failed-check index. A separate grading call receives the exact final user request and does that work. Your job ends after source validation, state preparation, the four request-writing fields, required results, and expected tool calls.

Reject this proposal if a capable assistant that has never seen the earlier messages would normally complete the requested work the same way. Do not reject real personalization: if Morgan's earlier messages say she chooses Blue Bottle for early pickups, Blue Bottle can be required even though another coffee shop would be reasonable.

The later grading call will check every remembered result and prove that every requested final action occurred. It receives the exact final request and the complete arguments of the same action as the field being checked. Make sure required_results contains only results that depend on the earlier messages. Each selected fact must support at least one required result. Do not write grading checks here.

Return outcome "reject" when the evidence, timing, tools, records, or memory dependence do not support the proposal. For a rejection, give the exact reason, keep source_message_meaning and reason_it_applies_on_test_date non-empty, and leave the state and tool lists empty.

Return exactly one JSON object with these fields:
{
  "outcome": "design or reject",
  "rejection_reason": "empty for a design; exact reason for a rejection",
  "source_message_meaning": "what the exact source messages establish",
  "reason_it_applies_on_test_date": "why it applies or does not apply on the evaluation date",
  "present_situation": "concrete present-day facts the user may state",
  "requested_work": "the concrete work the user asks for",
  "missing_information": "missing information type, without its value",
  "information_the_request_must_not_reveal": "claim the request must leave unstated, without its value",
  "existing_records": [
    {"state_key":"...","record_key":"","match":{},"remove_fields":[]}
  ],
  "new_records": [
    {"state_key":"...","record_key":"","record":{}}
  ],
  "required_results": [
    {"result":"a material result the completed work must achieve","fact_ids":[1]}
  ],
  "expected_tool_calls": ["tool_name"]
}
Use empty strings and lists for fields unused by a rejection. Do not add fields."""


BLIND_QUERY_SYSTEM = """Write one natural request from the user to the assistant.

You receive only four fact-free fields: present_situation, requested_work, missing_information, and information_the_request_must_not_reveal. Write the complete message the user would actually send. Use the first two fields for the concrete present-day context and requested work.

Do not add a date, label or classification, recipient, decision, or factual claim unless it appears in present_situation or requested_work. Do not state anything named in information_the_request_must_not_reveal. Leave the information named in missing_information for the assistant to determine from earlier messages and use in the completed work.

Do not invent the answer to any unstated choice. Do not mention earlier messages, memory, preferences, rules, or facts. Do not add phrases such as "my usual", "my normal", "my preferred", "as we discussed", "you know how I like it", "use what I told you", "already established", "from the context available to you", or "based on the background context" as a substitute for the missing answer. Ordinary uses of words such as "normal", "previous", and "earlier" are allowed when they identify real present-day work rather than unseen history. Do not refer vaguely to unseen material.

The assistant must use the missing information when completing the work. It may supply content to include, limit what may be shared or done, determine a choice, or change how the work is completed. Follow requested_work exactly. Include the concrete details from present_situation that the assistant needs. Do not replace them with a vague reference to an unavailable note or document. Treat those details as background, not automatically as content that must appear in the completed work. Do not turn a detail from present_situation into an instruction to include that detail unless requested_work asks for it. Do not write instructions to leave required values, owners, or boundaries blank, use TBD, or wait for later confirmation. A natural request such as "send an investor update with our latest wins and funding context" is valid: it leaves the funding details unstated while requiring the assistant to determine and include them.

Preserve the requested action exactly. Do not turn a draft into a sent message, a review into a posted comment, a suggestion into a scheduled event, or a new record into an update.

Return only this JSON object: {"query":"..."}."""


GRADING_DESIGN_SYSTEM = """Write the smallest fair set of grading checks for one completed test.

The input contains:
- final_user_request: the exact message the user will send;
- planned_required_results: every result whose correct value, content, or choice depends on the earlier messages, with the fact IDs that support it;
- selected_fact_evidence: the official facts and complete earlier user messages;
- current_facts_that_may_change_the_meaning: later facts that can replace or limit selected facts;
- tools: the complete contracts for the available tools;
- current_state_for_this_test: the readable application records available when the request arrives;
- expected_tool_calls: the tools the completed work should use;
- supported_assertion_shapes: every assertion shape the grader can evaluate.

Write checks only after reading the exact final_user_request. A check must measure a result that the request actually requires. Do not treat the request as evidence that the action happened. Do not require background details, optional tool arguments, delivery timing, recipient names already carried by a tool argument, or repeated wording unless the request requires that content in that field.

Write checks for every planned required result. Each selected fact must affect at least one check. Also inspect the tool contracts and prove that every requested final action occurred when the tool changes app state or has an external effect. A result check on that action already proves it occurred; otherwise add one tool_called check. Do not require a separate check for a supporting read unless a remembered result determines what that read must retrieve or how it must be performed. Do not add separate checks for ordinary details supplied by the final request or current state. Check a request-supplied recipient, CRM row, pull-request target, or other identifier only when it is needed to prove that the remembered result was applied to the right person, record, or action target. A request-supplied label or title for a newly created document or record is an ordinary detail; do not grade it solely because the request states it when the create check and required content checks already prove creation. Do not add a second check that merely proves the opposite of a result already checked.

Use exact checks for exact values, records, dates, times, counts, and numbers when the tool requires one literal value. Use field_equals only in that case. Some tools accept more than one identifier for the same person, such as a full name or a handle. In that case, use field_llm_judge on the recipient field and accept every unambiguous identifier for the intended person.

Use field_llm_judge only for prose that can be correct in different words. Write one check for each independently necessary detail. Do not combine several separate requirements in one criterion. Each criterion must state exactly one meaning the field must communicate and must state which close conflicting, negated, or incomplete meaning would fail. Do not use a regex for prose unless it accepts ordinary correct wording and rejects a contradiction or negation. Do not use an exact call count unless an additional call would itself make the work wrong. Accept every tool-supported way to complete the requested work correctly.

When the request says to keep a message narrow, write one check for that scope. Do not turn examples of unrelated content into separate checks. For example, "confirm only the title, without a biography or announcement" needs one check that allows the title, a greeting, and a sign-off while rejecting additional substantive content. It does not need separate checks for biographies, announcements, schedules, logistics, agendas, or every other kind of unrelated content.

Judge the field named by a check together with the exact request and complete arguments of that same action. Use that context to understand names, pronouns, abbreviations, and what the action is for; do not use another action as evidence. Accept different wording that communicates the same required meaning, but fail omitted, contradictory, incomplete, or negated meaning. "Blue Bottle Coffee" identifies Blue Bottle. A message sent to @sarah does not need to repeat Sarah in its body. Work about a temporary logging query does not require unrelated customer-usage or vendor-cost details merely because they appear in the same source message. The request and criterion describe what should happen; they do not prove that it happened.

List answer-bearing values that must not appear in the final request or readable state. Describe the result a capable assistant with the earlier messages should produce and the plausible result without them. Name one zero-based assertion index that the no-history result should fail.

For every planned required result, copy its result text exactly into requested_result_checks and list the assertion indexes that measure it. For every assertion, give one normal correct example it should accept and one incorrect or negated example it should reject. These examples are evidence for review; they are not additional grading checks.

Return only this JSON object:
{
  "grading_checks": [
    {"fact_ids":[1],"assertion":{"type":"...","tool":"tool_name"}}
  ],
  "answers_that_must_not_appear": ["answer-bearing value"],
  "with_history_expected_result": "the required completed work",
  "without_history_expected_result": "what a capable assistant without the earlier messages would do",
  "without_history_failed_assertion_index": 0,
  "requested_result_checks": [
    {"requested_result":"copy a planned required result exactly","assertion_indexes":[0]}
  ],
  "assertion_examples": [
    {"assertion_index":0,"correct_example":"a normal correct result","incorrect_or_negated_example":"a result that should fail"}
  ]
}
Use only assertion shapes supplied in the input. Do not add fields."""


REDESIGN_SYSTEM = """Correct one failed executable test design.

The input includes the current planned task, the complete source messages, the tool information, the previous design and request, and the exact failure. Follow the current planned task. Treat the previous design and request only as evidence of what failed.

The exact failure may list several issues. Fix every listed issue in this one redesigned test. Do not stop after the first issue. Keep the same facts unless the input explicitly says to replan from the same facts.

Use exactly the fact IDs in the current planned task. The request-writing fields must ask for work that needs the remembered results without revealing them. List only results that depend on the earlier messages in required_results, and name the facts that support each result. Reject the plan if a capable assistant without the earlier messages would normally complete the work the same way. Do not write grading checks here. A later call will write them after it sees the exact final request.

Do not broaden the source messages or add a new remembered fact. Reject rather than preserve an inherently leaking, overloaded, stale, or unsupported idea. If the current planned task cannot become a clean executable test, return outcome "reject" with the exact reason.

Follow the original design instructions and return only the same JSON shape."""


AUTHOR_TEST_SYSTEM = _prompt_text("writer.md") + "\n\n" + _prompt_text("writer_output_reference.md")


REAUTHOR_TEST_SYSTEM = """Correct one failed DolphinBench test using the same accepted idea and facts.

The input contains the original authoring input, previous_authoring, allowed_correction_fields, and the failure report. Return the complete test, changing only the fields listed in allowed_correction_fields to address that report. Preserve every other field exactly, including selected facts, approved required results, list order, and source evidence. Do not repair an unreported problem by changing another field. An unsupported accepted idea requires a newly approved plan, not a rewritten idea. Return only the same JSON shape."""
