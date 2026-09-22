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

## Morgan Status

Morgan's canonical release corpus is at
`construction/v2/morgan/release/500k_final/checkpoint/`. Its checkpoint identity
is `39dedbc65b3aea84919ab6dbac4069bde8c26d23519491f187f2345721f51567`.
It contains 2,765 sessions, 3,400 user messages, 751 facts, and 500,100
`o200k_base` user-message tokens. Fact selection and test authoring must use
this release checkpoint.

Construction was accepted through 2026-09-30 at
`construction/v2/morgan/quarters/2026_q3_final/checkpoints/2026-07-01_to_2026-09-30/`.
That source checkpoint's identity is
`19a27301f63a6dddcc81b2e78008e6b66e7891ea59aaa4a906ba53ce5e65757e`.
It contains 2,828 sessions, 755 facts, and 510,725 `o200k_base` user-message
tokens. It is retained as construction provenance and is not a test-authoring
input.

The accepted Q3 plan, chain, review, and
finalization receipt are stored under
`construction/v2/morgan/plans/quarters/2026_q3.json`,
`construction/v2/morgan/provenance/2026_q3/accepted_chain.json`,
`construction/v2/morgan/provenance/2026_q3/quarter_review.json`, and
`construction/v2/morgan/quarters/2026_q3_final/finalization.json`.
The deterministic release-cutoff receipt is stored at
`construction/v2/morgan/release/500k_final/finalization.json`.

The old Q1 schedule and rewrite tree remain available for historical
provenance. They are not active inputs. Only a `*_final` checkpoint may start a
new quarter.
