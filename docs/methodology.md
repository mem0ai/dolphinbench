# DolphinBench Corpus Methodology

This document describes the released corpus and the rules that future DolphinBench
construction must preserve.

For the exact construction commands and the active data boundary, see
[`../construction/CANONICAL_PIPELINE.md`](../construction/CANONICAL_PIPELINE.md).
New quarters use one accepted quarter plan followed by chained weekly windows;
historical schedules and rewrite artifacts are not active construction inputs.

## 1. Build the history

Each persona has one final history at
`registry/personas/<persona>/life_sim.yaml`. A session contains its date and
one or more user messages. This file is what a submitted memory system ingests
during evaluation.

The current histories were rendered from persona narratives and reviewed for
continuity. The earlier planning files are not release authorities and are not
included in the repository.

## 2. Ground remembered information in sessions

`registry/personas/<persona>/facts.yaml` is the only fact-to-history mapping.
Every fact records what the history establishes, when that statement applies,
and the session IDs whose complete user messages establish it.

The distinction between its two session lists is strict:

- `source_session_ids` are sufficient evidence for the fact and are the only
  sessions shown to a with-memory oracle.
- `related_history_session_ids` may help a human understand the surrounding
  narrative, but they are not needed to establish the fact and are never
  injected into the oracle.

The concise `statement` is for construction and review. It is not a substitute
for history and is never oracle input.

When the history changes, the fact registry must be rebuilt or reconciled
before tests are generated. A test cannot point directly to free-form session
IDs or carry a second copy of fact prose.

## 3. Build the present app world

Each test's `mock_state` is the database exposed by the simulated tools for
that test. Tool behavior is implemented in `mock_mcp/server.py`; each persona's
available tools are listed in `mock_mcp/manifests/<persona>.yaml`.

The correct action may combine three sources:

- parameters explicitly supplied in the current request;
- present information that an available read tool can return;
- remembered information established by the test's source sessions.

The state must not contradict the history. It may contain a superseded value
when the point of the test is to remember a later change, but it must not expose
the current remembered answer when that would remove the need for memory.

## 4. Design an action and its checks

Each test asks for a real tool-mediated action. The query should be natural and
contain enough current-task information to identify the action and its target,
without stating the remembered answer or unnaturally announcing exactly what
to retrieve.

The grading assertions check the parts of the performed action that determine
whether it was correct. Deterministic checks are used for exact values, dates,
numbers, and tool presence. A narrow semantic judge is used when faithful
paraphrases should be accepted. Formatting or wording is checked only when it
changes the requested action or is itself the remembered preference.

## 5. Write the query without the fact

The final user-facing request is written from the action design without access
to the fact statement or source-session text. This separation reduces answer
leakage. The designer still has access to the real tool schemas and current app
state so the task is executable.

## 6. Certify the test

`authoring/certify.py` runs four independent shots through the production tools
and grader:

- two shots receive only the complete messages from every referenced fact's
  `source_session_ids`;
- two shots receive no history.

The test ships only if both with-memory shots pass and both no-memory shots
fail. An infrastructure error is not counted as a task verdict.

This gate establishes empirical solvability and memory necessity. It does not
replace construction review: the query must still be a reasonable task, the
state must be coherent, and the checks must measure the correct action.

## 7. Evaluate a memory system

The evaluation runtime first replays the full persona history into the system
under test. It then runs each of the persona's 200 tests as a fresh task over
the same seeded memory. Each test receives its own simulated app state.

Systems are compared on task accuracy, total ingestion plus test cost, and
median task latency. Provider state must be isolated by run and persona.

## Scaling beyond v1

The same data boundary supports longer histories: create and human-accept a
quarter plan, execute it from the previous `*_final` checkpoint with the
canonical quarter runner, reconcile facts against the resulting complete
history, and only then design tests. At million-token scale, session generation must receive the current
narrative state plus retrieved prior sessions about the same people, projects,
tools, and decisions. That retrieval is construction context, not oracle
evidence. The oracle boundary remains unchanged: complete source sessions only.
