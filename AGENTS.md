# DolphinBench Repository Instructions

## Official Results

Use the selected release record for the exact test version and run. The
September 16 selection is recorded in the evaluation checkout at
`artifacts/website-results-evidence-20260916/README.md`. Its corrected-test
Hermes-Luna runs supersede the September 8 Morgan results; retain those older
files only as historical evidence. Do not mix the two result sets.

Verify downloaded evidence against the saved worker checksums. Report approved
pass counts and calculate cost and latency across all 600 individual tests.
Do not average persona medians. Before publication, make sure the public
dataset and grading checks match the evaluated release and review evidence
for credentials. Preparing a local preview does not authorize publication.

## Communication And Autonomy

Create documentation only when the user requests it or when execution,
recovery, or verification needs it. Use existing records instead of creating
duplicate status reports.

When the user asks to discuss or understand something, stay with that question.
Do not move into implementation or operations without agreement. When the user
asks to stop and discuss the plan, respond immediately. Do not inspect files,
edit files, launch work, poll processes, or terminate processes before that
response unless the user explicitly asks for one of those actions.

Report test-generation progress at the system level: what is working, which
stage is being fixed, and what remains uncertain. Handle minor implementation
fixes and local verification autonomously within the agreed work. Do not ask
the user to approve routine recovery or validation details. Still obtain
explicit approval for paid runs, substantive idea changes, publication, and
changes to accepted work.

For any history-construction task, read
`construction/CANONICAL_PIPELINE.md` before inspecting or changing code.

The only supported later-quarter workflow is:

1. `construction.plan_quarter_events` creates a candidate quarter plan for
   explicit human acceptance.
2. `construction.quarter_runner` executes that accepted plan from the previous
   authenticated `*_final` checkpoint.

`construction.build_history_window` is an internal implementation detail of
the quarter runner. Do not run or present it as a separate construction path.
Do not reintroduce the retired quarter schedule, quarter activity, or
standalone week-writer systems.

Files under provenance, rewrite, candidate-run, failed-run, and `*_current`
directories are records, not active starting points. A new quarter may start
only from the previous quarter's `*_final` checkpoint and an explicitly
accepted quarter plan.

Do not make paid model calls, promote a candidate plan, or replace an accepted
checkpoint without explicit user approval.

## Test Authoring

For any test-selection, test-planning, test-writing, grading, app-state, or
oracle-certification task, read
`authoring/CANONICAL_TEST_AUTHORING.md` before inspecting or changing code.
That contract is authoritative. If code, prompts, documentation, or saved runs
conflict with it, report the conflict before continuing.

The only public test-authoring workflow is:

1. `python -m authoring.propose` creates up to 250 test ideas through as many
   as ten parallel, non-overlapping fact shards and stops for human acceptance.
2. `python -m authoring.create_tests` designs the approved ideas, writes the
   blind request, builds the state and grading checks, runs two with-history
   and two no-history certification attempts, accepts complete clean evidence
   locally, sends exceptions to final model review, allows at most one correction
   at the step that owns a failure, and writes an accepted batch that
   `authoring.propose` can use directly on the next call.
3. `python -m authoring.publish_tests` performs local-only validation and
   publishes exactly 200 accepted tests after explicit approval.

`authoring.run`, `authoring.final_trace_review`, `authoring.certify`, and
`authoring._gate_shot` are internal components of that workflow, not alternative
operator paths. Historical temporary artifacts and manual target lists are
records only; they are never inputs to a new authoring run.

Do not interpret "memory changes the action" as requiring a different tool or
a branching decision. Memory may supply required tool arguments or required
content in an otherwise identical tool call. Do not globally reject a fact
because one proposed request used it badly.

Before changing the authoring system, identify the exact failing example and
which step owns the failure. Check every proposed rule against the required
counterexamples in the canonical contract. State the literal change and wait
for explicit approval before making paid calls or broad reruns. Preserve
accepted work and rerun only affected work.

## Evaluation

For any ingestion, memory-system, runtime, grading, scheduling, or benchmark
execution task, read `docs/CANONICAL_EVALUATION.md` before inspecting or
changing code.

The paper includes Hermes Luna, Hermes MiniMax, and Claude Code Sonnet, each
with Built-In, Mem0, Honcho, Hindsight, and Supermemory. Each configuration
ingests and evaluates independently. Temporary gaps in execution code do not
change that release scope.

Keep cleanup changes in a separate worktree so ongoing evaluations can
continue unchanged. Compare the retained code against the existing ingestion
and evaluation implementations. Cleanup does not require waiting for new
evaluation runs. Do not substitute a new ingestion procedure, share stores
between configurations, or exclude a launch configuration during cleanup.

`reference/` retains the ingestion, evaluation, recovery, provider, and result
recording code. Its README identifies the commands and code owners.
Preserve the exact source bundles, settings, receipts, traces, and approved
recovery records used for reported results.

Do not make provider calls, model calls, service deployments, database
migrations, or changes to active ingestions without explicit approval.
