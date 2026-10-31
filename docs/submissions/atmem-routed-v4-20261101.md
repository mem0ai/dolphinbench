# AtMem routed-v4 / i3: 479/600, self-tested

This submits the final routed-v4 campaign for maintainer review. Only this
479/600 result is proposed for listing. It does not change official results,
tasks, grading, website code or leaderboard entries. Earlier research data
remains archived on Hugging Face and is not a separate submission in this PR.

| Persona | Passed / 200 |
| --- | ---: |
| Morgan | 171 |
| Alex | 152 |
| Riley | 156 |
| **Total** | **479 / 600 (79.83%)** |

- [Public dataset](https://huggingface.co/datasets/atmem/dolphinbench-routed-v4-20261101), revision `aa5849e2a6bba876ba9af4dc777a94bfe398e3ba`.
- [Submission ZIP](https://huggingface.co/datasets/atmem/dolphinbench-routed-v4-20261101/resolve/aa5849e2a6bba876ba9af4dc777a94bfe398e3ba/submission.zip).
- [Validation](https://huggingface.co/datasets/atmem/dolphinbench-routed-v4-20261101/blob/aa5849e2a6bba876ba9af4dc777a94bfe398e3ba/validation.json).
- [Technical article](https://huggingface.co/datasets/atmem/dolphinbench-routed-v4-20261101/blob/aa5849e2a6bba876ba9af4dc777a94bfe398e3ba/ARTICLE.md).
- ZIP SHA-256: `4e3130b5be2ba2aab4fa23570c69867fe1cfa1289be735f782a658170e559a05`.

## Exact condition and reproduction

Installed AtMem 2.3.10b1 plus frozen research routed-v4/i3 source, final harness
commit `bd38c5fcce270033e0e13df0c090592562e6fd38`, condition
`371fc4aa324910033d1525032e2ed84f67722abf61c4d2090886e88a73618fb6`.
The 2.3.11b1 PyPI package remains beta and is a different artifact; the score
must not be relabeled as its exact package benchmark.

Reader: `openai/gpt-5.6-luna`, high, Hermes subscription bridge. Judge:
`gpt-5.6-sol`, medium, OpenRouter Azure-only. Frozen per-persona local memory
and embeddings; isolated simulated apps. 13,539 original chronological
ingestion records are reused. Public tasks informed development, including
the fixed 20-task diagnostic; this is not an unseen holdout.

The ZIP contains exactly `ingestion.json` and `tests.json`. At upstream
`81cb6f8405b40a9e76089cef650806a80af06ea2`, unchanged `harness.submission`
`read_zip`, `read_release` and `validate` reproduce 479/600 from saved judge
answers with zero new model calls. All 600 outcomes, including failures,
remain. Unsupported message metadata is projected with upstream `clean_message`.

## Per-action evidence

727 required action groups: 98/100 reads and 504/627 writes pass. Four
trace-wide constraints are separate (3 pass). All 1,920 checks expose
per-candidate mechanical/judged outcomes and saved reasons, same-call
eligibility and distinct-call assignment. Failed writes have descriptive
post-hoc labels: 68 missing required facts, 25 wrong values, 20 wording or
precision, 8 no-call, and 2 source-verified remembered-fact delivery omissions.
Only the last two establish delivery before the dispatched write. They are
a lower bound, not a causal classification of every memory failure.

## Provenance and costs

Original first 19 execution/grades were imported byte-for-byte. Morgan/020
was reconstructed offline after duplicate read-only tool suppression broke
export; its app action was not repeated. At 416 graded tasks, Riley/017 stopped
on disk-full conversation-save. Two completed reader responses requested
memory tools only; independent app logs/state were empty. The owner authorized
a retry, which passed. The interrupted attempt, a pre-provider startup collision,
original native homes and costs were archived privately. Original 416 grade hashes
remained unchanged; no selection among competing scored attempts occurred.

Paid judging USD 3.905882, zero unresolved/reserved, against original USD 20 cap.
Ledger: 602 judge calls, 3,925 subscription reader calls including two interrupted memory-only
responses; 3,923 reader responses in scored records. Subscription/hardware/energy costs
are separate. Submission `total_cost_usd=0` covers local/subscription ingestion
and reader incremental API cash; paid judging is in `accounting.json`.

Historical Mem0 reference 424/600 is 55 passes lower (+9.17 pp gap), under unmatched
end-to-end conditions. This is not a matched memory-only advantage or official
ranking. Morgan 085/Morgan 130 fixture limitations are unchanged; there is no
repeated-run variance estimate or learning claim. Please review eligibility
for a self-submitted listing; no independent verification is claimed.
