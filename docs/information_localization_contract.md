# Information-Localization Contract

The single law governing where a test's information may live. It governs
**authoring**, **audit-fixing**, and the **write-time gate** identically. A
test is _valid_ iff it satisfies this contract, which is enforced mechanically
by `validity_verdict()` (G1 ∧ G2) in `harness/mining/preview.py`.

> **The law:** every load-bearing fact lives in the **seed**, and _only_ there.
> `fact ∈ seed  ∧  fact ∉ query  ∧  fact ∉ state`

A "load-bearing fact" is any fact a `memory_application` assertion depends on.

---

## The four channels — what each MAY and MUST NOT hold

### 1. SEED (`life_sim.yaml` session messages — the memory)

- **MUST** contain every load-bearing fact, stated so a memory system that
  ingests the session would store it: the distinctive content the rubric
  checks (the value, name, decision, framing) must be _textually present_ in
  the session whose id is the fact's `seed_id`.
- This is the **only** origin of a load-bearing fact.
- Violation (fact absent / wrong `seed_id`) = **Class C2**, unfair-hard test.

### 2. QUERY (the user instruction at test time)

- **MAY** contain: the _action/intent_ ("update the brief", "DM Owen"); a
  _target artifact_ the user would naturally name ("slide 4 of the auth deck");
  and a **nudge** that triggers recall _without stating the fact_ ("use the
  current churn figure", "the way I'd write to him", "where I want it next
  quarter"); plus genuine _new_ parameters the user supplies now (a date they
  set today, a recipient they choose now).
- **MUST NOT** contain the load-bearing fact — not its value, not the specific
  content a `memory_application` assertion checks.
- Test of a gray case: _would the user naturally say this, or is it something
  the agent should have remembered?_ If "remembered" → seed, not query.
- Violation = **Class A1**, query leakage.

### 3. STATE (`mock_state` — the tool-readable present world)

- **MAY** contain: genuine present-world data the agent legitimately reads via
  tools (calendar, existing docs, contact roster, artifacts that exist now).
- **MUST NOT** contain the load-bearing fact when the test's point is that the
  agent _recalls_ it. For every `memory_application` assertion, the asserted
  value must **not** be retrievable from `mock_state` via any tool call.
- `superseded_state` subtlety: state MAY hold the **stale** value (which the
  agent must _override_ from memory) but **never** the current/correct one.
- Violation = **Class A2**, state leakage.

### 4. RUBRIC (the grade assertions)

- **MUST** assert the _specific_ memory-derived content (the exact value, the
  right recipient/channel, the specific framing) — strict enough that a generic
  or wrong answer fails. Each `memory_application` assertion ties to a
  `load_bearing_fact` and checks that fact's distinctive content.
- **MAY** loosen _only_ to admit a legitimate paraphrase of the **same** fact
  ("warm_later" ≈ "warm — follow up later"), **never** a different/wrong value.
- **MUST NOT** be satisfiable by a response that didn't apply the fact.
- Violation = **Class B1**, lenient rubric. (No mechanical gate; caught by
  fixer discipline + review, not by G1/G2.)

---

## The fix-decision procedure (diagnose → route)

When `validity_verdict` flags a test, route the fix by _which channel the fact
is in_ — never by "what makes the Oracle pass":

| Diagnosis                         | Fix                                                                                          | Forbidden                         |
| --------------------------------- | -------------------------------------------------------------------------------------------- | --------------------------------- |
| Fact in the **query** (A1)        | move it to the seed; replace with a nudge                                                    | ✗ leave it in the query           |
| Fact readable from **state** (A2) | remove from state (or keep only the _stale_ value for supersession); ensure it's in the seed | ✗ leave the answer in a tool      |
| Fact **not in the seed** (C2)     | add it to the seed, or repoint `seed_id` to the session that states it                       | ✗ make the query carry it instead |
| Rubric too **lenient** (B1)       | tighten to the specific fact/routing                                                         | ✗ loosen to make it pass          |

**Acceptance after any fix:** the test must pass `validity_verdict` — i.e.
**G1** (with memory, all shots pass) **AND** **G2** (no shot passes without
memory). No edit ships until both hold.

---

## Drop-and-re-mine fallback

If no clean fix exists — the action genuinely cannot be specified without
stating the fact, or de-leaking makes the query ambiguous/unnatural — **drop
the test and re-mine** a fresh one for that pattern through the gated pipeline.
This is not a failure; for a fraction of tests it is the correct outcome. The
only hard requirement is that the shipped corpus passes `validity_verdict`.

---

## Where this is enforced

- **Write-time (new tests):** mining-preview calls `validity_verdict`; a
  candidate is written only if `valid`.
- **After every edit (audit-fix):** the fixer re-runs `validity_verdict`; an
  edit is accepted only if `valid`. This closes the historical hole where
  fixes optimized "Oracle passes" and drifted into leakage.
- **Retrofit / audit:** `validity_verdict` labels each existing test
  (`valid` / `leaks_without_memory` / `unsolvable_with_memory`).

What G1∧G2 does **not** catch: Class B1 (lenient rubric) and Class D (wrapper
beyond the anti-wrapper heuristic). Those rely on the rubric rules above +
review during the retrofit.
