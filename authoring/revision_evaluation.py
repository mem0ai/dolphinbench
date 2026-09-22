"""Regrade authenticated evaluation answers as part of exact test corrections."""

from __future__ import annotations

import copy
import hashlib
import json
import tarfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from authoring.revision_execution import agent_inputs, read_bound, run_once
from authoring.revision_review import grading_identity
from graders.mechanical import grade_tool_trace
from harness.durable_json import atomic_json


def load_evaluation_answers(*, inputs: dict[str, Any], root: Path) -> dict[str, Any]:
    trace = inputs["trace"]
    bundle = root / trace["evaluation_bundle"]["path"]
    read_bound(bundle, trace["evaluation_bundle"]["sha256"])
    expected = {f"{i:03d}" for i in range(1, 201)}
    sources = {}
    with tarfile.open(bundle) as archive:
        for test_id in sorted(expected):
            member = archive.extractfile(f"workspace/repo/tests/{inputs['revision']['persona']}/{test_id}.yaml")
            if member is None or hashlib.sha256(member.read()).hexdigest() != inputs["revision"]["original_test_sha256"][test_id]:
                raise ValueError("evaluation bundle does not contain the original published tests")
        for provider, evidence in trace["evaluation"].items():
            metadata_path = root / evidence["metadata"]["path"]
            metadata = json.loads(read_bound(metadata_path, evidence["metadata"]["sha256"]))
            if (metadata.get("returncode") != 0 or metadata.get("error")
                    or metadata.get("input_identity") != evidence["saved_input_identity"]
                    or metadata.get("bundle_name") != bundle.name):
                raise ValueError("saved evaluation metadata has an unsuccessful or different execution")
            files = {}
            for row in metadata["files"]:
                path = (metadata_path.parent / row["path"]).resolve()
                if not path.is_relative_to(metadata_path.parent.resolve()) or row["path"] in files:
                    raise ValueError("saved evaluation contains an unsafe or duplicate file binding")
                files[row["path"]] = row["sha256"]
                read_bound(path, row["sha256"])
            manifest_file = archive.extractfile(metadata["manifest_relative_path"])
            if manifest_file is None or hashlib.sha256(manifest_file.read()).hexdigest() != evidence["manifest_sha256"]:
                raise ValueError("saved evaluation manifest differs from its frozen bundle")
            results_path = (root / evidence["results"]["path"]).resolve()
            relative = str(results_path.relative_to(metadata_path.parent.resolve()))
            if files.get(relative) != evidence["results"]["sha256"]:
                raise ValueError("saved evaluation answers are not bound by downloaded metadata")
            results = json.loads(read_bound(results_path, evidence["results"]["sha256"]))
            rows = results["test_results"]
            by_id = {f"{int(row['test_id']):03d}": row for row in rows}
            if (len(rows) != 200 or set(by_id) != expected or results.get("provider") != provider
                    or results.get("persona") != inputs["revision"]["persona"]):
                raise ValueError("saved evaluation must contain exactly 200 answers for its provider")
            for test_id, row in by_id.items():
                if (row.get("query") != inputs["originals"][test_id]["test"]
                        or row.get("driver_ok") is not True
                        or not isinstance(row.get("effective_tool_calls"), list)):
                    raise ValueError("saved evaluation answer has a different request or unsuccessful execution")
            sources[provider] = {"rows": by_id, "source": evidence["results"],
                                 "original_summary": results["summary"]}
    return sources


def regrade_evaluation_answers(
    *, inputs: dict[str, Any], sources: dict[str, Any], out: Path,
    grader: Callable[..., dict[str, Any]] = grade_tool_trace,
    validated_test_ids: set[str] | None = None,
) -> dict[str, Any]:
    approved_ids = set(inputs["edits"])
    validated = approved_ids if validated_test_ids is None else set(validated_test_ids)
    if not validated.issubset(approved_ids):
        raise ValueError("evaluation regrading validation includes unapproved tests")
    grading_ids = set(inputs["gates"]) & validated
    pending_grading_ids = set(inputs["gates"]) - validated
    changed_request_ids = approved_ids - set(inputs["gates"])
    for test_id in grading_ids:
        if agent_inputs(inputs["candidates"][test_id]) != agent_inputs(inputs["originals"][test_id]):
            raise ValueError("evaluation regrading cannot reuse answers to a changed request")
    identity = grading_identity()
    identity["code"] = {key: value for key, value in identity["code"].items()
                        if key.startswith("graders/") or key == "harness/environment.py"}
    result = {"status": "revision_validation_pending" if approved_ids - validated else "new_request_evaluation_pending",
              "providers": {}, "pending_validation_test_ids": sorted(approved_ids - validated),
              "new_agent_executions": 0, "published": False, "regrading_complete": False}

    def regrade_provider(provider: str, source: dict[str, Any]) -> dict[str, Any]:
        rows = []
        changes = []
        original_passes = 0
        for test_id, old in sorted(source["rows"].items()):
            if test_id in changed_request_ids or test_id in pending_grading_ids:
                continue
            row = copy.deepcopy(old)
            if test_id in grading_ids:
                candidate = inputs["candidates"][test_id]
                grade = run_once(
                    directory=out / provider / "grades" / test_id,
                    identity={"candidate": candidate, "source": source["source"],
                              "source_answer": old, "grading": identity},
                    action=lambda old=old, candidate=candidate: grader(
                        old["effective_tool_calls"],
                        {**candidate["grade"]["config"], "raise_on_judge_error": True},
                        test_message=candidate["test"]),
                )
                details = grade.get("details")
                assertions = candidate["grade"]["config"]["assertions"]
                if (type(grade.get("passed")) is not bool or grade.get("diagnostic")
                        or not isinstance(details, list) or len(details) != len(assertions)
                        or any(d.get("assertion") != a or type(d.get("ok")) is not bool
                               for d, a in zip(details, assertions, strict=True))
                        or grade["passed"] != all(d["ok"] for d in details)):
                    raise ValueError("saved evaluation regrading returned incomplete or inconsistent check results")
                row.update(grade=grade, passed=grade["passed"])
                changes.append({"test_id": test_id, "before": old["passed"], "after": row["passed"]})
            rows.append(row)
            original_passes += old["passed"] is True
        provider_result = {
            "source": source["source"], "original_summary": source["original_summary"],
            "test_results": rows, "regraded_test_ids": sorted(grading_ids),
            "pending_new_request_test_ids": sorted(changed_request_ids),
            "pending_revision_test_ids": sorted(pending_grading_ids),
            "preserved_test_count": len(rows) - len(grading_ids), "grade_changes": changes,
            "completed_passes": sum(r["passed"] is True for r in rows),
            "original_passes_on_completed_tests": original_passes,
            "pass_delta_on_completed_tests": sum(r["passed"] is True for r in rows) - original_passes,
            "completed_count": len(rows), "complete_200_test_run": False,
        }
        (out / provider).mkdir(parents=True, exist_ok=True)
        atomic_json(out / provider / "regraded_results.json", provider_result)
        return {key: value for key, value in provider_result.items() if key != "test_results"}

    with ThreadPoolExecutor(max_workers=min(3, max(1, len(sources)))) as pool:
        futures = {pool.submit(regrade_provider, provider, source): provider
                   for provider, source in sorted(sources.items())}
        for future in as_completed(futures):
            result["providers"][futures[future]] = future.result()
            atomic_json(out / "manifest.json", result)
    result["saved_answers_regraded"] = len(grading_ids) * len(sources)
    result["pending_saved_answers"] = len(pending_grading_ids) * len(sources)
    result["regrading_complete"] = True
    atomic_json(out / "manifest.json", result)
    return result
