Write one complete test for the approved idea. Preserve its situation, requested
work, and selected facts. Verify them against the actual dated messages, including
relevant later updates. If the work needs information that is not established,
report why you cannot write this idea. Do not silently change its facts or work.

Write the user request, starting records, and grading checks together. The request
must sound like a person delegating real work and make sense without the earlier
messages. Include the situation and non-memory inputs needed to act. Leave only
the required remembered information unstated. Do not add work to make a remembered
detail necessary, and do not announce that a hidden rule or preference controls it.
Keep the request short and natural, normally one or two sentences. Do not copy
the planner's explanation into it. Preserve inputs needed to act; omit background
that does not change the task. Prefer one coherent action unless the approved work
genuinely requires several related actions. Use plain, direct language rather than
scenario narration, benchmark language, or an evaluator-style checklist. Do not
target a fixed word count.

The app records are a selection catalog, not the assistant's starting state.
Only records you select or add become visible. Select records needed for reads,
updates, targets, dependencies, and scheduling conflicts. Do not drop a required
record to conceal an answer. New records must fit the tools and must not reveal
the remembered answer. Judge leakage in the final request and constructed state,
not in unselected catalog records. Select complete existing records; do not remove
fields. For list-valued collections, use an empty record_key and exact match
fields identifying one record. For dictionary-valued collections, use the supplied
record_key. New dictionary records need record_key; new list records use an empty
record_key and put their identifier in the record itself.

For each check, say what makes the completed action correct, why this work needs
that result, and which source passages support it. A true fact is not automatically
required output. Distinguish using information to choose correctly from being
asked to state that information. A past request does not establish its outcome.
Do not treat a dated fact as obsolete solely because it is old; read later updates.
An explicit newer instruction takes precedence over an older one.

Apply an omission test to every selected fact and every clause in a grading
criterion: if removing only that fact or clause still leaves a fully correct
completion of the approved work, do not grade it. If an approved selected fact
cannot change any necessary observable result, return cannot_write and identify
the planning mismatch instead of adding optional work or optional grading detail.

Grade every necessary result that depends on selected facts. Every selected fact
must affect at least one check. Also prove each requested state-changing or
external action occurred, using an existing result check when possible. Otherwise
add one tool_called check for that action. Do not separately grade ordinary
request details or supporting reads unless memory determines the read itself.
Check a request-supplied target only when needed to apply the remembered result
to the right person, record, or action. Avoid duplicate checks.
For a reminder whose remembered content belongs in its notes, checks on those
notes already prove the event was created. Do not add title, duration, or date
checks merely because the request supplies those ordinary details.

Preserve all allowed ways to complete the work. Do not turn a limit into an exact
value, choose arbitrary dates where several are valid, force one recipient
representation when the tool allows equivalents, or require content in multiple
places unless those places need it independently. Reject a contradictory current
claim, not an accurately labeled historical comparison. Required content may be
expressed in different words unless the source or request makes wording literal.

Choose each comparison explicitly from check_types. Use semantic criteria when
meaning or a relationship between arguments determines correctness. A mechanical
failure never falls back to semantic grading. The judge sees the complete current
request and all arguments of the same action, but these are not proof that the
required result occurred. Missing or contradicted meaning must fail.
Every field-check path starts with args., for example args.body or args.start.
Use the exact argument name from the tool contract, not a bare field name.

Give checks stable IDs. Checks describing one action share an action_id and must
pass on the same call. Different requested actions have different action IDs.
Do not impose a call count or prohibit additional actions without a requirement
that makes those additional actions wrong. Do not pretend a single-call check
can prove a rule about a whole sequence of calls. Report unsupported grading
needs rather than inventing a check type or weakening the requirement.

Quote exact source passages and reference them from the checks they support.
Several passages may support one fact; one passage may support several checks.
Copy the supplied session and message IDs. Do not copy whole source messages
repeatedly or write a second expected-answer list. Explain why a check is necessary
in one short sentence. Each selected fact needs evidence used by a check. Checks
for necessary request-supplied targets or final effects may have no evidence IDs.

For a written test, complete quality_audit after designing the request, state,
evidence, and checks. This is internal reasoning for the independent reviewer; it
never appears in the user's request or candidate test. State the reader and useful
goal, why the request is natural and its scope coherent, and why the request and
constructed state do not reveal the remembered results. List every required
non-memory input and point to /user_request or its exact JSON Pointer under
/starting_app_data as its source. Map each necessary remembered result to
the selected facts and checks that enforce it. Map each final action to its tool,
action_id, and checks. Audit every check as a remembered result, final action, or
target, using all roles that apply, and explain why it is necessary and not
duplicated. These claims must describe the completed response; do not use them to
add requirements that the approved work does not need.

Return status written, an empty problem, the complete test, and quality_audit; or
cannot_write, a concrete problem, test null, and quality_audit null. Use the
supplied provider response shape.
Flexible record match, record, value, and values fields are JSON-encoded strings
named match_json, record_json, value_json, and values_json in that response. They
contain actual JSON values, not descriptions. Code decodes them without changing
their meaning. Other fields remain ordinary structured fields.

When correction is supplied, return the complete revised response but change
only the allowed fields. Preserve everything else. Report any problem that needs
a change outside that permission instead of making the change.
Refresh quality_audit so it describes the complete corrected response; the audit
is not permission to change another test field.
When test.checks may change, preserve every entry named in protected_check_ids
exactly and in its original relative order. Do not change source evidence or
intended tools through a correction assigned to another part.
