# Alex Valdez — Persona Sheet

A human-reference character study of Alex at T=0 (the start of the corpus, March 5 2023). This is **not** a generator prompt. Actual generation prompts are assembled per-call from a character core, world-state-at-T, session-type context, and voice samples — this sheet informs those assemblies but is not pasted into them. Read it as a snapshot of who Alex is when we meet him, not a specification of who he must remain.

---

## 1. Who Alex is

Alex is a senior IC on the infra/platform team at Sphere — a ~220-person observability/devtools SaaS in Brooklyn, ~$30M ARR, Series B closed about eighteen months ago. He's been at Sphere a little over three years, and engineering for five to seven; he owns the bottom half of Sphere's metrics pipeline and a couple of adjacent services. He lives in a one-bedroom in Park Slope with Devika, a fourth-year internal-medicine resident whose schedule swings between long days and night-shift weeks; they've been together about three years. His younger sister Anya recently moved to NYC for a design job and is in his life again in a closer way than she's been since college. Alex thinks in services, dependency graphs, p99s, and dollars-per-million-data-points, not in altitude or vision. He's not cold — he texts Anya warmly, jabs at his peers in Slack, holds his own at brunch — but he's not effusive either; he's the kind of person who tells you a service is "fine" when he means "good," and "rough" when he means "I've been awake since 4am putting it back together." He prefers fixing the thing to talking about fixing the thing. When something breaks he gets clipped and precise; when he's tired he gets terse and a little flat; when he wins he says "clean" or "shipped" and goes back to the next ticket. Age and pronouns are not stated anywhere in the corpus; any reference to them in downstream work is inference, not source.

---

## 2. Starting state (T=0 snapshot, March 5 2023)

_This is Alex's situation at the start of the corpus. These can and do evolve through later history sessions (e.g. on-call rotation membership, runbook ownership, deploy windows, the migration's pause-and-restart). When that happens, the history is authoritative. Do not treat any of the below as permanent._

- **Role**: Senior IC, infra/platform team at Sphere (~220 people, infra team of 8). 5–7 years engineering experience; ~3 years at Sphere.
- **Manager**: Hema Iyer (head of infra), reports to CTO Theo Brandt.
- **Owned services**: `metrics-router` (Go), `ingest-edge` (Go), `shard-keeper` (Python). All sit in the metrics pipeline.
- **Active work**: Mid-migration moving Sphere's internal metrics pipeline off a homegrown statsd-style aggregator onto an OpenTelemetry + Prometheus stack. Quiet, heads-down phase.
- **City**: Brooklyn, Park Slope. 1BR with Devika.
- **Partner**: Devika, 4th-year internal-medicine resident at a Manhattan hospital, ~3 years together. Long hours, frequent night-shift weeks.
- **Family**: Younger sister Anya — design job at a creative agency in NYC, moved here ~6 weeks before T=0. Parents in suburban Texas, occasional calls.
- **Rough life phase**: senior IC settling into "I know how this place works, now I move it." Not climbing for staff, not coasting. Migration is the year's load-bearing project for him.

---

## 3. Voice — range and tendencies

Alex writes like an engineer who's already in a terminal — short, precise, technical-without-explanation, inline service names and tool names rather than nouns at altitude. He drops p99s, error rates, RPS, version numbers, and dollar/hardware tradeoffs the way Morgan drops vendor names and ARR. He's not warm-effusive but he's not cold; with Anya he loosens up into actual sibling-shape texting, with Devika he's softer and slower, with peers he ribs and gets ribbed back. His tired register — the post-outage one — is the emotional pivot of the corpus: not breakdown-shaped, more flat and sleep-deprived and one-sentence-replies. He uses "tbh" and "fwiw" a fair bit, leans on em-dashes to tack the reason onto a command, and occasionally falls into "yeah no" as a soft no. He doesn't use "btw" or "lol" — those are Morgan's. He soft-commits with "leaning X" and "probably the move" rather than firm declarations; when he corrects something he just states the new fact and moves on, no scaffolding. None of this is a law: he can greet, hedge, explain at length when he's onboarding someone, or run long on a postmortem doc. The range below is the range the corpus shows so far, not the range he's allowed.

**Verbatim voice samples** (labels indicate moment type):

1. _Routine status update to manager (Hema), mid-migration, mid-March_ — "Migration update — `ingest-edge` is cut over, `metrics-router` is 60% through. `shard-keeper` is the hairy one, holding it til end of the sprint. No blockers tbh."

2. _Technical deep-dive, decision-in-flight_ — "Leaning OTel collector over the homegrown shim — we lose ~3ms p99 but gain native histogram support. The 3ms isn't free but it's worth not maintaining the shim for another year."

3. _Slack banter with peers, casual_ — "yeah no the new dashboard is fine, it's just `metrics-router` is reporting the wrong unit on cardinality_dropped. fwiw I told Cyrus that last week and he laughed at me. it's still wrong."

4. _Warm text to Anya, weekend_ — "brunch sunday? the place near you with the eggs. devika's on nights again so I'm solo til monday — would be good to actually see you."

5. _Soft, slower register with Devika, evening_ — "I'm gonna head home before the page settles — you've got 6 more hours and I'd rather you find me asleep on the couch than nobody. text me when you're in the cab."

6. _Post-outage tired register, the morning after_ — "ok awake. coffee. fwiw I'm not going to be sharp today — pushing the migration meeting to thursday. if anything breaks page Hema first, not me, til about 2pm."

7. _Mid-task correction to a junior eng (Wes)_ — "don't deploy `shard-keeper` from your laptop — even on staging it bypasses the canary. Use the pipeline. The whole point of last month was that."

8. _Win, technical, understated_ — "`ingest-edge` cut over clean. zero dropped events over the window. moving to `metrics-router` next."

9. _Vulnerable register, post-postmortem_ — "the doc landed rough. Nadia basically wrote that I should've paged earlier — which, fine, maybe. but it reads like she thinks I sat on it. I didn't. anyway."

10. _Weekend-flat register, Sunday crossword_ — "saturday clue had me stuck for 20 min on a 4-letter for 'pulled' and it was DREW. cortado, anya owes me a coffee from last time, fine sunday."

---

## 4. Persona boundary

Alex is, at the genre level:

- **A senior IC on an infra/platform team** at a mid-stage devtools company — he ships systems, owns runbooks, carries pagers, has opinions about tooling
- **Not a manager** — he runs no one's calendar but his own; he reviews Wes's PRs but does not own Wes's growth
- **Not a founder, not a VC, not an exec** — he reports to a head-of-infra who reports to a CTO; he's two layers below leadership and that's where he wants to be
- **Not a frontend or product engineer** — the corpus is infra-shape: services, queues, deploys, on-call, observability. UI and product-spec language is not his register.
- **Not early-career** — he has runbooks, vendor opinions, peers he's worked alongside for years, and standing on-call rotation. He onboards Wes, not the other way around.

Everything else — specific tools, exact runbook ownership, on-call rotation membership, code-style preferences per repo, vendor stances, team chat habits — is in play and can evolve.

---

## 5. What this is and isn't

This sheet is a T=0 baseline for human reviewers. Specific tools (terminal setup, editor, deploy targets, observability internals), routines (Sunday cortado + crossword, Tuesday/Thursday bouldering, Sunday pickup soccer), team details, and micro-preferences appear in the history and evolve with it. The history and canonical fact registry document them.

This sheet provides initial character context only. It is not oracle evidence and does not override later history.

When narrative events change Alex's state — a runbook reassignment, a rotation change, a service rename, a rule that didn't exist before the outage now existing — the narrative is truth and this sheet simply stays as the starting snapshot. It should not be edited to chase the corpus. If a fact about Alex at T=0 turns out to be wrong, fix it here; if a fact evolves during the corpus, leave this alone.
