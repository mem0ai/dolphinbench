# DolphinBench

Mapping the Pareto frontier of agent memory.

DolphinBench evaluates whether an agent uses long-term memory to take the right
actions on a user's behalf. It contains three simulated users, approximately
500,000 tokens of history per user, and 600 tool-using tests across their work
and personal lives.

A test asks the agent to complete a task using information from earlier
conversations. Remembering that information can determine a recipient, a date,
a tool argument, or the content of an email or document. The benchmark grades
what the agent actually does through simulated apps, not just what it says it
remembers.

Use DolphinBench with your own agent harness, model, and memory implementation.
The repository includes the dataset, simulated apps, runner, graders, and the
reference ingestion and evaluation code for the paper's configurations.

[Browse the official runs](results/) for each agent's costs, ingestion records,
test conversations, tool calls, and grades.

## How the benchmark works

1. **Ingest the history.** Process the original dated messages in order through
   your agent and its normal memory-writing behavior. Keep a separate memory
   store for each persona and configuration.
2. **Preserve the completed memory.** Wait for memory processing to finish and
   record the completed checkpoint.
3. **Run the tests.** Start a fresh conversation and fresh simulated app state
   for each test. The agent can retrieve from its completed memory, but tests
   must not change the memory available to subsequent tests.
4. **Grade the actions.** Check the recorded app calls with deterministic and
   semantic graders. Preserve the conversation, tool results, model settings,
   usage, and grading evidence alongside the score.

The agent receives the task and its available app tools. Fact annotations,
expected answers, and grading criteria are benchmark evidence, not agent input.

## Evaluate your harness

Your harness owns the agent loop, model calls, and memory lifecycle. DolphinBench
supplies the history, test requests, simulated apps, grading, and submission
packaging. Start with the [harness integration guide](docs/DRIVER_CONTRACT.md).

From the repository root, install the participant dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m harness.runner init
```

This creates `my_harness.py` and a matching `run.yaml`. Connect your existing
agent by implementing the five TODO methods in `my_harness.py`, following the
integration guide. The starter already opens the local MCP app connection;
your agent selects tools and routes calls through that connection. No separately
hosted app server is required.

The guide covers preparation, history ingestion, evaluation, memory isolation,
and packaging recorded evidence into a submission ZIP. Configure your model,
memory, and grading credentials before paid execution.

The [Hermes built-in example](examples/reference/README.md) demonstrates one
integration. The [offline example](examples/offline_adapter.py) exercises the
runner with synthetic responses and no model calls.

## Dataset

| Persona | User-message tokens (`o200k_base`) | Tests |
| --- | ---: | ---: |
| Alex Valdez | 500,109 | 200 |
| Morgan Chen | 500,100 | 200 |
| Riley Tanaka | 500,056 | 200 |

For each persona, the repository provides:

- `registry/personas/<persona>/life_sim.yaml`: the dated user-message history.
- `registry/personas/<persona>/facts.yaml`: remembered information and the
  original messages that establish it.
- `tests/<persona>/001.yaml` through `200.yaml`: task requests, initial app
  state, fact references, and grading checks.

Morgan and Alex tests share a `tests/<persona>/state.json` file. Each test's
`mock_state_base` names and hashes that file; `mock_state` replaces only the
top-level collections that differ. The runner loads the complete state
automatically. Original evaluated files remain in the downloadable evidence
archives.

`manifest.json` records release-file hashes. `session_map.json` maps original
checkpoint messages to released message IDs while preserving their text and
order. The construction checkpoints retain the evidence used to build and
certify the dataset.

Validate the dataset locally without model calls:

```bash
python -m construction.validate_release
```

The [website](website/README.md) lets you browse all three personas, search
their histories, inspect each test's source messages and grading checks, and
read the Run and submit instructions.

## Paper configurations

The paper compares these harness/model combinations:

| Harness | Model |
| --- | --- |
| Hermes | Luna |
| Hermes | MiniMax |
| Claude Code | Sonnet |

Both Hermes models are paired with **Built-In, Mem0, Honcho, Hindsight, and
Supermemory**. Claude Code is paired with **Built-In, Mem0, and Honcho**.
Each pairing has its own history ingestion and evaluation.

[Reference reproduction](reference/README.md) contains the ingestion,
evaluation, recovery, and result-recording implementations. The
[evaluation guide](docs/CANONICAL_EVALUATION.md) explains how configurations,
completed memory, source versions, traces, and grades identify the execution
behind a reported score. Reference tooling has separate dependencies; running
your own harness does not require Modal or a reference configuration.

## Dataset construction

Tests must require useful work whose correct completion depends on the user's
history. Certification runs two attempts with the complete supporting messages
and two without history. Acceptance requires both with-history attempts to pass
and both no-history attempts to fail, together with checks for source support,
answer leakage, executable app state, and appropriate grading.

The [methodology](docs/methodology.md) describes history construction, fact
grounding, test design, and certification. To extend the dataset, follow the
[history construction guide](construction/README.md) and
[test authoring guide](authoring/README.md). Their workflows separate human
approval, generation, verification, and publication.

## Repository layout

| Directory | Contents |
| --- | --- |
| `registry/personas/` | Histories, fact annotations, and persona profiles |
| `tests/<persona>/` | 600 benchmark test specifications |
| `tests/unit/` | Software checks for construction, grading, integrations, and submissions |
| `results/` | Official runs, costs, ingestion records, test conversations, and grades |
| `mock_mcp/` | Simulated app server, tool definitions, and baseline state |
| `harness/` | Participant runner, execution records, and submission packaging |
| `graders/` | Deterministic and semantic action grading |
| `reference/` | Paper-configuration ingestion, evaluation, and recovery code |
| `examples/` | Harness template and example integrations |
| `construction/` | History construction, checkpoints, and release validation |
| `authoring/` | Test planning, creation, certification, and publication |
| `website/` | Dataset browser, results views, and participation instructions |
| `docs/` | Integration, evaluation, and methodology guides |

## Development checks

Run the offline software checks from the repository root:

```bash
pip install -r requirements.txt -r reference/requirements.txt jsonschema
python -m unittest discover -s tests/unit -t .
```

## License

[Apache License 2.0](LICENSE).
