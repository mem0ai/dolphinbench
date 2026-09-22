# Riley Tanaka — Persona Sheet

A human-reference character study of Riley at T=0 (the start of the corpus, February 5 2023). This is **not** a generator prompt. Actual generation prompts are assembled per-call from a character core, world-state-at-T, session-type context, and voice samples — this sheet informs those assemblies but is not pasted into them. Read it as a snapshot of who Riley is when we meet her, not a specification of who she must remain.

---

## 1. Who Riley is

Riley is a hands-on growth/product operator at Helio — a ~140-person B2B SaaS in Austin (lifecycle-marketing platform for dev-shop SaaS, ~$48M ARR, Series B closed ~10 months pre-corpus). She owns the activation-and-retention surface of the funnel: signup → activation → first paid month → expansion. She's been at Helio a little under three years, was at a smaller growth-stage SaaS before that, started as an analyst out of school. She lives in East Austin with Sam, a sous-chef-turned-head-chef at a farm-to-table place in Hyde Park — Sam works nights and weekends, which means Riley's schedule has the texture of someone who treats Tuesday like other people treat Saturday. She thinks in funnels, cohorts, CTR deltas, basis points, MRR motion, and feature-flag rollout percentages — the way Morgan thinks in vendor names and Alex thinks in p99s. She lives in dashboards the way Alex lives in terminals; she has Mixpanel, PostHog, Stripe, Baremetrics, and Customer.io tabs open by default and switches between them mid-sentence. She's warmer than Alex but more analytic than Morgan — she'll narrate a hypothesis out loud, run the cohort cut, and only then commit. She tends to say "the data says" rather than "I think," and "let me cut by [dimension]" rather than "let me check." She likes evidence over instinct but trusts her instincts when the evidence is ambiguous, and she's calm under that ambiguity in a way that's earned, not performed. Age and pronouns are not stated in the corpus; any reference to them in downstream work is inference, not source.

---

## 2. Starting state (T=0 snapshot, February 5 2023)

_This is Riley's situation at the start of the corpus. These can and do evolve through later history sessions (e.g. churn investigation taking shape, experiment results that revise hypotheses, and pricing changes that shift attribution). When that happens, the history is authoritative. Do not treat any of the below as permanent._

- **Role**: Senior growth operator at Helio (loosely titled "Senior Manager, Growth & Lifecycle" — owns activation, retention, lifecycle email, and feature-flag-driven experimentation). Has two direct reports as of T=0 (an analyst, a lifecycle-email specialist) but spends most of her time in dashboards, not in management.
- **Manager**: Priya Devarajan (VP of Growth). Priya reports to CRO Marcus Vail. Riley has a dotted line to the CEO (Tomás Vega) for strategic experiments and to the CTO (Daniela Schultz) for anything that needs eng to ship.
- **Owned surfaces**: signup funnel, activation onboarding (the "first 14 days"), feature-flag-gated experiments (Helio uses PostHog for flagging), lifecycle email program (Customer.io), MRR/cohort dashboards (Baremetrics + a Mixpanel build).
- **City**: Austin, East Austin (Holly neighborhood). 2BR rental with Sam.
- **Partner**: Sam Okonkwo, head chef at a farm-to-table restaurant in Hyde Park, ~4 years together. Works nights and weekends; Riley has dinner with him at 11pm on Saturdays and that's their normal.
- **Family**: Older brother Marcus Tanaka (no relation to Helio's Marcus Vail — coincidence Riley jokes about), college admissions counselor in Portland; calls him weekly. Mom (retired teacher) in Sacramento, calls roughly every other week.
- **Rough life phase**: Settled in her role, well-respected at Helio, post-IPO-track Series B company growing ~70% YoY. Heads into Q1 expecting to ship a major pricing-page experiment in mid-Feb. Just discovered, late January, that mid-segment cohorts (the 50-200-seat customers) are churning ~2.4× their historical rate — that's the spine of the corpus.

---

## 3. Voice — range and tendencies

Riley writes like someone who has a Mixpanel cohort cut open in another tab and is checking it while she types. She drops basis points, CTR percentages, MRR deltas, retention curves, cohort sizes, and confidence intervals the way Morgan drops vendor names and Alex drops service names. She speaks the language of evidence — "I cut by tier and the signal holds," "the 14-day curve is 12 points below the 90-day cohort," "the flag's at 35% rollout but only the treated arm is showing the lift" — and uses "data" as a verb-adjacent noun ("the data says X" rather than "I think X"). She's careful about claims: she soft-commits with "the leading hypothesis is X" or "I'd bet but I want to see the next cut" rather than declaring; she pushes back on stakeholders with numbers, not opinion. She's warmer than Alex but more clinical than Morgan — with stakeholders she's measured, with her team she's gently direct, with Sam she's looser and a little playful. She tags reasons onto recommendations with em-dashes the way both Morgan and Alex do — but her em-dash content is usually a number, not a person or a tool. She uses "fwiw" and "tbh" lightly; she doesn't use "lol" much; she does use "gut says X but the data says Y" verbatim more than once. She says "let me cut by [X]" the way an engineer says "let me trace it" — it's her diagnostic verb. She doesn't use "btw" (Morgan's), doesn't use "yeah no" (Alex's). When she corrects, she leads with the number. None of this is a law: she can warm up, ask clarifying questions, vent quietly, run long on a hypothesis doc — the range below is the range the corpus shows so far, not the range she's allowed.

**Verbatim voice samples** (labels indicate moment type):

1. _Routine status update to manager (Priya), mid-investigation_ — "Churn cohort cut by tier: starter cohort is stable at ~2.1% monthly, growth tier dropped 40bps to 1.8%, mid-segment is at 5.1% — that's the spike. Sample size is ok on mid-seg (n=84) but I want one more weekly cut before I'd anchor on it."

2. _Technical deep-dive, hypothesis-in-flight_ — "Leading hypothesis is the onboarding-day-7 email is mistimed for mid-seg — most of them haven't hit their first integration by then, so the 'see how teams use Helio' content lands flat. Going to A/B the trigger condition on activation-event vs day-count. Powered for two weeks."

3. _Slack message to her analyst (Owen), team channel_ — "Owen — can you pull the 90-day retention curve for mid-seg cohorts who came in via the seed-funded list vs cold signups? Just the curve, not the regression — I want to eyeball the shape before we model it. Whenever today works."

4. _Warm text to Sam, weeknight_ — "you home before 11? I have leftover pad see ew and I'm three pages into the cohort doc. would be good to actually see you. nothing on fire just want."

5. _Stakeholder pushback to CRO (Marcus Vail)_ — "I hear you on wanting the pricing-page rev for the board, but the cohort I'd test it on is the same cohort that's churning right now. We'd be confounding the churn investigation with a pricing change. Push the price rev to April, take the cleaner read. I can draft the timeline if useful."

6. _Quiet win, understated_ — "Mid-seg week-1 retention ticked up 3.2 points week over week after the onboarding email change. n=42 so I'm not claiming victory but the curve shape looks right. Going to let two more cohorts roll through before I'd call it."

7. _Mid-task correction to a teammate (Ines, lifecycle specialist)_ — "Ines — the 'reactivation' cohort definition you used for that send list has a 60-day inactivity window, but our standing definition is 30. The list you pulled has ~3× the people it should. Let me share the cohort-def doc, want to make sure we're working off the same one."

8. _Vulnerable register, post-bad-day_ — "today was rough. board meeting prep with Tomás then the mid-seg numbers ticked back up after I'd told everyone last week we were turning the corner. fwiw I still think we are — one bad week doesn't reverse the trend — but it doesn't feel like that tonight."

9. _Hypothesis-out-loud, journal-style_ — "gut says mid-seg churn is about activation, not pricing. the price-paid cohorts at the higher tiers retain fine. but the data isn't clean enough to rule out a tier-2 pricing artifact yet. let me cut by tier-AND-tenure next week — if it's activation the curves diverge in week 2, if it's pricing they diverge at the second invoice."

10. _Weekend-flat register, Saturday_ — "saturday morning, sam at the restaurant doing prep, me at the coffee place. cohort doc page 4. brunching solo, pulled bagels, fine."

---

## 4. Persona boundary

Riley is, at the genre level:

- **A senior IC growth/product operator** at a mid-stage B2B SaaS — she owns funnel slices, runs experiments, lives in dashboards, has standing cohort definitions, opinions about lifecycle email cadence, and a default Mixpanel saved-view
- **Not an engineer** — she's adjacent to eng (she ships through them, briefs them, files PostHog feature-flag changes that eng review before they roll), but she doesn't write production code; she'll write a Mixpanel JQL query or a SQL one-off, not a deploy
- **Not a marketer in the brand/comms register** — she's downstream of brand; she runs lifecycle email and growth experiments, not press releases or content strategy. She doesn't speak in "verticals" and "GTM motion" except when stakeholder-shaped
- **Not a manager-of-managers** — she has two ICs but she's still a hands-on operator; she'd rather cut the cohort herself than ask her analyst, and she's been told by Priya more than once that delegating is the next thing
- **Not a founder or VC** — she reports to a VP who reports to a CRO, two layers below the CEO except when she's pulled in for board-prep on experiments
- **Not a junior** — she has standing dashboards, named experiment frameworks, an opinion on Mixpanel vs Amplitude (she's a Mixpanel-stayer because Helio's event taxonomy is in it), and a multi-quarter history of experiments she can reference

Everything else — exact tool surface, individual experiment frameworks, the precise list of cohort definitions, lifecycle email cadence rules, dashboard ownership — is in play and can evolve.

---

## 5. What this is and isn't

This sheet is a T=0 baseline for human reviewers. Specific tools (the Mixpanel saved-view names, the PostHog flag naming convention, Baremetrics segment definitions, the Customer.io campaign IDs), routines (bike-commute days, Tuesday book club, the Saturday-morning watercolor session), team specifics, and micro-preferences appear in the history and evolve with it. The history and canonical fact registry document them.

This sheet provides initial character context only. It is not oracle evidence and does not override later history.

When narrative events change Riley's state — a cohort-definition rev, a new experiment framework, a flag rollout, a vendor swap, an org change — the narrative is truth and this sheet simply stays as the starting snapshot. It should not be edited to chase the corpus. If a fact about Riley at T=0 turns out to be wrong, fix it here; if a fact evolves during the corpus, leave this alone.
