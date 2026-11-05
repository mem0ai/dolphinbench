# DolphinBench: Hermes + Empirical, one `docker compose up`

Runs the official DolphinBench participant runner (`python -m harness.runner`) with Hermes
(`gpt-5.6-luna` on Azure, the leaderboard's Hermes settings) and Empirical as its memory provider,
and leaves a submittable `out/run/submission.zip`.

## Prerequisites

- Linux host with Docker (4 vCPU / 16 GB or more). Nothing else to install.
- Azure OpenAI resource with deployments named exactly `gpt-5.6-luna` and `gpt-5.6-sol`
  (the judge name is enforced by the benchmark).
- An OpenAI API key for the Empirical server's own calls. The `openai-meter` service logs every
  response's token usage, which is priced as the memory-processing cost.

## Run

```
cd benchmark/dolphinbench
./configure.sh --full     # asks for your own keys and writes .env (or copy .env.example by hand)
docker compose up --build --exit-code-from runner
```

Both Azure deployments use a custom content filter, `dolphinbench-high-only`: Azure's
`Microsoft.DefaultV2` with the hate, sexual, violence and self-harm thresholds raised from Medium
to High on prompts and completions; jailbreak and protected-material checks unchanged. With the
default, about one ingestion turn in 300 had the agent's reply cut off mid-stream
(`incomplete_details.reason: content_filter`) on ordinary history messages, after a memory write
had already gone through, which leaves that interaction ambiguous and stops the run.

`AZURE_OPENAI_ENDPOINT` is the bare resource root (`https://<resource>.openai.azure.com` or
`.services.ai.azure.com`); the judge appends its own path. `AZURE_OPENAI_BASE_URL` is that root
plus `/openai/v1`.

Output, all in `out/`:

- `run/submission.zip`, already checked with `harness.export_submission --check`
- `mongo.archive`, a `mongodump` of the memory store (required for a self-hosted provider)
- `run/`, the runner's evidence, cost totals and logs
- `images.tar.gz`, the exact server, runner and Hermes agent images the run used (no credentials)
- `SHA256SUMS`, fingerprints of `submission.zip`, `mongo.archive`, `images.tar.gz` and the cost ledger

## Resume

Run the same command again. Completed interactions are never repeated; setup keeps the existing
memories when `out/run` exists. An interaction whose delivery is uncertain stops the run for
inspection instead of being retried.

## Verify it yourself

Anyone with Docker can restore the published run and query each persona's memory, without our
source code or our credentials. Hand over this folder plus the run's `submission.zip`, `mongo.archive`,
`images.tar.gz` and `SHA256SUMS`, then:

```
./configure.sh     # asks for YOUR OpenAI API key (query embedding); writes .env, never shipped
./verify.sh        # checks SHA256SUMS, loads the images, restores mongo.archive, queries each persona
```

`verify.sh` runs as its own compose project (`dolphinbench-verify`), so it never touches a run on the
same machine, and its OpenAI meter writes to `verify-out/`, leaving `out/` and its checksums intact.
The run's own API key was stored only as a hash, so `setup.py` issues a fresh key for the restored
benchmark account and the script prints it with a ready `curl` example. `QUERY="..." ./verify.sh`
changes the sample question. Clean up with `docker compose -p dolphinbench-verify down -v`.

To re-run the whole benchmark instead, `./configure.sh --full` also asks for an Azure OpenAI resource
with `gpt-5.6-luna` and `gpt-5.6-sol` deployments; then use the Run command above.

## Local checks

- `PREPARE_ONLY=1`: stops after the free `prepare` stage (no model calls).
- `SLICE="--persona morgan --depth 10 --out /out/slice-morgan-10"`: the runner's own `Runner` on
  the first N history messages of one persona and the tests they support (see `slice.py`).
- `SERVER_COST=none`: reports agent cost only; not valid for submission. `openai_usage` reads the
  OpenAI organization usage report instead of the meter (lags an hour or more).
- `EMPIRICAL_HOST_PORT`: host port for the Empirical server (default 3105).

Offline unit tests of the adapter (inside the runner image):

```
docker compose build runner
docker run --rm -v "$PWD/tests:/tests" -v "$PWD/hermes-plugin:/repo-plugin" \
  -e REPO_PLUGIN_DIR=/repo-plugin/empirical -w /bench dolphinbench-runner \
  python -m unittest discover -s /tests
```

## What is here

| File | Purpose |
|---|---|
| `docker-compose.yml` | services `openai-meter`, `mongo` (Atlas Local, pinned), `empirical` (server), `workers` (memory repair), `runner` |
| `openai_meter.py` | forwards the server's OpenAI calls and logs their usage to `out/openai-usage.jsonl` |
| `server.Dockerfile`, `empirical.env` | the Empirical server image and its non-secret settings (all background jobs off) |
| `runner.Dockerfile` | DolphinBench @ pinned commit, Hermes @ pinned commit + `empirical-hermes/hermes-empirical.patch` |
| `empirical-hermes/` | the combined patch (official evaluation patch + `plugins/memory/empirical`) and its hash spec |
| `hermes-plugin/empirical/` | readable copy of the plugin (a test checks it matches the patch) |
| `empirical_hermes_adapter.py` | the only new logic, a subclass of the runner's Hermes adapter |
| `setup.py` | vector indexes, benchmark account and persona workspaces on a fresh database |
| `runner-entrypoint.sh`, `slice.py` | the full run, and the small-slice check |
| `configure.sh` | writes `.env` from your own credentials (`--full` adds the Azure agent and judge) |
| `verify.sh`, `docker-compose.verify.yml` | restore the published run from `out/` and query it |

## How the adapter differs from the runner's Hermes adapter

The runner's own Hermes adapter supports only Hermes's built-in memory. Everything below is what an
external provider needed; benchmark files are not modified (the adapter extends module-level tables
at import).

- Profiles use the reference `hermes-builtin-luna` recipe with `memory.provider: empirical` and one
  other change: Hermes's automatic post-answer review is off (`auxiliary.background_review.enabled:
  false`). Hermes starts that best-effort skill/memory review after a turn with 10 or more tool
  steps; in a one-shot CLI turn it is cut off at exit, and when its model calls are recorded first
  the transcript ends on a tool call with no result, which the runner rejects. The benchmark does
  not configure this setting either way.
- Hermes source identity is the pinned base plus one combined patch, verified by hash.
- The combined patch also canonicalises tool-call arguments that repeat a key: tool execution
  parses them with `json.loads` (the last value wins), but the benchmark's strict submission
  recorder rejects a repeated key and loses the whole response event (seen once: gpt-5.6-luna
  emitted a tool_search call repeating `limit`/`queries` around junk tokens). Only such calls are
  re-serialised, to exactly the object that runs; every other call is recorded as the model sent it.
- Every Hermes turn runs in the benchmark's isolated agent container; the adapter adds the plugin's
  variables to that container's environment allowlist and the receipt file to its outputs.
- An ingestion message counts as delivered only when the plugin wrote its receipt.
- During ingestion the plugin added recalled memory to the user message; it is recorded, verbatim, as
  a separate `developer` message just before it, so the user message equals the history message. The
  tests ran with no such block: the agent calls `empirical_recall` itself.
- App calls keep the arguments the model sent and the app server's result (the server's log fills
  unset optional parameters with their defaults or an empty value, which would otherwise fail the
  runner's matching). The ingestion rows saved before this alignment covered every app tool were
  re-aligned the same way before packaging (796 rows); 28 rows whose logged call the server had
  transformed (a vector replaced by its length, a dropped field) carry no `app_calls`. Originals and a
  ledger are kept with the run evidence.
- The pinned Hermes cannot price Azure, so agent cost is priced from Hermes's recorded tokens with
  the benchmark's own `pricing/latest.json`; server cost is the metering proxy's ledger priced
  the same way, split by phase window.
- The agent image is the official `reference.execution.build_agent_image` build plus one layer
  (`agent-deps.Dockerfile`) holding what Hermes otherwise downloads in every per-message
  container: boto3 and edge-tts at the versions its lazy installer pins, and the tirith command
  scanner pinned to v0.4.2 (Hermes fetches GitHub's latest release). The agent's tool set is
  unchanged; setup is about 5-7 s faster per message.
- Three recording normalizations, for Hermes core behaviors that no literal recording can export.
  Every occurrence is listed and the untouched recording is kept under `out/run/hermes/normalized-raw/`.
  1. Empty reply after a tool call. Hermes's stock recovery resends the conversation with that reply
     replaced by `(empty)`; the runner rejects the changed history ("Conversation context was
     rewritten") and an empty assistant message fails its message check too. The adapter records the
     reply as `(empty)`, the placeholder the model saw for the rest of the turn, keeping its
     API-reported usage and timing. It applies only to an empty reply (no text, no tool calls) whose
     next request carries exactly `{"role": "assistant", "content": "(empty)"}` in its place; any
     other rewrite still fails. On the Responses API Hermes can instead replay the model's raw output,
     whose text the recorder's hook saw as an empty reply; that resent text (no tool calls) is recorded
     as the reply. It can also resend a tool-call reply with the commentary sentence the model wrote
     beside the calls, which the hook did not see as content; when the resent message matches the
     recorded reply exactly apart from that text (same tool calls), the text is recorded with it.
     Listed in `out/run/hermes/empty-reply-placeholders.jsonl`.
  2. Hermes's own scaffolding sent as user messages: recovery and continuation re-prompts (such as
     "You just executed tool calls but returned an empty response..." and "[System: Continue now.
     Execute the required tool calls...]"), compression and iteration-limit requests, and
     background-process and todo notices. Hermes classifies these as not a real human turn
     (`ContextCompressor._is_synthetic_compression_user_turn`); the runner allows exactly one user
     message per interaction. A later user message matching Hermes's rule is recorded as a
     `developer` message, like the recalled-memory block. The texts are read from the pinned,
     hash-verified Hermes source at run time, not copied. Listed in
     `out/run/hermes/synthetic-user-turns.jsonl`.
  3. An empty reply Hermes dropped and asked again. When a reply is completely empty (no text, no
     tool calls), Hermes can discard it and send the identical request again; the model never saw
     that reply, so it is left out of the trace (its tokens stay in Hermes's cost records). It
     applies only when the next request is exactly the same as the empty reply's own request.
     Listed in `out/run/hermes/discarded-empty-replies.jsonl`.
  The question is open with the maintainers as mem0ai/dolphinbench#9.
- Resumes. Ingestion was interrupted several times (output-archive rejections, an Azure rate limit,
  server memory limits, an Azure subscription cutoff, and a move of the stack to another machine) and
  resumed each time with the official runner, which never repeats a completed interaction. The 24
  interactions that were in progress at a stop were set aside unchanged, each with a record of what it
  had already written, and run again on resume; a rerun's memory writes went through normal dedupe
  against the first attempt's. Nothing was skipped: all 13,539 history messages were ingested. The
  server's Node heap cap was raised to 6 GB during ingestion; the server code and the memory data
  were not changed.
- Trace files set aside during ingestion. The harness copies a persona's whole Hermes profile into
  a fresh container and back out on every turn, and refuses an output over 2 GiB. The profile grows
  by every turn's raw recording in `submission-traces/`, so by 2026-10-28 alex's profile was
  1.78 GiB and riley's 1.92 GiB, and turns had slowed from about 27 s to about 120 s. Nothing
  reads an old recording: each one is read once, at the end of its own turn, into that turn's
  record under `out/run/ingestion/`, which is what packaging uses. From 2026-10-28 23:30Z the
  recordings of alex's and riley's completed turns (8,480 files by 23:39Z, about 3 GB) were moved unchanged
  to `out/run/hermes/archived-submission-traces/<persona>/`, with every action listed in its
  `ledger.jsonl`, and after the run they are moved back into
  `hermes/profiles/<persona>/submission-traces/`. As a result, `alex-frozen` and `riley-frozen` (the end-of-ingestion snapshots
  the tests start from) do not contain those older recordings, unlike `morgan-frozen`, which was
  frozen on 2026-10-10. The agent never reads them, so its behavior is unaffected.
- Tests. The 600 tests ran on 2026-11-04 and 05 on the frozen ingestion. Two were set aside and rerun
  the way the ingestion resumes were: `tests/riley/182` (two identical parallel tool calls, rewritten by
  Hermes; the first attempt's posted comment stayed in its own sandbox copy) and `tests/riley/184`
  (Hermes returned an incomplete turn after posting its comment). The first attempts are kept with the run evidence.
- Judge transcripts. The grader saves the judge model's reply as the OpenAI SDK returns it; the SDK in
  this run's runner image adds two always-empty fields (`annotations`, `refusal`) that the packager's
  message check rejects. Only those two keys were removed; originals and a ledger are kept with the run evidence.
- Cost. Ingestion $105.27 and tests $5.32 are the harness's own phase totals.
- Package size. The packager writes the complete settings, including the whole tool list, into every
  assistant message of a row whose recorded tool definitions differ from the first row's, and Hermes
  writes the profile folder and the deferred-tool count into three tool descriptions, so the three
  personas' rows all differ and `ingestion.json` comes to 1.87 GiB against the format's 512 MiB cap
  (about 575 MiB with identical settings; the system prompts, which differ by persona and day, cannot be
  hoisted). The ZIP was written by the official packager with only that size check raised; its content
  is exactly what the packager produces.
- Production's memory repair runs beside the server, as it does in production: the `workers`
  service with only `memory-enrichment-retry`, `memory-embedding-backfill` and
  `memory-chunked-embedding` (`WORKERS_JOB_ALLOWLIST`). It retries an enrichment that failed at
  write time (for example an OpenAI timeout) and fills a missing vector; its OpenAI calls go
  through the metering proxy, so they count in the memory-processing cost.
- `freeze` waits until every memory is search-indexed, no memory is still enriching
  (`pending`, `failed`, `pending_chunked`, `chunking_in_progress`) and every search row has an
  embedding, then records a workspace fingerprint that `verify_checkpoint` recomputes. A memory
  that ends `permanently_failed` stops the run instead of freezing incomplete memory.
