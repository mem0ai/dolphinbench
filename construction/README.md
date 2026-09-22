# DolphinBench History Construction

This directory contains the supported system for extending a DolphinBench persona's
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

## Supported Execution

Read [`CANONICAL_PIPELINE.md`](CANONICAL_PIPELINE.md) before creating a
quarter. The required order is: create a candidate plan with
`construction.plan_quarter_events`, obtain human acceptance, create the
accepted config, and then run `construction.quarter_runner` from a `*_final`
checkpoint. Old schedules are historical provenance outside this checkout.
