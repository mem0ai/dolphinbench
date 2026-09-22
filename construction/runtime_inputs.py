"""Read-only inputs shared by the canonical quarter construction runtime."""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text()) or {}


def load_seed_session_purposes(seed_dir: Path) -> list[dict[str, str]]:
    """Load saved purposes or reconstruct them from accepted source labels."""
    purposes_path = seed_dir / "session_purposes.json"
    if purposes_path.is_file():
        rows = json.loads(purposes_path.read_text())
        if not isinstance(rows, list):
            raise ValueError("session_purposes.json must contain a list")
        return rows

    manifest_path = seed_dir / "manifest.yaml"
    if not manifest_path.is_file():
        return []
    manifest = load_yaml(manifest_path)
    source_value = (
        manifest.get("source_hashes", {})
        .get("life_sim_sessions", {})
        .get("path")
    )
    if not source_value:
        return []
    source_path = Path(str(source_value))
    if not source_path.is_absolute():
        source_path = ROOT / source_path

    source_sessions = load_yaml(source_path).get("sessions") or []
    source_by_id = {str(row["id"]): row for row in source_sessions}
    seed_sessions = load_yaml(seed_dir / "life_sim.yaml").get("sessions") or []
    seed_by_id = {str(row["id"]): row for row in seed_sessions}
    mappings = load_yaml(seed_dir / "session_id_map.yaml").get("mappings") or []

    rows: list[dict[str, str]] = []
    for mapping in mappings:
        source_id = str(mapping.get("source_id") or "")
        target_id = str(mapping.get("target_id") or "")
        source = source_by_id.get(source_id)
        target = seed_by_id.get(target_id)
        if source is None or target is None:
            raise ValueError(
                f"seed session-purpose mapping does not resolve: {source_id} -> {target_id}"
            )
        purpose = " ".join(str(source.get("label") or "").split())
        if not purpose:
            purpose = " ".join(
                str((source.get("generator_metadata") or {}).get("notes") or "").split()
            )
        if not purpose:
            raise ValueError(f"source session has no purpose label: {source_id}")
        rows.append(
            {
                "session_id": target_id,
                "narrative_date": str(target["narrative_date"]),
                "purpose": purpose,
            }
        )
    return rows


def exact_persona_tools(persona: str) -> dict[str, dict[str, Any]]:
    """Return exact signatures for every simulated tool enabled for a persona."""
    manifest = load_yaml(ROOT / "mock_mcp" / "manifests" / f"{persona}.yaml")
    enabled = set(manifest.get("tools") or [])
    contracts = load_yaml(ROOT / 'mock_mcp' / 'tool_contracts.yaml').get("tools") or {}
    tree = ast.parse((ROOT / "mock_mcp" / "server.py").read_text())
    schemas: dict[str, dict[str, Any]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in enabled:
            continue
        positional = list(node.args.posonlyargs) + list(node.args.args)
        positional_required = len(positional) - len(node.args.defaults)
        keyword_required = len(node.args.kwonlyargs) - sum(
            default is not None for default in node.args.kw_defaults
        )
        arguments = [argument.arg for argument in positional + list(node.args.kwonlyargs)]
        required = [argument.arg for argument in positional[:positional_required]]
        if keyword_required:
            required.extend(
                argument.arg
                for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults)
                if default is None
            )
        schemas[node.name] = {
            "arguments": arguments,
            "required_arguments": required,
            "description": ast.get_docstring(node) or "",
            "state_effect": contracts.get(node.name, {}),
        }
    missing = enabled - schemas.keys()
    if missing:
        raise ValueError(f"could not recover exact tool schemas for {sorted(missing)}")
    return schemas


def readable_app_state(
    tools: dict[str, dict[str, Any]], app_state: dict[str, Any]
) -> dict[str, Any]:
    """Return only app state exposed by an enabled simulated read tool."""
    readable_keys = {
        str(key)
        for schema in tools.values()
        for key in (schema.get("state_effect") or {}).get("reads_state_keys") or []
    }
    return {
        key: copy.deepcopy(app_state[key])
        for key in sorted(readable_keys)
        if key in app_state
    }


def current_fact_ids(facts: list[dict[str, Any]]) -> set[int]:
    superseded = {
        int(fact_id)
        for fact in facts
        for fact_id in fact.get("supersedes") or []
    }
    return {int(fact["id"]) for fact in facts} - superseded


def facts_with_current(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current = current_fact_ids(facts)
    return [dict(fact, current=int(fact["id"]) in current) for fact in facts]
