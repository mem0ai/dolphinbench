# Hermes built-in participant example

This optional example shows how one team can connect Hermes with built-in
memory to the public runner. Use it only if that is the harness you want to
evaluate. You can implement the same runner interface for another harness.
The release team's 15-condition result workflow is documented separately in
[the release evaluation procedure](../../docs/CANONICAL_EVALUATION.md).

## Prerequisites

Use Linux, Git, uv, and Python 3.11 for Hermes and the app server. Install the
benchmark's `requirements.txt` in a separate environment with Python 3.11 or
later. The agent requires access to Azure `gpt-5.6-luna`; semantic grading
requires Azure `gpt-5.6-sol`. These deployment names are not a promise of public
model availability. Keep endpoint and API-key values in your environment.

The commands below use `BENCHMARK` for the absolute path to this repository.
Use new directories for the Hermes checkout, app environment, and run output.
Do not use an existing ingestion or modify an official evaluation checkout.

## Install the pinned runtime

Set the benchmark path and clone the upstream base:

```bash
export BENCHMARK="/absolute/path/to/dolphinbench"
git clone https://github.com/NousResearch/hermes-agent.git hermes-reference
git -C hermes-reference checkout 935c1e8962d43de3ef0adde30e80dda55d0247db
git -C hermes-reference apply --index "$BENCHMARK/examples/reference/hermes-evaluation.patch"
```

Install the locked Hermes dependencies, including its MCP and Honcho extras.
Honcho is a runtime dependency here; this configuration uses built-in memory.

```bash
cd hermes-reference
uv sync --frozen --no-dev --extra mcp --extra honcho --python 3.11
export DOLPHINBENCH_HERMES_SOURCE="$PWD"
export DOLPHINBENCH_HERMES_PYTHON="$PWD/.venv/bin/python"
```

Create the app server environment separately. Its MCP version differs from
the Hermes client's version:

```bash
uv venv --python 3.11 ../dolphinbench-apps
export DOLPHINBENCH_APP_PYTHON="$(cd ../dolphinbench-apps && pwd)/bin/python"
uv pip install --python "$DOLPHINBENCH_APP_PYTHON" mcp==1.29.0 pyyaml==6.0.3
cd "$BENCHMARK"
```

## Verify locally

Set `AZURE_OPENAI_BASE_URL` to your agent endpoint. For an offline-only check,
you can use `https://example.invalid/openai/v1` in a separate output directory.
The endpoint becomes part of the run identity; changing it requires a new run.
The prepare command uses your benchmark Python, not the Hermes Python:

```bash
python -m harness.runner prepare --config examples/configs/hermes-builtin.yaml
python -m unittest tests.unit.runner.test_hermes_reference tests.unit.runner.test_harness_connection
```

Preparation checks the source diff, lockfile, installed runtime versions,
Python minor version, executable interpreter, and generated profile recipe.
It creates a run identity but does not start Hermes, ingest history, or call
a model. The tests verify the pinned source setup and the local MCP app
connection without making a model call.

## Execute after approval

These are full-release commands, not a bounded pilot. Ingestion processes all
three personas; evaluation runs all 600 tests. Obtain approval for that scope
and cost before adding `--confirm-paid-calls`. Configure agent and fixed-judge
credentials before execution: `AZURE_OPENAI_API_KEY` supplies the key,
`AZURE_OPENAI_BASE_URL` supplies the agent API base URL, and
`AZURE_OPENAI_ENDPOINT` supplies the judge's Azure resource endpoint. Keep the
judge defaults (`gpt-5.6-sol`, medium reasoning) unchanged.

```bash
python -m harness.runner ingest --config examples/configs/hermes-builtin.yaml --confirm-paid-calls
python -m harness.runner evaluate --config examples/configs/hermes-builtin.yaml --confirm-paid-calls
python -m harness.runner package --config examples/configs/hermes-builtin.yaml
```

The output is `tmp/hermes-builtin-reference/submission.zip`. Completed agent
calls and grading records resume without rerunning them. Interrupted attempts
with uncertain delivery stop for inspection. Packaging is local-only and
requires complete evidence. See the [harness integration guide](../../docs/DRIVER_CONTRACT.md)
for custom agents, evidence fields, and recovery behavior.

## Verified scope and remaining limits

- `hermes-builtin.json` records the upstream base, evaluated commit, full patch
  hash, lockfile hash, and profile settings. Source verification rejects a
  changed tracked source tree or unexpected untracked source files.
- The runtime uses the supplied Hermes lock. App MCP and PyYAML are pinned;
  installed transitive app versions are recorded, but the historical worker's
  complete transitive environment is unavailable. Exact environment identity
  with that worker is not established.
- The 31 schema fixtures come from accepted Morgan traces and retain source
  paths and hashes. They do not cover every Morgan tool or establish historical
  schema equivalence for Alex and Riley.
- Assistant responses retain observed usage and settings, including changed
  tool definitions. Historical traces with only aggregate usage cannot be
  converted into complete per-response evidence. Rewritten model context and
  terminal failures without complete evidence still stop export.
- Host paths, current dates, remembered content, and stochastic outputs can
  differ across runs. Matching code and settings does not guarantee identical
  prompts or scores. Auxiliary internal model-call accounting still needs a
  live-agent audit before claiming complete cost coverage.
- Ingestion cost comes from the three fresh Hermes profiles after processing
  completes. Evaluation cost uses the change in profile costs for each attempt,
  including retries, excluding costs copied from the frozen ingestion profile.
  The adapter uses the per-model usage table when available,
  including auxiliary calls, and otherwise the session cost table. Missing or
  incomplete costs prevent packaging; they are not replaced with zero. The
  adapter retains temporary profiles when their costs could not be recorded.
- External memory providers and Claude Code are not supported by this optional
  example. The website submission flow is a consumer of the public runner's
  output, not a separate execution or grading implementation.
