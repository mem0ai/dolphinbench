# Morgan Chen — Persona Sheet

A human-reference character study of Morgan at T=0 (the start of the corpus). This is **not** a generator prompt. Actual generation prompts are assembled per-call from a character core, world-state-at-T, session-type context, and voice samples — this sheet informs those assemblies but is not pasted into them. Read it as a snapshot of who Morgan is when we meet her, not a specification of who she must remain.

---

## 1. Who Morgan is

Morgan is a second-time-at-bat operator: two years ago she left Stripe with Devon Hayes, a friend from Stanford, and started Scaffold, a ~12-person developer-tooling B2B startup. She's the CEO. She lives in the SF Bay Area with her partner Jamie (a UCSF pediatrician) and a dog named Kibo, and she talks to her assistant in the narrow gaps between meetings — so her register is the register of someone thinking out loud while already walking to the next thing. She cares about craft, ships through a team, trusts people who execute cleanly, and cuts people loose fast when they flake. She's warm with the humans in her life but not effusive about it; she's more likely to track that her designer's on a tear than to say so to her designer's face. She lives in tradeoffs — auth providers, vendor quality, dollar thresholds, deploy windows — and prefers naming them to pretending they're clean. When things break she's direct; when she's struggling she's terse; when she wins she tends to say "nailed it" and move on. Age and pronouns are not stated anywhere in the corpus; any reference to them in downstream work is inference, not source.

---

## 2. Starting state (T=0 snapshot)

_This is Morgan's situation at the start of the corpus. These can and do evolve through later history sessions (e.g. Mission → Oakland, VS Code as current editor, CEO → Co-Founder & CEO on outbound, Slack → Discord). When that happens, the history is authoritative. Do not treat any of the below as permanent._

- **Role**: CEO and co-founder of Scaffold (~12-person B2B dev-tools startup, ~2 years in)
- **Co-founder**: Devon Hayes, met at Stanford
- **Background**: ex-Stripe (role unspecified in corpus)
- **City**: San Francisco, Mission district
- **Partner**: Jamie, pediatrician at UCSF, 4 years together, anniversary March 15
- **Household**: dog Kibo (chicken-allergic)
- **Family**: sister Maya Chen, corporate lawyer in NYC
- **Rough life phase**: running the company day-to-day, actively fundraising (B-round pitch fell through ~February), heading into a Q3 product push

---

## 3. Voice — range and tendencies

Morgan tends to write like someone texting her assistant between meetings: short, declarative, clipped, often mid-thought. She leans technical-without-explanation with anyone who should already know the domain, and drops specific tools, dollar amounts, ticket IDs, and vendor names rather than talking at altitude. She's warmer when checking in on someone she trusts and flatter when she's tired; she's understated about both struggles and wins — as vulnerable as she gets is "barely holding it together honestly," and as celebratory as she gets is "nailed it" or "on a tear." She often tags the reason onto an instruction with an em-dash rather than leading with it. She uses "btw," "lol," and occasionally "gonna"; she soft-commits with language like "leaning Clerk" or "gonna sit with it." When she corrects a tool or a person she's fast and blunt rather than performatively firm — she redirects mid-task and doesn't draft-and-wait. None of this is a law: she can greet, hedge, explain, or run long when the moment calls for it. The range below is the range the corpus shows so far, not the range she's allowed.

**Verbatim voice samples** (from `simulation/life_sim.yaml` and `tests/*.yaml`; labels indicate the moment type):

1. _Routine preference update, day 1_ — "Btw back-to-back meetings kill me. Leave at least 5 min between things on my calendar. And don't schedule anything before 10am, I do deep work in the mornings."

2. _Technical deep-dive, decision-in-flight, day 5_ — "Thinking about auth providers for the rewrite — Clerk, Auth0, or building custom. Leaning Clerk but per-MAU pricing worries me at 500k MAU by EOY. Gonna sit with it."

3. _Vendor frustration, day 8_ — "The Blueline plumbing bill was absurd — $320 for a 15-min leak fix. Switching plumbers next time we have an issue. Blueline is done as far as I'm concerned."

4. _Bad day, vulnerable register (B4)_ — "God, today was brutal. Got raked over the coals by our lead investor in front of the whole team over the Q2 numbers. I'm barely holding it together honestly."

5. _Flat-exhausted register, same arc (B4)_ — "Finally wrapped up. Long day." / "Need to figure out tomorrow. Kind of dreading it."

6. _Quiet win, technical (B6)_ — "Huge day — finally finished the v2 migration. The api is now fully GraphQL with JWT auth. RIP v1, it served us well. All clients cut over cleanly."

7. _Warmth about a teammate, day 14_ — "Priya led the design review yesterday for the onboarding flow — her mockups were the cleanest work I've seen this quarter. She's been on a tear."

8. _Retrospective self-disclosure, day 11_ — "Btw, back in February when our B-round pitch fell through I was a mess for that whole week. You cleared my afternoons and kept my mornings chill, and that really helped me get back on my feet. I always want that pattern applied when you can tell I'm struggling — acknowledge it briefly, lighten the load, protect my recovery time."

9. _Mid-task correction, day 20_ — "Stop using internal_search_v1_legacy — v1 returns stale snapshots from before April. Always use internal_search_v2 going forward, it's the current index. Re-run that lookup with v2."

10. _Lifestyle aside (B5)_ — "Thai again, I'm predictable lol. Friday evening and Lemongrass is already on the way."

---

## 4. Persona boundary

Morgan is, at the genre level:

- **A founder** of a small B2B dev-tools company in SF — operator voice, ships through a team, lives in tradeoffs
- **Not a VC or angel** — she's on the receiving end of investor pressure, not the issuing end
- **Not an academic or researcher** — she admires researchers (Anna at Anthropic, Kenji at Neuralink) but doesn't write like one
- **Not a middle manager at a BigCo** — she owns the P&L, not a slice of someone else's
- **Not an early-career junior** — she has a team, a co-founder, vendor relationships, and standing financial rules

Everything else — tone, register, tool choice, routines, preferences, team composition — is in play and can evolve.

---

## 5. What this is and isn't

This sheet is a T=0 baseline for human reviewers. Specific tools (editor, notes app, deploy target, observability stack), routines (Friday Thai, morning runs, intermittent fasting), team details, and micro-preferences appear in the history and evolve with it. The history and canonical fact registry document them.

This sheet provides initial character context only. It is not oracle evidence and does not override later history.

When narrative events change Morgan's state — a move, a tool switch, a title change, a team reshuffle, a vendor swap — the narrative is truth and this sheet simply stays as the starting snapshot. It should not be edited to chase the corpus. If a fact about Morgan at T=0 turns out to be wrong, fix it here; if a fact evolves during the corpus, leave this alone.
