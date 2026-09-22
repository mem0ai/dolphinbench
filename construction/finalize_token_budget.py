"""Create an authenticated release checkpoint at a complete-session token boundary."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from construction.build_history_window import update_unfinished_threads
from construction.checkpoints import (
    data_hash,
    dump_json,
    file_hash,
    history_tokens,
    load_checkpoint,
    load_json,
    save_checkpoint,
    validated_checkpoint_identity,
)
from construction.quarter_state import project_app_operations


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Finalize a corpus at the first complete session at or above a token target."
        )
    )
    parser.add_argument("--persona", required=True)
    parser.add_argument("--source-checkpoint", type=Path, required=True)
    parser.add_argument("--base-checkpoint", type=Path, required=True)
    parser.add_argument("--window-plan", type=Path, required=True)
    parser.add_argument("--target-tokens", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tool-python", type=Path, required=True)
    return parser.parse_args()


def first_complete_session_at_or_above(
    history: list[dict[str, Any]], target_tokens: int
) -> tuple[int, int]:
    if target_tokens <= 0:
        raise ValueError("target tokens must be positive")
    tokens = 0
    for index, session in enumerate(history, start=1):
        tokens += history_tokens([session])
        if tokens >= target_tokens:
            return index, tokens
    raise ValueError(
        f"source history has {history_tokens(history)} tokens, below target {target_tokens}"
    )


def referenced_session_ids(row: dict[str, Any]) -> list[str]:
    return [
        *[str(value) for value in row.get("source_session_ids") or []],
        *[str(value) for value in row.get("related_history_session_ids") or []],
    ]


def main() -> None:
    args = parse_args()
    source_checkpoint = args.source_checkpoint.resolve()
    base_checkpoint = args.base_checkpoint.resolve()
    window_plan_path = args.window_plan.resolve()
    output = args.output.resolve()
    if output.exists() or output.with_name(output.name + ".pending").exists():
        raise ValueError(f"output already exists: {output}")

    source_identity = validated_checkpoint_identity(source_checkpoint)
    base_identity = validated_checkpoint_identity(base_checkpoint)
    source_metadata, source_state = load_checkpoint(source_checkpoint)
    base_metadata, base_state = load_checkpoint(base_checkpoint)
    source_operations = load_json(source_checkpoint / "operation_results.json")
    base_operations = load_json(base_checkpoint / "operation_results.json")
    if not isinstance(source_operations, list) or not isinstance(base_operations, list):
        raise ValueError("checkpoint operation results must be lists")

    cutoff_count, actual_tokens = first_complete_session_at_or_above(
        source_state["history"], args.target_tokens
    )
    retained_history = copy.deepcopy(source_state["history"][:cutoff_count])
    retained_ids = {str(row["id"]) for row in retained_history}
    if len(retained_ids) != len(retained_history):
        raise ValueError("retained session IDs must be unique")
    if source_state["history"][: len(base_state["history"])] != base_state["history"]:
        raise ValueError("base checkpoint history is not an exact source-history prefix")
    if len(base_state["history"]) >= cutoff_count:
        raise ValueError("base checkpoint must precede the selected cutoff")

    suffix = retained_history[len(base_state["history"]) :]
    suffix_ids = [str(row["id"]) for row in suffix]
    window_plan = load_json(window_plan_path)
    planned_by_id = {
        str(row["session_id"]): row for row in window_plan.get("sessions") or []
    }
    missing_planned = [session_id for session_id in suffix_ids if session_id not in planned_by_id]
    if missing_planned:
        raise ValueError(f"window plan is missing retained sessions: {missing_planned}")
    partial_sessions = [copy.deepcopy(planned_by_id[session_id]) for session_id in suffix_ids]
    for history_row, planned_row in zip(suffix, partial_sessions, strict=True):
        expected = {
            "id": planned_row["session_id"],
            "narrative_date": planned_row["narrative_date"],
            "messages": planned_row["messages"],
        }
        if history_row != expected:
            raise ValueError(
                f"source history does not match the accepted plan for {history_row['id']}"
            )

    partial_plan = {
        "period_start": partial_sessions[0]["narrative_date"][:10],
        "period_end": partial_sessions[-1]["narrative_date"][:10],
        "new_entities": [
            copy.deepcopy(row)
            for row in window_plan.get("new_entities") or []
            if str(row.get("introduced_in_contact_id") or "")
            in {str(session["contact_id"]) for session in partial_sessions}
        ],
        "sessions": partial_sessions,
    }

    work_dir = output.parent / "replay"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    projected_state, replayed_operations, _ = project_app_operations(
        persona=args.persona,
        plan={"contacts": partial_sessions},
        starting_state=base_state["app_state"],
        work_dir=work_dir,
        tool_python=args.tool_python,
    )
    retained_operations = [
        copy.deepcopy(row)
        for row in source_operations
        if str(row.get("session_id") or "") in retained_ids
    ]
    expected_operations = [*base_operations, *replayed_operations]
    if retained_operations != expected_operations:
        raise ValueError("replayed operations do not match the accepted source prefix")

    retained_purposes = [
        copy.deepcopy(row)
        for row in source_state["session_purposes"]
        if str(row.get("session_id") or "") in retained_ids
    ]
    if [str(row["session_id"]) for row in retained_purposes] != [
        str(row["id"]) for row in retained_history
    ]:
        raise ValueError("retained session purposes are incomplete or out of order")
    retained_entities = [
        copy.deepcopy(row)
        for row in source_state["entities"]
        if not row.get("introduced_in") or str(row["introduced_in"]) in retained_ids
    ]
    retained_facts = [
        copy.deepcopy(row)
        for row in source_state["facts"]
        if all(reference in retained_ids for reference in referenced_session_ids(row))
    ]
    for fact in retained_facts:
        missing_supersedes = [
            value
            for value in fact.get("supersedes") or []
            if value not in {row.get("id") for row in retained_facts}
        ]
        if missing_supersedes:
            raise ValueError(
                f"retained fact {fact.get('id')} supersedes removed facts {missing_supersedes}"
            )

    retained_state = {
        "history": retained_history,
        "session_purposes": retained_purposes,
        "entities": retained_entities,
        "facts": retained_facts,
        "app_state": projected_state,
        "unfinished_threads": update_unfinished_threads(
            base_state.get("unfinished_threads") or [], partial_plan
        ),
    }
    cutoff_contract = {
        "target_tokens_o200k_base": args.target_tokens,
        "selection": "first complete session at or above target",
        "cutoff_session_id": retained_history[-1]["id"],
    }
    pending = output.with_name(output.name + ".pending")
    try:
        save_checkpoint(
            path=pending,
            persona=args.persona,
            week={
                "week_id": f"{args.persona}_{args.target_tokens}_token_release",
                "end_date": retained_history[-1]["narrative_date"][:10],
            },
            previous_hash=source_identity,
            inputs={
                "source_checkpoint": source_identity,
                "base_checkpoint": base_identity,
                "window_plan.json": file_hash(window_plan_path),
                "cutoff_contract": data_hash(cutoff_contract),
            },
            state=retained_state,
            operation_results=retained_operations,
            covered_events=list(
                dict.fromkeys(
                    [
                        *[str(value) for value in base_metadata.get("covered_quarter_event_ids") or []],
                        *[
                            str(value)
                            for session in partial_sessions
                            for value in session.get("development_ids") or []
                        ],
                    ]
                )
            ),
            system_sha256=data_hash(
                {
                    "finalizer": file_hash(Path(__file__)),
                    "source_checkpoint": source_identity,
                }
            ),
            session_budget={
                **cutoff_contract,
                "actual_tokens_o200k_base": actual_tokens,
            },
            revalidated_against_current_state=True,
            progress={"construction_path": "deterministic_release_cutoff"},
            identity_version=2,
        )
        final_identity = validated_checkpoint_identity(pending)
        pending.rename(output)
    except Exception:
        if pending.exists():
            shutil.rmtree(pending)
        raise

    receipt = {
        "version": 1,
        "persona": args.persona,
        "source_checkpoint": str(source_checkpoint.relative_to(ROOT)),
        "source_checkpoint_identity": source_identity,
        "release_checkpoint": str(output.relative_to(ROOT)),
        "release_checkpoint_identity": final_identity,
        "selection": cutoff_contract,
        "retained": {
            "sessions": len(retained_history),
            "user_messages": sum(len(row.get("messages") or []) for row in retained_history),
            "tokens_o200k_base": actual_tokens,
            "facts": len(retained_facts),
            "entities": len(retained_entities),
            "operations": len(retained_operations),
        },
        "removed": {
            "sessions": len(source_state["history"]) - len(retained_history),
            "user_messages": sum(
                len(row.get("messages") or []) for row in source_state["history"]
            )
            - sum(len(row.get("messages") or []) for row in retained_history),
            "tokens_o200k_base": source_metadata["history_tokens_o200k_base"]
            - actual_tokens,
            "facts": len(source_state["facts"]) - len(retained_facts),
            "entities": len(source_state["entities"]) - len(retained_entities),
            "operations": len(source_operations) - len(retained_operations),
        },
    }
    dump_json(output.parent / "finalization.json", receipt)
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
