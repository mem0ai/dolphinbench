# Canonical DolphinBench Test Authoring

Read `authoring/CANONICAL_TEST_AUTHORING.md` first. It defines test validity.
The only public workflow has three commands:

1. `python -m authoring.propose` makes up to 250 ideas across one through ten
   non-overlapping fact shards and stops for human acceptance.
2. `python -m authoring.create_tests` turns accepted proposals into executable
   tests, certifies them, and writes an accepted batch.
3. `python -m authoring.publish_tests` performs local-only validation and
   publishes exactly 200 accepted tests after explicit approval.

The commands are separated by approval boundaries. Proposal creation does not
create tests. Test creation does not publish tests. Publishing does not make
model calls.

## How test generation works

The approved `workflow: production` runtime policy simplifies unfinished
version-4 creation to writing, local structural checks, four real assistant
attempts, and one independent review of passing candidates. It omits synthetic
preflight examples and the duplicate writer audit. Source explanations may
paraphrase; references must identify supplied original messages, and the reviewer
checks their meaning. One targeted correction may update affected checks and
their supporting evidence while protecting unrelated work. Failed candidates
are parked with their results. Existing accepted tests are unchanged. The
canonical contract defines this opt-in mode; the default workflow is below.

The planner proposes useful work for your approval. The writer builds one complete
test from that idea. The reviewer checks the test before execution and examines
exceptions afterward.

`authoring.propose` makes one through ten concurrent model calls. Each sees an
assigned shard of current facts in compact form, related context, tool
capabilities, and summaries of accepted, approved, and rejected work.
It can reuse facts for genuinely different tasks. It returns the situation,
requested work, selected facts, intended tools, why history is needed, and the
likely result without history. It does not write the final request, source
quotations, or an answer checklist. Code assigns IDs and checkpoint metadata.
The command writes `candidate_plan.json` and stops for human acceptance.

`authoring.create_tests` requires those approved version-4 ideas. One writer call
per idea reads the original dated messages and writes the request, starting app
records, evidence, and grading checks together. Every selected fact must support
a necessary checked result. The code validates the output and copies the checks
unchanged into the test; it does not invent additional requirements.

The same reviewer prompt applies before certification and to exceptions afterward. Before execution, it
checks source support, dates, missing inputs, answer leakage, and grading. An
approval supplies one complete correct example and one incorrect variant that
must fail a named remembered-result check. The actual grader checks both examples.
A disagreement stays pending instead of triggering a generic rewrite.

A rejection claiming that an existing check grades incorrectly must also supply
an executable example. Code tests the named check, saves the result, and keeps
unreproduced claims pending. Missing-check proposals must identify a remembered
result, an unproven final action, or the target receiving a remembered result.
The reviewer receives the actual semantic-judge prompt and full-call input contract.

To verify a reviewer fix without starting certification, resume a written version-4
draft with `--stop-after-preflight`. This option requires `--resume-new-test-run`,
forbids new correction permissions, and stops after review and grading examples.
A successful result remains `certification_pending`; it is not an accepted test.

Certification makes two assistant attempts with the selected facts' original
messages and two without them. The assistant receives an ordinary work request,
not an answer key or grading instructions. New attempts save the exact inputs,
tool calls and results, reply, errors, and per-check grades. Both history attempts
must pass and both no-history attempts must fail necessary remembered-result
checks without technical errors. Code accepts complete, authenticated clean
evidence locally. The reviewer inspects every exception.

## Records, checks, and corrections

The writer receives complete records for declared reads, including dependencies.
A collection needed only as a write destination does not require its existing
records. Code constructs the starting state from selected complete records and
new records; an empty selection means empty state. It never removes a required
record to conceal an answer.

The default writer-input budget is 100,000 serialized `o200k_base` tokens.
Set `writer_max_input_tokens` in the persona config to change it. Oversized
inputs stay pending before the writer call; they are not silently truncated.
This budget does not establish the provider's context limit.

New tests use grading `check_version: 2`. Checks for one action must pass on the
same call, and separate requested actions need separate calls. Each comparison
is explicit. Mechanical failures do not fall back to model judgment. Remembered
content is valid even when the tool name stays the same.

By default, a test gets at most one model-written correction, preserved across resumes.
Fix only the responsible part. A grading-only change regrades saved executions;
a request or state change requires new executions. An execution-only failure
permits one unchanged rerun. An invalid approved idea needs new human approval.
Code protects checks not identified by the review. Parse errors, missing evidence,
and system errors stay pending, not rejected. An explicitly approved staged
runtime policy permits up to three field-scoped corrections, with durable counts
and a stop when a candidate repeats. It does not increase the execution-rerun
allowance or change accepted work.

Existing accepted tests, approved plans, saved responses, and recorded grading
behavior remain unchanged. Older formats are retained for their existing work,
not converted into new version-4 ideas.

## Inputs and outputs

The active prompts are `planner_v4.md`, `writer_v4.md`, and `reviewer_v4.md`
under [prompt_text](prompt_text). Older prompt files support authenticated saved
work. The ordinary assistant prompt lives in `harness/oracle.py`.

The planner reads the verified persona-state file named in the config. This file
links the published release and working accepted batches, approved plans, and
rejected-test files by hash. Preparation saves one prompt, request, response schema,
and manifest without making a model call. A confirmed planning call also saves
the raw response and candidate plan.

Writer and review calls save their exact requests, instructions, schemas, and raw
responses. Flexible record and argument values use JSON-encoded strings in the
provider response; code validates and decodes them into ordinary objects without
changing their meaning. The writer's normalized response is saved separately.

Each test saves progress independently. `pending_tests.json` identifies unfinished
work. `accepted_batch` contains only certified tests accepted locally or by final review,
and the persona state makes that batch available to the next planner automatically.

`authoring.publish_tests` validates accepted provenance, checkpoint identity,
fact and tool references, candidate hashes, and exactly release IDs 1 through 200.
An explicitly approved release mapping can assign those positions to immutable
candidate IDs. Publication does not make model calls or rerun certification.

## Commands

Prepare and inspect the planning request without making a model call:

```bash
python -m authoring.propose \
  --config authoring/configs/<persona>.yaml \
  --replace-test-id <published-id> \
  --count 10 \
  --out <planning-run>
```

After reviewing the saved prompts, schemas, and request sizes, add
`--confirm-paid-calls` to the same command. This makes the configured planning
calls and writes `candidate_plan.json`.

```bash
python -m authoring.create_tests \
  --config authoring/configs/<persona>.yaml \
  --plan <approved-candidate-plan.json> \
  --start-id <first-test-id> \
  --out <test-creation-run> \
  --confirm-paid-calls
```

Resume selected unfinished IDs from an unpublished new-test run after a
grading-only final review, a shared local-validator failure, a pending early
review, or a final-review response-schema failure. The command
authenticates the saved inputs and writes only to the new output directory. It
reuses saved executions when the candidate is unchanged. A shared validator fix
may rebuild the exact saved authoring response into a fresh candidate and rerun
certification; it does not count as the test's one model-written correction:

```bash
python -m authoring.create_tests \
  --config authoring/configs/<persona>.yaml \
  --plan <unchanged-approved-candidate-plan.json> \
  --resume-new-test-run <source-run> \
  --test-ids <unfinished-test-ids> \
  --out <fresh-resume-run> \
  --confirm-paid-calls
```

Use comma-separated IDs, such as `1,4`. Obtain approval before running this
command with paid-call confirmation. Current progress records bind completed
artifacts and the approved inputs by hash. A saved accepting review is reused
only when its request matches the candidate and successful certification.
Tests already included in a completed accepted batch are not resume targets.
Inspect `manifest.json`, `pending_tests.json`, and the saved per-test results
after the run. An unchanged grader-example disagreement stays pending until
resolved; resuming repeatedly does not resolve it.

For explicitly approved staged work, add `--runtime-policy <policy.json>` to
the resume command. The policy selects evidence preparation, separate bounded
stage queues, provider pacing and timeouts, and reference-agent settings.
The run records the policy and runtime source hashes, per-stage events, and
transport timing. Pacing configuration is not evidence of provider quota.
Saved completed writer responses are reused; accepted tests cannot be selected.

To package completed clean acceptances from an interrupted run without model
calls, use the same resume command with `--finalize-accepted-only` instead of
`--confirm-paid-calls`. Select only completed, unregistered acceptances, use a
fresh output directory, and omit `--runtime-policy`. The command authenticates
preflight evidence and all four attempts, then registers the accepted batch.
Pending tests cannot use this mode.

For a version-4 draft stopped by local validation, a human may approve one
correction to its request and checks. Pass `--approved-corrections <file>` with
the resume command. The file uses this structure:

```json
{
  "version": 4,
  "corrections": [{
    "test_id": 3,
    "source_response_sha256": "<hash of the saved authoring response>",
    "allowed_correction_fields": ["test.request", "test.checks"],
    "protected_check_ids": ["<unchanged check ID>"],
    "specific_problem": "<diagnosed failure>",
    "required_correction": "<exact scope approved by the human>"
  }]
}
```

List exactly the selected IDs. Protected check IDs must exist in the saved
draft; use an empty list only when every check needs correction. The command
verifies the saved draft and unused correction allowance before calling the
writer. Source evidence, intended tools, and starting records remain unchanged.
The corrected draft must pass the normal review and certification. Resume an
interrupted correction without resubmitting this permission file.

After the one writer correction, a grading-only final-review rejection may
instead receive an exact human-approved criterion edit. Use the same version-4
file and resume command, with each correction containing `test_id`,
`source_response_sha256`, `source_gate_sha256`, and `criterion_replacements`.
Each replacement contains `check_id`, `previous_criterion`, and `criterion`.
The command changes only matching semantic criterion text, checks grading
examples again, regrades the four saved attempts, and runs final review.
It does not call the writer or assistant, or reset the correction count.

The following two compatibility modes apply only to authenticated historical
plans, not version-4 ideas. They do not provide a way to start new test generation
from historical artifacts.

For human-approved grading-only changes to that saved work, pass a hash-bound
`--approved-corrections` file. That path replaces only the named assertions,
regrades the four saved executions locally, and runs final review only after the
required 2/2 with-history and 0/2 no-history result. It does not rerun authoring
or Hermes, including when failed certification prevented the original final
review from running.

For an explicitly approved replacement batch, use the same public command with
`--approved-revisions`. This mode consumes exact YAML candidates and an approved
`CandidateTaskBatch`; it makes no authoring calls and does not require saved
authoring output. It uses the existing four-shot certification and final
trace review for every candidate. Failed certification or rejected review keeps
the candidate out of the accepted batch.

The JSON manifest has exactly this shape. Paths are absolute or relative to the
manifest file. The plan tasks map to candidate IDs in ascending numeric order.
`published_sha256` binds each replacement to the current published file without
changing that file.

To reuse four saved executions after changing only grading, add `saved_run`
(the previous completed run's `manifest.json` path) and `saved_run_sha256` to
that candidate's entry. The command verifies the same history, date, request,
selected source facts, and app state, then regrades the saved outputs. A changed
request or source selection needs new executions; omit those two fields for it.
For an unchanged test needing only a new review, also set `review_only: true`.
That explicit option preserves its saved grades and reruns only the reviewer.
It must not be used when the grading code or intended grading behavior changed.

```json
{
  "version": 1,
  "approved_plan": "approved_plan.json",
  "candidates": {
    "003": {
      "path": "003.yaml",
      "sha256": "<sha256 of exact approved YAML>",
      "published_sha256": "<sha256 of current tests/morgan/003.yaml>"
    },
    "035": {
      "path": "035.yaml",
      "sha256": "<sha256 of exact approved YAML>",
      "published_sha256": "<sha256 of current tests/morgan/035.yaml>"
    }
  }
}
```

When all 200 accepted tests are ready, validate them without publishing by
omitting `--confirm-publish`. Add that flag only after the final set is approved:

```bash
python -m authoring.publish_tests \
  --config authoring/configs/<persona>.yaml \
  --accepted-batch <accepted-batch> \
  --confirm-publish
```

For noncontiguous candidate IDs, add `--release-map <mapping.json>` to the
publication command. The mapping selects exactly 200 distinct accepted candidate
IDs and hashes and assigns each release position once. Preview it without
`--confirm-publish`. Publication requires approval of that exact mapping and
`--approved-release-map-sha256 <approved-hash>` as well as `--confirm-publish`.
Source candidates remain unchanged; derived copies retain their certification
provenance and original identities. See the canonical contract for the mapping
schema.

## Internal Components


`authoring.run`, `authoring.final_trace_review`, `authoring.certify`, and
`authoring._gate_shot` are internal components used by the three public commands.
They are not alternative authoring paths.

Historical temporary artifacts, manual target lists, and older plans are
records only. They may document earlier work, but supported commands must not
load them as inputs to a new authoring run.
