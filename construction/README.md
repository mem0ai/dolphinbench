# DolphinBench History Construction

This directory contains the supported system for extending an DolphinBench persona's
user-message history. The single canonical workflow is documented in
[`CANONICAL_PIPELINE.md`](CANONICAL_PIPELINE.md).

## Release Checkpoints

Test authoring uses these authenticated checkpoints:

- [Morgan](v2/morgan/release/500k_final/checkpoint/)
- [Alex](v2/alex/release/500k_final/)
- [Riley](v2/riley/release/500k_final/)

Each checkpoint preserves its original session grouping. The released
single-message histories and their mapping are at the repository root under
`registry/personas/` and `session_map.json`. Validate both representations
against `manifest.json` and the frozen authoring release manifests.

Longer quarter histories and old construction runs are not included in this
checkout. Release checkpoints cannot start a new quarter: extending history
requires the previous accepted quarter's authenticated `*_final` checkpoint
and explicit acceptance of a new quarter plan.

## Alex And Riley Initial Preparation

`construction.prepare_initial_history` is the one-time local boundary for
repairing Alex's or Riley's existing short history before their first accepted
quarter. The preparation order is:

1. Keep the original messages and registry as the immutable source.
2. Write one request. Its response includes every session/message pair in
   `messages_checked`, so deterministic code can reject skipped messages.
   `problems` contains only messages needing repair; later-
   outcome narration is a repair problem, but real plans and deadlines are not.
3. Have a human accept the problem list.
4. Write one request that rewrites only the accepted messages.
5. Use deterministic code to apply only those replacements to the repaired
   `life_sim.yaml`.
6. Have a human review the changes.
7. Use the repaired messages for the entity inventory, protected-fact subjects,
   and chronological missing-fact list.
8. After missing facts are accepted, write one saved request that classifies
   every repaired message's app effects. The request includes every tool in
   `mock_mcp/manifests/<persona>.yaml`, each tool's exact server argument
   signature, and its contract metadata when available. It only identifies
   incoming records, reported completed changes, available requested actions,
   and unsupported requests. It does not create records, fill tool arguments,
   or replay actions, and stops for human acceptance.
9. Do dates, app state, record construction, action replay, and
   candidate-checkpoint work later.
10. Treat the run 002 entity response as diagnostic provenance only, not active
   input.
11. Make no paid call and write no next request without explicit approval.

`initial_history_repair_003` predates the `messages_checked` coverage field and
was completed by an explicit local audit that amended the accepted list.

`construction.execute_initial_history_requests` executes only an explicitly
named saved request after that request is approved, with a resumable cache. It
validates the response against the schema saved in the request and requires
`--confirm-paid-calls`. The local preparation command itself never calls a
model, and it does not assemble a complete candidate checkpoint.

The rejected two-call Alex diagnostic under
`v2/alex/work/initial_history_repair_001/` is provenance only and is not an
input to this flow.

This command is not a way to generate a quarter. After an initial checkpoint
is reviewed and explicitly accepted, all later construction uses only
`construction.plan_quarter_events` and `construction.quarter_runner` as
documented below.

## Supported Execution

Read [`CANONICAL_PIPELINE.md`](CANONICAL_PIPELINE.md) before creating a
quarter. The required order is: create a candidate plan with
`construction.plan_quarter_events`, obtain human acceptance, create the
accepted config, and then run `construction.quarter_runner` from a `*_final`
checkpoint. Old schedules are historical provenance outside this checkout.
