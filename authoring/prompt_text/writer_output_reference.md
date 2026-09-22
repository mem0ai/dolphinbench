# Output reference

Return only the supplied JSON shape, with `schema_version` 3. This reference
describes the existing fields; it does not add fields or authorize new requirements.

## Design and evidence

Use `design.outcome` to return `design` or `reject`. A rejection needs a specific
`rejection_reason`; a design uses an empty reason. Explain the source meaning and
date applicability in their design fields.

Copy `planned_task.required_results` into `design.required_results` unchanged,
including fact IDs and work-item references. An unsupported or unnecessary
approved result requires rejection, not an edited list. List all tools needed to
perform the work, including necessary reads, in `design.expected_tool_calls`.

Select existing app records with `state_key`, `record_key`, `match_json`, and
`remove_fields`. Describe new records with `state_key`, `record_key`, and
`record_json`. The two JSON-text fields each encode an object. Use an empty
`record_key` for list-backed state and put a new list record's identifier in its
`id` field. Do not remove a required input to conceal an answer. Empty record
selections mean empty starting state.

For each selected fact, provide one or more `history_dependent_results` items with the fact
and source-session IDs, a continuous exact supporting passage, what the request
states, what only history supplies, and why the request alone does not determine
the result. List the answer-bearing values in `hidden_values`. A topic name alone
is not necessarily an answer leak. Use only supplied facts and source messages.
Use separate passages when a fact establishes several results or changes over
time. Each passage is validated; there is no one-passage-per-fact limit.

## Checks

For `expected_arguments`, encode the value in `value_json` and choose the intended
comparison: `exact`, `number`, `date`, `instant`, or `list_includes`. These are
mechanical comparisons, not guesses about meaning. Use `prose_requirements` with
a generated criterion when correctness needs interpretation, including recipient
arrangements that cannot be described by one literal field comparison. Choose the
method now; a failed mechanical check never falls back to a language model.

Give every argument, empty-argument, and prose check a unique `check_id` and an
`action_id`. Checks for the same completed action share an action ID and must all
pass on one tool call. For an investor email, its recipient and required funding
content belong to the same action. Two incomplete emails do not make one correct
email. Separate requested actions need different IDs, even if they use the same
tool. Extra calls are allowed unless an explicit restriction forbids them.

Each check cites the historical facts that require it or an exact continuous
`request_requirement_quote` where a permitted request-based check is needed.
Request-based checks can identify the person, existing record, or action target
receiving the remembered result. They do not separately check ordinary titles,
labels, or other new request details.

Use `expected_empty_arguments` and `expected_tool_counts` only for explicit
restrictions that affect correctness. Singular wording does not establish
exclusivity or an exact count. Respect permitted recipient arrangements and
equivalent values. Put the supporting request excerpt in the corresponding check.
`request_requirement_quotes` is retained for saved-response compatibility; use
an empty list for new work. An ordinary request detail does not need a check.

Quote the natural request as written, not an ISO timestamp or resolved identifier
that does not occur in it. Early review checks the interpretation using the full
request and supplied context. This does not change the actual grading comparison.

A prose criterion measures one independently necessary meaning in the named tool
field. It must reject an omission, contradiction, or negation while accepting an
equivalent completion. The judge sees the request and all arguments of that same
action: use that context without requiring the body to repeat its recipient's name.
The request is not evidence that the action occurred.

Every selected fact must support an expected argument or prose requirement. Avoid
duplicate checks, including checking a result and separately forbidding its opposite.
The program adds a final-action check only when no result check already proves the
action occurred. Supporting reads need a separate check only when a remembered
result determines what the read must retrieve or how it must be performed.

## Result mapping

In `requested_result_checks`, copy each approved result's text and list its
`check_ids`. Use the IDs you assigned, not positions in the compiled assertion
list. IDs beginning with `program:` are reserved for program-added checks.

Map a remembered result only to checks supported by its facts. A separate
request-supplied target check does not become fact-backed by being nearby in the
list. Cover every approved result and every fact-backed check without duplication.

Describe the expected with-history result and the single likely no-history result
in their response fields. These are predictions, not execution evidence. The
program determines which fact-backed checks require memory.
