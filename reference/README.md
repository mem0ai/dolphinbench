# Reference reproduction

This directory contains reference ingestion, evaluation, recovery, and result
recording code. Reproduce a reported result using its recorded source bundle
and settings, not a later version of this directory.

The paper covers Hermes Luna, Hermes MiniMax, and Claude Code Sonnet, each with
Built-In, Mem0, Honcho, Hindsight, and Supermemory. Each configuration has its
own ingestion and evaluation.

To evaluate your own harness, use the [integration guide](../docs/DRIVER_CONTRACT.md).
The reference configurations are examples and reproduction code for the paper.

## Code ownership

There are 20 implementation files, including the command entrypoint:

| Files | Responsibility |
| --- | --- |
| `__main__.py`, `ingest.py` | Commands, actual Hermes ingestion, and completed-session checkpoints |
| `plan.py`, `evaluate.py` | Preparation, input hashes, evaluation, saved results, and suite validation |
| `artifacts.py` | Completed-ingestion verification, profile materialization, and app-schema checks |
| `runtimes/hermes.py` | Hermes profile configuration |
| `runtimes/claude_code.py` | Claude native memory and official-plugin ingestion |
| `runtimes/claude_history.py` | History execution, matching runtime/database checkpoints, and database checks |
| `runtimes/claude_recovery.py` | Interrupted-turn recovery and provider completion checks |
| `memory/hindsight.py`, `memory/supermemory.py`, `memory/honcho.py` | Provider-specific processing, verification, and recovery |
| `memory/proxy.py`, `memory/services.py` | Authenticated services, retrieval-only proxies, and API compatibility |
| `execution/hermes.py`, `execution/claude.py`, `execution/claude_native.py` | Ingestion workers and their persistent storage |
| `execution/images.py`, `execution/modal.py`, `execution/worker.py` | Runtime images, remote scheduling, checkpoint persistence, and downloads |

Mem0 uses the harness's official memory plugin and hosted service. Its ingestion
and recovery logic lives with the corresponding harness; it does not need a
separate client implementation in this directory.

`source-map.json` maps the earlier files to their consolidated owners and
records the source snapshots reviewed during cleanup. It is not a provenance
bundle for a completed benchmark result.

## Commands

Install the root and reference requirements in a dedicated environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r reference/requirements.txt
python -m reference --help
```

### Isolated Hermes execution

New Hermes calls run in a separate agent container. For local Docker, build its image
from the selected Hermes checkout (tracked working-tree files only):

```bash
python -m reference.execution.build_agent_image --source /path/to/hermes-checkout --memory builtin
export DOLPHINBENCH_AGENT_IMAGE=$(docker --context default image inspect --format '{{.Id}}' dolphinbench-hermes-agent:local)
```

Replace `builtin` with the selected memory plugin: `mem0`, `honcho`,
`hindsight`, or `supermemory`; `all` installs all four plugins' dependencies.
The builder installs the selected plugin's declared
Python dependencies along with Hermes.

Prepare a new run after selecting the image. Its immutable image ID is part
of the configuration; do not switch an existing run to another image.
The image includes Hermes and its dependencies, not the benchmark dataset.
The launcher copies the selected profile, a small recording helper, and any
explicit `DOLPHINBENCH_AGENT_WORKSPACE` into a disposable container. Only put
task-visible files in that workspace. It does not inherit the controller's
environment or mount its repository, run directory, or Docker socket.

The app server stays on the controller. A Unix socket (Docker) or an
authenticated TLS connection (Modal) carries the existing MCP protocol
without giving the agent the underlying app database. The agent
keeps normal Hermes CLI tools, including terminal and file tools. Evaluation
still disables plugin memory writes and automatic capture; ingestion does not.
The launcher exports profiles and receipts even on an interrupted turn and
rejects symbolic links, special files, and escaping output paths.

Local Docker requires the default local daemon and memory/model endpoints
reachable from the agent container, not controller loopback.

For Modal, build a clean agent image and set `DOLPHINBENCH_AGENT_IMAGE` to
the `im-...` ID printed by this command before preparing the run:

```bash
python -m reference.execution.build_agent_image --backend modal --source /path/to/hermes-checkout --memory all
export DOLPHINBENCH_AGENT_IMAGE=im-REPLACE_WITH_BUILT_IMAGE_ID
```

The reference worker starts a disposable Modal Sandbox for each turn. It
copies the profile and recording helpers, but mounts no benchmark volumes
and passes no controller credentials. Memory requests travel through
authenticated forwards to the configured services. The controller retains
its existing private-network access, including Tailscale and loopback
services. The agent cannot choose a different forwarding destination.
After a turn, the controller retrieves the profile and receipts, closes the
connection, and terminates the sandbox. Interrupted turns retain output
when the sandbox remains reachable; a lost sandbox still requires the
provider's existing recovery checks before replay.

The managed Hindsight/Supermemory ingestion command also reads this image
variable when its worker image is built. Its saved ingestion receipt records
the image ID and rejects a resume with a different image. Use the recorded
source bundle and settings to resume older runs; do not silently replace
their execution configuration. The participant adapter interface is unchanged.

The local container boundary test uses an explicitly selected existing image
with Python and the MCP SDK and makes no model calls:

```bash
DOLPHINBENCH_CONTAINER_TEST_IMAGE=sha256:... python -m unittest reference.tests.test_agent_container
```

The explicit Modal check uses temporary compute and fixture app/memory data,
with no model or real memory-provider calls:

```bash
modal run reference/tests/modal_agent_smoke.py --agent-image "$DOLPHINBENCH_AGENT_IMAGE"
```

The [evaluation instructions](../docs/CANONICAL_EVALUATION.md) describe
independent ingestion, the simulated app connection, and the retained worker
commands. Runtime source checkouts, official plugins, and service credentials
must match the selected run's recorded configuration.

### Worker installations

For Modal evaluation suites that include Hermes, set the source checkout used
for the selected run:

```bash
export DOLPHINBENCH_HERMES_SOURCE=/path/to/hermes-checkout
```

For Claude evaluation or ingestion workers, select the Claude Code executable:

```bash
export DOLPHINBENCH_CLAUDE_BIN=/path/to/claude
```

If you omit `DOLPHINBENCH_CLAUDE_BIN`, the worker builder uses `claude` from
`PATH`. It checks that the executable reports version 2.1.259 before building.
The Hermes builder checks the checkout structure; prepared manifests retain
their source hashes and the evaluation worker restores the bundled source.

The evaluation scheduler selects packages from each manifest's `agent_runtime`.
Hermes-only suites do not require Claude installed, and Claude-only suites do
not require a Hermes checkout. Status checks and downloads require neither.
Each Claude plugin worker includes only its selected plugin. These settings
do not change the separate managed-ingestion images in `execution/hermes.py`.

## Verification and release

The regression tests cover history ordering, provider completion checks,
interrupted writes, checkpoint restoration, evaluation resume, and recording:

```bash
python -m unittest discover -s reference/tests -p 'test_*.py'
python -m unittest discover -s harness -p 'test_*.py'
```

These local checks do not establish a successful paid reproduction. For each
score reported in the paper, publish the exact source bundle, configuration,
receipts, traces, grades, and recovery records that produced it. The complete
result artifacts are not included in this checkout.
