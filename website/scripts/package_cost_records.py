"""Package reviewed cost calculations with the original configuration records."""

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def read(path):
    return json.loads(path.read_text())


def encode(value):
    return (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()


def package(audit: Path, originals: Path, website: Path):
    approved = read(audit / "reported-costs.json")
    agents = read(audit / "agent-cost-calculation.json")
    estimates = read(audit / "cost-proposals-NOT-APPROVED.json")
    notes = (audit / "release-review.md").read_text()
    report_path = website / "content/official-results.json"
    report = read(report_path)
    evidence = read(website / "content/official-evidence.json")
    output = website / "public/leaderboard/cost-records"
    output.mkdir(parents=True, exist_ok=True)
    scope = "Cost includes agent and memory-system processing during ingestion and testing."
    for row in report["configurations"]:
        system = row["memory"]["id"]
        cost = approved["systems"][system]
        if abs(cost["agent_usd"] + cost["memory_usd"] - cost["total_usd"]) > 1e-8:
            raise ValueError(f"Cost components do not sum for {system}")
        name = f"{system}-configuration.zip"
        source = originals / name
        original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        if original_hash != evidence[name]["sha256"]:
            raise ValueError(f"Original configuration checksum differs: {system}")
        label = {"builtin": "Built-in", "mem0": "Mem0", "honcho": "Honcho", "hindsight": "Hindsight"}[system]
        section = notes.split(f"## {label}: configuration-notes.md\n\n", 1)[1].split("\n## ", 1)[0].rstrip() + "\n"
        calculation = {
            "configuration_id": row["id"],
            "scope": approved["scope"],
            "cost": cost,
            "original_configuration_sha256": original_hash,
            "results": {p: r["source"] for p, r in row["personas"].items()},
            "agent_method": agents["method"],
            "agent_personas": {
                p: {k: data[k] for k in ("expected_sources", "recorded_main_sessions", "usage_groups", "totals")}
                for p, data in agents["systems"][system]["personas"].items()
            },
        }
        if system == "honcho":
            calculation["memory_components"] = estimates["honcho_backend_components"]
            calculation["background_allocation"] = estimates["honcho_background_allocation"]
        elif system == "hindsight":
            calculation["memory_calculation"] = estimates["hindsight_calibration"]
        # Retain run records exactly; only append the reviewed accounting records.
        destination = output / name
        with ZipFile(source) as src, ZipFile(destination, "w", compression=ZIP_DEFLATED) as dst:
            for info in src.infolist():
                dst.writestr(info, src.read(info.filename))
            for filename, data in (("configuration-notes.md", section.encode()),
                                   ("cost-calculation.json", encode(calculation))):
                info = ZipInfo(filename, date_time=(2026, 9, 18, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                dst.writestr(info, data)
        with ZipFile(source) as src, ZipFile(destination) as dst:
            assert all(src.read(name) == dst.read(name) for name in src.namelist())
        row["total_cost_usd"] = cost["total_usd"]
        row["total_cost_scope"] = scope
        row["evidence"]["configuration"] = {
            "url": f"https://dolphinbench.ai/leaderboard/cost-records/{name}",
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        }
    report_path.write_bytes(encode(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--originals", type=Path, required=True)
    args = parser.parse_args()
    package(args.audit, args.originals, Path(__file__).resolve().parents[1])
