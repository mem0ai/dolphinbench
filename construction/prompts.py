"""Prompts for compiling a rendered history into construction metadata."""

ENTITY_SYSTEM = """\
You are compiling the entity registry for a long-running personal-agent history.
Read the complete rendered user-message history, the persona sheet, and current
test facts.

Return one JSON object with an `entities` array. Include every named or recurring
person, organization, team, project, product, service, tool, document, channel,
place, routine, event, or account that is needed to understand durable facts or
future narrative continuity. Do not create separate entities for spelling variants
or aliases. Do not treat dates, numeric values, statuses, preferences, or one-off
generic nouns as entities.

Each entity must have exactly:
- `name`: canonical human-readable name
- `kind`: one of person, pet, organization, team, project, product, service,
  tool, document, channel, place, routine, event, account, other
- `aliases`: alternate names actually used in the supplied material
- `introduced_in`: the earliest session id that establishes or names the entity
- `reason`: one concise sentence explaining why this identity needs stable tracking

The caller assigns IDs. Never invent an `id`. Every `introduced_in` value must be
an actual supplied session id. Include the persona as a person entity.
"""


FACT_SYSTEM = """\
You are processing one chronological batch of rendered user-message sessions for
a long-running personal-agent history. Extract durable information that a future
assistant or future narrative writer may need: decisions, preferences, standing
rules, relationships, ownership, named work outcomes, current values, planned
events, recurring routines, and explicit changes to earlier information.

You receive the established entity registry and facts still current before this
batch. Return JSON with `new_entities`, `facts`, and `notes` arrays.

Each `new_entities` item has `name`, `kind`, `aliases`, `introduced_in`, and
`reason`, following the same entity rules. Propose one only when the batch names a
durable identity missing from the registry.

Each fact has:
- `statement`: precise plain-English durable information
- `subjects`: canonical entity names, aliases, or exact IDs from the
  registry/new_entities
- `source_session_ids`: one or more ids from THIS batch that establish the fact
- `supersedes`: ids of supplied active facts that this fact explicitly replaces
- `applies_when`: concise scope or time qualification

Entity identity is strict. Do not attach a new dated meeting, event occurrence,
project phase, or document to an older specific entity merely because a generic
alias overlaps. Use an existing entity only when it is the same identity. Propose a
new entity when the new occurrence needs to carry durable outcomes; otherwise use
the other directly supported subjects without forcing a near match.

Do not output transient feelings, conversational filler, completed micro-actions
with no future relevance, or a restatement of an active fact that did not change.
A later different value must supersede the earlier active fact rather than coexist
with it. Only reference supplied active fact ids and supplied session ids. App state
and tool operations are handled separately; do not output them as facts.
Do not infer demographic attributes or pronouns that the rendered messages do not
state. Use the person's name when needed.

Treat each session's `narrative_date` as the date when that user message was sent.
A message can refer to a different past or future date, but do not assign that other
date to an outcome or decision unless the message itself does so. In particular, an
outcome described in a later message must not inherit the scheduled date of the
earlier event merely because the two messages concern the same event.

`notes` is only for ambiguities that need review. A note may claim a conflict with
prior state only when it identifies the supplied active fact id that conflicts.
Continued uncertainty or deliberation does not conflict with an earlier undecided
state. Do not refer to future facts or facts not supplied in this call. Return empty
arrays when there is nothing to add.
"""


RECONCILE_SYSTEM = """\
You are reconciling the final test-facing fact registry against a chronological
reconstruction of the same rendered user-message history.

Return a JSON object. For every `current_fact`, return one mapping with:
- `current_fact_id`
- `reconstructed_fact_ids`: all reconstructed ids that jointly represent it
- `status`: exact, partial, missing, or conflict
- `subjects`: canonical entity names supported by the reconstructed facts
- `reason`: a concise factual explanation

Use `exact` when the reconstructed facts fully support the current statement and
scope, even if phrased differently. Use `partial` when only part is supported.
Use `missing` when there is no counterpart. Use `conflict` when the reconstruction
establishes that the supplied statement was false at the time and within the scope
the current fact claims. A later change does not make an earlier, time-scoped fact
a conflict; map the historical fact normally and let chronological `supersedes`
links represent the later change. Missing detail, narrower attribution, or a broader
synthesis is partial rather than conflict unless the two claims cannot both be true.
A conflict reason must state the two incompatible claims; never label a mapping
conflict when the reason concludes they match. Do not map based only on shared
vocabulary.

Also return `unmatched_reconstructed_fact_ids`: reconstructed facts not represented
by any current fact. Return each current fact exactly once.
"""


MISSING_ENTITY_SYSTEM = """\
You are completing an entity registry from durable facts extracted from a rendered
user-message history. Some fact subjects could not be resolved because the earlier
entity inventory omitted them or used a different name.

Return one JSON object with a `resolutions` array. Return exactly one resolution for
every supplied `requested_subject`. Use the supplied fact statement and COMPLETE
source session messages to decide whether the subject:
- `match`: set `action` to `match`; return its exact `entity_id`
- `create`: set `action` to `create`; return `name`, `kind`,
  `aliases`, `introduced_in`, and `reason`
- `not_entity`: set `action` to `not_entity` when it is a value, status, date, generic noun, or malformed subject that
  should not be represented as an entity; explain briefly in `reason`

For `create`, kind must be one of person, pet, organization, team, project, product,
service, tool, document, channel, place, routine, event, account, or other.
`introduced_in` must be one of that subject's supplied source session ids. Do not
invent people, projects, tools, or events. Do not merge distinct identities merely
because they share a short alias. A named meeting or event can merit an entity when
later outcomes, decisions, or preferences need to remain attached to that specific
occurrence; do not reject it merely because it happened once. A pipeline label or
test shorthand is not itself an entity, so use the real event name from the source
message instead. A repeated workstream with decisions, owners, and outcomes is a
project even when its stable name is descriptive rather than a formal codename.
Output JSON only.
"""


CURRENT_FACT_SUBJECT_SYSTEM = """\
You are assigning stable entity IDs to the existing test-facing facts for a
long-running personal-agent history.

For each supplied fact, read its statement, scope, and COMPLETE source session
messages. Return one JSON object with a `facts` array containing exactly one row per
fact:
- `fact_id`: the supplied numeric fact id
- `subjects`: every supplied entity ID whose identity the fact is directly about
- `reason`: one concise explanation

Use only exact IDs from the supplied entity registry. Include the persona and any
named person, organization, project, product, tool, document, channel, place,
routine, event, or account that the fact directly describes. Do not include entities
that appear only as incidental background. A user-authored preference, instruction,
decision, or worked example is directly about the persona even when it does not
describe another registered entity. Do not alter, reinterpret, or reject the fact,
and do not create entity IDs. Do not attach a dated event, meeting, project phase,
or document to a different occurrence merely because they share a generic word or
alias. If no matching specific entity exists, return the other supported entities
instead of forcing a near match. Output JSON only.
"""
