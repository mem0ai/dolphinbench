QUARTER_EVENT_STORY_SYSTEM = """\
Write the fixed story of one quarter. You are deciding what happens in the
person's life and work, not maintaining a fact database.

Plan an evolving whole person, not only the dominant company project. Across
the quarter, lasting developments may come from work, relationships, family,
health, pets, home life, routines, preferences, travel, or personal plans when
those changes follow naturally from the established person. Do not force every
area into the quarter, but do not make company milestones the only possible
source of lasting change. Every development still needs a concrete cause,
consequence, and place in the continuing story.

Use the multi-year overview, persona sheet, generator context, current people
and projects, current fact registry, recent messages, recent session purposes,
and previous quarter plan. The multi-year overview gives headline direction; it
is not a complete list of what happens during this quarter. The exact recent
messages are authoritative: what they say has already happened. Continue from
those outcomes rather than recreating or redating them. Current facts are
existing rules and states. Treat them as constraints, not developments to
repeat or rewrite, unless an event actually changes one or resolves a concrete
violation. You must not choose fact links, new facts, or supersessions in this
call.

The input also lists current conditions established by accepted sessions.
These conditions are already true even when they are not benchmark facts.
Keep them true unless a proposed event explicitly changes them. Do not recreate
a completed matter or describe an already completed matter as unresolved.

The input includes `first_date_for_new_events`. Dates before it are already
covered by accepted history. Every event you create must begin on or after that
date, and no event may end after `period_end`. Do not recreate an accepted event
on a later date. `period_start` and `period_end` still name the complete calendar
quarter and must be copied exactly into the response.

Every event must contain a concrete new input, result, decision, change, or
consequence that is not already established by the current facts, the exact
recent history, or the completed previous-quarter plan. A future-dated accepted
fact is already established; its effective or start date alone is not a new
event. If removing restated current facts or boundaries leaves no new
occurrence, omit the event.

Merely confirming a routine, plan, owner split, boundary, or stable condition is
not a new quarter event. Include it only when a concrete new input, result,
decision, changed condition, or resolved uncertainty occurs. A scheduled routine
happening normally is not enough.

For every business development, name the concrete new request, observation,
result, disagreement, decision, or consequence. Generic follow-up, review,
interest, or evidence-gap language without the underlying substance is not an
event; omit it or make the underlying substance concrete.

A dated meeting, appointment, review, or other commitment may appear only when
it is already scheduled in the supplied accepted context, or when the story
contains a concrete new trigger and an earlier or same-event scheduling
decision that explains it.

When a development ends, cuts, reopens, replaces, or changes an existing plan
or state, the accepted history, a current fact, or an earlier development in
this same quarter must have established that plan or state first. Do not assume
an earlier plan or state merely because changing it would make a plausible
story. If it has not been established, plan the earlier event that establishes
it before planning the event that changes it.

Do not say that someone completed a target, expectation, requirement, or plan
unless the supplied history or an earlier event states exactly what it
required. Otherwise state only the concrete work that was completed.

Plan the quarter as one chronological list of durable events. The weekly
planner will later decide what ordinary same-week messages, short-lived
occurrences, and assistant contacts happen around these events. Do not create
weekly rows or ordinary occurrences here.

Also add protected_changes for lasting changes that are scheduled later and
could be introduced too early. Each one names affected subjects, the earliest
date, and a short boundary. State only the boundary needed to preserve order;
do not expose the later result in unnecessary detail.

Include the headline developments and the concrete work and personal
developments that connect them: new inputs, results, decisions, interruptions,
consequences, and later follow-ups. Keep distinct moments separate. Do not
compress an input, a decision, its outcome, and a later follow-up into one
summary event.

A smaller occurrence belongs only when it has a specific cause and result. Do
not add unchanged routines, category representatives, invented busywork, or
activity or token quotas. Each event must describe something that actually
happens: do not fill the quarter with canceled alternatives, unchanged rules,
or other non-events. Let overlapping work and personal threads produce distinct
consequences. Do not make general caution, scope boundaries, or repeated
statements of what will not happen the plot of most events.

This event list is the only source of lasting quarter developments. The weekly
planner may later create concrete same-week inputs, intermediate results, and
self-contained contacts around these events, but it may not create another
lasting storyline or durable fact. Include every lasting occurrence needed to
connect the quarter's story now.

Before returning, compare all proposed events with one another. If a later
event repeats the same outcome or boundary without a new input or consequence,
omit the later event.

A conclusion in a later event must be supported by earlier dated events in this
quarter or by concrete current facts. If a customer, account, or example
materially causes a durable decision or conclusion, it must be a named existing
subject or an explicitly introduced new entity and must appear in a dated event.
Anonymous aggregate examples may supplement a conclusion, but they may not
carry it. If the plan says work shifted or was assigned and later says it
succeeded, include a dated outcome event; do not turn an announced intention
into an accomplished quarter result. A quarter closeout may say that a metric,
workflow, owner lane, or product quality held or improved only when a dated
event in this quarter demonstrates that. Otherwise omit that claim. Use the
existing builds_on_event_ids field to express the causal chain; do not add
fields. Every value in builds_on_event_ids must be copied character-for-character
from the id of an earlier event already written. Before returning, compare every
reference with those earlier IDs and correct any value that is not an exact match.

Each event must say who is involved, when it happens, what concretely happens,
and what remains true afterward. Preserve conditions and chronology. An earlier
event cannot claim a decision, result, or ownership change that happens later.
For each proposed event, read every supplied current fact involving the same
people, projects, companies, products, or places. When the event uses an
existing rule or process, preserve every prerequisite, every condition that
stops the process, and every required escalation unless this event explicitly
changes it.
Put every detail that will remain action-relevant after the event plainly in
what_happens or lasting_outcome, so later messages can establish it directly.
If an event signs, renews, or changes an agreement, recurring charge, schedule,
limit, ownership assignment, or other lasting terms, state every exact date,
amount, limit, owner, scope, and condition that could change a later action.
Do not replace those details with phrases such as "agreed terms," "the existing
charges," or "the approved scope."

Use the exact name of every measurement, count, amount, date, and unit from the
event or current facts. Do not treat related measurements as the same thing.
Do not write messages, documents, tool calls, app operations, fact IDs, fact
statements, or replacement decisions.

When an event says that one date must be within a stated number of days or
weeks of another date, calculate the interval before returning the event. Do
not choose a date outside that limit.

Reuse an existing person, company, account, product, project, or place when it
fits the event. Introduce a new named person, company, account, product,
project, or place only when a concrete event genuinely depends on identifying
it and no existing entity fits. Do not add a name just to provide background or
decoration. Every new entity must have a concrete role in the event that first
introduces it. An event can build on an earlier event when it directly
continues it.

If an event creates a rule that can change a later action, state the condition a
person can observe and the action that follows. Do not write a vague rule such
as "when appropriate" or "if compatible" without saying what is observed and
what the person should do.

Return JSON only:
{
  "story": {
    "period_start": "YYYY-MM-DD",
    "period_end": "YYYY-MM-DD",
    "protected_changes": [
      {
        "id": "protected stable id",
        "subject_ids": ["entity id"],
        "not_before": "YYYY-MM-DD",
        "description": "lasting change that must not happen before this date"
      }
    ],
    "new_entities": [
      {
        "id": "new stable entity id",
        "name": "display name",
        "kind": "entity kind",
        "role": "the entity's stable identity and broad purpose",
        "introduced_in_event_id": "event id"
      }
    ],
    "events": [
      {
        "id": "unique event id",
        "start_date": "YYYY-MM-DD",
        "end_date": "YYYY-MM-DD",
        "subjects": ["entity id"],
        "builds_on_event_ids": ["earlier event id"],
        "what_happens": "plain account of the concrete development",
        "lasting_outcome": "what remains true after this event"
      }
    ]
  }
}

Events must be in chronological order. Order them by end_date, then start_date.
Include every shown field, using an empty list when needed. Return no prose
outside the JSON.
"""


QUARTER_EVENT_FACT_FINALIZATION_SYSTEM = """\
Annotate the supplied fixed quarter story with durable facts. Do not copy or change the story. The code adds your annotations to its fixed events.

The current facts govern at the start of the quarter. They are constraints, not new story material. For each event, select only current numeric fact IDs that directly affect it. Add only durable decisions, preferences, owners, recurring processes, relationship states, commitments, or other information that changes a later action. Leave facts_to_establish empty for one-time events with no lasting result.

The input also lists current conditions established by accepted sessions. They
are already true even when they are not benchmark facts. Do not create a fact
that contradicts them or treats a completed matter as unresolved. Their state
keys are not fact IDs and must not appear in relevant_existing_fact_ids.

When an event establishes or changes lasting terms, copy every exact date,
amount, limit, owner, scope, and condition that could change a later action into
the fact statement. Do not replace exact terms with phrases such as "agreed
terms," "the existing charges," or "the approved scope." For example, a fact
about a signed lease must state its recurring rent and fees when the event
states them.

Use the exact name of every measurement, count, amount, date, and unit from the
event or the fact being replaced. Do not treat related measurements as the same
thing. For example, an accepted batch is not an accepted point.

A fact must not apply more broadly than the event that establishes it. If the event covers only named accounts, a pilot, a place, or a stated period, keep that limit in both the fact statement and applies_when.

Do not treat a deadline or planned date as proof that the work happened on that
date. If an event says work must be completed by a date, record the deadline and
keep the fact applicable until actual completion. Say that deployment or another
result happened on that date only when the event explicitly says it happened.

When a fact records evidence used to decide whether something should continue or change, include each concrete result from the event that could change that decision. Do not keep only totals or requests while dropping a named example of actual use.

For a brand-new fact, every clause must be stated directly in that event's what_happens or lasting_outcome. For a replacement fact, every new or changed clause must be stated directly in that event. An unchanged clause may be carried from the older fact identified in fact_replacements; it does not need to be repeated in the event. Do not use merely relevant facts, the overview, or background knowledge as hidden support. Keep facts short and in plain English.

Do not convert a field's meaning into a required value. If an event requires
current, consistent state, say that. Do not require a specific yes, no, active,
present, or absent value unless the event explicitly requires it.

Create a fact only for genuinely new durable information, not an unchanged rule restated. If the event says an existing rule remains unchanged, leave the existing fact current instead of copying that rule into a new fact. Restate an older rule only when this event changes it or when a fact this event replaces contained that rule and the rule must remain true. A temporary arrangement does not end a permanent rule that will apply again later. A later event cannot change the meaning of an earlier event. When an older fact has independently actionable clauses, treat each as still current unless this fixed event explicitly changes or ends it. Account for every clause: it remains in one of the listed replacement facts, or this event changes or ends it. If old and new facts can both govern without conflict, keep the old fact.

When an event decides a change that starts on a later date, the old fact remains true through the day before that date. The replacement fact must state both the rule that remains true until then and the exact date the new rule begins.

An event may explain a rejected option, but do not put that rejected option in a
durable fact unless remembering it would change a future action. A durable fact
should record what remains relevant after the event, not every alternative that
was considered.

When a fact replaces an earlier fact, preserve every person or other subject
from the earlier fact and every detail that is still true. Omit a person or
detail only when the event explicitly changes or ends it.

Every new fact key must be different from every key in Current facts and every
key already created in this response. Do not reuse an older key when replacing
that fact, when restoring an older value, or when the older fact no longer
governs. Give the new fact a new key that identifies its current meaning.

Write each fact once. Do not repeat generated fact keys in a replacement row; the code creates the final replacement references. For an older starting fact, identify it by numeric ID. For a fact created earlier in this response, identify the earlier fixed event ID and its 1-based position in that event's facts list. Identify replacement facts by their 1-based positions in the current event's facts list. For example, if event "may_review" has two facts and its second fact replaces starting fact 123, use fact_id 123 and replacement_fact_numbers [2]. If event "june_followup" replaces the first fact from "may_review", use event_id "may_review", fact_number 1, and the positions of the replacement facts in "june_followup". The earlier event must precede the event that replaces its fact.

still_current_information items must be copied verbatim from the listed new fact statements, apart from harmless whitespace differences. Do not summarize them. changed_or_ended_information must be nonempty and state information this event explicitly changes or ends. Preserve exact people, projects, owners, scopes, triggers, exceptions, conditions, cadences, and qualifiers.

Return JSON only. The top level contains exactly event_fact_annotations and fact_replacements:
{
  "event_fact_annotations": [
    {"event_id": "an event id copied exactly from the fixed story", "relevant_existing_fact_ids": [123], "facts_to_establish": [{"key": "unique_snake_case_key", "statement": "short plain-English durable fact", "applies_when": "when this fact governs later action", "subjects": ["known entity id whose durable state this fact governs"]}]}
  ],
  "fact_replacements": [
    {"event_id": "event that establishes the replacement facts", "replaced_fact": {"source": "starting_checkpoint", "fact_id": 123}, "replacement_fact_numbers": [1], "still_current_information": ["complete information that still governs"], "changed_or_ended_information": ["information explicitly changed by the event"]},
    {"event_id": "later event", "replaced_fact": {"source": "this_quarter", "event_id": "earlier event", "fact_number": 1}, "replacement_fact_numbers": [1], "still_current_information": [], "changed_or_ended_information": ["information explicitly changed by the event"]}
  ]
}

Include an annotation row only when an event uses a current fact or establishes a durable fact; omitted events receive empty lists. Every event ID must be copied exactly from the fixed story and appear at most once. Each fact contains exactly key, statement, applies_when, and subjects. Use only exact supplied entity IDs in subjects. Return no prose outside JSON.
"""

QUARTER_EVENT_FACT_REPAIR_SYSTEM = """\
Repair fact annotations for the events named in `event_ids_to_repair`. The story is accepted and cannot change. Do not add, remove, rename, move, or rewrite an event.

Change only information named by the review failure. Do not add another date, meeting, review, decision, or condition unless the repaired event or a cited existing fact states it.

The input lists current conditions established by accepted sessions. They are
already true even when they are not benchmark facts. Do not create a repaired
fact that contradicts them or treats a completed matter as unresolved. Their
state keys are not fact IDs.

Read the supplied events and fact changes in date order. A starting fact stops governing after an earlier event in this quarter replaces it. It remains true only when a fact from that earlier event explicitly carries the relevant clause forward. Remove unsupported information. Preserve every independently useful owner, scope, trigger, condition, exception, cadence, and qualifier from a replaced fact unless the fixed event explicitly changes or ends it. Keep exact concrete distinctions; do not broaden rules or create unrelated facts.

If an event says an existing rule remains unchanged, leave the existing fact
current instead of copying that rule into a new fact. Restate an older rule
only when this event changes it or when a fact this event replaces contained
that rule and the rule must remain true.

Do not convert a field's meaning into a required value. If an event requires
current, consistent state, say that. Do not require a specific yes, no, active,
present, or absent value unless the event explicitly requires it.

When an event decides a change that starts on a later date, the old fact remains true through the day before that date. The replacement fact must state both the rule that remains true until then and the exact date the new rule begins.

When the supplied event establishes or changes lasting terms, copy every exact
date, amount, limit, owner, scope, and condition that could change a later
action into the repaired fact statement. Do not replace those details with a
vague reference to agreed, existing, or approved terms.

Use the exact name of every measurement, count, amount, date, and unit from the
event or fact being repaired. Do not treat related measurements as the same
thing. For example, an accepted batch is not an accepted point.

A fact must not apply more broadly than the event that establishes it. If the event covers only named accounts, a pilot, a place, or a stated period, keep that limit in both the fact statement and applies_when.

Do not treat a deadline or planned date as proof that the work happened on that
date. If an event says work must be completed by a date, record the deadline and
keep the fact applicable until actual completion. Say that deployment or another
result happened on that date only when the event explicitly says it happened.

When a fact records evidence used to decide whether something should continue or change, include each concrete result from the event that could change that decision. Do not keep only totals or requests while dropping a named example of actual use.

`later_events_that_depend_on_repaired_facts` contains exact replacement rows from later supplied events that currently point to facts in an earlier supplied event. Keep the later event's facts unless the earlier fact is removed or changed. In that case, update the later row to point to the correct surviving fact, or remove the row when no replacement is needed. `earlier_same_quarter_changes_to_starting_facts` shows earlier facts and replacement rows that already changed starting facts used by a supplied event. Use that earlier change when deciding which fact still governs.

Every repaired fact key must be different from every key in Current facts and
every key already created elsewhere in the quarter. Do not reuse an older key
when replacing that fact, when restoring an older value, or when the older fact
no longer governs. Give the repaired fact a new key that identifies its current
meaning.

Write each fact once. The top-level JSON object has exactly two fields: `event_fact_annotations` and `fact_replacements`. Each repair annotation row has exactly these three fields and no others:

{"event_id": "one supplied failed event id", "relevant_existing_fact_ids": [123], "facts_to_establish": []}

Use `relevant_existing_fact_ids` for earlier facts whose information this event uses or carries forward. You may keep, add, or remove IDs for the supplied failed event. Use only numeric IDs from `exact_starting_facts_used_or_replaced`. Do not add a fact merely because it shares a subject; add it only when the repaired event or fact relies on its information. You are changing only the earlier-fact links and facts established by the supplied event. Facts contain exactly key, statement, applies_when, and subjects.

An event may explain a rejected option, but leave that option out of a durable
fact unless remembering it would change a future action. Keep the event's
rationale when it helps explain the decision; the durable fact records only
what remains relevant afterward.

When a repaired fact replaces an earlier fact, preserve every person or other
subject from the earlier fact and every detail that is still true. Omit a person
or detail only when the event explicitly changes or ends it.

For each replacement, read the complete older fact before writing the new
facts. Remove only information that the fixed event explicitly changes or
ends. Copy every other still-true completed action, name, date, amount, rule,
condition, and limit into the new facts. When a later event replaces a fact
from an earlier event in this repair, repeat this comparison using the repaired
earlier fact rather than its rejected version.

A fact has a statement and an `applies_when` field. Compare both fields when
replacing a fact. If the older `applies_when` names a future use that the fixed
event does not end, at least one replacement fact must still cover that use.

Each replacement row has exactly these five fields: `event_id`, `replaced_fact`, `replacement_fact_numbers`, `still_current_information`, and `changed_or_ended_information`. Both `still_current_information` and `changed_or_ended_information` are JSON lists of strings, even when a list contains one item. Identify a starting fact as `{"source": "starting_checkpoint", "fact_id": 123}`. Identify a fact from earlier in the same quarter as `{"source": "this_quarter", "event_id": "earlier event id", "fact_number": 1}`. Use 1-based positions for replacement facts in the current event. For example, [2] means the second fact in that event's facts_to_establish list. Do not repeat generated fact keys; the code creates final replacement references. Copy still-current information verbatim from the listed new fact statements. `changed_or_ended_information` must not be empty.

Return a replacement row only when at least one repaired fact replaces that older fact. If no repaired fact replaces it, omit the row and leave the older fact current. Never return an empty `replacement_fact_numbers` list.

Do not turn evidence, a result, or a recommendation into a decision. Say that a decision was made only when the fixed event directly records that decision.

Return exactly one annotation row for every ID in `event_ids_to_repair`, even when its lists are empty. Return all and only replacement rows for those events. Copy supplied event IDs exactly. Return JSON only.
"""

QUARTER_EVENT_EVENT_REPAIR_SYSTEM = """\
Repair only the quarter events named in the supplied failed-event review rows.
The other events are accepted and must not be changed.

Read every fact in `accepted_current_facts` before changing an established
term, field, rule, product, project, person, or relationship. Those facts are
already true. Preserve their meaning. Do not guess a new meaning from the
failed event or from the reviewer's short explanation.

The input also lists current conditions established by accepted sessions.
Those conditions are already true even when they are not benchmark facts.
Preserve them unless the repaired event explicitly changes them. Do not reopen
or recreate a completed matter.

Use the exact name of every measurement, count, amount, date, and unit from the
failed event or current facts. Do not treat related measurements as the same
thing.

Do not say that someone completed a target, expectation, requirement, or plan
unless the supplied history or an earlier event states exactly what it
required. Otherwise state only the concrete work that was completed.

The supplied event objects are the complete events you may replace. Each
replacement must have exactly these fields: id, start_date, end_date,
subjects, builds_on_event_ids, what_happens, and lasting_outcome. Normally copy
each failed event's id exactly. `approved_event_id_renames` is an explicit list
of exceptions. For each listed old ID, return the listed replacement ID exactly.
Do not rename an event unless it appears in that list. You may correct its
dates, description, or outcome.
Reuse an existing person, company, account, product, project, or place when it
fits the event. Add a new named entity only when a concrete repaired event
genuinely depends on identifying it and no supplied existing entity fits. Do
not add a name for background or decoration. Every new entity must appear in
the subjects of its `introduced_in_event_id`, which must be one of the repaired
events and must be its first appearance. Later repaired events may use that
same entity. Do not change any event that is not listed in the failed events.

If an event creates a rule that can change a later action, state the condition a
person can observe and the action that follows. Do not use a vague rule such as
"when appropriate" or "if compatible" without saying what is observed and
what the person should do.

Edit the event only when the reviewer identifies an error in the event's own
dates, people, references, what_happens, or lasting_outcome. If the reviewer
only says that a proposed fact claims more than the event says, return the
event unchanged. The fact repair step handles that problem.

If the reviewer says an earlier rule still governs, preserve every part of
that rule. If this event replaces the earlier rule, state exactly what ends and
what rule applies afterward. Do not silently weaken, broaden, or omit part of
an earlier rule.

Keep the quarter internally consistent. An event may say that a decision was
made only when the event directly says that a person made that decision. Do
not turn evidence, a result, a recommendation, or support for a choice into a
decision. Keep dates between the supplied first date for new events and the
quarter end. Keep each build-on reference exactly as supplied unless the
replacement must correct that event's own earlier-event reference.

Return JSON only in this form:
{
  "new_entities": [
    {
      "id": "new stable entity id",
      "name": "display name",
      "kind": "entity kind",
      "role": "what this entity is and why this repaired event needs it",
      "introduced_in_event_id": "the exact repaired event id"
    }
  ],
  "events": [
    {
      "id": "the exact failed event id, or its explicitly supplied replacement id",
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD",
      "subjects": ["known entity id"],
      "builds_on_event_ids": ["earlier event id"],
      "what_happens": "what concretely happens",
      "lasting_outcome": "what remains true afterward"
    }
  ]
}

Return one replacement for every supplied failed event, and no other event.
Do not return any extra field or any explanation outside the JSON object.
"""

QUARTER_EVENT_QUALITY_REVIEW_SYSTEM = """\
Check one proposed quarter before any weekly history is written. The user input is plain text and
is arranged in date order.

If the input contains `Correction review scope`, this is a narrow review after a saved plan was
corrected. Review only the listed changed event rows, fact rows, and replacement rows. Do not
return rows for any other event, fact, or replacement. If the input includes an earlier whole-plan
problem, decide only whether the listed changes fix that exact problem. Otherwise set plan_review
to passed with an empty reason because the saved plan-level result remains unchanged.

Read these sections:
- Current facts relevant to the proposed quarter.
- Current conditions established by accepted sessions.
- The previous quarter and the long-range story.
- Recent accepted messages when an event cites them.
- Earlier proposed events that a repaired event continues.
- The complete proposed-quarter timeline.

Do four checks in this order.

1. Check the quarter as a whole.
Decide whether it is a believable continuation of this person's work and life. Fail unexplained
novelty, contradictions, repeated occurrences, repeated outcomes under different wording, or an
unnatural pattern repeated across many events. Compare the proposed quarter with the previous
quarter and the long-range story. Also compare it with the current conditions from accepted
sessions. Fail a plan that reopens a completed matter or contradicts a current condition without
explicitly changing it. Do not require drama, quotas, new entities, or tidy endings.

2. Check every proposed event.
Check its dates, people and other subjects, earlier-event references, description, and lasting
outcome. A claimed result must follow from something that happens in this event or in an earlier
event that it explicitly continues. A scheduled result needs an accepted schedule or an earlier
concrete trigger and scheduling decision. Fail an event that claims an undefined criterion,
decision, agreement, threshold, success, or resolution. If only a proposed fact is wrong, pass the
event and fail the fact. When an event signs, renews, or changes lasting terms, fail it if it uses a
vague phrase such as "agreed terms" instead of stating the exact dates, amounts, limits, owners,
scope, and conditions that could change a later action.
When an event uses a defined unit or named measurement from earlier history, it must keep the same
unit and name. Fail an unexplained change such as calling an accepted batch an accepted point, or
renaming a measurement. Pass the change only when the event plainly defines the new unit or name
and says what it replaces.
For each event, read every fact shown under Relevant existing facts. When the event uses an existing
rule or process, verify that it preserves every prerequisite, every condition that stops the process,
and every required escalation unless the event explicitly changes it. Fail an event that keeps only
part of the existing rule.

3. Check every fact created by every proposed event.
A new fact may say only what the current event states. A replacement fact may also keep unchanged
information from the exact older fact it explicitly replaces. A merely related fact, the overview,
or a similar subject does not supply missing evidence. Compare exact meanings and qualifiers:
review is not approval, a narrow rule is not a general rule, and a partial list is not a complete
set. A fact should record durable information that could matter later, not temporary explanation,
recap, rejected alternatives, or unchanged state. One fact may contain the details needed to apply
one coherent decision, policy, role arrangement, care plan, schedule, or status change. Fail a fact
that omits an exact date, amount, limit, owner, scope, or condition from the event when that detail
could change a later action. For example, a signed lease fact must include the recurring rent and
fees stated in the event; "agreed recurring charges" is not enough.
If an event says an earlier rule or agreement remains unchanged, and the new fact does not replace
the earlier fact, do not require the new fact to copy it. Check the current facts and accepted
history to confirm it. The new fact should record only what this event adds or changes.

4. Check every replacement.
Read the complete old fact and all replacement facts created by the same event. Account for every
owner, rule, condition, exception, date, scope limit, and other detail that could still change a
later action. Each detail must remain in a replacement fact, or the current event must clearly
change or end it. A narrower exception does not end the old rule outside that exception. The
replacement accounting shown in the input is a claim to verify, not evidence. Do not pass an
omission because the accounting says the old information remains.

Earlier proposed events shown only as supporting context are not events to review. Return rows only
for events in the Proposed-quarter timeline.

Return JSON only:
{
  "passed": true,
  "plan_review": {"passed": true, "reason": ""},
  "event_reviews": [
    {"event_id": "exact event id", "passed": true, "reason": ""}
  ],
  "fact_reviews": [
    {"event_id": "exact event id", "fact_key": "exact fact key", "passed": true, "reason": ""}
  ],
  "replacement_reviews": [
    {
      "event_id": "exact event id",
      "fact_key": "exact fact key",
      "replaced_fact_ref": "exact id:123 or key:earlier_fact reference",
      "passed": true,
      "reason": ""
    }
  ]
}

Return exactly one event row for every event in the Proposed-quarter timeline, in that order.
Return exactly one fact row for every fact created by those events. For every numeric ID under a
fact's Supersedes heading, return one replacement row using `id:<number>`. For every key under its
Supersedes fact keys heading, return one replacement row using `key:<fact key>`.

Use an empty reason for every passing row. Give a short, concrete reason for every failing row. If
the whole quarter fails because several events form a repeated or contradictory pattern, fail each
contributing event as well. Set the top-level passed field to true only when the plan and every
event, fact, and replacement pass. Do not rewrite the plan and do not request more events, facts,
messages, documents, or tool calls.
"""


_RETIRED_HISTORY_WINDOW_PLANNING_PROMPT = """\
Decide everything that naturally happens between the supplied start and end
dates when the person contacts their assistant. Do not write the messages and
do not choose exact tool arguments. Return only the fixed chronological
sequence that a second writer will turn into final sessions.

The supplied quarter_developments_for_these_dates and accepted contacts are
required milestones, not the whole history. Keep each development's causes,
dependencies, outcomes, dates, lasting facts, named entities, and boundaries
unchanged. Complete a development when must_finish_in_this_window is true. Do
not complete a later development early or invent a replacement story.

Work in two steps inside this one response. First decide the concrete one-time
things that happen during these dates from the supplied active facts, entities,
unfinished matters, fixed milestones, and recent history. A happening may be a
specific new input, result, interruption, follow-up, decision, or personal
occurrence. Each one needs a concrete cause and outcome. Second, choose which
of those happenings naturally lead the person to contact the assistant. Natural
ordinary contacts and one-way updates are valid when the person wants the
assistant to know something that could matter later. Do not invent a reply,
task, or external action to justify such a contact.

Do not create happenings by sweeping through categories, replaying routines,
or filling activity or count. A standing rule, preference, cadence, or current
fact constrains the details after a happening exists; it does not itself prove
that something new happened or cause a contact.

An ordinary contact needs a concrete new occurrence since the latest related
accepted contact. It is normal for a date range to have no contact in a
particular life area and for quiet dates to have no contact.
Repeating or confirming an unchanged routine or action at its next interval is
not a new trigger. Changing only the surrounding work context or opening
wording is not enough.

Keep genuinely distinct moments separate; do not compress a new input, a
decision, a later update, and a follow-up into one contact merely because they
concern the same thread. Do not invent filler, force activity on every day,
create a new named entity, or add an occasion to reach a count or token target.

Inspect every unfinished matter. Advance or close it when something would
naturally happen during these dates. Otherwise leave it open. Do not force
every unfinished matter to change.

The accepted contact chronology from the preceding eight weeks is evidence of
the recent pace, level of detail, active subjects, and unresolved work. Use it
to understand the granularity of real contacts and to avoid repeating finished
events. Its historical event IDs describe the past only. Never copy one of
those IDs into a new occurrence unless that same ID is explicitly present in
quarter_developments_for_these_dates. The chronology is not a count, quota, or
template.

valid_existing_continues_from_ids is the complete list of existing-history
references that the validator accepts. When continues_from refers to existing
history, copy the value character-for-character from that list. Do not invent,
rename, shorten, or change the prefix of an ID. The session_id and
planned_interaction_id fields in the accepted chronology show which exact
session and contact realized each prior occurrence. event_ids are story labels;
never transform an event ID into a contact ID. Use an event ID as a reference
only when that exact event ID itself appears in
valid_existing_continues_from_ids and the new occurrence directly continues
that event. An occurrence may also continue an earlier occurrence created in
this same response by copying that earlier contact_id exactly. For an ongoing
thread, prefer its latest relevant accepted contact unless a specific earlier
contact is directly needed.

available_assistant_capabilities lists the kinds of external actions the
simulated assistant can perform. Each item contains only a tool name and a
plain description, not its arguments or current state. If an occurrence needs
an external result, requested_response_or_action must describe a result within
one or more of those capabilities. If the person asks a question or wants
conversational help, describe the response they want without inventing an
external action. requested_response_or_action must be null for a one-way
update. Do not invent a task or action merely to justify a contact, and do not
design work that requires an unavailable external action.

Use only known entity IDs and the required new entity IDs. Do not invent named
people, organizations, products, projects, or places. A required new entity may
first appear only in its accepted introduction contact.

Every accepted contact supplied for these dates must appear exactly once, on
its accepted date, with exactly its supplied subjects, development IDs, and
prior references. All required developments must occur. A new occurrence may
use only an ID in quarter_developments_for_these_dates.
anchor_item_ids is reserved for IDs copied from
accepted_contacts_that_must_appear_in_these_dates. An occurrence realizing one
of those accepted contacts must contain that accepted contact's ID. Every
ordinary occurrence planned here must use an empty anchor_item_ids list. Never
put contact_id, a historical contact ID, or a development ID in
anchor_item_ids unless that exact value is also an explicitly supplied accepted
contact ID for these dates.

For each occurrence, state concretely what happens, why the person contacts the
assistant, whether literal incoming material must be shown, whether the person
wants a response or action, and what remains afterward. source_material is
non-null only when literal incoming material such as an email, notes, rows, a
draft, feedback, or requirements must actually be shown to the assistant. It
then contains that material's real origin and complete factual contents; do not
leave the later writer to invent them. Ordinary facts, context, and provenance
belong in what_happens and do not make source_material non-null. Do not relabel
the person's own knowledge or a summary of what happened as source material.
requested_response_or_action is null for a one-way update. Otherwise it is a
concrete plain-English description of the response or action the person wants.
Do not invent a task or action to justify the contact.

thread_id is the identity of the ongoing unresolved matter, not a convenient
schedule label, meeting label, or description of the current contact. Reuse the
same thread_id whenever a later occurrence advances or resolves that matter.
Thread state is tracked separately for each thread_id. A later occurrence with
a different thread_id does not close the earlier thread, even when it discusses
the same subjects or refers to the earlier contact. continues_from records a
causal or reference link to accepted history or an earlier occurrence; it does
not close another thread. Use a new thread_id only for an independent matter.
Before returning, follow every thread_id through the window and make sure the
last occurrence for that thread truthfully says whether the matter remains open
at the end of the requested dates.

Return JSON with exactly one top-level field named plan. plan must contain
exactly period_start, period_end, and occurrences. The dates must match the
request. occurrences must be strictly chronological.

Each occurrence must contain exactly contact_id, narrative_date, thread_id,
continues_from, subject_ids, development_ids, anchor_item_ids, what_happens,
why_contact_assistant, source_material, requested_response_or_action,
thread_open_after_contact, and what_remains_after_contact.

continues_from, subject_ids, development_ids, and anchor_item_ids are lists of
unique strings. source_material is either null or an object containing exactly
origin and factual_contents, both as non-empty plain-English strings.
narrative_date must be a full offset-aware ISO 8601 datetime, such as
2025-02-03T14:30:00-08:00, using the correct offset for the supplied narrative
timezone. A bare date such as 2025-02-03 is invalid. thread_open_after_contact
is a boolean. what_remains_after_contact is a non-empty string when the thread
remains open and null when it closes.
"""


_RETIRED_HISTORY_WINDOW_WRITING_PROMPT = """\
Turn the accepted chronological occurrence plan into final user sessions. The
occurrences are fixed. Write exactly one session for every occurrence, in the
same order. Do not add, remove, combine, split, move, or redesign an occasion.

Preserve every occurrence's contact ID, date and time, thread ID, prior
references, subjects, development IDs, accepted contact IDs, reason for
contact, and post-contact thread state exactly. The session purpose must be
exactly why_contact_assistant from that occurrence. Realize what_happens,
source_material, and requested_response_or_action faithfully in the final
messages and operations without returning those planning fields in the
session schema.

fixed_accepted_contacts_for_this_window contains the complete accepted rows for
the fixed contacts in these dates. These rows are hard constraints. The
planner's wording does not replace or weaken them. For a session carrying a
fixed accepted-contact ID, realize every observable result in that fixed row.
When that fixed row contains `required_app_operations`, preserve the operations
in their listed order. Copy each `tool` exactly and JSON-encode that operation's
exact `args` object into `args_json`. Do not add, omit, reorder, rename,
paraphrase, or change any argument key, string, list, or value.
If a session carries multiple fixed contacts, encode their required operations
by concatenating the fixed rows in anchor_item_ids order.
Copy exact record IDs, person names, list shapes including null slots, tags,
timestamps, and values without paraphrasing or substituting equivalents. If a
person is optional or has a special role, put that explanation in the user's
message or in an authored body. Never append a role or optionality qualifier to
the person's identity string.

exact_prior_context_for_continues_from contains exact accepted messages and any
recorded app operations and results for prior contacts that this plan explicitly
continues. Treat that context as authoritative. When the new session continues
or updates an existing record, reuse the exact record identity from the prior
message or operation. Do not create a differently named deck, document, event,
plan, row, or other record for the same continuing object. A continues_from
value with no supplied prior context may be an event or another non-session
reference; do not invent context for it.

Write the exact words the person sends to the assistant at that moment. The
messages must make sense without exposing internal IDs, planning fields, fact
keys, or tool names. Use the persona and recent exact messages as evidence of
voice, not text to copy. Avoid repetitive openings, artificial recaps, generic
management language, and unexplained names.

Render what_happens as a coherent message about what is happening at that
moment. When source_material is non-null, include its complete factual_contents
as the actual email, notes, rows, draft, feedback, requirements, or other
incoming text, with enough surrounding words to explain its origin. When it is
null, do not invent or paste source material; express the relevant ordinary
facts and context from what_happens directly. Never paste material merely to
make a message longer.

When requested_response_or_action is non-null, express that request or question
clearly in the message. When it is null, write a natural one-way update and do
not add a request, question, task, or operation. For a one-way update, state the
update and stop. Do not append "just sharing," "nothing needed," "no action
needed," or a similar explanation unless the fixed occurrence specifically
requires it.

Use only the supplied entity IDs. Create exactly the required new entities and
no others. Create exactly the required lasting facts and no others. Copy each
required fact's statement, applies_when, subjects, and replacement references
without changing them. Every new fact must cite the actual session or sessions
whose messages establish it.

`current_facts` is the complete set of accepted facts that still govern when
this window begins. In each session's `uses_facts`, include only integer IDs
that appear in `current_facts`, and include only facts that the session actually
uses. Historical messages may contain an older fact that has since been
replaced; preserve the history, but never put that older fact's ID in
`uses_facts`. Use the current replacement fact instead when it governs the
session.

Translate requested_response_or_action into exact simulated app operations only
when an external effect is required and supported by the supplied tools and
tool-visible state. Use an empty operation list for conversational help or a
one-way update. Every operation must use an exact supplied tool name and
argument name, include every required argument, and obey visible state. Do not
refer to another operation's result.

Every update argument must contain the literal value that the app will store,
not an instruction to transform the old value. In particular,
update_crm_row.notes replaces the complete notes field. When old notes must be
preserved, copy the existing notes from tool-visible state and add the new text
after them. Never submit text such as "append to existing notes" as the value.

A legacy fixed contact without `required_app_operations` but with one or more
observable results must still have the app operation or operations needed to
make all of those exact results true. Do not return an empty operation list for
such a contact.

For outgoing or stored text, write the final text once. Put that identical
literal title, subject, body, message, note, or other authored value in both the
user's authorizing message and the operation arguments. Do not claim an
operation already happened; the user message is the input that authorizes it.

Python assigns session IDs after validation. Do not return session_id.

Return JSON with exactly one top-level field named plan. plan must contain
exactly period_start, period_end, new_entities, and sessions. The dates must
match the accepted occurrence plan.

Each new entity must contain exactly id, name, kind, role, reason, and
introduced_in_contact_id.

Each session must contain exactly contact_id, narrative_date, thread_id,
continues_from, subject_ids, development_ids, anchor_item_ids, purpose,
messages, uses_facts, new_facts, app_operations,
thread_open_after_contact, and what_remains_after_contact. continues_from,
subject_ids, development_ids, anchor_item_ids, messages, uses_facts, new_facts,
and app_operations are lists. messages is a non-empty list of non-empty user
message strings. narrative_date is an ISO datetime with the supplied timezone
offset. Sessions must be strictly chronological.

Each new fact must contain exactly key, statement, applies_when, subjects,
supersedes, and evidence_contact_ids. Each app operation must contain exactly
tool and args_json. args_json must be a JSON string whose decoded value is one
object containing the exact argument names and values for that tool. Do not put
an args object beside it and do not encode any value other than one JSON object.
"""


# The canonical weekly path has no quarter-level contact schedule. These
# definitions intentionally replace the retired schedule-centric contracts above.
_retired_history_window_planning_system = """\
Plan the complete week in a person's life with an always-available personal
assistant. Do not write user messages. Decide the real occurrences first, then
return every occasion on which this high-use assistant user would naturally
contact the assistant during the requested dates.

Quarter developments are major milestones, not a contact list. Keep their
causes, dates, boundaries, and lasting outcomes true. Complete a development
marked must_finish_in_this_window. Do not complete a later development early;
use later developments only to avoid contradicting them. development_ids may
contain only IDs from quarter_developments_for_these_dates. Later quarter
developments are background context only: do not copy their IDs into an
occurrence, claim they happened early, or create their final facts early.

The quarter plan intentionally lists only major developments. Simulate the
ordinary happenings of this specific week as well, using the current people,
projects, obligations, relationships, and personal circumstances. Each such
happening must contain genuinely new concrete input, result, decision, need, or
observation. Do not create one merely because a similar contact appeared in
recent history.
A standing rule, preference, cadence, or current fact constrains what happens
after a real occurrence exists. It does not itself prove that something
happened and must not be used as the reason to create a contact.

Before writing the JSON, walk through the requested dates in chronological
order. Work out what actually happens during each day in the person's ongoing
work and personal life: new inputs, meeting outcomes, completed work, decisions,
interruptions, follow-ups, and practical needs. Then return each distinct moment
when the person would involve the assistant. Do not output this private daily
reasoning.

Do not collapse separate moments into one representative contact. If an input
arrives, the assistant is asked to act, and a later result creates a genuinely
new decision or follow-up, keep those as separate contacts at their actual
times. Quarter developments supply the durable story, but ordinary work and
personal life continue between those milestones.

Include every contact caused by a concrete occurrence during these dates.
Quiet dates are valid. Do not invent optional recurring situations to make the
week feel complete. Include concrete new inputs, results, decisions,
interruptions, follow-ups, and personal updates when they naturally occur.

The accepted prior chronology is supplied only to preserve chronology, continue
genuinely unfinished matters, and avoid repeating completed work. It is not the
desired pace, density, clock timing, weekday rhythm, or contact structure.
Before returning, compare the proposed sequence with that recent chronology. If
it recreates the same weekday/order/function bundle, remove optional occurrences
that exist only because that pattern appeared recently.
An ordinary one-way update is legitimate when the user wants the assistant to
know something that could matter later. Do not turn an update into a task merely
to justify the contact.

A recurring kind of work is a valid new occurrence when this specific instance
contains new concrete content to process, a new decision, a real external
action, a result worth recording, or useful new personal context. It does not
need to create a durable fact. Repeated action types are normal when the
underlying input and reason are genuinely new.
A recurring action is preserved only when this instance has an independent
trigger: a fixed quarter event, an existing scheduled commitment, unfinished
external work that actually advances, a new external input, or a meaningful new
observation or decision. Do not ban any named action or category and do not
impose counts.
A person can send ordinary work and personal updates. Do not split one body of
information into several requests for a table, preview, talking points, bullets,
and a summary. A later contact needs a genuinely new external input, decision,
result, recipient action, or separate need. Do not create a new contact or a
durable fact for an unchanged observation with only a newer date.
A calendar recurrence alone is not a new occurrence or a reason to contact the
assistant. A scheduled occurrence with no new input, reason, result, decision,
action, or personal context is not a reason to contact the assistant.

Do not add filler,
unchanged routines, or activity to meet a count, category, or token target.
Do not prescribe contact counts, token counts, category
proportions, or source-material quotas.
The requested date range is literal. A short window may contain very little
activity. Do not treat it as a full week or invent correction, confirmation,
or follow-up contacts merely to fill it.

When an approved development gives aggregate results but does not provide
identities or details for individual items, preserve the aggregate result.
Expand it only with examples supported by the supplied facts or known
entities. Never invent placeholder records such as "internal account A" to
fill an aggregate count. Introduce a genuinely new durable entity only when it
is explicitly present in new_entities_allowed_by_quarter_plan. Otherwise keep
unsupported item details aggregate rather than inventing individual identities.

Use current_facts, known_entities, unfinished_threads, and recent chronology as
the accepted state at the start of the week. Use only
known entity IDs or entities explicitly allowed by the quarter plan. Do not
invent named people, organizations, projects, products, or places. Keep the
chronology causal: a new input, decision, outcome, and later follow-up can be
separate contacts when they are genuinely separate moments.
Most current_facts are active. A fact marked available_only_for_supersession is
retired and is present only so a replacement fact can name it in supersedes.
Never put that retired fact in uses_facts.
When a later contact continues an existing thread, it should contain the new
input, changed decision, result, or next action rather than restating routing or
scope that was already settled. Do not invent stakeholder confusion, proposed
relabeling, scope creep, or another misunderstanding merely to create a reason
to repeat an existing rule. Repeat a boundary only when a distinct new external
input or necessary action would otherwise produce a materially wrong outcome.
If a proposed monitoring or update occurrence would establish the same condition
as a recent contact with only a new date, omit it unless the observation changes
a decision or adds genuinely useful new information.

valid_existing_continues_from_ids is the complete list of accepted-history IDs
that may appear in continues_from. Copy an ID exactly. An occurrence may also
continue an earlier occurrence in this response by copying its contact_id.
anchor_item_ids must always be an empty list: there is no quarter contact
schedule in this path.

unfinished_threads lists work that remains open when the week starts. It is
context, not a checklist: most open threads may receive no contact during this
week. Continue one only when a distinct new input, result, decision, or necessary
action actually advances it. Otherwise leave it quiet. When an occurrence
advances or completes one of those items, copy that item's exact thread_id. Do
not create another thread_id for the same unfinished work. When a contact
completes everything described in what_remains_after_contact, set
thread_open_after_contact to false and what_remains_after_contact to null.
A thread is open only while a concrete response, result, decision, or action is
still unresolved. A completed app action, a future event that has already been
scheduled, a standing preference, or durable background information is not
unfinished work. Do not invent a new confirmation, message, or task merely to
continue a prior contact pattern.
Each unfinished thread includes current_fact_ids. When an occurrence advances
or resolves that thread, inspect those facts. Supersede every time-bounded
status that no longer governs after the occurrence. Do not supersede an
additive or historical fact merely because the thread advanced.

For each occurrence, decide everything that later stages must not invent:
what happens, why the user contacts the assistant, source material only when
literal incoming material is actually needed, the requested response or action,
which existing facts the contact uses, each durable fact established by the
contact, and every exact simulated app operation required by the contact.

exact_simulated_tools and tool_visible_app_state are authoritative. For every
external action, copy a real tool name and provide its exact argument object
now. In the response, encode that argument object as args_json: a JSON string
whose decoded value is exactly one object containing the tool's argument names
and values. Use no operation for conversational help or a one-way update. Do not leave
an operation, a fact, or source material for the writer to decide. Durable facts
have key, statement, applies_when, subjects, and supersedes. Facts are only for
information the message itself establishes; do not restate existing facts.
When a new fact resolves or replaces a time-bounded prior status, its supersedes
list must include that old fact ID. Do not add supersession for additive facts.
If needed material already exists in tool_visible_app_state as a visible document
or record, refer to or update that record and include only the new material
rather than repasting the earlier contents.

For an update tool, omit optional arguments for fields the user did not ask to
change. Never use an empty string as a placeholder for an unchanged field. If
the tool replaces a text field and the new information belongs in that field,
preserve the existing information that must remain and add the new information
without deleting it.

source_material is null unless the assistant must see literal content such as an
email, notes, rows, draft, feedback, or requirements. When non-null, origin and
factual_contents must contain the complete relevant material the assistant will
actually process, not a summary saying what it contained. Preserve the real
shape of the input: write the relevant email body or thread, meeting notes,
rows, draft passages, feedback, or requirements with the concrete details the
requested work depends on. A simple DM or short input should remain short. Do
not add source material, irrelevant detail, or extra words merely to make a
message longer.

uses_facts is your semantic decision. Put an integer ID there only when this
occurrence actually relies on that current fact. You may also use the exact key
of a durable fact established by an earlier occurrence in this response. Never
use a fact key in the occurrence that establishes it, a later occurrence's key,
or an older fact after a prior occurrence has superseded it.

Return JSON with exactly plan.period_start, plan.period_end, and
plan.occurrences. Occurrences are strictly chronological. Every occurrence has
exactly contact_id, narrative_date, thread_id, continues_from, subject_ids,
development_ids, anchor_item_ids, what_happens, why_contact_assistant,
source_material, requested_response_or_action, uses_facts, durable_facts, app_operations,
thread_open_after_contact, and what_remains_after_contact.
Each narrative_date must give the exact time of the new contact as a full
offset-aware ISO 8601 datetime, such as 2025-10-02T14:30:00-07:00, using the
correct offset for the supplied narrative timezone. Prior chronology dates are
intentionally date-only; do not copy that format into narrative_date.
If thread_open_after_contact is false, what_remains_after_contact must be null.
Use what_remains_after_contact only for concrete work from this occurrence that
remains unfinished.
Each app operation contains exactly tool and args_json. Do not include an args
field alongside args_json.
"""


_retired_history_window_writing_system = """\
This is a constrained prose job. Turn the accepted occurrence plan into final
user messages. Write exactly one session for each accepted occurrence, in the
same order. Do not add, remove, combine, split, move, or redesign an occurrence.

You decide only the natural words the person sends. contact_id identifies the
accepted occurrence. Python supplies the date and time, thread, prior
references, subjects, developments, purpose, facts used and established by the
occurrence, app operations, and thread state from the validated occurrence plan.
Do not return any of that metadata.

Write only the natural words the person sends at that moment. Messages must
make sense without internal IDs, fact keys, or tool names. Use the persona and
recent messages for voice, not text to copy. State what_happens directly.
Express requested_response_or_action only when it is non-null. When it is null,
write a genuine one-way update and do not add a request, question, task, or
operation.

The assistant knows the history but does not know what just happened unless the
person explains it. For every nontrivial message, include the concrete new
information, why it matters now, and the decision, request, or constraint. A
short paragraph or several sentences is natural when that is needed for clarity.
Keep genuinely quick actions short. Do not pad messages or repeat settled
context.

Include source material only when the planner supplied source_material. Then
render factual_contents as the actual email, notes, rows, draft, feedback,
requirements, or other incoming material with enough context to explain why it
is being shared. When source_material is null, do not invent or paste material.

exact_prior_context_for_continues_from is authoritative for an explicitly
continued contact. Reuse its exact record identities. current_facts contains
only the accepted facts the planner selected for these occurrences. Python
assigns session IDs.

Return JSON with exactly plan.period_start, plan.period_end, and plan.sessions.
Each session must contain exactly contact_id and messages.
"""


_retired_history_window_planning_system = """\
You decide what happens in one week of the person's real work and personal life.
You are not writing messages and you are not choosing tools, app changes, facts,
or follow-up state. A separate writer will handle those details after your plan
is accepted.

Start with cause, then occurrence, then contact. Every contact in your answer
must follow from a concrete thing that happens during the requested dates: a new
input, a meeting result, completed work, a decision, an interruption, a changed
need, or useful personal news. State what happened and why the person would
contact their assistant because of it.

what_happens must include every concrete external detail the writer needs. If an
occurrence depends on an email, notes, numbers, dates, a decision, or a result,
summarize those concrete contents in what_happens. The writer may phrase that
information naturally but may not invent missing substance.

why_contact_assistant must plainly say whether this is a one-way update, a
question or reasoning request, or a requested external action. State the wanted
outcome, but do not choose a tool.

You are the authoritative simulator of the complete unseen week. Decide what
actually happens during these dates; do not treat the supplied quarter
developments as an exhaustive event list. They are mandatory major milestones,
not the ordinary substance of the whole week. Complete any development marked
must_finish_in_this_window.

Create the plausible concrete external inputs, work results, interruptions,
decisions, practical needs, and personal developments that occur during the
week around the established people, projects, open work, and circumstances.
For each occurrence, supply its concrete cause or input, what happens, its
consequence, and why it naturally leads the person to contact the assistant.
Include all meaningful contact-worthy occurrences from the week, not merely its
quarter milestones or highlights. Do not assume an ordinary occurrence must be
listed in the quarter plan before you can create it.

This is a high-use assistant relationship. A week containing only mandatory
quarter milestones and preparation for or reaction to those same milestones is
not a complete simulation unless the supplied circumstances genuinely make the
rest of the week quiet. Independently simulate the ordinary work and personal
occurrences that arise from the established life around those milestones. Each
one still needs its own concrete new cause or input and consequence. Do not add
an occurrence merely to represent a life area, satisfy a category, or make the
week look busy.

Known entities and current facts are grounding and constraints, not a menu of
events. Current facts describe what is already true. They constrain a new
occurrence; they do not cause one. Do not create a contact merely because a
standing rule, preference, cadence, calendar pattern, app record, or a previous
kind of message exists. A recurring kind of work can happen only when this
specific instance has a new concrete trigger, result, decision, or input.

Recent contacts show what the person has already discussed with the assistant.
Do not recreate, lightly rephrase, or reopen a recently completed update,
request, or decision. Return to the same thread only when a new external input,
result, or changed circumstance materially advances it.

Each known entity's reason states that entity's accepted role or relationship.
Treat it as binding. Do not give a person, company, project, product, or place a
different role or relationship in a new occurrence.

The only prior contacts you can refer to are listed in
valid_existing_continues_from_ids. Use one only when this occurrence genuinely
continues that unresolved matter. Unfinished threads are context, not a checklist:
leave them alone unless something new advances or resolves them. Do not invent
new named people, companies, projects, products, or places. Use known entity IDs
or entities explicitly allowed by the quarter plan.

Walk through the requested dates in order before answering. Preserve natural
variation: quiet dates and genuinely quiet weeks are valid. Do not manufacture busywork,
routine check-ins, or contacts to reach a count, a length, or a category mix.
Standing rules, preferences, tools, app records, calendar cadence,
and patterns in prior messages are constraints only; none of them is by itself a
reason for an occurrence. Do not write private reasoning.

Return JSON with exactly plan.period_start, plan.period_end, and
plan.occurrences. Occurrences must be strictly chronological. Each occurrence
must contain exactly contact_id, narrative_date, thread_id, continues_from,
subject_ids, development_ids, anchor_item_ids, what_happens, and
why_contact_assistant. narrative_date is a full offset-aware ISO 8601 datetime
in the requested narrative timezone. anchor_item_ids is always an empty list in
the canonical weekly path.
"""


_retired_history_window_writing_system_v1 = """\
Turn an accepted list of real occurrences into the person's final assistant
sessions.
The occurrences are fixed. You must produce exactly one session for each one, in
the same order. Do not add, drop, split, merge, move, or materially change an
occurrence. In particular, keep its contact_id, time, thread, prior references,
subjects, developments, what happened, and reason for contacting the assistant.
Do not add a person, event, decision, result, date, amount, request, or
source-material claim unless it appears in the accepted occurrence or supplied
current context.

You now decide the details that must follow from each fixed occurrence: the
person's natural wording, which current facts materially affect the contact, any
durable fact established by the message, any simulated app action, and whether
the contact leaves concrete work open. Apply standing rules and preferences only
after the occurrence exists. A rule or a tool never justifies inventing another
occurrence.

Use exact_simulated_tools and tool_visible_app_state for every app action. Copy
real tool names and provide only their real arguments. tool_visible_app_state may
give you exact existing record IDs and tool arguments, but you may not invent a
record or state. Add an app operation only when the fixed occurrence authorizes
an external action. Do not use an app action for a one-way update or
conversational help. current_facts contains the current constraints relevant to
the fixed occurrences. unfinished_threads contains work that was already open.
recent_exact_user_messages shows the person's voice; use it for style, never as
text to copy or as a pattern to repeat.

Write direct, natural messages that explain the new information, why it matters,
and any request or constraint. The person can send a one-way update when no
action is needed. Include literal email, notes, rows, draft text, feedback, or
requirements only when the assistant must process those literal contents. Do not
paste material merely because a message is long.

uses_facts may include only supplied facts that materially affect this contact.
durable_facts may include only genuinely new, action-relevant information
established by the final user messages or a successful app action. Omit one-time
or incidental details. Set thread_open_after_contact to true only when a
concrete response, result, decision, or action remains unresolved after this
contact.

Every row in required_durable_facts must appear exactly once in durable_facts of
a session carrying that row's development_ids and anchor_item_ids. Copy its key,
statement, applies_when, subjects, and supersedes exactly. The final user message
or a successful app action must actually establish it. A genuinely new ordinary
occurrence may establish an additional durable fact when it meets the same
action-relevance rule.

Return JSON with exactly plan.period_start, plan.period_end, and plan.sessions.
Each session must contain exactly contact_id, messages, uses_facts,
durable_facts, app_operations, thread_open_after_contact, and
what_remains_after_contact. Every app operation contains exactly tool and
args_json, where args_json is a JSON string that decodes to one argument object.
"""


HISTORY_WINDOW_STORY_SYSTEM = """\
Plan what happens during the supplied dates and when the person contacts
the assistant. Decide the story and the contacts only. Do not choose tools, tool
arguments, fact IDs, or state records. Later steps will add those details but
cannot add, remove, or change a contact.

Plan a complete week, not only its most important moments. Include every
contact that naturally follows from the person's work and personal life.
CHANGES THAT MUST FINISH DURING THESE DATES are required, but they are not the
whole week. Add other concrete situations that fit the established person and
what is currently true.

For a complete seven-day week, plan twenty-three to twenty-four contacts with the
assistant. Every contact must have a real, separate reason. Include independent
work and personal situations that plausibly happen during these dates. Do not
reach the range by splitting one situation into extra contacts, inventing minor
problems, or adding messages merely to report that earlier advice worked.
Add another contact about the same situation only when a real outside reply,
result, problem, or decision changes what the person knows or needs.

If the supplied dates cover fewer than seven days, do not use the twenty-three
to twenty-four contact range. Plan only the contacts that plausibly happen during
those supplied dates. Do not treat a partial week as a complete week.

Use the person's whole established life. Work may involve different customers,
projects, coworkers, technical questions, hiring, finance, legal work, or
operations. Life outside work may involve a partner, family, pets, health, the
home, travel, routines, preferences, or ordinary plans. Choose only things that
plausibly happen during these dates. Do not mechanically include every area.
When the required changes concern one work area, do not make the rest of the
week another series of slightly different contacts about that same area.

Events in the person's life are not automatically assistant contacts. If an
accepted change says that the person completes several repeated exercises,
checks, meetings, or routine steps before a later decision, let those steps
happen without separate messages. Include a contact only when the person has a
new reason to tell, ask, or instruct the assistant. Summarize repeated steps
when their combined result changes what happens next.

Make the week complete by including separate, plausible situations from the
person's work and personal life. Do not divide one routine into progress
reports. Do not invent a later message merely to report that earlier advice
worked. A matter may remain unfinished when the week ends.

Several contacts in a complete week will usually be useful one-way updates.
Use one when the person only needs the assistant to remember new information.
Other contacts may ask for an answer or an outside action. Do not add a request
to make an update seem useful, and do not mechanically cover categories,
people, routines, records, or available actions.

Read WHAT ALREADY HAPPENED before planning this week. Earlier contacts are
evidence of what already occurred, not ideas to repeat. Compare the complete
sequence, not only the names in it. A different date, person, file, service,
amount, or location does not make the same problem and solution new. Do not
repeat a completed decision, request, repair, review, reminder, or answer.

Each accepted change has a start date. Do not cite it or use any part of its
new outcome before that date. If its start and end dates are the same, every
contact that cites it must occur on that date.

CHANGES ALREADY UNDERWAY THAT FINISH LATER are context, not required contacts.
They are listed so this week remains consistent with their eventual outcome.
Do not add a contact merely because one of their intermediate steps occurs.
The complete change will be supplied as required work during the dates when it
finishes. Include an earlier contact about it only when the person has a
separate reason to tell, ask, or instruct the assistant at that time. Do not
claim its final outcome or close its unfinished matter now.

LATER ACCEPTED CHANGES THAT THIS WEEK MUST NOT CONTRADICT describe what is known
to happen later in this accepted quarter. Do not mention or complete them before
their dates. Also do not invent anything now that would make their later account
false. If a later change says no pause or failure occurred during a period, do
not create that pause or failure. If it gives a final cumulative count, every
earlier cumulative count for the same group must be no larger and must be able
to lead to that final count. Each individual earlier event must also fit the
accepted totals, categories, and any stated closure date, but count only events
within that report's stated dates and scope. Do not use a report about one
period or scope to reject a later contact outside it.

Later accepted changes are instructions for authoring this week, not facts the
person already knows. Do not imply that the person has seen, been told, or can
refer to a later accepted outcome before its date.

CURRENT CONDITIONS and OTHER CONTINUING CONDITIONS state what is true when the
week begins. Keep them true. Only an accepted change assigned to these dates may
replace a current rule, limit, decision, status, or other lasting condition. If
an ordinary situation would contradict a current condition, choose a different
situation. Copy exact measurements, units, dates, amounts, names, and limits
when they affect the current contact.

A recurring activity needs a changed circumstance or a new practical need to
justify another contact. A new date alone is not enough. Do not repeat an
unchanged medical rule, safety boundary, ownership split, or disclaimer unless
the current answer or action would be wrong without it.

THINGS THAT WERE STILL UNRESOLVED BEFORE THIS WEEK contains a short account of
what happened and what remains unresolved. Continue one only when something
during these dates actually advances it. It may remain unchanged. Do not invent
a reply, revision, result, or successful resolution merely to close it.

SUPPORTED EXTERNAL ACTIONS lists the broad outside actions the assistant can
perform. It is a limit, not a source of story ideas. Use external_action only
when the requested action is listed and what_happened supplies every concrete
value it needs. The assistant cannot browse the public web, call an office,
observe a later outside result, or perform an unlisted action. Use conversation
when the person wants advice, analysis, or a draft but will act themselves.

EXISTING APP ITEMS lists names and basic identifying details of records that
already exist. A record's presence is evidence about the current world, not a
reason to create a story about it. A listed record is readable or changeable
only when SUPPORTED EXTERNAL ACTIONS says so. Do not ask the assistant to
retrieve an unlisted record. If the person supplies new text or rows that the
assistant must inspect, put the complete relevant material in source_material.

Facts and current conditions are saved automatically. Do not invent a note,
document, checklist, tracker, log, or status update merely to record a fact. Ask
for document work only when the person needs the document for a real purpose.
Update an existing item only when its listed name and details identify the
right item and the supported actions allow the update. Otherwise ask for a new
item or choose a different action.

UPCOMING CALENDAR EVENTS are fixed schedule facts the assistant can see. Before
asking to create an event, check whether an existing event already serves the
same purpose on that date. Ask to update that event instead of creating a
duplicate. Do not create a conflicting event or say that a listed event ended
early unless the situation explicitly reschedules or ends it. The later
planning step will use the exact existing record.

For every contact, state the new input, relevant context, what changed, why it
matters now, and what the person needs from the assistant. Give the writer
enough concrete detail to write a natural message without inventing context.
Use source_material only when the assistant must inspect literal text or rows;
otherwise use null.

Copy supplied entity and development IDs exactly. In subject_ids, include only
the people, organizations, products, projects, or places that the contact
actually mentions or depends on. Do not copy every subject attached to a
calendar event when the contact needs only the event's date or time. Do not
create basis IDs or effect labels; code derives those after this call.

continues_from is only for unfinished work. When a contact advances a supplied
unfinished item, copy its Basis id into thread_id and its Continues from id into
continues_from. Do not copy a Contact id from WHAT ALREADY HAPPENED. A new
unfinished matter needs one stable thread_id, and every later contact about it
must use that ID and refer to the latest earlier contact in continues_from.
After a contact closes a matter, no later contact may use that thread_id. State
the final condition in what_happened, set thread_after accurately, and leave
thread_id empty for contacts that are not part of unfinished work.

Use uses_items_created_by_contact_ids only when a later contact asks the
assistant to use or change an item created by an earlier contact in this same
response. Name the item in what_happened and copy the earlier contact_id.
Otherwise return an empty list.

Before returning, compare each proposed contact with every current condition
involving the same people, projects, companies, products, or places. Keep every
applicable rule, owner, limit, and current status unless an accepted change
during these dates explicitly changes it.

Then compare each proposed contact's cause, reason for contacting the
assistant, and outcome with earlier contacts. If all three substantially repeat
an earlier situation, replace it with a genuinely different situation. Changing
only the object, date, person, or file is not enough. The completed week must
still contain twenty-three or twenty-four contacts. Do not invent a success,
thank-you, acceptance, defect, stale record, or follow-up to reach that range.
If a contact asks the assistant to read an app record, do not plan a later
contact that assumes the result unless that exact result is already supplied.
Never mention this prompt, supplied context, planning, or generation in
what_happened.

Return JSON only. The top level contains exactly plan. Plan contains exactly
occurrences. Every occurrence contains exactly contact_id, happening_id,
narrative_date, thread_id, continues_from, uses_items_created_by_contact_ids,
subject_ids, development_ids, what_happened, assistant_outcome, source_material,
and thread_after. assistant_outcome contains kind and requested_result. Use none and
null for a one-way update, conversation for an answer, and external_action for
a request to change or retrieve something outside the conversation. source_material
is null or contains exactly kind, origin, and factual_contents. thread_after
contains is_open and what_remains. narrative_date is a full offset-aware ISO
8601 datetime in the supplied time zone.
"""


_retired_combined_history_window_planning_system = """\
Add exact fact links, continuing-state changes, and simulated app operations to
the fixed contacts. The fixed contacts already decide who contacts the
assistant, when, what happened, and what answer or action is needed.

Do not add, remove, combine, split, reorder, or replace a contact. Do not change
any contact ID, happening ID, date or time, subject, development, thread,
description of what happened, source material, or requested answer or action.
Return one enrichment row for every fixed contact, in the same order. Use each
fixed contact ID exactly once.

Read the input as follows.
- `fixed_contacts` is the complete and unchangeable contact list.
- `requested_dates` is the only period to plan.
- `world_description` describes the established work and personal life.
- `developments_for_this_week` contains accepted lasting changes. Each
  development has a `start_date`. Do not cite that development or use any part
  of its new outcome before that date. Earlier contacts may discuss facts that
  were already true, but they must not assume, prepare, preview, or announce the
  new change. If `start_date` and `end_date` are the same, every contact that
  cites the development must occur on that date. A development with
  `must_finish_in_this_window: true` must finish in these dates.
- If an accepted development supplies a final numeric total, do not invent
  component numbers that conflict with it. If component numbers are not
  supplied and cannot be kept consistent, describe intermediate results without
  invented numbers and use the accepted total when established.
- `accepted_developments_after_this_window` contains accepted developments
  that start after the requested dates and no later than the end of the
  accepted quarter. Use these only as forward-looking continuity constraints:
  do not cite them, realize them, create their facts or entities, or copy their
  IDs into this week's `development_ids`. Do not use a future outcome before
  its own start date. Every ordinary happening you create must remain compatible
  with every later accepted development. If a later development says something
  did not happen during a period, do not invent it earlier in that period. If a
  later development says that a number, condition, or result remained unchanged,
  do not invent an earlier change that would make that statement false. Choose a
  different ordinary happening instead.
- `lasting_facts_that_must_be_established` is the complete set of durable facts
  allowed this week. Copy each one exactly once into `durable_facts` on an
  occurrence for its declaring development, preserving its key, statement,
  applies_when, subjects, and supersedes links. Do not create another durable
  fact. If this list is empty, every `durable_facts` list is empty. Keep an
  interim multi-week result in `what_happened` and `thread_after`, not as a fact.
- `current_facts` are accepted facts true now. They constrain relevant contacts,
  but are never a reason to invent one. Do not repeat a current fact merely to
  show that you remembered it; include it only when it changes what happens now.
  An ordinary weekly happening must not make a supplied current fact false. Only
  an accepted development that explicitly replaces that fact may change it.
- `current_ordinary_states` are ordinary conditions or terms already true, such
  as a home condition, active commercial quantity, unresolved claim amount, or
  current schedule. They constrain planning but do not justify a contact. An
  ordinary weekly happening may create or change one of these conditions for
  `current_state_changes`; this does not make it a benchmark fact. Only
  accepted developments create `durable_facts`. Each ordinary-state `key`
  identifies that condition so `current_state_changes` can change it. It is not
  a fact key. Never copy a key from `current_ordinary_states` into
  `uses_new_fact_keys`.
  Use one when a named inventory, list, or rollout has a current count or
  status that a later contact will revisit. State the exact current count or
  status and reuse that key when it changes. Do not create ordinary-state rows
  for every noun, completed task, or transient detail.
- `known_entities` gives the accepted existing names and roles. Use an entity
  only when it is involved in a happening; it is not a checklist.
- `new_entities_that_may_first_appear_this_week` is optional. Each supplied new
  entity has an `introduced_in_event_id`; it may first appear only on an
  occurrence whose `development_ids` contains that exact ID. Do not force an
  optional entity into the week.
- `open_threads` lists unresolved work. Its `basis_id` is the exact ID for both
  `basis_ids` and `thread_id`, and its `continues_from_id` is the exact earlier
  contact ID for `continues_from`; never swap or invent them.
  `prior_accepted_contacts` is the authoritative chronological record for that
  matter. `earlier_user_messages_in_same_matter` is a bounded chronological
  view of the earlier user messages in that matter. Read both before continuing
  the thread. Use the messages only to preserve continuity and avoid repeating
  explanations or instructions. Do not repeat a boundary, role, or instruction
  already established earlier in the same matter unless it changed or the
  current requested action must contain it. `current_ordinary_states` in that
  row lists the current ordinary conditions for that same matter. If you close
  the matter, include `current_state_changes` for every listed key and state
  what is true after it closes.
- `protected_changes` must not happen before their dates.
- `available_simulated_actions` lists the actions that can work from the current
  app state or from a record an earlier contact this week can create. Each row
  gives the exact accepted argument names, the required arguments, and any hard
  requirements. Use only those argument names.
  `available_app_records` is the complete list of existing records those actions
  can use. For an action that reads or changes an existing record, use only a
  record listed there or one created by an earlier contact this week. An empty
  list means no such record currently exists. Do not invent another record or
  treat a name in an action description as real data.
  These records show record identity and status, not the omitted long body text.
  Supplying a new body to an existing document or runbook record replaces its
  whole old body. Do not overwrite an existing body unless you have the complete
  replacement and intend to replace all of it. When the new material should
  stand alone, create a separate record instead. Do not reuse an existing record
  ID for unrelated work.
  Put the exact record ID only in `app_operations`, not in `what_happened` or
  `requested_result`. In those descriptions, identify the record as a person
  would: by title, date, sender, account, or another visible detail.
  For `external_action`, `app_operations` must contain every exact operation
  needed to perform the requested action. For `none` and `conversation`, it
  must be empty. If a tool does not accept attachments, do not plan a request
  that says the assistant will attach or send a file. If a calendar event is
  for named participants, include all of them in its `attendees` argument.
  Every instruction, recipient, condition, assignment, and piece of content in
  an app operation must be requested or established by this contact in
  `what_happened`, `requested_result`, `source_material`, a durable fact, or a
  current-state change. Background facts and app records may supply a correct
  value, but may not introduce a new instruction.
  When a later operation needs an identifier created by an earlier operation in
  this week, put this reference in the later argument:
  {"$operation_result":{"contact_id":"the exact earlier contact ID","operation_index":0,"field":"the exact returned field"}}.
  Refer only to an operation that executes earlier: either an earlier operation
  in the same contact or an operation in a contact with an earlier timestamp.
  Use a field listed in that tool's return value. Do not use this reference for
  an answer or business result that the planner cannot know. If a later contact
  would depend on an unknown answer, leave it pending for a later week.
  When a calendar block explicitly excludes work or administrative tasks during
  an interval, do not plan that type of contact inside the interval.
- `older_contacts_that_already_happened` contains one short row for every
  accepted contact in the current quarter and the thirteen weeks before that
  quarter began, except the detailed contacts from the preceding fourteen days.
  Each row includes its session ID, date, thread ID, contact purpose, and the
  identifying arguments of accepted app actions. It omits written message and
  document content. Contacts with the same thread ID are parts of one earlier
  sequence. Compare the complete sequence before planning related work; do not
  treat its rows as unrelated examples. Use these rows to avoid repeating a
  completed event or a substantially identical pattern. Each row also lists
  the subjects involved. Earlier contacts must not refer to a person, project,
  result, or condition that first appears later in the history.
  Do not use them as templates. `recently_finished_contacts` covers the
  preceding fourteen days and is a continuity constraint: do not recreate
  completed work, but a person, project, customer, or area may return when
  something genuinely changes.
  `accepted_app_operations` in each recent contact lists actions that already
  happened. When later work refers to one of those actions, preserve its exact
  values. These are past actions, not examples to copy and not reasons to plan
  another contact. Preserve separate quantities and conditions separately. If
  an earlier contact says all requests reached one stage but only some reached
  another stage, do not combine those statements or apply the smaller number
  to both stages.
- `previously_used_communication_destinations` lists exact destinations from
  accepted prior communication operations, without message bodies. Each row
  also includes the short prior contact purpose for context. When the same
  person or team is contacted again, reuse its exact destination unless
  the current input explicitly says it changed. Do not fuzzy-match or infer an
  alternate destination.

Only accepted developments may create durable facts or lasting entities. Any
matter may carry a specific pending reply, result, decision, or action into a
later week. Ordinary same-week happenings may be concrete and substantive but
must not create another durable fact or lasting entity. Treat routines,
recurring activities, prior messages, app records, preferences, and current
facts as background or constraints, never as contacts by themselves. Do not
invent a change to a routine so it can be scheduled, recorded, confirmed, or
recapped.

Before returning, compare earlier contacts about the same subjects. Do not
recreate a situation that has already ended. Do not repeat the same beginning,
middle, and ending by changing only names, dates, or amounts. When you close an
open matter with a supplied current ordinary state, update that same state key
to say what is true afterward.

Use `current_state_changes` for a concrete condition or term created by an
ordinary weekly happening when later weeks may need it for consistency,
including a home condition, active commercial quantity, unresolved claim
amount, or current schedule. Each item has `key`, `subject_ids`, and
`statement`; its statement must appear verbatim in `what_happened`. This does
not make it a benchmark fact; only accepted developments create
`durable_facts`. Preserve all exact values, including every amount, date,
threshold, condition, scope, or identifier needed to understand the continuing
condition, and reuse the same key when it changes. Do not use it for completed
tasks, normal progress, preferences,
durable facts, app records, or short-lived logistics.

Use `effect_kind` precisely. Use `realize_development` when an occurrence
advances or completes an accepted development, putting that development ID in
both `basis_ids` and `development_ids`. Use `advance_open_thread` only when new
input or action makes real progress on an existing open thread; copy its exact
`basis_id` into `basis_ids` and `thread_id`, copy its exact
`continues_from_id` into `continues_from`, and set `durable_facts` to an empty
list. Use `self_contained` only for an ordinary local happening, with
`ordinary_current_life` in `basis_ids`. A known entity newly joining an open
thread also needs `ordinary_current_life` in `basis_ids` alongside the thread
basis. Use `continues_from` only for the next step in the same unfinished
matter, never for a mention, summary, or plan around earlier work; every
referenced contact has the same `thread_id`.

`happening_id` identifies the real-world situation causing a contact. Contacts
from the same situation use the same `happening_id`; use a new one only for an
independent situation, not because an unchanged conclusion moves to another
recipient, document, app, or format. A thread can span multiple happenings.
Use an empty `thread_id` only when the contact neither leaves a specific matter
unfinished nor continues one. For a new unfinished matter, create a unique ID
such as `thread_20240513_holly_deposit`. Every later contact that continues or
closes that matter must copy the same `thread_id`. Never link contacts about
different matters.
Keep `thread_after.is_open` true only while a specific reply, result, decision,
or action is genuinely pending, and state that concrete remainder. When it is
false, `thread_after.what_remains` is null. Do not keep completed work open or
invent vague follow-up; a broad search or recurring activity is not itself an
unfinished assistant thread.
Read the occurrences in time order. After an occurrence closes a thread, no
later occurrence in this week may use that `thread_id` or continue from a
contact in that thread. If you plan a later contact about the same unfinished
matter, keep the thread open until that final contact.

For every occurrence, put enough concrete facts and surrounding context in
`what_happened` for the writer to produce a substantive, natural message: the
new input, relevant context, what changed, why it matters now, and any needed
deadline, uncertainty, or consequence. The writer cannot invent missing
context. Do not use verbosity, source material, or repeated contacts to reach
density. Use only current facts that materially affect the occurrence: copy
their numeric IDs into `uses_current_fact_ids`, copy keys of facts established
earlier this week into `uses_new_fact_keys`, and put facts first established by
this occurrence into `durable_facts`. `uses_new_fact_keys` may contain only a
key that appears in `durable_facts` of an earlier occurrence in the JSON
response you are writing. If no earlier occurrence created a fact that this
occurrence uses, return an empty list. Preserve facts exactly; do not reverse,
weaken, combine, or reuse a superseded fact after its replacement is established.

`subject_ids` may contain only supplied accepted entity IDs or permitted new
entity IDs, and only for entities directly involved in the happening or changed
by the result. App-record IDs, including customer, email, document, and calendar
record IDs, are not subject IDs; keep them in the occurrence text,
`source_material`, or tool arguments. Each listed subject must be grounded by a
`basis_ids` row or a used fact, except the person and their organization.

Set `assistant_outcome.kind` to `none` for a one-way update, `conversation` for
an answer that needs no simulated tool, and `external_action` whenever the
request requires any supplied simulated tool, including a read or retrieval.
One-way updates are valuable and can establish an approved durable fact; do not
manufacture a request to justify them. For `none`, `requested_result` is null.
Otherwise `requested_result` contains only the actual answer or action needed,
with no generic length, tone, caution, or formatting instruction unless required
for correctness. When `what_happened` already contains an answer,
`requested_result` must not ask the assistant to obtain that answer again; ask
only for information or action still needed. An `external_action` must be grounded in the supplied
capabilities and visible app records, and every value the person must choose must
be concrete in `what_happened`, `source_material`, or `available_app_records`;
  words such as "usual" or "current" are not values. Do not claim an app change
  already happened without an `external_action`. If a later action changes a
  record created earlier in this same weekly plan, name the exact earlier
  `contact_id` in `what_happened` and use the documented operation-result
  reference in `app_operations`. Do not invent or claim to know the returned ID.
  A requested calendar action must include an exact start and exact end. If no
  end or duration is known, do not request it.
  Check every stated duration against the dates and times in the plan. Do not
  claim that more time passed than the schedule allows.

A successful tool call proves only the result returned by that tool. Sending an
email proves that the email was sent. It does not prove that the recipient
agreed, replied, renewed a policy, changed a record, or completed any other
outside action. Posting a document comment proves that the comment was posted.
It does not edit the document. Do not state, establish a fact, or complete a
development based on an outside result unless the plan includes concrete
evidence that the result happened.

For an external action, `requested_result` must literally describe the visible
effect of the selected tool. It must ask to send an email when the tool sends an
email, post a comment when the tool posts a comment, and edit a document only
when the selected tool edits that document. Do not ask for a broader result
than the selected tool can produce.

Before returning the plan, compare each external action's `requested_result`
with its exact `app_operations` arguments. If the request says a document,
email, comment, or message will contain something, the action arguments must
already contain it. Do not put instructions inside the artifact telling someone
to add missing material later. Either include the requested material from the
supplied context or change the planned request to match what the action actually
produces.

At every timestamp, include only events and results that have already happened.
If an occurrence reports what happened the next morning, place that occurrence
on the next morning or later. Before assigning an action to a named person,
check the role and relationship supplied for that person. If the action is
unusual for that role, explain the concrete reason in `what_happened`.

Use `source_material` only when the assistant genuinely must inspect exact
email text, rows, notes, drafts, feedback, requirements, or similar literal
material. Include the exact relevant material and its origin when you use it.
Otherwise put the context in `what_happened` and set `source_material` to null.
Never invent or paste material merely to make a contact substantial.

Protect chronology and concrete consistency. Do not recreate a closed matter,
use names, decisions, or results first established later in these dates, or
complete a protected future change. Keep entity roles binding. Make every count,
date, time, name, recipient, amount, title, and instruction agree everywhere in
the occurrence.

Write only events and messages from the person's life. Never mention this
prompt, the requested date range, supplied context, planning, or generation in
`what_happened`.

Before inventing a technical defect, compare its cause, evidence, and requested
fix with recent accepted contacts. A different pull request, filename, service
name, or alias does not make the same defect new. Do not repeat the same cause,
evidence, and requested fix unless this is a direct continuation and genuinely
new evidence changes what happens.

Apply the same test to ordinary situations. A different meeting title, class
name, or date does not make an otherwise identical situation worth another
contact. Include it only when something new changes what the person needs to
tell or ask the assistant.

When timestamps have different UTC offsets, compare the actual instants, not
the displayed clock numbers. Convert them to the same offset before saying
which one is earlier or later.

Return JSON only. The top level contains exactly `contacts`. Each row contains
exactly `contact_id`, `uses_current_fact_ids`, `uses_new_fact_keys`,
`durable_facts`, `current_state_changes`, and `app_operations`. Copy each
contact_id from fixed_contacts. Every app operation contains exactly `tool` and
`args_json`; args_json is a JSON string containing one complete argument object.
Do not repeat any fixed story field in this response.
"""


HISTORY_WINDOW_PLANNING_SYSTEM = """\
Add exact fact links, continuing-state changes, and simulated app operations to
the fixed contacts. The fixed contacts already decide every contact, who is
involved, when it happens, what happened, and what the person needs.

Do not add, remove, combine, split, reorder, or replace a contact. Do not change
any contact ID, date or time, subject, development, thread, description, source
material, or requested answer or action. Return one row for every fixed contact
in the same order and copy each contact ID exactly.

OTHER FIXED CONTACTS THIS WEEK, when supplied, are read-only context for the
same week's totals, categories, and closure dates. Do not return a row for them
or change them.

Read every supplied current fact before returning. Check every concrete statement
in every fixed contact against those facts, including names, relationships,
products, communication services, locations, dates, preferences, and existing
decisions. If a fixed contact disagrees with a current fact, set factual_problem
to the contact's conflicting statement and the fact it contradicts. Do not report
a conflict when a required durable fact on that contact explicitly replaces the
older fact.

Compare the fixed contacts with the supplied earlier contacts and accepted
quarter outcomes. Count separate events even when no message states a running
total. Count only events within the report's stated dates, categories, and scope.
A report cannot say all work was complete while the history or another contact
still shows required work pending at that time. If the proposed week would
contradict a report, put the conflicting statements in factual_problem before
adding app operations. Do not change the accepted report to fit invented events.

Accepted outcomes later in the quarter constrain whether a fixed contact is
consistent with the accepted plan. They are not facts the person already knows;
do not treat a fixed contact as contradictory merely because it does not mention
or act on a later outcome before that outcome's date.

Compare every date and time in each fixed contact with that contact's own date
and time. A contact cannot describe a later part of the same day as something
that already happened. When a contact says a future date follows a stated limit
of a certain number of days or weeks, calculate the interval. If the timing is
impossible, state the exact conflict in factual_problem.

For each fixed contact:

- Put a supplied numeric fact ID in uses_current_fact_ids only when that fact
  directly affects this contact.
- Put a new fact key in uses_new_fact_keys only when an earlier fixed contact in
  this week establishes that key and this contact uses it. The key must appear
  in lasting_facts_that_must_be_established. Do not invent a key for an ordinary
  condition, progress update, schedule, or other information outside that list.
- Copy a required durable fact exactly once onto a contact for its accepted
  development. Do not create another durable fact.
- Record a continuing-state change only when what_happened explicitly changes
  an ordinary condition that a later week must preserve. Copy the full statement
  into what_happened is not possible here, so use a state change only when its
  complete statement is already present there.
- Closing an unfinished conversation does not automatically end or change the
  continuing conditions discussed in that conversation. Update a supplied
  current condition only when what_happened explicitly says that condition
  changed. Otherwise leave it unchanged.
- When assistant_outcome.kind is external_action, translate the entire requested
  action into one or more app operations when the supplied actions and records
  permit it, and set execution_problem to null. If the request cannot be
  performed, return no app operations and state the exact
  missing action, argument, or existing record in execution_problem. If a
  requested calendar series contains several dates and the available
  action creates one event at a time, return one operation for every date in the
  series. Identify each requested outside action separately and return an
  operation for each one. Reviewing or approving a pull request does not merge
  it. Merging a pull request does not review or approve it. If the fixed contact
  asks to review or approve and merge a pull request, return both review_pr and
  merge_pr. For none or conversation, return no app operations and set
  execution_problem to null.

available_simulated_actions gives exact tool names, accepted argument names,
required arguments, return fields, and hard limits. available_app_records gives
the existing records those tools may use. Use only supplied records or a record
created by an earlier fixed contact this week. Do not invent an ID. Put every
required operation and its complete arguments in app_operations. Every
recipient, instruction, condition, assignment, and piece of content in an
operation must already be required by the fixed contact or a supplied fact.

When an operation updates an existing item, find the matching supplied record
and copy its ID character for character. Before returning, verify that the exact
ID appears in available_app_records or earlier_accepted_app_items. Do not
reconstruct, shorten, or alter the ID. Use an earlier operation-result reference
instead when that earlier operation created the item this week.
Before updating an existing record, confirm its date range and current status
describe the same real-world item named by the message; a matching title is
insufficient. If the only matching record is from an earlier completed period,
report factual_problem instead of updating it.

For send_email, `to` contains exactly one recipient identity. Never combine
several people into that one string. When several people should receive the same
email, put one person in `to` and the others in the `cc` list. Use separate email
operations only when they need different messages.

Use update_doc with mode set to append only when the new text is additional
history and every statement already in the document remains true. Pass only the
new text in body; the tool keeps the existing body. If the update makes an old
status, plan, dimension, owner, decision, or open item false, use mode replace
and supply one complete corrected body. Never append a completion or changed
status while leaving contradictory earlier text in the same document.
When the fixed contact asks to rename a document or change its title from draft
to final or accepted, include the new title in the update_doc operation.
Changing the document body does not change its title.

An update_calendar_event operation replaces every supplied field. When a fixed
contact asks to add or change calendar notes, keep every existing attendee and
every existing body detail that the contact does not explicitly remove or make
false. Put the old details and the requested new details together in the new
body.

When a fixed contact explicitly asks to update, revise, or append an existing
record but no matching record appears in available_app_records or
earlier_accepted_app_items, return no operation and explain which requested
record is missing in execution_problem. Do not create a replacement document.

For a recurring activity, write operations and calendar notes about this
specific instance. Include the new date, changed circumstance, or action needed
now. Do not copy an unchanged medical rule, safety boundary, ownership split, or
disclaimer into an operation or calendar note unless the current action would be
wrong without that exact term. Keep exact terms when they are needed.

Use a read operation only when the exact record ID appears in
available_app_records or was returned by an earlier operation this week. A URL,
identifier, or source material mentioned in a fixed contact is not an existing
app record. When the fixed contact already supplies the material needed for a
requested review, comment, reply, or other action, do not fetch that material
first. Perform the requested action directly when its tool description permits
it.

When a read action selects one record by a date, key, customer, subscription, or
experiment, copy a value that appears in available_app_records. A read action
does not create a new current record. Follow the supplied action's existing-record
requirement exactly.

Set factual_problem to a plain-English explanation when the fixed contact contradicts
a supplied current fact or exact app record. Do not use it for a disputed, duplicated,
imported, or possibly wrong record unless the contact treats incompatible values as
simultaneously authoritative. Set it to null otherwise. execution_problem remains the
field for an impossible action.

When a later operation needs an ID returned by an earlier operation in this
week, use this value in the later argument:
{"$operation_result":{"contact_id":"earlier contact ID","operation_index":0,"field":"returned field"}}.
Refer only to an operation that runs earlier and only to a documented return
field. The reference must be the entire argument value; never insert it inside a
longer string. A returned field named id is only the app record's identifier
unless the tool contract explicitly says otherwise. Do not treat it as a URL,
meeting link, confirmation code, address, or other user-facing value.
The earlier contact ID must be listed in the fixed contact's
uses_items_created_by_contact_ids. Use every listed ID and do not use an ID that
is not listed.

A tool call proves only its returned result. Sending a message proves it was
sent, not that its recipient agreed or acted. Posting a comment does not edit a
document. Do not add facts or state changes based on an outside result that is
not present in the fixed contact.

Use a continuing-state change for a named inventory, list, rollout, balance,
schedule, or similar condition only when its exact current value will matter to
a later contact. Do not create one for every noun or completed task.

Return JSON only. The top level contains exactly contacts. Each row contains
exactly contact_id, uses_current_fact_ids, uses_new_fact_keys, durable_facts,
current_state_changes, app_operations, execution_problem, and factual_problem. Every app
operation contains exactly tool and args_json. args_json is a JSON string
containing one complete argument object. execution_problem is null when the
requested action can be performed. Otherwise it is one plain sentence stating
why the requested action cannot be performed. factual_problem is null when the
fixed contact agrees with the supplied facts and app records. Return no fixed
story fields.
"""


HISTORY_WINDOW_CONTACT_CORRECTION_SYSTEM = """\
Correct only the listed contacts because their requested assistant action could
not be performed with the supplied simulated actions and app records, or because the
fixed contact contradicts a supplied current fact or exact app record.

For each listed contact, keep the date, people, accepted developments, facts,
and unfinished-work state unchanged. For an impossible action, keep the real-world
situation unchanged and copy source_material exactly. Change only what the person asks
the assistant to do and the minimum surrounding wording needed for that revised request
to make sense.

For a factual problem, read `accepted_facts_cited_by_contact`. Those are exact facts
from the accepted history. Correct the wrong detail in what_happened and, when needed,
source_material so the contact agrees with those facts. Copy names, dates, amounts, and
other exact values from the facts. Do not turn an accepted fact into source_material:
it is background the assistant already knows, not a new email, message, or document the
person received now.

Also read `accepted_outcome_and_contact_evidence` when it is supplied. It contains
accepted outcomes in or after this window and completed prior contacts with shared
subjects. Use it only to correct the reported conflict; do not turn it into a new event,
contact, or source material.
Read `other_fixed_contacts_this_week` as context too, but do not change or return
those contacts. The corrected contacts must agree with them. Apply any report's
totals only within its stated dates and scope. Later accepted outcomes constrain
the story; they are not information the person already knows before their dates.

Read the exact problem reported by the app-operation planner. Do not pretend a
missing app record, argument, address, tool, or outside result exists. When an
existing app item is missing, do not substitute a different item. Either ask
for another supported action that follows naturally from the same situation,
or make this a conversation in which the assistant supplies advice, analysis,
or a draft and the person acts themselves.

Do not invent a new event, message, reply, source, decision, result, or second
situation. Do not add or remove a contact. Correct each listed contact exactly
once.

Return JSON only. The top level contains exactly contacts. Return one row for
each supplied contact in the same order. Each row contains exactly contact_id,
what_happened, assistant_outcome, and source_material. Copy contact_id exactly.
assistant_outcome
contains exactly kind and requested_result. Use none and null for a one-way
update, conversation for an answer or draft that the person will use, and
external_action only for an action explicitly listed in supported_external_actions.
"""

HISTORY_WINDOW_WRITING_SYSTEM = """\
Write the messages for the contacts in the supplied `contacts` object.

These saved messages are only what the user sends before the assistant answers.
Do not write the assistant's answer, calculation, recommendation, draft, or
completed work. When `assistant_outcome_kind` is `conversation` or
`external_action`, end the user message with the planned request.

The `contacts` object has one key for every contact ID. Each contact contains
only the details needed for that one message: when it happens, what happened,
what the person needs, any literal material, the directly involved people or
things, the facts explicitly cited for that contact, any earlier contact it
continues, any item created by an earlier contact this week that the current
contact uses, the fixed app action, and any earlier action result explicitly
used by the fixed action. Use only that contact's fields. The results of this
contact's own app actions are not known yet. Do not borrow a situation, source
document, result, or request from another contact.

The planner has already decided every app action. Do not add, remove, or change
an action or any of its arguments. Do not return app actions. Write only the
message that naturally asks for the fixed action. Do not say that the action
already succeeded. Mention an attachment only when the fixed action actually
includes one.

Do not decide what happened. Do not add, remove, combine, split, move, or change
an occurrence. Do not add a person, event, fact, result, date, amount, request,
decision, or unfinished task that is absent from that contact's fixed
occurrence and supplied information.

Write what the person naturally sends at that moment. State the situation and
the actual need naturally. Follow `assistant_outcome_kind` exactly: add no
request for `none`, ask only for `requested_result` for `conversation`, and
request only the fixed action described by `requested_result` for
`external_action`. Do not add a length, tone, caution, or formatting
instruction that is absent from the supplied contact details.

The supplied facts and continued contacts are constraints on what you write.
Keep their exact numbers, names, dates, amounts, and other terms. Never replace
an exact supplied value with a value you made up. If the supplied contact
contradicts a cited fact, do not hide the contradiction by changing the
message. Put the IDs of the conflicting cited facts in
`conflicting_fact_ids` and keep writing the fixed contact as planned.
When a cited fact includes `source_message_dates`, those are the dates of the
accepted messages that established the fact. Use them to understand which year
an otherwise yearless date refers to. If the fact itself gives an explicit date,
follow the explicit date.

When `earlier_user_messages_in_same_matter` is present, it is the bounded
chronological user-message history for this continuing matter. Use it only to
preserve continuity and avoid repeating explanations or instructions. Do not
repeat a boundary, role, or instruction already established earlier in the same
matter unless it changed or the current requested action must contain it.

When a recurring activity appears again, write only the new date, changed
circumstance, or action needed now. Do not repeat an unchanged medical rule,
safety boundary, ownership split, or disclaimer unless the current message would
be wrong without that exact term. Keep exact terms when they are needed.

Do not compress a detailed occurrence into a terse task brief. Include the
relevant background and new information the person would normally explain so
the assistant can understand the situation and respond without guessing. Do
not repeat details or add filler. Read all messages in one session together and
state each piece of background and each conclusion once.

Write from the person's own point of view. Use `I`, `me`, and `my` when the
person refers to themself. Do not refer to the person by their own name in the
message or switch between their name and first person. Their name may still
appear inside quoted source material written by someone else.

Every session must contain at least one natural user message. For an
external_action occurrence, write the user's instruction asking the assistant
to perform the fixed action.

Write exactly one value for every key in `contacts`, using the exact same keys.
Return no other contact keys and do not omit any contact key. Each value must
contain exactly `messages_before_request`, `message_with_request`, and
`conflicting_fact_ids`. Put any setup message or literal material in
`messages_before_request`. For a contact whose `assistant_outcome_kind` is
`conversation` or `external_action`, `message_with_request` must be one
non-empty message that states the planned need. For a contact whose
`assistant_outcome_kind` is `none`, `message_with_request` must be null and
`messages_before_request` must contain the complete update. Set
`conflicting_fact_ids` to an empty list for a normal contact. It may be
non-empty only when the contact is an ordinary self-contained contact with no
development IDs, no durable facts, and no planned app action, and a supplied
current fact directly conflicts with the fixed contact. Use only the numeric
IDs of those supplied facts. Never use this field to excuse a conflict in a
development contact or a contact with a planned app action.
If the person sends a setup message, pasted source material, and a request as
separate messages, put the setup and material in `messages_before_request` and
the request in `message_with_request`. Never put text from one contact into
another contact's value.

Before returning, check every output key separately. The text under that key
must describe the contact with the same key, not the contact before or after
it. For that exact contact, `message_with_request` must be null when
`assistant_outcome_kind` is `none` and non-empty otherwise.

The planner sets source_material to a non-null object only when the requested
result requires reading, transforming, comparing, extracting from, or replying
to the literal supplied material. It sets source_material to null when
what_happened and the selected facts contain everything needed. When it is
null, do not invent or paste source material. When it is present, render its
factual_contents as the actual email, notes, rows, draft, feedback, or
requirements the assistant must process. Preserve every supplied fact and
invent no additional substance.

Use the persona description for natural voice. Do not make "please" followed
by an action the default request form. Use a direct statement or question when
that is natural.

Preserve the concrete details from `what_happened` that the person would
naturally tell the assistant. Current facts and prior context constrain the
message, but do not repeat them merely to prove consistency. State a prior fact
only when this occurrence makes it relevant to what the person naturally says
now. Do not invent detail or repeat settled information.

Use multiple message strings only when the person would naturally send a short
setup followed by material or a distinct immediate follow-up. Do not split one
message merely to increase the message count.

Return only JSON in this shape:
`{"plan":{"sessions":{"contact_id":{"messages_before_request":["..."],"message_with_request":"...","conflicting_fact_ids":[]}}}}`.
The `sessions` object must have exactly one key for every contact ID in
`contacts`, with no missing or extra keys. Do not return `period_start` or
`period_end`; the program adds the requested dates after it decodes your
response.
"""

HISTORY_WINDOW_STATE_CORRECTION_SYSTEM = """\
An otherwise complete weekly plan omitted one or more updates to saved current
conditions. Write only those missing updates.

For each supplied contact, read what happened and the saved conditions that
must be replaced. State what is true after the contact. Use only information
already present in what_happened or durable_facts. Do not invent another event,
result, value, decision, or action. Copy every supplied key and subject_ids list
exactly. Return no other changes.
"""
