"""Build one accepted quarter by chaining canonical seven-day history windows."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from construction import build_history_window
from construction import history_quarter_corrections
from construction import history_quarter_review
from construction.checkpoints import (
    dump_json,
    load_checkpoint,
    load_json,
    load_yaml,
    validated_checkpoint_identity,
)
from construction.construction_io import resolve_path


CONFIG_KEYS = {
    "version",
    "persona",
    "quarter_id",
    "quarter_start",
    "quarter_end",
    "spec",
    "persona_sheet",
    "generator_context",
    "overview",
    "starting_checkpoint",
    "quarter_plan",
}
FILE_KEYS = {
    "spec",
    "persona_sheet",
    "generator_context",
    "overview",
    "quarter_plan",
}


def parse_date(value: Any, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date") from exc


def quarter_period(quarter_id: str) -> tuple[date, date]:
    match = re.fullmatch(r"(\d{4})_q([1-4])", quarter_id)
    if match is None:
        raise ValueError("quarter_id must use YYYY_qN")
    year = int(match.group(1))
    quarter = int(match.group(2))
    start_month = 1 + (quarter - 1) * 3
    start = date(year, start_month, 1)
    end = date(year, 12, 31) if quarter == 4 else date(year, start_month + 3, 1) - timedelta(days=1)
    return start, end


def quarter_windows(start: date, end: date) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=6), end)
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def write_yaml(path: Path, value: dict[str, Any]) -> None:
    pending = path.with_name(f".{path.name}.pending-{uuid.uuid4().hex}")
    pending.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True))
    pending.replace(path)


def checkpoint_persona(checkpoint_dir: Path) -> str:
    personas = {
        str(load_yaml(checkpoint_dir / filename).get("persona") or "")
        for filename in ("facts.yaml", "entities.yaml")
    }
    if "" in personas or len(personas) != 1:
        raise ValueError("checkpoint facts.yaml and entities.yaml must declare one persona")
    return personas.pop()


def validate_config(config_path: Path) -> dict[str, Any]:
    document = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(document, dict):
        raise ValueError("config must contain an object")
    if set(document) != CONFIG_KEYS:
        missing = sorted(CONFIG_KEYS - set(document))
        extra = sorted(set(document) - CONFIG_KEYS)
        raise ValueError(f"config keys must match exactly; missing={missing}, extra={extra}")
    if document["version"] != 1:
        raise ValueError("config version must be 1")
    persona = document["persona"]
    if not isinstance(persona, str) or not persona.strip():
        raise ValueError("persona must be a non-empty string")

    quarter_start = parse_date(document["quarter_start"], "quarter_start")
    quarter_end = parse_date(document["quarter_end"], "quarter_end")
    expected_start, expected_end = quarter_period(str(document["quarter_id"]))
    if (quarter_start, quarter_end) != (expected_start, expected_end):
        raise ValueError(
            "quarter_start and quarter_end must match quarter_id: "
            f"expected {expected_start}..{expected_end}"
        )

    paths = {key: resolve_path(str(document[key])) for key in FILE_KEYS}
    paths["starting_checkpoint"] = resolve_path(str(document["starting_checkpoint"]))
    for key in sorted(FILE_KEYS):
        if not paths[key].is_file():
            raise ValueError(f"{key} is not a file: {paths[key]}")
    if not paths["starting_checkpoint"].is_dir():
        raise ValueError(
            f"starting_checkpoint is not a directory: {paths['starting_checkpoint']}"
        )

    spec = load_yaml(paths["spec"])
    if str(spec.get("persona") or "") != persona:
        raise ValueError("spec persona does not match config persona")
    quarter_plan = load_json(paths["quarter_plan"])
    if not isinstance(quarter_plan, dict):
        raise ValueError("quarter_plan must contain an object")
    plan = quarter_plan.get("plan") or quarter_plan
    if not isinstance(plan, dict):
        raise ValueError("quarter_plan must contain a plan object")
    plan_period = (
        parse_date(plan.get("period_start"), "quarter plan period_start"),
        parse_date(plan.get("period_end"), "quarter plan period_end"),
    )
    if plan_period != (quarter_start, quarter_end):
        raise ValueError(
            "quarter_plan dates must match the configured quarter: "
            f"expected {quarter_start}..{quarter_end}, got "
            f"{plan_period[0]}..{plan_period[1]}"
        )

    checkpoint_identity = validated_checkpoint_identity(paths["starting_checkpoint"])
    checkpoint, _ = load_checkpoint(paths["starting_checkpoint"])
    if checkpoint_persona(paths["starting_checkpoint"]) != persona:
        raise ValueError("starting checkpoint persona does not match config persona")
    accepted_through = parse_date(checkpoint.get("accepted_through"), "accepted_through")
    earliest = quarter_start - timedelta(days=1)
    if not earliest <= accepted_through <= quarter_end:
        raise ValueError(
            "starting checkpoint accepted_through must be between "
            f"{earliest} and {quarter_end}, got {accepted_through}"
        )

    return {
        "raw": document,
        "paths": paths,
        "quarter_start": quarter_start,
        "quarter_end": quarter_end,
        "starting_checkpoint_identity": checkpoint_identity,
        "accepted_through": accepted_through,
    }


def _write_root_manifest(output_dir: Path, manifest: dict[str, Any]) -> None:
    dump_json(output_dir / "manifest.json", manifest)


def _append_window_ledger(root_ledger: Path, window_output: Path) -> None:
    window_ledger = window_output / "call_ledger.jsonl"
    if window_ledger.is_file():
        with root_ledger.open("a") as destination:
            destination.write(window_ledger.read_text())


def _ledger_summary(ledger: Path) -> dict[str, int]:
    summary = {"paid_calls": 0, "cache_hits": 0}
    if not ledger.is_file():
        summary["ledger_entries"] = 0
        return summary

    with ledger.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                raise ValueError(f"call ledger row {line_number} is blank")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"call ledger row {line_number} is not valid JSON") from exc
            if not isinstance(row, dict):
                raise ValueError(f"call ledger row {line_number} must be an object")
            cache_hit = row.get("cache_hit")
            if not isinstance(cache_hit, bool):
                raise ValueError(
                    f"call ledger row {line_number} cache_hit must be a boolean"
                )
            summary["cache_hits" if cache_hit else "paid_calls"] += 1

    summary["ledger_entries"] = summary["paid_calls"] + summary["cache_hits"]
    return summary


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _window_totals(manifest: dict[str, Any]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for field in ("sessions", "new_facts", "app_operations", "model_calls"):
        value = manifest.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"window manifest {field} must be a non-negative integer")
        totals[field] = value
    stages = manifest.get("generation_stages") or []
    correction_used = (
        isinstance(stages, list) and "missing_state_correction" in stages
    )
    action_correction_stages = {
        "unexecutable_contact_correction",
        "corrected_occurrence_enrichment",
    }
    present_action_correction_stages = action_correction_stages.intersection(stages)
    if present_action_correction_stages and (
        present_action_correction_stages != action_correction_stages
    ):
        raise ValueError(
            "history window action correction must include both correction stages"
        )
    action_correction_used = present_action_correction_stages == action_correction_stages
    expected_model_calls = (
        3 + int(correction_used) + (2 if action_correction_used else 0)
    )
    if totals["model_calls"] != expected_model_calls:
        raise ValueError(
            "history window used an unexpected number of model calls: "
            f"expected {expected_model_calls}, got {totals['model_calls']}"
        )
    return totals


def _window_configuration(
    *, config: dict[str, Any], validated: dict[str, Any]
) -> dict[str, Any]:
    paths = validated["paths"]
    return {
        "persona": config["persona"],
        "quarter_start": validated["quarter_start"].isoformat(),
        "quarter_end": validated["quarter_end"].isoformat(),
        "spec": str(paths["spec"]),
        "persona_sheet": str(paths["persona_sheet"]),
        "generator_context": str(paths["generator_context"]),
        "overview": str(paths["overview"]),
        "quarter_plan": str(paths["quarter_plan"]),
    }


def _passed_window_record(
    *,
    manifest: dict[str, Any],
    window_output: Path,
    window_start: date,
    window_end: date,
    expected_starting_identity: str,
    persona: str,
) -> tuple[dict[str, Any], dict[str, int], Path, str]:
    """Authenticate one already-passed window before reusing it."""
    if manifest.get("status") != "passed":
        raise ValueError("passed window manifest must have status passed")
    if manifest.get("persona") != persona:
        raise ValueError("passed window persona does not match config persona")
    if manifest.get("period_start") != window_start.isoformat():
        raise ValueError("passed window period_start does not match expected window")
    if manifest.get("period_end") != window_end.isoformat():
        raise ValueError("passed window period_end does not match expected window")
    if manifest.get("starting_checkpoint_sha256") != expected_starting_identity:
        raise ValueError("passed window starting checkpoint identity does not match")

    totals = _window_totals(manifest)
    candidate = window_output / "candidate_checkpoint"
    if not candidate.is_dir():
        raise ValueError(f"passed window candidate checkpoint is missing: {candidate}")
    candidate_identity = validated_checkpoint_identity(candidate)
    if manifest.get("candidate_checkpoint_sha256") != candidate_identity:
        raise ValueError("passed window candidate checkpoint identity does not match")
    candidate_metadata, _ = load_checkpoint(candidate)
    if checkpoint_persona(candidate) != persona:
        raise ValueError("passed window candidate checkpoint persona does not match")
    if parse_date(candidate_metadata.get("accepted_through"), "candidate accepted_through") != window_end:
        raise ValueError("passed window candidate accepted_through does not match window end")
    if candidate_metadata.get("previous_checkpoint_sha256") != expected_starting_identity:
        raise ValueError("passed window candidate checkpoint chain does not match")

    record = {
        "start_date": window_start.isoformat(),
        "end_date": window_end.isoformat(),
        "output": str(window_output),
        "starting_checkpoint_sha256": expected_starting_identity,
        "status": "passed",
        "candidate_checkpoint_sha256": candidate_identity,
        **totals,
    }
    return record, totals, candidate, candidate_identity


def _resume_existing_output(
    *,
    output_dir: Path,
    expected_window_config: dict[str, Any],
    windows: list[tuple[date, date]],
    starting_checkpoint: Path,
    starting_checkpoint_identity: str,
    persona: str,
) -> tuple[Path, Path, list[dict[str, Any]], dict[str, int], int, Path, str]:
    """Validate an incomplete run and return its authenticated passed prefix."""
    if not output_dir.is_dir():
        raise ValueError(f"existing output path is not a directory: {output_dir}")
    window_config_path = output_dir / "window_config.yaml"
    if not window_config_path.is_file():
        raise ValueError("existing output is missing window_config.yaml")
    if load_yaml(window_config_path) != expected_window_config:
        raise ValueError("existing output window_config.yaml does not match current config")

    root_manifest_path = output_dir / "manifest.json"
    if root_manifest_path.is_file():
        root_manifest = load_json(root_manifest_path)
        if not isinstance(root_manifest, dict):
            raise ValueError("existing root manifest must contain an object")
        if root_manifest.get("status") in {"passed", "final"}:
            raise ValueError("existing output is already completed")
    if (output_dir / "final_checkpoint").exists():
        raise ValueError("existing output already contains final_checkpoint")

    root_ledger = output_dir / "call_ledger.jsonl"
    if not root_ledger.is_file():
        raise ValueError("existing output is missing root call_ledger.jsonl")
    windows_dir = output_dir / "windows"
    if windows_dir.exists() and not windows_dir.is_dir():
        raise ValueError("existing output windows path is not a directory")
    windows_dir.mkdir(exist_ok=True)

    prefix: list[dict[str, Any]] = []
    totals = {"sessions": 0, "new_facts": 0, "app_operations": 0, "model_calls": 0}
    checkpoint_path = starting_checkpoint
    checkpoint_identity = starting_checkpoint_identity
    gap_index = len(windows)
    for index, (window_start, window_end) in enumerate(windows):
        window_output = windows_dir / f"{window_start.isoformat()}_to_{window_end.isoformat()}"
        manifest_path = window_output / "manifest.json"
        if not manifest_path.is_file():
            gap_index = index
            break
        manifest = load_json(manifest_path)
        if not isinstance(manifest, dict):
            raise ValueError(f"window manifest must contain an object: {manifest_path}")
        if manifest.get("status") != "passed":
            gap_index = index
            break
        record, window_totals, candidate, candidate_identity = _passed_window_record(
            manifest=manifest,
            window_output=window_output,
            window_start=window_start,
            window_end=window_end,
            expected_starting_identity=checkpoint_identity,
            persona=persona,
        )
        prefix.append(record)
        for field, value in window_totals.items():
            totals[field] += value
        checkpoint_path = candidate
        checkpoint_identity = candidate_identity

    for window_start, window_end in windows[gap_index + 1 :]:
        manifest_path = (
            windows_dir
            / f"{window_start.isoformat()}_to_{window_end.isoformat()}"
            / "manifest.json"
        )
        if not manifest_path.is_file():
            continue
        manifest = load_json(manifest_path)
        if isinstance(manifest, dict) and manifest.get("status") == "passed":
            raise ValueError("passed window exists after an incomplete window")

    return (
        root_ledger,
        window_config_path,
        prefix,
        totals,
        gap_index,
        checkpoint_path,
        checkpoint_identity,
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.confirm_paid_calls:
        raise ValueError("refusing model calls without --confirm-paid-calls")
    config_path = resolve_path(args.config)
    output_dir = resolve_path(args.out)
    tool_python = resolve_path(args.tool_python)
    source_failed_quarter = getattr(args, "source_failed_quarter", None)
    approved_history_corrections = getattr(args, "approved_history_corrections", None)
    if bool(source_failed_quarter) != bool(approved_history_corrections):
        raise ValueError(
            "--source-failed-quarter and --approved-history-corrections must be supplied together"
        )
    stop_after_weeks = getattr(args, "stop_after_weeks", None)
    stop_after_story = bool(getattr(args, "stop_after_story", False))
    if stop_after_weeks is not None and (
        isinstance(stop_after_weeks, bool)
        or not isinstance(stop_after_weeks, int)
        or stop_after_weeks <= 0
    ):
        raise ValueError("stop-after-weeks must be a positive integer")
    if stop_after_story and stop_after_weeks is not None:
        raise ValueError("--stop-after-story cannot be combined with --stop-after-weeks")
    stage = "config_validation"
    windows_manifest: list[dict[str, Any]] = []
    totals = {"sessions": 0, "new_facts": 0, "app_operations": 0, "model_calls": 0}
    expected_model_calls: int | None = None
    expected_weekly_model_calls: int | None = None
    root_ledger: Path | None = None
    prepared = False
    try:
        if not tool_python.is_file():
            raise ValueError(f"tool-python is not a file: {tool_python}")
        validated = validate_config(config_path)
        config = validated["raw"]
        paths = validated["paths"]

        next_date = validated["accepted_through"] + timedelta(days=1)
        windows = quarter_windows(next_date, validated["quarter_end"])
        expected_window_config = _window_configuration(config=config, validated=validated)
        if source_failed_quarter:
            if stop_after_weeks is not None or stop_after_story:
                raise ValueError("stop options are not supported in correction mode")
            return history_quarter_corrections.run(
                args=args,
                validated=validated,
                config=config,
                expected_window_config=expected_window_config,
                windows=windows,
                review_api=history_quarter_review,
            )
        # Each complete quarter has three weekly calls per window plus one
        # quarter-wide review. A stopped run has not reached the review yet.
        expected_weekly_model_calls = 3 * len(windows)
        expected_model_calls = expected_weekly_model_calls + 1

        stage = "window_configuration"
        if output_dir.exists():
            (
                root_ledger,
                window_config_path,
                windows_manifest,
                totals,
                resume_index,
                checkpoint_path,
                checkpoint_identity,
            ) = _resume_existing_output(
                output_dir=output_dir,
                expected_window_config=expected_window_config,
                windows=windows,
                starting_checkpoint=paths["starting_checkpoint"],
                starting_checkpoint_identity=validated["starting_checkpoint_identity"],
                persona=config["persona"],
            )
        else:
            output_dir.mkdir(parents=True)
            root_ledger = output_dir / "call_ledger.jsonl"
            root_ledger.touch()
            window_config_path = output_dir / "window_config.yaml"
            write_yaml(window_config_path, expected_window_config)
            resume_index = 0
            checkpoint_path = paths["starting_checkpoint"]
            checkpoint_identity = validated["starting_checkpoint_identity"]

        os.environ["DOLPHINBENCH_CALL_LEDGER"] = str(root_ledger)
        os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"] = output_dir.name
        prepared = True
        remaining_windows = windows[resume_index:]
        if stop_after_story:
            remaining_windows = remaining_windows[:1]
        if stop_after_weeks is not None:
            remaining_windows = remaining_windows[:stop_after_weeks]
        for index, (window_start, window_end) in enumerate(remaining_windows, start=resume_index + 1):
            stage = f"window_{index:02d}_{window_start}_to_{window_end}"
            window_output = (
                output_dir / "windows" / f"{window_start.isoformat()}_to_{window_end.isoformat()}"
            )
            record: dict[str, Any] = {
                "start_date": window_start.isoformat(),
                "end_date": window_end.isoformat(),
                "output": str(window_output),
                "starting_checkpoint_sha256": checkpoint_identity,
            }
            window_returned = False
            try:
                window_manifest = build_history_window.run(
                    argparse.Namespace(
                        config=window_config_path,
                        resume_from_checkpoint=checkpoint_path,
                        start_date=window_start,
                        end_date=window_end,
                        out=window_output,
                        tool_python=tool_python,
                        confirm_paid_calls=True,
                        stop_after_story=stop_after_story,
                    )
                )
                window_returned = True
                if window_manifest.get("status") == "story_only":
                    if not stop_after_story:
                        raise ValueError(
                            "history window stopped after story without an explicit request"
                        )
                    if window_manifest.get("persona") != config["persona"]:
                        raise ValueError("story-only window persona does not match config")
                    if window_manifest.get("period_start") != window_start.isoformat():
                        raise ValueError("story-only window start date does not match")
                    if window_manifest.get("period_end") != window_end.isoformat():
                        raise ValueError("story-only window end date does not match")
                    if window_manifest.get("starting_checkpoint_sha256") != checkpoint_identity:
                        raise ValueError(
                            "story-only window starting checkpoint does not match"
                        )
                    if window_manifest.get("model_calls") != 1:
                        raise ValueError("story-only window must use one model call")
                    if (window_output / "candidate_checkpoint").exists():
                        raise ValueError(
                            "story-only window must not emit a candidate checkpoint"
                        )
                    record.update(
                        {
                            "status": "story_only",
                            "sessions": window_manifest.get("sessions", 0),
                            "model_calls": 1,
                        }
                    )
                    windows_manifest.append(record)
                    ledger_summary = _ledger_summary(root_ledger)
                    paused = {
                        "status": "paused",
                        "pause_reason": "story_review",
                        "persona": config["persona"],
                        "quarter_id": config["quarter_id"],
                        "quarter_start": validated["quarter_start"].isoformat(),
                        "quarter_end": validated["quarter_end"].isoformat(),
                        "window_config": str(window_config_path),
                        "windows": windows_manifest,
                        "totals": totals,
                        "model_calls": {
                            "expected": expected_model_calls,
                            "accepted_stage_calls": totals["model_calls"],
                            **ledger_summary,
                            "actual": ledger_summary["paid_calls"],
                        },
                        "call_ledger": str(root_ledger),
                        "construction_run_id": output_dir.name,
                        "next_unbuilt_date": window_start.isoformat(),
                        "next_unbuilt_window": {
                            "start_date": window_start.isoformat(),
                            "end_date": window_end.isoformat(),
                        },
                        "story_output": str(window_output / "story_response.json"),
                    }
                    _write_root_manifest(output_dir, paused)
                    return paused
                _append_window_ledger(root_ledger, window_output)
                if window_manifest.get("status") != "passed":
                    raise ValueError("history window did not pass")
                window_totals = _window_totals(window_manifest)
                candidate = window_output / "candidate_checkpoint"
                candidate_identity = validated_checkpoint_identity(candidate)
                if window_manifest.get("candidate_checkpoint_sha256") != candidate_identity:
                    raise ValueError("window manifest candidate checkpoint identity does not match")
                candidate_metadata, _ = load_checkpoint(candidate)
                if checkpoint_persona(candidate) != config["persona"]:
                    raise ValueError("candidate checkpoint persona does not match config persona")
                if parse_date(candidate_metadata.get("accepted_through"), "candidate accepted_through") != window_end:
                    raise ValueError("candidate checkpoint accepted_through does not match window end")
            except Exception as exc:
                if not window_returned:
                    _append_window_ledger(root_ledger, window_output)
                record.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
                windows_manifest.append(record)
                raise

            record.update(
                {
                    "status": "passed",
                    "candidate_checkpoint_sha256": candidate_identity,
                    **window_totals,
                }
            )
            windows_manifest.append(record)
            for field, value in window_totals.items():
                totals[field] += value
            checkpoint_path = candidate
            checkpoint_identity = candidate_identity
            os.environ["DOLPHINBENCH_CALL_LEDGER"] = str(root_ledger)
            os.environ["DOLPHINBENCH_CONSTRUCTION_RUN_ID"] = output_dir.name

        next_window_index = resume_index + len(remaining_windows)
        if stop_after_weeks is not None or next_window_index < len(windows):
            next_window_start = (
                windows[next_window_index][0]
                if next_window_index < len(windows)
                else None
            )
            next_window_end = (
                windows[next_window_index][1]
                if next_window_index < len(windows)
                else None
            )
            ledger_summary = _ledger_summary(root_ledger)
            paused = {
                "status": "paused",
                "persona": config["persona"],
                "quarter_id": config["quarter_id"],
                "quarter_start": validated["quarter_start"].isoformat(),
                "quarter_end": validated["quarter_end"].isoformat(),
                "window_config": str(window_config_path),
                "windows": windows_manifest,
                "totals": totals,
                "model_calls": {
                    "expected": expected_model_calls,
                    "accepted_stage_calls": totals["model_calls"],
                    **ledger_summary,
                    "actual": ledger_summary["paid_calls"],
                },
                "call_ledger": str(root_ledger),
                "construction_run_id": output_dir.name,
                "next_unbuilt_date": (
                    next_window_start.isoformat() if next_window_start else None
                ),
                "next_unbuilt_window": (
                    {
                        "start_date": next_window_start.isoformat(),
                        "end_date": next_window_end.isoformat(),
                    }
                    if next_window_start and next_window_end
                    else None
                ),
            }
            _write_root_manifest(output_dir, paused)
            return paused

        stage = "model_call_count"
        # Each window has already proved that it used either the normal three
        # calls or those calls plus the narrowly recorded state correction.
        expected_weekly_model_calls = totals["model_calls"]
        expected_model_calls = expected_weekly_model_calls + 1

        stage = "history_quarter_review"
        quarter_plan = load_json(paths["quarter_plan"])
        review_request = history_quarter_review.build_review_request(
            config=config,
            quarter_plan=quarter_plan,
            starting_checkpoint=paths["starting_checkpoint"],
            final_checkpoint=checkpoint_path,
            window_records=windows_manifest,
        )
        review = history_quarter_review.run_review(
            output_dir=output_dir,
            request=review_request,
            rendered_input=history_quarter_review.render_review_input(review_request),
        )
        totals["model_calls"] += 1
        if not review["validation"]["valid"]:
            raise ValueError(
                "quarter history review response was invalid: "
                + "; ".join(review["validation"]["errors"])
            )
        if not review["validation"]["passed"]:
            raise ValueError(
                "quarter history review found "
                f"{review['validation']['finding_count']} concrete defect(s)"
            )
        if totals["model_calls"] != expected_model_calls:
            raise ValueError(
                "construction model-call count does not match the canonical path: "
                f"expected {expected_model_calls}, got {totals['model_calls']}"
            )

        stage = "final_checkpoint"
        final_checkpoint = output_dir / "final_checkpoint"
        pending = output_dir / f".final_checkpoint.pending-{uuid.uuid4().hex}"
        shutil.copytree(checkpoint_path, pending)
        if validated_checkpoint_identity(pending) != checkpoint_identity:
            raise ValueError("copied final checkpoint identity does not match source")
        pending.replace(final_checkpoint)
        final_identity = validated_checkpoint_identity(final_checkpoint)
        ledger_summary = _ledger_summary(root_ledger)
        manifest = {
            "status": "passed",
            "persona": config["persona"],
            "quarter_id": config["quarter_id"],
            "quarter_start": validated["quarter_start"].isoformat(),
            "quarter_end": validated["quarter_end"].isoformat(),
            "window_config": str(window_config_path),
            "windows": windows_manifest,
            "totals": totals,
            "model_calls": {
                "expected": expected_model_calls,
                "accepted_stage_calls": totals["model_calls"],
                **ledger_summary,
                "actual": ledger_summary["paid_calls"],
            },
            "call_ledger": str(root_ledger),
            "construction_run_id": output_dir.name,
            "quarter_review": review,
            "final_checkpoint": str(final_checkpoint),
            "final_checkpoint_identity": final_identity,
        }
        _write_root_manifest(output_dir, manifest)
        return manifest
    except Exception as exc:
        if not prepared or root_ledger is None:
            raise
        ledger_summary = _ledger_summary(root_ledger)
        failed = {
            "status": "failed",
            "stage": stage,
            "error": f"{type(exc).__name__}: {exc}",
            "windows": windows_manifest,
            "totals": totals,
            "model_calls": {
                "expected": expected_model_calls,
                "accepted_stage_calls": totals["model_calls"],
                **ledger_summary,
                "actual": ledger_summary["paid_calls"],
            },
            "call_ledger": str(root_ledger),
            "quarter_review": locals().get("review"),
        }
        _write_root_manifest(output_dir, failed)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build one complete DolphinBench quarter.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tool-python", type=Path, required=True)
    parser.add_argument("--confirm-paid-calls", action="store_true")
    parser.add_argument("--stop-after-story", action="store_true")
    parser.add_argument("--stop-after-weeks", type=_positive_int)
    parser.add_argument("--source-failed-quarter", type=Path)
    parser.add_argument("--approved-history-corrections", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        manifest = run(args)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"{type(exc).__name__}: {exc}") from exc
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
