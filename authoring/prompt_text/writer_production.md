Write one complete test for the approved work, using the supplied dated user
messages, tools, and app records. Preserve the approved situation, work, and
selected facts. Return cannot_write with a specific reason if that work cannot
be made executable and genuinely dependent on the history. Do not invent remembered
historical facts, an earlier action's outcome, missing tools, or a new task.

Write a natural present-day request. Supply the non-memory inputs needed to act,
but do not reveal the remembered answer or announce a hidden preference. The
runner adds the requester's name to the request in both conditions; do not add
a separate identity sentence yourself. The with-history assistant receives the
complete selected original messages, not your evidence explanations or checks.
Respect dates and newer explicit instructions. Old information is not obsolete
merely because it is old.

Select complete app records needed for reads, update targets, dependencies, and
scheduling conflicts. Unselected records are invisible. An empty selection means
empty state. Do not omit required records to hide an answer. For list collections
use an empty record_key and exact identifying match fields. Dictionary records
use their actual record_key. New records must fit the tools and not reveal the
remembered answer.

You may create new present-day mock records to instantiate the approved situation
when the supplied tool contracts provide their collection and shape. A missing
current PR or document is not, by itself, a reason to refuse. These records may
supply ordinary non-memory inputs, but not missing historical source evidence or
the required remembered answer. Do not replace a supplied record with a
contradictory version merely to hide an answer.

Write only checks needed to establish correct completion. Every selected fact
must determine a necessary checked result. Required content in an email or
document counts even when memory does not change the tool name. Do not require
every detail in a source message. If removing a requirement leaves fully correct
work, omit that requirement. A selected fact that cannot affect necessary work
is an idea problem, not a reason to invent extra work.

Prove every requested final action occurred. A check on its result already does
that; otherwise add one tool_called check. Do not grade supporting reads, their
order, or ordinary request details unless memory determines that result. Do not
require a remembered result in both title and body when either location conveys
it correctly. Distinct necessary results can have separate checks on the same
action. Protect allowed alternatives in amounts, dates, recipients, and wording.

Choose comparisons from check_types. Use exact comparisons for actual identifiers,
numbers, and dates when exactness is required. Use field_llm_judge for meaning;
never use keywords or regular expressions as a substitute for understanding a
message. The meaning check sees all arguments of the same call. Name a specific
field only when that field matters; when any content field is acceptable, state
that explicitly in the criterion. Missing or contradicted meaning must fail.
Field paths use actual tool arguments, such as args.body or args.start.

Give checks stable IDs. Checks for one action share an action_id and must pass
on the same call; different actions use different IDs. Do not invent grading
types or restrict extra calls without a substantive requirement.

Every check's tool must appear in test.expected_tools. Do not add tool_not_called
checks for tools absent from that list: the assistant cannot call them. Do not
add tools solely to make such redundant checks meaningful.

For each source-backed check, reference evidence IDs. Each evidence entry names
supplied session/message IDs, selected fact IDs, and a short faithful explanation
or excerpt in quote. Exact copied wording is not required; the independent
reviewer checks the meaning against the original message. You may cite a relevant
later supplied message that updates the selected fact. Never fabricate a source
reference. Explain briefly why each check is needed. Final-action and target
checks need no source evidence unless they also check a remembered result.

Return status written, empty problem, the complete test, and quality_audit null.
Do not repeat the checks in another audit table. For cannot_write return a
concrete problem, test null, and quality_audit null. Flexible match, record,
value, and values fields use the supplied JSON-encoded provider fields and must
contain real JSON, not descriptions.

For a correction, return the complete response but change only allowed fields.
Preserve all protected checks and protected evidence entries exactly. When
dependent evidence changes are permitted, update or remove only the affected
supporting entries. Do not change the selected facts, approved work, or unrelated
checks. Describe a problem outside the permission instead of silently changing it.
