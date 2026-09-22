"""Source-preserving dependency selection for explicitly enabled staged runs."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from authoring.complete_test import source_context, source_records, strict_schema
from authoring.context import _supersession_chain_ids, dump_json
from construction.runtime_model_calls import cached_client_complete


SELECTION_SYSTEM = """Select the evidence dependencies needed to interpret the
selected facts correctly on the evaluation date. This is evidence preparation,
not test planning or a decision that a fact is suitable for testing.

The catalog contains facts sharing subjects with the selected facts and explicit
replacement chains. Shared people or projects do not by themselves make an
entire history relevant. Select every fact that supplies a necessary identity,
qualification, temporal condition, exception, contradiction, or later update to
a selected fact. Keep later explicit instructions that narrow or replace it.
Do not infer expiration merely from age. Do not select unrelated decisions just
because they mention the same colleague. Preserve the original fact meaning.

Code includes mandatory facts and related-history links independently of your
selection. Return additional_fact_ids from the catalog and a concise rationale
explaining dependencies and temporal coverage. You are not writing a summary
that will replace the original messages: code will load them completely.
Required remembered content is as valid as a tool choice or argument. Do not
reject descriptive facts, rewrite any fact, or propose an assistant request.
"""


class DependencySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    additional_fact_ids: list[StrictInt]
    rationale: StrictStr = Field(min_length=1)


def compact_fact(context: Any, fact: dict[str, Any]) -> dict[str, Any]:
    row = {key: copy.deepcopy(fact[key]) for key in (
        "id", "statement", "applies_when", "supersedes", "related_history_session_ids",
    ) if key in fact}
    row["source_dates"] = sorted({
        str(context.sessions_by_id[str(sid)]["narrative_date"])
        for sid in fact.get("source_session_ids", [])
    })
    return row


def _messages(context: Any, fact_ids: set[int]) -> list[dict[str, Any]]:
    sources = source_records(context, fact_ids)
    seen = {row["message_id"] for row in sources}
    related: dict[str, list[int]] = {}
    for fact_id in sorted(fact_ids):
        for sid in context.facts_by_id[fact_id].get("related_history_session_ids", []):
            related.setdefault(str(sid), []).append(fact_id)
    for sid, ids in related.items():
        session = context.sessions_by_id[sid]
        messages = ([session["message"]] if isinstance(session.get("message"), str) else [])
        messages += [m for m in session.get("messages", []) if isinstance(m, str)]
        if not messages:
            raise ValueError(f"related source {sid} has no original user messages")
        for index, message in enumerate(messages):
            message_id = f"{sid}:message:{index}"
            if message_id not in seen:
                sources.append({"source_session_id": sid, "message_id": message_id,
                                "date": str(session["narrative_date"]), "text": message,
                                "fact_ids": ids})
                seen.add(message_id)
    return sorted(sources, key=lambda row: (row["date"], row["message_id"]))


def prepare_evidence(*, context: Any, idea: Any, evaluation_date: str,
                     out: Path, client: Any, cache_root: Path | None = None) -> Any:
    original = source_context(context, idea)
    mandatory = _supersession_chain_ids(context, idea.fact_ids)
    catalog_ids = set(idea.fact_ids) | {row["id"] for row in original["related_updates"]} | mandatory
    catalog = [compact_fact(context, context.facts_by_id[i]) for i in sorted(catalog_ids)]
    payload = {"checkpoint_identity": context.checkpoint_identity,
               "evaluation_date": evaluation_date, "selected_fact_ids": idea.fact_ids,
               "mandatory_fact_ids": sorted(mandatory), "fact_catalog": catalog,
               "selected_original_messages": _messages(context, set(idea.fact_ids))}
    dump_json(out / "request.json", payload)
    if catalog_ids - mandatory:
        identity = {"system": SELECTION_SYSTEM, "payload": payload,
                    "schema": strict_schema(DependencySelection.model_json_schema()),
                    "model": client.model, "reasoning_effort": getattr(client, "reasoning_effort", None)}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        cache = ((cache_root / key) if cache_root is not None else out / "work") / "response_cache.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        with cache.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            raw = cached_client_complete(
                cache, SELECTION_SYSTEM, payload, client,
                response_schema=identity["schema"],
                response_schema_name="dolphinbench_evidence_dependencies_v1",
            )
        if cache != out / "work" / "response_cache.json":
            dump_json(out / "work" / "response_cache.json", json.loads(cache.read_text()))
        selection = DependencySelection.model_validate({k: v for k, v in raw.items() if not k.startswith("_")})
        if len(selection.additional_fact_ids) != len(set(selection.additional_fact_ids)):
            raise ValueError("dependency selection repeats a fact ID")
        if set(selection.additional_fact_ids) - catalog_ids:
            raise ValueError("dependency selection names a fact outside its catalog")
        selected = mandatory | set(selection.additional_fact_ids)
        # A selected dependency cannot lose its own replacement chain.
        selected = _supersession_chain_ids(context, sorted(selected))
    else:
        selection = DependencySelection(additional_fact_ids=[], rationale="Only mandatory dependencies exist.")
        selected = mandatory
    packet = {"selected_facts": copy.deepcopy(original["selected_facts"]),
              "sources": _messages(context, selected),
              "related_updates": [compact_fact(context, context.facts_by_id[i])
                                  for i in sorted(selected - set(idea.fact_ids))],
              "related_fact_catalog": catalog,
              "evidence_selection": {"version": 1, "fact_ids": sorted(selected),
                                     "rationale": selection.rationale}}
    packet_hash = hashlib.sha256(json.dumps(packet, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    dump_json(out / "packet.json", {"sha256": packet_hash, "packet": packet})
    return replace(context, evidence_packets={tuple(sorted(idea.fact_ids)): packet})
