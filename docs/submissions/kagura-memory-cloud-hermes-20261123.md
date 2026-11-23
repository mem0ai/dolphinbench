# Kagura Memory Cloud (Hermes): 455/600 (75.8%), self-tested

This is a self-tested run of DolphinBench at release `81cb6f8`. The maintainers have not verified it. The ZIP is over
the website's 256 MiB limit, so it was not submitted there. This file records the run publicly, so that anyone can
check it.

| Persona | Passed | Total | Success |
| --- | ---: | ---: | ---: |
| Morgan | 165 | 200 | 82.5% |
| Alex | 145 | 200 | 72.5% |
| Riley | 145 | 200 | 72.5% |
| **Total** | **455** | **600** | **75.8%** |

## Evidence

- Submission ZIP: [kagura-memory-cloud-hermes-submission.zip](https://github.com/kagura-ai/dolphinbench/releases/download/kagura-memory-cloud-hermes-20261123/kagura-memory-cloud-hermes-submission.zip)
- ZIP SHA-256: `86918af18ed6b85dcccf94cc6e3fca240414b874da1f42f1a05c2a0269d677f4` (330 MiB compressed)
- The ZIP holds `ingestion.json` (all 13,539 history messages, in order) and `tests.json` (all 600 test
  conversations, app calls and saved judge answers, failures included).
- Written by the release's packager (`harness.runner` `package`, at `81cb6f8`) with one local change: the
  512 MiB per-file cap raised for that process. `ingestion.json` is 1,193 MiB uncompressed (`tests.json` 73 MiB). The
  packager copies the complete settings into every assistant message of a row whose settings differ from the first
  row's, and Hermes writes each persona's deferred-tool count into the `tool_search` description, so the three
  personas' rows differ (the same cause as in the Empirical submission). The content is what the packager
  produces; only the size check was relaxed. `harness.submission.validate` accepts the ZIP under the raised cap and
  gives 455/600 from the saved judge answers, with no model calls.

## Source

The plugin, the adapter and the lists behind the counts in this note are in the fork, at the release's tag:
[`docs/submissions/kagura-memory-cloud-hermes-20261123-source`](https://github.com/kagura-ai/dolphinbench/tree/kagura-memory-cloud-hermes-20261123/docs/submissions/kagura-memory-cloud-hermes-20261123-source).

- `plugins/kagura/`: the Hermes memory plugin as used (tree `09d470e3…`; `plugin_sha256` in each `run.json`).
- `adapter/`: the adapter and its gate, the run, grade and packaging scripts, the stack helper and compose overlay.
  Local paths are replaced by `/path/to/`.
- `runs/`: each ingestion run's `run.json` (identity and launcher changes) and `rewritten.jsonl` (recording cases).
- `normalizations.jsonl` and `redactions.jsonl`: every record changed before packaging.

## Configuration

- **Agent:** Hermes at `935c1e8` with the release's evaluation patch (patch SHA-256 `24e3d352…`), agent image
  `sha256:84a1e7bd…`, `gpt-5.6-luna` at reasoning effort high through the OpenAI API (Responses), 60 turns at
  most. Toolsets: `dolphinbench-apps`, `memory` and `session_search` (the published runs' set). Hermes's built-in
  memory stays on.
- **Memory:** Kagura Memory Cloud v0.156.0, self-hosted, through a Hermes memory plugin that gives the agent
  `kagura_recall`, `kagura_reference` and `kagura_remember`. Each recall also runs a keyword search, and the two result
  lists are interleaved, up to 1.5 × k (k = 20). The turn's first recall also searches with the user's message, up
  to 2 × k. After each turn the plugin saves the turn to memory. The plugin's system-prompt block asks the agent to
  search memory before acting and adds one sentence on writing what it recalled into the deliverable. Nothing is
  injected into the prompt before the agent asks. The full prompt is in `tests.json`.
- **Server models (self-hosted on the submitter's GPU server):** embeddings `qwen3-embedding:0.6b`, reranker
  `qwen3-reranker-0.6b`, and `deepseek-v4-flash` for the check before a new memory replaces an older one. Background
  consolidation is off.
- **Memory isolation:** the store is frozen after ingestion and restored from its snapshot before each test, so a
  test cannot change what the next one reads.
- **Judge:** `gpt-5.6-sol` at medium effort, sent to the OpenAI API instead of Azure (we could not get Azure quota
  for the model). The model, settings and prompts are the release's. The OpenAI replies carry an empty `refusal` and
  an empty `annotations` field, which the release's message form does not take. They were dropped from the 1,184
  judge messages in which they were empty, as the release's `clean_message` does.

## Cost: $97.90

- Ingestion $91.82 and tests $6.08: the agent's tokens from Hermes's usage table, priced with the release's
  `pricing/latest.json`.
- Not included: the memory server's own models, which ran self-hosted and are not metered in dollars. The checks
  before a replacement made 9,542 calls (7.73 M input and 0.62 M output tokens) over the three stores. Embedding and
  reranker calls are not counted.
- Median test latency: 41.0 s.

## Limits

- **Not held out:** we chose the plugin's recall settings and prompt sentence after reading failures and replaying
  recalls on the public tests.
- **Agent route:** the agent ran through the OpenAI API, not Azure.

## Departures from the release's runner

The adapter extends the runner's Hermes adapter. Each run's `run.json` lists its launcher changes, and each is also
listed here.

- `KAGURA_API_KEY` is added to the environment names the launcher passes into the container.
- Payload files are made writable before they are copied into the container.
- During ingestion, `HERMES_MEMORY_SYNC_DRAIN_TIMEOUT_SECONDS=300`, so that the plugin's save after a turn finishes
  before Hermes exits.
- A test copies the plugin into its profile, in place of the copy frozen with the store (same plugin).
- Recording, where the release's recorder would stop. Each case is counted in each run's `hermes/rewritten.jsonl`
  (400 ingestion conversations of 13,539 have at least one):

  | Case | Ingestion conversations |
  | --- | ---: |
  | a later request shows an earlier reply differently; recorded in its later form | 384 |
  | a user-role message Hermes adds after the request; recorded with the role `developer` | 15 |
  | a reply the recorder could not parse; taken from the next request, without its usage | 8 |
  | an empty reply that Hermes asked again; left out | 1 |

## Normalizations before packaging

The validator matches app calls exactly against the transcript and requires usage on every reply. Before
packaging, each record was normalized as listed below, and every change is listed with its record. Nothing was
changed in the agent's messages.

| Change | Ingestion | Tests |
| --- | ---: | ---: |
| app call: argument the mock app filled with its default (not sent by the agent) left out; the app's result kept | 2,125 keys | 185 keys |
| app call with no matching call in the transcript left out | 21 | 2 |
| missing usage of one reply derived from Hermes's per-session total minus the other replies | 8 | 0 |

## Memory-server errors during ingestion

The memory server's models run on one self-hosted GPU server, which was unreachable for short periods during the
runs. All saves succeeded (16,575 of 16,575). Recalls that returned an error during ingestion: Morgan 636 of 20,124
(3.2%), Alex 6 of 29,302 and Riley 8 of 29,832. Most were the plugin's own lookup before a save. In 69 ingestion
messages (Morgan 68, Riley 1) the error reached the agent as the result of its `kagura_recall` call; these messages
were not sent again. Tests that ran during such an error were run again (see below).

That error message named the server's address on our local network. Before packaging, the address was replaced by
the word `redacted-host` in those 69 records, and nothing else was changed in them.

## Re-runs and interruptions

- **Gate errors:** tests that ran while the memory server returned errors were run again, and the re-run's record
  is used: Morgan 019–029 (11 tests) and Riley 080.
- **Records that could not be completed:** Riley 078 and Morgan 049 and 072 had a reply without usage and no
  session total to derive it from (a test's profile is deleted after the test). They were run again under a rule
  fixed before the re-run: the re-run's record and grade are used, whatever they are. All three had passed in the
  first run.
- **Ingestion:** Riley's message 1596 stopped the run (an empty reply asked again, before the adapter handled that
  case). It had been delivered and answered, and its save had been stored, so it was not sent again: its record was
  written from the saved recording. Morgan's ingestion was interrupted twice by the machine (a restart and a
  crash). Each time, the message in flight had made no memory or app call, so it was sent again.
