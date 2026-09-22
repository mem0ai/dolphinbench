# DolphinBench reference evaluation

The paper evaluates these harness/model combinations with all five memory
systems:

| Harness and model | Memory systems |
| --- | --- |
| Hermes Luna | Built-In, Mem0, Honcho, Hindsight, Supermemory |
| Hermes MiniMax | Built-In, Mem0, Honcho, Hindsight, Supermemory |
| Claude Code Sonnet | Built-In, Mem0, Honcho, Hindsight, Supermemory |

Each configuration ingests the history and evaluates independently. The
release retains the implementation used for each reported result. Cleaning
and checking that code does not require new evaluation runs.

## Ingestion and evaluation

For each configuration:

1. Record the history and test hashes, harness source or executable, model
   settings and endpoint, official plugin build, and memory-store identity.
2. Send the original history messages in order through that harness and its
   normal memory hooks. Prefix each message with its original ISO-8601 date
   and time. Preserve both the original user text and the generated response.
3. Record completed turns and provider writes. Wait for the provider's
   processing checks before treating its ingestion as complete.
4. Preserve the completed runtime files and, for self-hosted services, the
   matching database snapshot and receipt.
5. Run the 200 published tests with fresh conversations and app state.
   Preserve the configuration's completed memory for retrieval, and prevent
   tests from changing the memory used by subsequent tests.
6. Save the execution traces, app calls, grading evidence, failures, and
   approved recovery records.

A completed assistant turn alone does not prove a background memory write
finished. An interrupted turn must retain its evidence until its delivery
state can be established. The provider-specific completion and recovery code
in `reference/` owns those checks.

The history is processed over hours or days using the real processing clock.
Historical dates appear in message text; the benchmark does not simulate years
of elapsed provider time.

## How the simulated apps connect

The benchmark supplies `mock_mcp/server.py`, a local Model Context Protocol
(MCP) server that communicates over standard input and output.

For Hermes, the profile names the server's Python executable, script, and
environment under `mcp_servers.dolphinbench-apps`. For Claude Code, the driver
passes the same server definition through an MCP configuration file using
`--strict-mcp-config --mcp-config`.

For the isolated Hermes reference path, the controller starts the app server
as a subprocess and forwards its MCP connection to the agent container.
The agent does not receive the app database, tests, or grader files. Claude's
retained driver uses its recorded MCP subprocess configuration.
The runner supplies the persona's tool definitions, the test's initial app
state, and paths for the mutable app state and tool-call log. The grader reads
that log and the saved model execution after the test. The next test receives
fresh app state.

## Retained execution commands

These commands use the consolidated implementations. Preparing inputs is
local; execution still requires explicit approval for model and provider
costs. Use the exact source, model settings, service configuration, and
completed-ingestion records associated with the selected run.

### Hermes

First build the selected clean agent image and set `DOLPHINBENCH_AGENT_IMAGE`
as described in [isolated Hermes execution](../reference/README.md#isolated-hermes-execution).
Use a Docker image ID for local execution or a Modal image ID for Modal
execution. Prepare new runs with that image; keep older runs on their
recorded code and settings.

Prepare an ingestion manifest with the selected persona, model, memory system,
and authenticated history checkpoint:

```bash
python -m reference prepare-ingestion --help
```

Run or resume that prepared ingestion:

```bash
python -m reference ingest \
  --manifest /path/to/launch_manifest.json \
  --confirm-paid-calls
```

Add `--resume` to continue its recorded progress. This command performs the
history ingestion through the existing runner; it does not merely package
pre-existing memory.

The Hindsight and Supermemory workers also own their local databases and
matching snapshots. Their existing managed-ingestion command is:

```bash
modal run --detach reference/execution/hermes.py::ingest \
  --provider hindsight --confirm-paid-calls
```

Select the persona through `DOLPHINBENCH_MEMORY_INGESTION_PERSONA` and the Hermes
model through `DOLPHINBENCH_MEMORY_INGESTION_AGENT` (`luna` or `minimax-m3`).
The worker keeps their stores separate.

### Claude Code

The official-plugin ingestion worker accepts a selected provider:

```bash
modal run --detach reference/execution/claude.py::main \
  --providers mem0 --output /path/to/claude-ingestion.json \
  --confirm-paid-calls
```

The native-memory worker retains its separate runtime lifecycle:

```bash
modal run --detach reference/execution/claude_native.py::ingest \
  --confirm-paid-calls
```

These workers process the source messages through Claude Code, preserve
runtime-specific memory, and retain the provider's completion and recovery
checks. The provider, persona, account, plugin, and store settings must match
the prepared ingestion.

### Evaluation and results

The public benchmark does not require Modal. Use the
[harness integration guide](DRIVER_CONTRACT.md) to evaluate your own harness.
The reference directory retains Modal scripts because some recorded runs used
them; those scripts are not a requirement for participant implementations.

The retained suite builder prepares Hermes and Claude Built-In evaluations.
It is not a universal launcher for every paper configuration. Claude's
official-plugin setup, ingestion, recovery, and driver code remain separate
components; this suite builder does not execute Claude external-memory runs.

For suite preparation, put `completed_ingestions` inside each `runtimes`
entry, keyed by the memory systems selected in `memory_providers`. Hermes
entries supply `manifest` and `state`, or the existing Hindsight/Supermemory
`snapshot` inputs. Claude Built-In supplies `receipt`, which references its
saved native-memory archive. Resolve relative filenames from the plan file's
directory.

Set separate ingestion inputs for Hermes Luna and Hermes MiniMax. The planner
checks that the ingestion model matches the evaluation model and rejects reuse
of one ingestion record across configurations. The older top-level
`completed_seeds` format remains available for single-Hermes-model plans and
Claude Built-In.

```bash
python -m reference plan --config /path/to/evaluation.yaml
python -m reference evaluate \
  --manifest /path/to/test_manifest.json --confirm-paid-calls
python -m reference modal --help
```

Remote execution retains the original scheduler, immutable bundles, resource
limits, saved call IDs, result hashes, and resume behavior. Existing live runs
continue with their original bundles. A changed implementation requires a new
prepared run rather than edited hashes in an old manifest.

The evaluation runner records each phase and writes `combined_results.json`
for each configuration. Missing costs or usage remain missing; they must not
be replaced with invented zero values. Preserve the complete execution and
grading evidence alongside the score.

## Source and service behavior

The cleanup retains provider-specific behavior used by the existing runs,
including official-plugin ingestion, Mem0 write verification, Honcho
verification, Hindsight processing and database recovery, and Supermemory
processing barriers and stopped-database snapshots.

Service compatibility changes and recovery exceptions belong in the result's
provenance. For example, the self-hosted Supermemory Hermes compatibility patch
stores the original user-and-assistant conversation through the supported
document API. That configuration must be identified as patched self-hosted
Supermemory, not the unmodified hosted service.

The runtime images retain their own dependency pins. In particular, the
current evaluation worker installs Hindsight client 0.6.1, while the reference
requirements specify 0.9.2 for the separate service tooling. Record the actual
image and dependencies used for each result.

## Accepted Morgan results

The accepted Morgan reporting inputs are:

```text
artifacts/morgan-release-final-20260908/results/builtin.json
artifacts/morgan-release-final-20260908/results/mem0.json
artifacts/morgan-release-final-20260908/results/honcho.json
```

Use that directory's `summary.json` and `paired_scores.json`. Built-In scores
82/200 (41%), Mem0 scores 138/200 (69%), and Honcho scores 126/200 (63%),
including all 18 approved September 9 reruns. Keep these accepted results
unchanged while the remaining paper evaluations finish.

## Before publication

For every score reported in the paper, include its source bundle, settings,
completed-ingestion records, traces, and grading evidence. Match each score to
the code that produced it. Local regression tests check the implementation;
they do not replace the recorded evaluation evidence.
