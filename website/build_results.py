"""Validate and export approved website results without running or regrading tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
CANONICAL_DIRECTORY = "artifacts/morgan-release-final-20260908/results"
SOURCE_HASHES = {
    "summary.json": "2f806cb246c504c515f829217ab1d2947f08d73927bd47747bbca005b59d9708",
    "paired_scores.json": "f531b6654e5155487d6e991b04a0b2935f3576f728d95e40531491f5d168ab2c",
    "builtin.json": "182e9dd2709a429b50c388ac0425c52af2598704465671d02456c3c80409d15c",
    "mem0.json": "112cb105e911b0ae6041ecace3a0d08072c0acd69856e8e17c65d1c2ed6d7836",
    "honcho.json": "e040013086fccd1fa7e4cb73f1aafafe3498f26924c387aed7c6522d74c5667d",
}
PERSONAS = ("alex", "morgan", "riley")


def artifact(value: dict, *, local_preview: bool = False) -> None:
    url = urlsplit(value["url"])
    local = local_preview and re.fullmatch(r"/leaderboard/evidence/[a-zA-Z0-9_.-]+/", value["url"])
    if not local and (url.scheme != "https" or not url.hostname or url.username or url.password
            or any(c.isspace() for c in value["url"]) or "\\" in value["url"]):
        raise ValueError("Evidence must have an HTTPS URL without credentials")
    if not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise ValueError("Evidence must include its file's SHA-256")


def project_complete(report: dict, *, local_preview: bool = False) -> dict:
    """Add the overall score from the three approved persona summaries."""
    result = deepcopy(report)
    if result.get("preview") and not local_preview:
        raise ValueError("A local results preview cannot be published")
    if result["schema_version"] != 2 or not isinstance(result["configurations"], list):
        raise ValueError("Expected results schema version 2 and configurations")
    seen = set()
    ids = set()
    for row in result["configurations"]:
        identity = tuple(row[key]["id"] for key in ("harness", "model", "memory"))
        for key in ("harness", "model", "memory"):
            for field in ("id", "name"):
                if not isinstance(row[key][field], str) or not row[key][field].strip():
                    raise ValueError(f"Missing {key} {field}")
        if not isinstance(row["model"]["provider"], str) or not row["model"]["provider"].strip():
            raise ValueError("Missing model provider")
        if not isinstance(row["id"], str) or not row["id"].strip():
            raise ValueError("Missing configuration ID")
        if identity in seen or row["id"] in ids:
            raise ValueError("Duplicate configuration")
        seen.add(identity)
        ids.add(row["id"])
        if set(row["personas"]) != set(PERSONAS):
            raise ValueError("Each configuration requires Alex, Morgan, and Riley")
        for name in PERSONAS:
            summary = row["personas"][name]
            if type(summary["total"]) is not int or summary["total"] != 200:
                raise ValueError(f"{name} requires exactly 200 tests")
            if type(summary["passes"]) is not int or not 0 <= summary["passes"] <= 200:
                raise ValueError(f"Invalid pass count for {name}")
            if "pass_rate" in summary and summary["pass_rate"] != summary["passes"] / 200:
                raise ValueError(f"Inconsistent pass rate for {name}")
            artifact(summary["source"], local_preview=local_preview)
        for key in ("configuration", "source", "recordings", "grades"):
            artifact(row["evidence"][key], local_preview=local_preview)
        if "runs_url" in row:
            runs_url = urlsplit(row["runs_url"])
            if runs_url.scheme != "https" or runs_url.hostname != "github.com" or not runs_url.path.startswith("/mem0ai/dolphinbench/tree/main/results/"):
                raise ValueError("Official runs must link to the public results directory")
        for key in ("total_cost_usd_test_calls", "median_latency_seconds", "p95_latency_seconds"):
            value = row[key]
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError(f"Invalid {key}; use null for unavailable measurements")
        median, p95 = row["median_latency_seconds"], row["p95_latency_seconds"]
        if median is not None and p95 is not None and p95 < median:
            raise ValueError("p95 latency cannot be below median latency")
        if row["total_cost_usd_test_calls"] is not None and not row.get("agent_inference_cost_scope", "").strip():
            raise ValueError("Reported cost requires a cost scope")
        cost = row.get("total_cost_usd")
        if cost is not None:
            if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
                raise ValueError("Invalid total_cost_usd")
            if not row.get("total_cost_scope", "").strip():
                raise ValueError("Total cost requires a cost scope")
        if ("cost_note" in row
                and (not isinstance(row["cost_note"], str) or not row["cost_note"].strip())):
            raise ValueError("Cost note must be nonempty text")
        row["total"] = 600
        row["passes"] = sum(summary["passes"] for summary in row["personas"].values())
        row["pass_rate"] = row["passes"] / 600
    return result


def summarize_tests(records: dict[str, dict]) -> dict:
    """Verify saved grades and aggregate measurements without regrading."""
    if set(records) != set(PERSONAS):
        raise ValueError("Expected all three personas")
    tests = []
    for persona, result in records.items():
        rows, summary = result["test_results"], result["summary"]
        if (result["persona"] != persona or len(rows) != 200
                or len({row["test_id"] for row in rows}) != 200
                or summary["total"] != 200 or not summary.get("complete_200_test_run")):
            raise ValueError(f"Incomplete or duplicate tests for {persona}")
        if (any(type(row["passed"]) is not bool for row in rows)
                or sum(row["passed"] for row in rows) != summary["passes"]):
            raise ValueError(f"Saved pass count disagrees with test rows for {persona}")
        tests.extend(rows)

    def measurements(key: str) -> list[float] | None:
        values = [row.get(key) for row in tests]
        if any(value is not None and (type(value) not in (int, float)
               or not math.isfinite(value) or value < 0) for value in values):
            raise ValueError(f"Invalid per-test {key}")
        return None if any(value is None for value in values) else values

    costs, latencies = measurements("cost_usd"), measurements("latency_seconds")
    p95 = None
    if latencies is not None:
        ordered = sorted(latencies)
        position = (len(ordered) - 1) * .95
        low = math.floor(position)
        p95 = ordered[low] + (ordered[math.ceil(position)] - ordered[low]) * (position - low)
    return {
        "total_cost_usd_test_calls": math.fsum(costs) if costs is not None else None,
        "median_latency_seconds": statistics.median(latencies) if latencies is not None else None,
        "p95_latency_seconds": p95,
    }


def build_complete(source: Path, output: Path, *, check: bool = False) -> None:
    report = json.loads(source.read_text())
    result = project_complete(report)
    if release_hash := report.get("release_sha256"):
        actual = hashlib.sha256((HERE.parent / "manifest.json").read_bytes()).hexdigest()
        if actual != release_hash:
            raise ValueError("Results refer to a different dataset release")
    if check:
        if report != result:
            raise ValueError("Overall scores must match the three persona summaries; regenerate the report")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


def project(source: Path) -> dict:
    records = {}
    for name, expected in SOURCE_HASHES.items():
        raw = (source / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f"Not the approved Morgan results: {name}")
        records[name] = json.loads(raw)
    models = set()
    for provider in ("builtin", "mem0", "honcho"):
        result = records[f"{provider}.json"]
        if (result["kind"] != "canonical_official_results"
                or result["persona"] != "morgan"
                or result["provider"] != provider
                or result["summary"] != records["summary.json"][provider]):
            raise ValueError(f"Inconsistent canonical result: {provider}")
        models.add(result["model_id"])
    if len(models) != 1:
        raise ValueError("The comparison requires the same agent model")
    # Report the saved summaries directly. Never rebuild scores from executions.
    return {
        "schema_version": 1,
        "persona": "morgan",
        "agent": "Hermes",
        "model_id": models.pop(),
        "source_directory": CANONICAL_DIRECTORY,
        "source_sha256": SOURCE_HASHES,
        "summary": records["summary.json"],
        "paired_scores": records["paired_scores.json"],
    }


def build(source: Path, output: Path) -> None:
    result = project(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--check", action="store_true", help="Validate the complete results without writing files")
    parser.add_argument("--legacy-morgan", action="store_true", help="Export only the preserved historical Morgan report")
    args = parser.parse_args()
    if args.legacy_morgan:
        if args.check:
            parser.error("--check is for complete results")
        build(args.source or HERE.parent / CANONICAL_DIRECTORY, args.out or HERE / "content/morgan-results.json")
    else:
        build_complete(args.source or HERE / "content/official-results.json",
                       args.out or HERE / "content/official-results.json", check=args.check)
