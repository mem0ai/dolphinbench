# Hermes + GPT 5.6 Luna + OpenViking: 475/600 (79.17%), self-tested

| Persona | Passed | Total | Accuracy |
| --- | ---: | ---: | ---: |
| Morgan | 164 | 200 | 82.00% |
| Alex | 149 | 200 | 74.50% |
| Riley | 162 | 200 | 81.00% |
| **Total** | **475** | **600** | **79.17%** |

## Evidence

- [Submission ZIP](https://github.com/PapriKey/dolphinbench/releases/download/openviking-hermes-475-20261117/DolphinBench-OpenViking-Hermes-475.zip): `DolphinBench-OpenViking-Hermes-475.zip`.
- Size: **151,695,012 bytes (144.67 MiB)**.
- SHA-256: `042b155d170250b0bf3980cb49abc609807d1f85bfe42273a94d2c401bfc2c20`.

The ZIP contains only `ingestion.json` and `tests.json`: all **13,539 original ingestion records**, in each persona's released order, and all **600 test conversations**, including failed outcomes, actual app calls and saved Judge responses. Original ingestion bytes are retained. Recorded prompts, model/tool settings and usage are preserved in the JSON files.

## Configuration

We evaluate OpenViking with Hermes, following the published Hermes + Mem0 reference configuration for the reader model and inference parameters, the DolphinBench test set, and the grading protocol.

- **Harness:** Hermes Agent **0.21.0**.
- **Reader:** `gpt-5.6-luna`, reasoning effort **high**, context length **1,050,000**, and a **60-turn** limit.
- **Judge:** `gpt-5.6-sol`, reasoning effort **medium**, **8,000** max completion tokens and JSON output, using the released grader, settings and prompts.
- **Memory:** OpenViking with separate persona identities, together with Hermes native memory and session history. Each test uses a fresh conversation and isolated simulated app state. Test-time memory writes are blocked; the selected runs report **zero memory writes**.

Benchmark source revision: `0cac824baa281f11736657c422fb86abca7a4857`.

## Verification

Offline replay with the released mechanical/explicit grader and saved Judge responses reproduces **all 600 verdicts and all 1,920 check Booleans**, yielding **475 passes**. ZIP integrity and file hashes were verified locally.
