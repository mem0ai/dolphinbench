# Connect your agent to DolphinBench

Keep your existing model, agent loop, and memory system. DolphinBench sends
each history message and test request to your agent, provides simulated apps,
records the results, and packages the submission.

You implement the connection to your agent and memory. You do not write the
dataset loop, initialize app state, run the grader, or assemble submission JSON.
The participant runner has local checks; a paid end-to-end run remains
unverified. The paper's evaluation code is documented [separately](CANONICAL_EVALUATION.md).

## 1. Create your starter files

After installing the repository requirements, run this from the repository root:

```bash
python -m harness.runner init
```

This creates `my_harness.py` from the [Python template](../examples/harness_template.py)
and a matching `run.yaml`. It makes no model calls and refuses to overwrite
existing files. The configuration already selects your starter file and saves
the run under `tmp/my-agent/`; you do not need to write it yourself.

Open `my_harness.py`. Implement its five TODO methods:

| Method | What you supply |
| --- | --- |
| `identity()` | Code and dependency versions, model settings, and memory configuration as JSON. Exclude credentials. |
| `run_agent(request, tools, call_app)` | One fresh agent conversation, returning its settings and complete recorded messages. |
| `freeze(persona)` | Wait for memory writes, preserve the completed memory, and return its snapshot ID or content hashes as JSON. |
| `verify_checkpoint(persona, checkpoint)` | Check the stored memory against that identity; raise if it is missing or changed. |
| `total_cost_usd(phase)` | Total agent and memory-processing cost for the completed phase, across all three personas. |

Keep imports, the constructor, and `identity()` local-only. The runner checks
them before executing your agent. Put credentials in environment
variables, not in the Python file or submission.

## 2. Connect your agent

In `run_agent`, send `request.dated_message` once, unchanged, as the user
message in a fresh conversation. Use `request.persona` to select the memory
store and `request.phase` to choose between ingestion and evaluation.

### App tools

The template already opens the local Model Context Protocol (MCP) connection.
For each interaction, the runner supplies `request.apps`: the server command,
arguments, and environment. The template starts that process, initializes MCP,
and passes the resulting tool definitions to `run_agent` as `tools`.

Convert those definitions to your model's tool format. When the model chooses
an app action, execute `await call_app(name, arguments)` and return the result
to the model. Your memory tools remain alongside these app tools. The template
closes the MCP connection when the conversation finishes, including on errors.
There is no hosted app URL or separate server-start command.

If your harness already manages MCP, replace the template's `run_interaction`
method instead: register `request.apps` unchanged as a stdio server in your
runtime. Do not start a second connection. A remote harness must make the supplied
server and state files accessible inside its execution environment.

### Recorded messages

Return `InteractionRecord(settings, messages)`. This example shows the shape,
not real benchmark evidence:

```python
from harness.adapter import InteractionRecord

record = InteractionRecord(
    settings={"model": "your-model-id"},
    messages=[
        {"role": "user", "content": request.dated_message},
        {"role": "assistant", "content": "Recorded response.",
         "usage": {"input_tokens": 10, "output_tokens": 3}},
    ],
)
```

Include system prompts, retrieved context, assistant responses, tool calls, and
the tool results your agent actually saw. Every assistant response needs its
API-reported token usage; do not estimate it or divide a session total across
responses. Record the actual tool definitions and model settings.

The runner adds persona and interaction IDs, measures duration, and records
grading evidence. You can return `duration_ms` when your runtime measures it
directly. See [the return types](../harness/adapter.py) for optional fields.

For changing model settings or tool definitions, attach the complete
`settings` to the affected response. Context rewriting that cannot be
represented as an append-only conversation is not supported by this export
format. Disclose internal model calls your recording cannot capture.

The runner reads app actions from the server log. If you supply `app_calls`
yourself, each action must match a recorded conversation call, including
duplicates. Preserve the visible tool-result wrappers; do not replace them
with a different structured result.

## 3. Preserve memory between conversations

During ingestion, start with a separate empty memory store for each persona.
Process every history message through your agent and its normal memory hooks,
with a fresh conversation each time. Retain memory between these conversations
and account for required writes before returning.

After ingestion, `freeze` must wait for background processing and preserve
completed memory. Store it durably: ingestion and evaluation run in separate
processes, so an in-memory Python object is not enough. A transcript alone does
not prove that a memory service finished processing.

During tests, `verify_checkpoint` must check the actual saved memory, not just
its label. Use that completed memory for retrieval, with writes and deletion
blocked, including automatic memory capture. Start each test with a fresh
conversation and isolated runtime state. A prompt asking the agent not to
write is insufficient.

The runner resets the simulated apps. Your harness enforces memory isolation.
Keep dataset files, facts, grading criteria, and real app connectors outside
the agent's context and tools. Do not give the agent unrestricted access to
the run directory, which contains controller evidence.

### Cost

Implement `total_cost_usd(phase)` using your harness's usage records and memory
service accounting. The runner calls it after ingestion finishes, including
`freeze` for all personas, and again after evaluation finishes. `phase` is
`"ingestion"` or `"tests"`.

Return the total in USD for that phase across all three personas. Include agent
calls, memory processing, retries, and background work attributable to this run.
Wait for pending processing before returning the total. Exclude grading and
infrastructure. Count each charge once, including memory calls already recorded
as agent calls. Use zero only when the phase actually has no included cost.

Keep accounting records across process restarts. This method must read those
records, not run the agent again. If collection fails, resolve the accounting
error and resume the same stage: completed conversations and grades will not repeat.
The runner saves each phase total and adds the two totals for the submission;
it does not add token-priced conversation costs again. It validates the amounts,
but your integration is responsible for their coverage and calculation.

## 4. Check the connection

After implementing the five methods:

```bash
python -m harness.runner prepare
```

This validates the release, loads your harness, and saves its identity in
`tmp/my-agent/run.json`. It does not call your agent or prove that its memory
and tool handling work.

Check tool routing, response recording, and memory isolation with mocked model
responses before paying for a run. If MCP startup fails, use
`python -m examples.mcp_connection --persona morgan` to check local server
startup and tool discovery without model calls.

## 5. Run and submit

Set your agent credentials and the grading credentials:
`AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_API_KEY`. Grading uses Azure
`gpt-5.6-sol` with medium reasoning.

The following commands process the full history and all 600 tests. Run them
only after approving the model and grading costs:

```bash
python -m harness.runner ingest --confirm-paid-calls
python -m harness.runner evaluate --confirm-paid-calls
python -m harness.runner package
```

Upload `tmp/my-agent/submission.zip` on the website. Packaging validates saved
evidence without model calls and refuses to overwrite an existing ZIP.

The ZIP contains exactly `ingestion.json` and `tests.json` at its root,
covering all three personas and all 600 tests, including failures. Each file
includes its phase's automatically recorded `total_cost_usd`.
Missing or invalid totals prevent acceptance. Keep
credentials outside the archive. The website accepts ZIPs up to 256 MiB.
The local exporter allows 1,026 MiB compressed; each JSON file is limited to
512 MiB uncompressed, with at most 1 GiB total. Only regular files using
Deflate or no compression are accepted; no encryption, links, directories,
or extra entries. JSON rejects duplicate keys, non-finite numbers, and nesting
over 128 levels. See [the validator](../harness/submission.py) for the full format.

### Configuration and resuming

Commands use `run.yaml` by default; use `--config FILE` for another run.
Its `options` mapping is passed to your constructor. Keep `release` and
`adapter` unchanged for the starter setup. Paths resolve relative to the
configuration file.

Use a new `output` directory when changing the model, memory system, code, or
configuration. Completed interactions are not repeated when resuming the same
run; unfinished grading reuses saved outputs.

An `.inflight` marker means an interaction may already have reached your
agent. Reconcile that evidence before resuming; deleting the marker does not
make replay safe. Your harness owns retries and must preserve their evidence.
