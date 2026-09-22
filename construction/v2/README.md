# Canonical DolphinBench History Construction

This directory contains the data and configuration for the supported quarter
execution path. Read [`../CANONICAL_PIPELINE.md`](../CANONICAL_PIPELINE.md)
first. The workflow has two separate stages: `construction.plan_quarter_events`
creates a candidate quarter plan for human acceptance, and
`construction.quarter_runner` consumes the accepted plan from an authenticated
`*_final` checkpoint. The runner does not generate a separate quarter schedule
or schedule contacts.

## Configuration

A configuration contains exactly these keys:

```text
version
persona
quarter_id
quarter_start
quarter_end
spec
persona_sheet
generator_context
overview
starting_checkpoint
quarter_plan
```

`starting_checkpoint` is the accepted state immediately before execution.
`quarter_plan` is the accepted plan the runner executes. Only the configured
accepted plan and checkpoint are inputs for quarter execution.

## Weekly Execution

The runner starts on the day after the accepted checkpoint and chains
consecutive seven-day windows through `quarter_end`. It is the only supported
quarter execution command. `build_history_window` is an internal helper, not a
second workflow.

Each week has exactly three model calls:

1. GPT-5.6 Sol decides what happens and every natural assistant contact. It
   receives no fact IDs, app records, or tool details.
2. GPT-5.6 Sol adds exact fact links, continuing-state changes, and app
   operations without changing the fixed contacts.
3. GPT-5.6 Sol at medium reasoning writes the messages for those
   contacts. Before this call, code retrieves every still-current fact and
   exact recent accepted message about the non-primary subjects in each
   contact. It also supplies the fixed app operations.

Deterministic code validates the outputs, replays the exact app operations, and
writes the next authenticated checkpoint. The structured requests are saved as
JSON and sent to the models as labeled plain text. There is no per-week LLM
review, repair, contact scheduling, quota, or automatic retry. The runner stops
on the first failure. After all weeks pass, one GPT-5.6 Sol call reviews the
complete generated quarter. The runner writes a final checkpoint only when
that review also passes.

Use `--stop-after-story` to inspect the fixed story for only the next unfinished
week. The command pauses before facts, app operations, messages, and checkpoint
creation. Rerunning the same command and output directory without that flag
reuses the saved story and continues normally.

## Released histories

The three released checkpoints are listed in the
[history construction guide](../README.md#release-checkpoints).
They contain the histories and supporting records used for test authoring.
Earlier quarter plans and intermediate construction runs are not included in
this checkout.
