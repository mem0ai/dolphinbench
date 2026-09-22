# DolphinBench Test Authoring Contract

This document defines what a DolphinBench test is and how test-authoring work must
proceed. Read it before inspecting, changing, or running the test-authoring
system. If code, prompts, another document, or a saved run conflicts with this
contract, stop and report the conflict before taking further action.

## The Benchmark Requirement

A DolphinBench test gives the assistant realistic present-day work. Completing that
work correctly must require information from the user's earlier messages.

Memory may change any necessary, observable part of the completed work:

- which tool is called;
- whether a tool is called;
- a recipient, record, date, time, amount, option, or other tool argument;
- required factual content in an email, message, document, comment, or record;
- required wording, tone, framing, or scope when the user established it;
- any other result whose omission or alteration makes the completed work wrong.

The remembered information does **not** have to change the tool name or create
a branching decision. A no-memory assistant may call the same tool and still
fail because its arguments or output content omit or contradict necessary
information.

The paper's statement that memory must "change the action" uses *action* to
mean the complete observable result, including tool arguments and generated
content. The supplementary prompt confirms this by allowing a task to use an
exact remembered value in an external action.

## Inclusion Rules

A test may ship only when all of these are true:

1. The user asks for realistic work that a personal assistant could perform.
   The test is not a standalone trivia or recall question.
2. One or more earlier user messages supply information required to complete
   that work correctly.
3. The final request and readable app state do not reveal the required value.
4. The cited source messages actually establish the required value.
5. The test uses the value that is correct for the test date. Later messages
   that replace or limit it are respected.
6. Every required tool, record, identifier, and non-memory input is available
   from the request, readable app state, or source messages.
7. A capable assistant given the source messages can complete the work.
8. A capable assistant without memory fails at least one necessary grading
   check.
9. Every grading check measures a distinct result required for successful
   completion. Do not grade optional details or the same result twice.
10. Grade every result whose correct value or content depends on the earlier
    messages. Each selected fact must change at least one such result, and the
    grading checks must test that result. Also prove that every requested final
    action occurred when it changes app state or has an external effect, such
    as sending a message, creating a document, or booking a trip. A result
    check on that action already proves it occurred; otherwise add one simple
    tool-used check. Do not add separate checks for ordinary details supplied
    by the new request or current app state. Do not add checks for supporting
    reads unless the remembered result determines what that read must retrieve
    or how it must be performed. Check a request-supplied identifier only when
    it is needed to prove that the remembered result was applied to the right
    person, record, or action.

The four-run gate proves rules 7 and 8: both runs with the cited source
messages must pass, and both runs without memory must fail.

## Facts And Tests Are Different Decisions

A fact is not unsuitable merely because it is descriptive, historical, or
used as content. Test suitability depends on the fact and the proposed work
together.

For example, "Scaffold raised an $18M Series B led by Northstar" can support a
valid test when Morgan asks the assistant to email an investor with the
company's latest wins and funding context. The email is new work, and the
remembered fact supplies required content that is absent from the request and
apps.

The fact record is responsible for only these questions:

- What did the user actually establish?
- Which source messages establish it?
- When is it true?
- Did later information replace, narrow, or contradict it?

The proposed test is responsible for these different questions:

- Is the present-day work realistic?
- Does the work genuinely need this information?
- Does the request leave the required value unstated?
- Would memory change a necessary observable result?
- Would a capable no-memory assistant fail a required check?

Do not reject a fact globally because one poorly written request leaks its
answer. Rewrite or replace that request. Reject or limit the fact itself only
when its source evidence or temporal interpretation is wrong, or when careful
consideration shows that no available realistic action can use it without the
answer already being present.

## Responsibilities Of Each Step

### Approved production simplification (September 8, 2026)

The explicitly approved `workflow: production` runtime policy replaces the
staged preflight and repeated exception-review sequence for unfinished version-4
work. It uses the same three public commands and preserves accepted tests.
For fresh explicitly approved version-4 plans, `authoring.create_tests` accepts
the production `--runtime-policy` with the ordinary `--start-id` or `--test-ids`
arguments. It records the approved plan and authenticates per-test progress
before paid work, using the same queues as a resume. Fresh creation does not
require a synthetic source run. New plans still require explicit human acceptance.

1. Write the complete test from its approved idea and complete relevant dated
   messages. Keep one set of checks and source references; do not require a
   second writer audit table.
2. Validate structure locally: supplied source IDs, selected facts, tools,
   argument names, available records, and check bindings. Source explanations
   may paraphrase the referenced message. Do not use literal quote matches,
   prose keywords, or regexes to decide whether their meaning is supported.
   The independent reviewer compares the explanation with the actual original
   messages, including newer instructions. Original history is never rewritten.
3. Run the two with-history and two no-history attempts. Park failed candidates
   with their evidence rather than sending every failure through model review
   and an unchanged execution rerun. Technical attempt limits still apply.
4. Independently review each passing candidate once, using the real calls and
   their results instead of synthetic positive/negative examples. Code verifies
   complete four-attempt evidence first. Approval still requires useful work,
   supported and current facts, no answer leakage, complete executable inputs,
   necessary checks, and actual no-history failure on a remembered result.
5. Permit at most one targeted production correction. Keep its count across
   resumes and preserve the earlier total correction count and ceiling. Checks
   may be corrected together with their dependent source evidence; unrelated
   checks and their evidence remain protected. Grading/evidence-only changes
   reuse executions when the request, state, history, and agent settings match.
   Changed requests or state require new executions. Park unresolved results.

The requester name is supplied in the actual request in both certification
conditions and in the resulting test used by final evaluation, not in private
oracle instructions. This fixes missing first-person attribution without
exposing a remembered role or decision. Shared source selection still preserves
mandatory supporting messages and replacement links. Reviews save complete
local evidence, but model inputs do not repeat the same source history four times.

Optional provider-header feedback uses reported remaining capacity conservatively,
subtracts outstanding local reservations, observes the reported model-specific
limit, and retains FIFO admission and rate-limit cooldown. Invalid or stale
headers fall back to configured pacing; headers do not establish billing rules.

These rules supersede the preflight, literal-quotation, duplicate-audit, and
exception-review requirements below only for explicitly selected production
work. The six counterexamples, human acceptance of ideas, four-run standard,
accepted-work preservation, and separate publication approval remain in force.

An explicitly approved grading-only follow-up may set
`grading_only_recovery: true` in a production runtime policy and resume saved
unfinished tests through `authoring.create_tests`. It authenticates the approved
plan, source records, candidate, and four complete saved oracle attempts before
reviewing failed grading. This review may diagnose incorrect, incomplete,
unnecessary, duplicate, or unfairly narrow checks. It must establish necessary
results from the approved work and original dated sources, not from which
answers happened to pass. Meaning that permits varied wording uses semantic
judging; genuinely exact requirements retain exact comparisons.

Only affected checks and dependent evidence may change, within the remaining
one-production-correction allowance. Unrelated checks, accepted tests, request,
app state, tools, facts, history and assistant settings remain unchanged. Regrade
all four saved attempts; missing or incompatible execution evidence stays
pending and never falls back to new assistant execution. Genuine assistant
mistakes, missing source support, answer leakage and grader application errors
do not justify weakening correct requirements. Acceptance still requires two
with-history passes, two no-history failures on necessary remembered results,
and independent source-grounded review. This follow-up does not authorize new
ideas, publication, or final benchmark evaluation.

To correct an older production status that misclassified an authenticated
`pending` review as a rejected idea, use `authoring.create_tests` with
`--reconcile-review-statuses`, the original plan and `--resume-new-test-run`,
selected `--test-ids`, and a fresh `--out`. This local-only operation refuses
paid-call confirmation, genuine rejections, accepted tests and changed evidence.
It preserves the original run and accepted work, writes resumable pending
records, and replaces only the affected rejection-file reference used by future
planning. It makes no model calls and changes no test or review decision.

### Approved staged creation policy (September 8, 2026)

An explicitly supplied `--runtime-policy` on `authoring.create_tests` enables
the approved staged workflow for unfinished version-4 work. The policy and
source-run identity are saved by hash. It does not change accepted candidates,
published grading, checkpoint facts, or the approved situation and work.

Evidence preparation selects dependencies from compact dated fact records once
per checkpoint and selected fact set. Code always retains the selected facts'
original messages, explicit replacement chains, and related-history links.
Shared subjects provide a selection catalog, not an instruction to include all
their original messages. Save the selection response and complete selected
messages. The reviewer also receives the compact candidate dependency catalog
to check omissions; missing temporal support remains pending. The writer and
with-history assistant receive the same complete selected source messages, but
only the writer receives fact statements, grading instructions, and evidence
annotations. Never truncate a source message or introduce an answer key into
certification.

The staged workflow has separately bounded preparation, writing, preflight,
certification, and exception-review queues. Save completion before enqueueing
the next stage. Provider timeouts and technical attempts have explicit limits;
an exhausted or uncertain call remains pending and releases its worker. A
stage failure must not discard unrelated completed tests.

Provider admission uses a shared first-in, first-out queue with configured
token and request pacing. A rate-limit response pauses new calls to that model
for its retry interval. Save allowlisted rate-limit headers and separate local
queue time from provider time; configured pacing is not evidence of a provider
quota. Reference-agent tool turns use Responses when the staged policy selects
that API. Preserve reasoning items and tool outputs across turns.

An enabled policy permits up to three model-written, field-scoped corrections.
Persist their count and failed candidate fingerprints; stop without another
correction if the candidate repeats. Never reset this allowance on resume.
Execution-only recovery remains limited to one unchanged four-attempt rerun;
technical failures retain their separate bounded attempt policy. Grading-only
changes reuse saved executions. A semantic-judge application error is a system
grading failure, not an assistant execution failure or permission to change a
correct criterion.

Optional action-batched semantic judging returns an independent result for
each named criterion against the same complete call. It is explicitly
versioned in candidate grading configuration. Exact comparisons and distinct
same-action call assignment remain code-owned. Existing accepted tests retain
their previous judging behavior. New behavior requires regression and pilot
validation before broader use.

These opt-in rules replace the one-correction and selected-fact-only history
rules below only for their authenticated staged work. All inclusion rules,
the four-run gate, human acceptance of new ideas, and separate publication
approval still apply. Candidate identities may exceed 200; an explicitly
approved release mapping assigns positions 1 through 200 while retaining each
original candidate identity, content hash, and certification provenance.

### 1. Fact records

Store the official fact text, source-message IDs, dates, and explicit
replacement relationships. Do not decide how a future request should be
phrased here.

### 2. Test planning

`authoring.propose` makes one through ten planning calls concurrently. Code
partitions selectable current facts into non-overlapping shards while keeping
explicit replacement chains together. Facts about the same concrete subjects
may appear in another shard as non-selectable context so later information is
still visible without assigning one fact to two planners. Each call
receives the evaluation date, one assigned fact shard in compact form, concise
tool capabilities, and the same compact accepted, approved, and rejected work.
Each fact lists the accepted tests that already use it. Every current fact remains
available in exactly one shard for new work; prefer unused facts only when ideas
are otherwise equally useful.

The planner does not receive full source messages, app records, traces, or
grading data. Each shard returns up to the requested one through 25 ideas, or
explains why it could not fill the count. The merged plan contains at most 250
ideas. An idea contains the present situation, requested
work, selected fact IDs, intended tools, why history is necessary, and the most
likely result from a capable assistant without history. It does not write the
final request, a detailed answer checklist, source quotations, or grading checks.
Code assigns idea IDs and checkpoint metadata and writes `candidate_plan.json`.
The command stops for explicit human acceptance.

Code verifies the checkpoint identity, shard assignment, current facts, tool
names, unique ideas, and requested count. It saves each shard response and its
usage before validating it, so one incomplete or malformed response does not
erase completed shards. Such a response remains a recorded shard error, not a
global fact rejection. Matching authenticated response caches may be reused
without a provider call.

The planner must not propose standalone recall, announce an unspecified hidden
preference, invent a standing rule or completed action, or invent a document
solely to make recall look like useful work. Required remembered content in an
otherwise identical tool call is valid. Do not keep an idea when its likely
no-history completion already satisfies the intended work.

### 3. Complete test writing

One writer call reads the exact approved idea, selected facts, full original
source messages with their dates, related facts and replacement links, exact
tool contracts, and relevant app records. Each original message appears once,
with a program-assigned message ID. The writer verifies the idea against these
messages and writes the request, starting records, and grading checks together.
New ideas identify this workflow with `authoring_version: 4`.

Approval fixes the situation, work, and selected facts. It does not make an
unsupported claim true. The writer must not expand the work, invent source
support, or silently replace the approved idea. An earlier request establishes
what the user asked for, not whether that past action occurred. Preserve temporal
conditions and later updates; source age alone does not establish expiration.
A newer explicit instruction takes precedence over older information.

The request must provide the non-memory inputs needed to act while leaving the
required remembered information unstated. Keep platform, routing, recipient,
record, and other details necessary to execute the work. The assistant must be
able to discover any input not supplied directly. Do not hide a missing input
by dropping part of the approved task.

#### Starting app records

Baseline records are a selection catalog, not the assistant's starting state.
Code copies only selected complete existing records and new records into
`mock_state`. Empty selections produce empty state. Unselected baseline records
are invisible to the assistant.

Do not supply an entire existing collection merely because a tool writes into
it. Supply complete collections for declared reads, including dependencies.
The planner should include necessary supporting reads among intended tools.
Keep tool definitions and list/dictionary state-shape metadata even when a
write destination's existing records are omitted.

Narrowing by explicit record IDs or dates is allowed only when that scope covers
the complete task and dependencies. The initial implementation retains complete
read collections rather than guessing scope from prose. No keyword ranking,
silent truncation, or extra record-selection model call is allowed.
`writer_max_input_tokens` sets a serialized writer-input budget, defaulting to
100,000 `o200k_base` tokens for the instructions, payload, and response schema.
An oversized input stays pending before the writer call; it does not invalidate
the idea. This local budget is not a claim about a provider's context limit.

Do not omit a required read or update target to hide the answer. The reviewer
checks leakage in the actual constructed state and request, not in unused
baseline records. Reject the idea when every executable starting state
necessarily reveals its remembered answer.

#### Evidence and checks

The writer returns one written test, or `cannot_write` with a concrete reason.
It cites exact passages using supplied session and message IDs. Several passages
may support one fact, and one passage may support several checks. Every selected
fact needs evidence connected to at least one check. Each check explains why
its result is necessary for this work. Evidence is not a second answer list.

New written responses also include an internal quality audit. It names the useful
work, sources of necessary non-memory inputs, each selected fact's necessary
observable result, final actions, and every check's role and distinct purpose.
Code validates only structural bindings such as IDs, record locations, tools,
actions, and coverage. The independent reviewer decides whether the request is
natural, the work and checks are necessary, the state leaks an answer, and the
writer's explanations are true. No deterministic word or sentence limit, prose
keyword filter, or other natural-language style heuristic is applied. Historical
authenticated writer responses without this audit remain readable.

Code validates types, IDs, source membership, literal quotations, record
selectors, tool fields, and fact coverage. It collects independent errors when
safe. It does not interpret natural prose with substring rules or require a
natural-language date to contain its serialized ISO value. Parse and validation
errors remain pending for diagnosis; they do not authorize an unrestricted rewrite.
After diagnosis, a human may explicitly approve a bounded correction to a saved
version-4 draft's request and checks. Resume with a version-4
`--approved-corrections` file bound to the saved response hash. This requires
an uncorrected validation-pending draft, preserves all other fields and named
unaffected checks, and consumes the one model-written correction. It runs the
normal early review and certification afterward. Interruption preserves that
permission and correction count; it never authorizes a fresh unrestricted draft.

The writer writes the existing version-2 assertions directly, with stable check
IDs and action IDs. Code copies them unchanged into the candidate. It never adds
a recipient restriction, call count, ordinary-detail check, or another grading
requirement during conversion. The reviewer checks that every necessary remembered
result, target, and final effect is covered, with no optional or duplicate checks.
A result check can prove its final effect; otherwise the writer adds one
`tool_called` check. Ordinary supporting reads remain ungraded unless memory
determines what that read must retrieve or how it must be performed.

All checks for one action must pass on the same exact tool call; separate actions
require separate calls. Additional calls are permitted unless an explicit
restriction forbids them. Reuse `graders/explicit.py` and `check_version: 2`.
The writer selects each literal, numeric, date, instant, list, or semantic
comparison explicitly. Mechanical failures never fall back to semantic grading.

Preserve allowed alternatives. A maximum is not an exact value. Equivalent
recipient representations, dates, and wording remain valid when the task and
tools permit them. Do not require repeated content in separate places unless
those places need it independently. Semantic judges receive the current request
and complete arguments of the same call, but these are not evidence that the
requested meaning was communicated. Missing, incomplete, contradictory, or
negated required meaning must fail.

Provider responses use explicit types for fixed fields and JSON-encoded strings
only for flexible record, selector, tool-argument, and comparison-value payloads.
Code decodes and validates those payloads once at the boundary, preserves the
raw response and normalized object, and uses ordinary objects afterward.
It does not repair malformed JSON or change its meaning.

### 4. Review and certification

Use the same short reviewer prompt before and after the four assistant attempts.
The reviewer receives the approved work, complete relevant dated messages,
actual starting state, tool contracts, writer evidence, and every check.
It reports approval, concrete issues, or pending. Each issue names the
responsible part, exact location, supplied evidence, and required change.
Missing evidence or a system error is pending, not an idea rejection.
Approval is not permission to publish.

Current version-4 reviews receive the actual semantic-judge prompt and its input
contract, including complete same-call arguments. A rejection alleging that an
existing check grades incorrectly must supply a complete executable counterexample,
the named check, its predicted result, and the opposite required result. Run the
actual grader and save its full result. An unrelated check failure does not prove
the claim. A disagreement or service error stays pending, not permission to edit.

A proposed missing check must identify its role: remembered result, final action,
or target. Remembered results require original source quotations; target checks
must link the existing remembered checks on the same action. Final actions require
request evidence and a distinct action not already proven by existing checks.
Extra request-supplied content in one email is not a second action. These bindings
validate references and reproduce grading behavior; they do not mechanically
prove a model's interpretation of natural-language requirements.

Before certification, approval requires one complete correct set of tool calls
and one plausible incorrect variant. The incorrect variant changes or omits a
necessary remembered result, uses valid arguments, and names the check that must
fail for that reason. Run the actual grader on these two complete examples and
save the results. A failure of an unrelated check does not validate the intended
negative example. The shared same-action behavior is covered by local regression
tests, not repeated model-generated split-call fixtures for every test.

A malformed review or grader/example disagreement stays pending. Reuse example
grades only when the calls, request, checks, grading code, and judge settings
match. A transport error is not a passing negative example. Repeated resumes
without resolving the disagreement are not a repair strategy.

Certification runs the same test twice with the selected facts' complete original
user messages and twice without them. New tests use ordinary assistant
instructions: complete the current request, use available information, respect
original dates and newer instructions, do not invent facts, and ask for necessary
missing information. Earlier messages are context, not requests to execute now.
No answer key, writer explanation, grading criterion, or review example enters
the assistant prompt. Apart from the history block, instructions, tools, state,
and current request are identical across conditions.

Save the exact initial messages and tool definitions, a hash of the fixed starting
state, actual indexed tool calls and results, continuation messages, final reply,
errors, and every grading check's complete result. Shared initial inputs may be
stored once and referenced by attempts. Do not reduce grading evidence to one
Boolean. Errors do not count as no-history failures.

When an attempt has a definite infrastructure failure, retry only that attempt
at most twice. Preserve every failed attempt and its retry number. Provider,
transport, timeout, and harness-read failures are technical; an assistant's bad
tool call or incomplete work is not. Technical retries do not consume the one
assistant-execution rerun allowed after exception review.

Accept a new version-4 test without final model review only when its preflight
review and grading examples passed, both with-history attempts completed and
passed every check, both no-history attempts completed without technical errors,
and each no-history attempt failed at least one check whose writer-audit role is
`remembered_result`. Candidate, preflight, audit, check, and execution records
must match their authenticated hashes and IDs. Save the complete local decision.

Send every exception to final model review: either with-history attempt fails;
either no-history attempt passes; a no-history attempt does not fail a
remembered-result check; an execution or grade is incomplete, contradictory, or
technically unsuccessful; or the local evidence cannot be authenticated. The
reviewer distinguishes assistant mistakes from invalid tests or incorrect
grading. A genuine assistant mistake does not justify weakening a requirement.

Authenticated older plans, writer responses, reviews, and accepted tests keep
their recorded meaning and grading behavior. They are not automatically migrated,
rewritten, regraded, or recertified. Compatibility code exists only for their
existing approved work; new test creation requires version-4 ideas.

### 5. A test that still fails

Preserve every correct part of a test. Route the correction to the step that
caused the failure:

- If execution alone fails, rerun the unchanged candidate once.
- If grading is wrong, change only the grading checks.
- If the request is wrong, change the request and the checks that depend on it.
- If starting app data is wrong, change that data and the checks that depend on it.
- If the accepted idea is wrong, stop and require a newly approved idea.

A test gets at most one model-written correction. Fixing shared validation,
grading, or execution code does not count as another model-written correction.
After fixing such a code defect, rerun the unchanged test. Do not rewrite its
request, facts, state, or expected result merely to make it pass.

A model-written correction receives the saved authoring response and an
explicit list of fields it may change. Code rejects changes outside that list.
Save the correction and execution-rerun counts across review errors and
resumes. After a correction or execution rerun, apply the same clean-evidence
rule. Run final model review again only when the corrected result is still an
exception. That review does not grant another model-written correction.

An explicit human-approved criterion edit is not another writer attempt. For a
version-4 test whose final review identifies only a grading defect after its
one correction, a hash-bound `--approved-corrections` file may supply exact
before-and-after criterion text for named semantic checks. Apply only that
text, preserve the correction count and all other test fields, and make no
writer or assistant-execution call. Recheck grading examples, regrade the four
saved attempts, and obtain final review. This is a recorded human override,
not an automatic retry or permission to weaken necessary requirements.

For an explicitly approved exact repair, the version-4 `--approved-corrections`
file may instead contain `kind: exact_test_repair`. Bind the saved writer
response, candidate, and gate by hash and name complete before-and-after values
for each changed request, record selection, evidence list, or check list. Apply
those values locally, preserve correction and execution-rerun counts, and permit
no additional model-written correction. Normal validation, early review, grading
examples, certification, and final review still apply. Grading-only repairs reuse
the saved executions when available; a draft stopped before its first
certification receives its first four attempts after the repair passes early
review. Request, state, or supplied-history changes require new
executions. An unsuccessful exact repair stays pending for diagnosis.

An exact grading repair may explicitly recover a draft whose final-review
correction returned `cannot_write` because a shared routing defect granted no
writable fields. Bind the failed progress, response, request, and mixed
grading/execution review by hash. Recover only the authenticated written response
in that failed request, and verify that it reconstructs the bound candidate.
This may complete an execution retry already reserved but never started by the
defective routing code. Preserve both recorded counts, make no writer call, and
record the reserved retry's start and complete result durably. A completed result
may be reused; an uncertain started retry must stay pending rather than run again.
Do not use this recovery to roll back a valid correction or obtain another retry.

A provenance repair may add named existing checkpoint source sessions to a
selected fact. Bind the original fact and complete added messages by hash. Save
this as a per-test authoring overlay and use it consistently for writer evidence,
certification history, and review. Never edit the authenticated checkpoint or
change a fact's statement, selected ID, or temporal meaning through this repair.
The overlay remains part of that test's authenticated resume inputs.

An input-pending test with no writer response may receive `kind:
writer_input_budget` through the same approval file. Bind its saved budget by
hash and record the new `writer_max_input_tokens` for that test only. Preserve
the original config and full input; this does not permit truncation or replay of
a completed writer call.

An accepted working batch may receive an explicit grading-only exact revision,
not an ordinary resume. Bind its planning and provenance files, select every
accepted entry in that batch, and preserve the original files. Only after every
previously accepted entry passes the revision workflow may the persona state
atomically replace that working-batch reference and archive its old reference.
Published releases cannot be replaced through this operation.

## Accepted Examples

- Morgan asks for an investor email containing recent wins and funding
  context. Memory supplies the $18M Northstar-led Series B. The email body is
  graded.
- Morgan asks the assistant to send a first-contact external email. Memory
  supplies Sarah's CC address and Morgan's external signature.
- Morgan asks the assistant to book a work trip from several priced options.
  Memory supplies her combined flight-and-hotel limit, changing the booking.
- Morgan asks the assistant to place Friday dinner. Memory supplies the
  restaurant and dish used in the order.
- Morgan asks for a project update. Memory supplies the current owner, scope,
  or product boundary required in the document.
- Morgan asks for a small latte from a coffee shop. Memory supplies only the
  shop. Grade the shop and prove the order occurred; do not separately grade
  the request-supplied drink size.
- Morgan asks for a scratchpad built from a notes search. Memory supplies the
  vector-search namespace. Grade that namespace and prove the document was
  created; do not separately grade an embedding lookup used only to support
  the search.
- Morgan asks to book a flight and hotel. Memory supplies the spending limit.
  Grade the remembered limit where it changes the selected options and prove
  both bookings occurred; do not separately grade request-supplied route and
  dates unless they are needed to identify the correct bookings.

## Rejected Examples

- "What funding round did we raise?" This is standalone recall rather than
  realistic assistant work.
- "Write this in my preferred style." The request explicitly announces that a
  stored preference must be retrieved.
- A request or app record already states the remembered answer.
- A task grades an ordinary default that a capable stranger would choose
  without memory.
- A task uses an old value after a later message replaced it.
- A task requires a record, address, identifier, or tool capability that does
  not exist.
- Two grading checks separately require a result and the absence of its
  opposite when either check alone already proves success.

## Required Check Before Changing The System

Before changing a prompt, filter, validator, or generation step, test the
proposed rule against all of these statements:

1. It must accept the Series B investor-email example.
2. It must reject the standalone funding-round question.
3. It must accept required remembered content even when the tool name is
   unchanged.
4. It must reject a request or app state that reveals the answer.
5. It must not turn a task-writing failure into a global fact rejection.
6. It must not allow memory to override a newer explicit user instruction.

A proposed rule that fails any statement above is wrong and must not be
implemented.

## Working Rules

Before any implementation or run:

1. State the exact current counts and canonical input files.
2. Show the concrete failure with an actual example.
3. Name which step owns the failure: fact interpretation, planning, design,
   request writing, app-state preparation, grading, or certification.
4. Explain the literal change, the files it affects, and its likely effects on
   later steps.
5. Check the change against the six statements above.
6. Handle minor implementation fixes and local verification autonomously within
   the agreed work. Obtain explicit approval for paid runs, substantive idea
   changes, publication, or changes to accepted work. Explain progress at the
   system level rather than asking the user to manage routine implementation
   details.

Do not launch follow-on work merely because an earlier approved step finished.
Do not rerun unaffected work. Do not generalize from one failed test without
checking counterexamples. When the user asks to stop and discuss the plan,
respond before inspecting, editing, launching, or terminating anything else.

## Active Implementation

The supported implementation has exactly three public commands:

1. `python -m authoring.propose` makes one through ten concurrent planning calls
   over non-overlapping related-fact shards and writes up to 25 ideas per shard,
   with at most 250 version-4 ideas in one merged plan for human acceptance. It
   reads compact current facts, including accepted-test usage, concise tool descriptions,
   and prior work from the persona-state file named in the config. That file
   identifies the published release manifest and authenticated working batches,
   approved plans, and rejected-test records. Callers do not enumerate historical
   batches. Rejections retain their concrete failure reason; pending technical
   problems do not become idea rejections. The planner writes no source-evidence
   response and no detailed required-answer list.

2. `python -m authoring.create_tests` takes explicitly approved version-4 ideas.
   One writer call creates each complete test from original dated messages and
   relevant app data. Code validates the returned evidence, records, and checks
   without adding requirements. The short reviewer and its two complete grading
   examples must pass before certification. Two with-history and two no-history
   attempts are accepted locally when they meet the complete clean-evidence rule;
   only exceptions go to the final model reviewer.
   A test gets at most one model-written correction. A planning defect requires
   a newly approved idea; an execution-only failure permits one unchanged rerun.
   A grading-only correction changes only affected checks and regrades saved
   executions rather than rerunning the assistant. Request and state corrections
   require new executions. Code preserves checks not identified by the review.
   Malformed responses and system errors remain pending. A shared code fix does
   not consume or reset the model-written correction allowance.
   Starting this command records its approved plan in the persona state.
   Completing it records any rejected ideas there. When it writes an
   `accepted_batch`, it also records that authenticated batch in the persona
   state so the next planning call includes the accepted tests automatically.
   A state that points to a frozen published release may also contain later
   working batches; planning reads both without changing the frozen release.
   Planning authenticates published tests against the frozen manifest and reads
   them from the current checkout. Historical batch paths in that manifest are
   provenance, not required planning inputs. Later working batches still require
   their complete hash-bound plans, candidates, and provenance.
   Each accepted provenance result records the SHA-256 hash of its exact
   candidate YAML. Publication rejects missing hashes and candidates changed
   after review.

   Each test saves its progress independently. A review error on one test does
   not discard another test's accepted result. Unfinished work appears in
   `pending_tests.json`, not in the rejected ideas given to future planning.
   New progress records bind the selected plan positions, config, checkpoint,
   and completed artifacts by hash. Resume rejects changed inputs and writes
   only into a fresh output directory. Select only the unfinished IDs; a test
   already included in a completed accepted batch must not be resumed, except
   through the explicit grading-only exact revision described above.

   To package completed clean acceptances from an interrupted run, use
   `--resume-new-test-run` with `--finalize-accepted-only`, the original `--plan`,
   selected `--test-ids`, and a fresh `--out`. This local-only mode authenticates
   the completed decisions, preflight evidence, and four saved attempts. An
   unchanged execution retry may inherit an authenticated earlier preflight for
   the same candidate. The command preserves source files, registers the new
   accepted batch, and makes no provider calls. Pending tests and already
   registered acceptances cannot use this mode.

   An unpublished new-test run may resume through the same public command when
   a shared reviewer defect is fixed. To bound that verification to early review
   and grading examples, add `--stop-after-preflight` to a resume of a written
   version-4 draft with no certification. This option forbids writer corrections
   and assistant execution and saves successful preflight as `certification_pending`.
   It cannot be combined with new correction permissions. A later approved resume
   reuses the matching review and examples before certification.

   An unpublished new-test run may resume through the same public command when
   a shared validator or the final-review response schema stopped it. The resume
   mode authenticates the current config, checkpoint, accepted plan, selected
   test IDs, saved authoring manifest, candidate, saved executions, review
   request, and any saved raw response. It writes only to a fresh output
   directory. A grading-only decision regrades the four saved executions. A
   candidate blocked only by a shared local-validator defect is rebuilt from its
   saved authoring response and then certified normally; that shared code fix is
   not a model-written correction. A raw final-review response rejected by an
   old schema is preserved as evidence and the final review is rerun under the
   corrected schema. The normal one-correction rule then applies. It never
   reruns an authoring call unless the corrected final review assigns a
   model-written test-design or request correction. It never writes into the
   source run. Current progress records can also resume a pending early review
   or a missing final review. An authenticated accepting review can be reused
   when its request matches the current candidate and successful certification.
   If authoring never completed, resume may make the unfinished authoring call;
   it does not repeat an authenticated completed response.
   For authenticated historical work, when a human approves exact grading-check replacements, pass a
   hash-bound `--approved-corrections` file. The command changes only those
   assertions, regrades the four saved executions locally, and runs the final
   review only if the result is two with-history passes and two no-history
   failures. This also applies when failed certification stopped the test before
   any final review. It does not rerun authoring or Hermes. Use this command for
   saved test 3:

   ```bash
   python -m authoring.create_tests \
     --config authoring/configs/morgan.yaml \
     --plan tmp/authoring/morgan_replacements_3_16_58_two_stage_plan_20260901_v3/candidate_plan.json \
     --resume-new-test-run tmp/authoring/morgan_replacement_003_atlas_create_20260901_v3 \
     --test-ids 3 \
     --out tmp/authoring/morgan_replacement_003_atlas_resume_patch_20260902_v1 \
     --concurrency 1 \
     --confirm-paid-calls
   ```

   The same command may review an existing published set with
   `--review-published-tests`. By default it reviews every published test. It
   always enumerates all expected published files and records a hash for every
   one, so a missing, duplicate, or changed file is detected. For a bounded
   review, add `--test-ids 10,32,61`; the command still validates and hashes the
   full set, but resolves saved authoring evidence and calls the reviewer only
   for those IDs. The selected IDs are recorded in both `review_inputs.json`
   and `manifest.json`.

   The same command may repair a completed published-test review with
   `--repair-published-tests` and `--review-dir`. It first verifies every
   current published file against the hashes recorded by that review. A review
   selected IDs, and the repair copies or changes only those IDs. A review made
   without `--test-ids` continues to process the complete published set. In
   either case, accepted files are copied unchanged and rejected tests change
   only the step named by the review. A test gets at most one model-written
   correction. A shared code fix does not consume that correction and reruns
   the unchanged test. It writes accepted batches that `publish_tests`
   validates normally. In repair mode,
   rerunning the same `--out` directory with `--test-ids 22,176` continues
   only those existing unresolved repair results. It reads each test's latest
   saved reviewer decision and changes only the step named there. It does not
   restart those tests with a generic redesign. It saves the old unresolved
   result before writing the continuation result and refuses to repeat the
   same saved failure. Accepted results and accepted batches remain unchanged.
   Some older repair results contain a final-review decision with one problem
   and no `issues` list. Before correcting one of those results, continuation
   reviews its latest authenticated candidate and four saved executions once
   with the current reviewer. If that review accepts the candidate,
   continuation preserves the old result and accepts the unchanged candidate.
   If it rejects the candidate, continuation uses the complete new issue list
   to choose its one correction. Results that already contain an `issues` list
   do not receive this extra review.
   If a failed certification has no new reviewer decision and a human has
   approved an exact correction, pass a JSON file with `--approved-corrections`.
   That file must name the same tests as `--test-ids`, bind each correction to
   the saved result, candidate, and gate by SHA-256 hash, and provide one exact
   assertion replacement, one exact request replacement, or one concrete
   instruction for redesigning the same test from the same facts. The command
   rejects changed inputs and still certifies and reviews the corrected test.
   Before creating repair output or making model calls, the public repair command verifies that the Python used
   by `authoring.certify` exists, runs, and can import `mcp`. Set
   `DOLPHINBENCH_GATE_PYTHON` to a Python executable with `mcp` when the
   current interpreter cannot import it.
   For exact human-approved edits to existing published tests, the same command
   accepts `--approved-revisions <file>`. This JSON file names one approved
   planning batch and one through ten edited YAML files. Each YAML is bound to
   its recorded SHA-256 hash and the hash of the published test it replaces.
   Tasks in the planning batch correspond to ascending test IDs. Update the
   planned requirements and source evidence when the approved edit changes
   them; do not give the reviewer the old requirement that caused the defect.
   The command validates the checkpoint, selected facts, sources, dates, tools,
   and grading fields before paid work. It copies the approved YAML unchanged,
   runs the existing two with-history and two no-history attempts, and sends
   those results to the existing final trace reviewer. Only tests that pass
   certification and review enter its normal accepted batch. It saves failures
   for inspection without rewriting the approved tests or planning new ideas.
   This mode does not publish tests or start evaluation.

   When only grading changes, a candidate entry may name a completed prior run
   through `saved_run` and `saved_run_sha256`. The command rejects reuse when
   the request, source facts, app state, history, or evaluation date differs.
   Otherwise it applies the revised checks to the four saved outputs and runs
   final review. An explicit `review_only: true` also reuses the saved grades
   for an unchanged test when only the reviewer changed. Do not use that option
   after changing the grading code or grading requirements.

   Before validating an explicitly approved exact release proposal, use
   `--prepare-release-revision <proposal.json>` with
   `--approved-proposal-sha256 <approved-hash>` and a fresh `--out` directory.
   This local-only preparation verifies all 200 published hashes against the
   current release, replays the proposal's exact ordered edits, validates the
   changed test schemas, and preserves the original release and test bytes in
   the output. It stages 200 candidate files without writing an accepted batch
   or changing persona state. It refuses paid-call confirmation and other
   creation modes. The output remains `validation_pending`; preparation is
   not certification, publication permission, or an evaluation launch input.
   Matching historical candidate and gate hashes alone does not establish
   unchanged execution inputs. Missing reuse evidence remains pending and does
   not authorize additional assistant executions. This preparation mode does
   not itself execute the subsequent legacy release-validation workflow.

   To prepare that validation locally, pass `--approved-revisions <file>` and
   `--prepare-only`. A version-2 `kind: exact_release_revision` file binds the
   prepared `revision.json` and saved-run trace by hash. Its
   `legacy_reuse_approval` must explicitly approve the exact grading-only IDs
   and the recorded limitation of older execution evidence. Code rechecks the
   original and revised 200-file sets, exact edits, linked source messages,
   saved outputs, tool arguments, and checkpoint records. It preserves approved
   tool-description differences and missing full-transcript evidence as named
   limitations, not reconstructed settings. It writes review inputs and work
   counts to a fresh directory without making calls, accepting tests, changing
   persona state, or preparing evaluation. After explicit paid-run approval,
   omit `--prepare-only` and add `--confirm-paid-calls` to execute these exact
   revisions. Regrade saved certification answers for grading-only changes;
   run four new attempts only when the approved request changed. Preserve
   completed steps on resume and stop uncertain steps without repeating them.
   Use existing usage and timing records; an upfront cost calculation or cost
   limit is not a prerequisite. This command does not publish the release or
   launch evaluation.

   An explicitly approved follow-up to that exact release validation uses a
   version-3 `kind: exact_release_revision_followup` approval through the same
   `--approved-revisions` command. Bind the proposal, parent approval, completed
   decision, exact pending-test amendments, and one output directory. Preserve
   accepted outcomes by candidate and review evidence; reuse saved evaluation
   grades only with identical candidate, answer, grader, and judge inputs.
   `--prepare-only` stages the amended set locally. Paid execution regrades
   grading-only changes and certifies changed requests anew. An unchanged
   execution retry requires a named one-set allowance and matching completed
   preflight and example evidence. Each fresh set is durably recorded before
   execution and cannot be repeated after completion or uncertain interruption.
   This follow-up does not authorize publication or final-evaluation launches.

3. `python -m authoring.publish_tests` makes no model calls. After explicit
   approval, it validates the accepted batches locally and publishes exactly
   200 accepted tests. It then points the persona state at the new release and
   clears the completed working records.

   For ordinary creation batches with noncontiguous candidate IDs, supply
   `--release-map` to assign release positions without changing the accepted
   sources. The version-1 JSON object names the persona, checkpoint identity,
   evaluation date, and a `tests` array. Each entry contains `release_id`,
   `candidate_id`, and the original `candidate_sha256`. Select exactly 200
   distinct accepted candidates and use every release position from 1 to 200
   once. Preview makes no changes. Publication requires both `--confirm-publish`
   and `--approved-release-map-sha256` matching the explicitly approved mapping.
   Derived batch copies change only the candidate ID; they retain the original
   plans and bind the accepted sources and mapping by hash. The release keeps
   both the source and derived batch provenance. Later planning reads the
   derived release IDs, including when excluding tests for replacement.

`authoring.run`, `authoring.final_trace_review`, `authoring.certify`, and
`authoring._gate_shot` are internal components used by the three public commands.
They are not alternative operator workflows. Historical temporary artifacts and
manual target lists are records only and are never inputs to a new authoring
run.

There is no separate model call that declares facts testable. Code excludes
only missing, explicitly rejected, or superseded facts. The planner chooses a
fact and realistic new work together. Older plans are records only and cannot
start new test creation. Their authenticated saved work may use the documented
resume and repair operations.
