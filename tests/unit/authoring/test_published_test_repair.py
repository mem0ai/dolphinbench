from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from authoring import create_tests as module
from authoring.final_trace_review import FinalTraceDecision
from authoring.models import (
    FactApplicationExplanationOnEvaluationDate,
    PlannedTask,
    RequiredResult,
)


def _candidate(test_id: int) -> dict:
    return {
        "id": f"{test_id:03d}",
        "narrative_anchor_date": "2026-09-14",
        "test": f"Please send update {test_id}.",
        "load_bearing_facts": [1],
        "expected_tool_calls": ["send_email"],
        "grade": {
            "type": "tool_trace",
            "config": {
                "assertions": [
                    {
                        "type": "field_equals",
                        "tool": "send_email",
                        "path": "args.to",
                        "value": "person@example.com",
                    }
                ]
            },
        },
        "mock_state": {},
    }


def _task() -> PlannedTask:
    return PlannedTask(
        task_description="Send the update.",
        fact_ids=[1],
        required_results=[RequiredResult(result="Send it to the right person.", fact_ids=[1])],
        expected_tools=["send_email"],
        action_without_memory="The assistant sends it to a different person.",
        why_memory_changes_result="Fact 1 supplies the correct recipient.",
        fact_application_explanations_on_evaluation_date=[
            FactApplicationExplanationOnEvaluationDate(
                fact_id=1,
                why_fact_still_applies_on_evaluation_date="No later fact changes it.",
            )
        ],
        why_listed_tools_can_complete_requested_work="send_email sends the update.",
    )


def _gate(path: Path, candidate_path: Path | None = None) -> None:
    shots = [
        {
            "with_memory": flag,
            "passed": flag,
            "tool_calls": [
                {
                    "tool": "send_email",
                    "args": {
                        "to": "person@example.com" if flag else "wrong@example.com"
                    },
                }
            ],
            "response_text": "Done.",
        }
        for flag in (True, True, False, False)
    ]
    path.write_text(
        json.dumps(
            {
                "candidate_sha256": hashlib.sha256(
                    (candidate_path or path.with_name("candidate.yaml")).read_bytes()
                ).hexdigest(),
                "result": {"valid": True, "shots": shots},
            }
        )
    )


def _grading_patch(request: dict[str, object]) -> dict[str, object]:
    """Return a no-op-shaped patch tied to every grading issue in a fixture review."""
    assertions = request["current_assertions"]
    issues = request["reviewer_grading_issues"]
    operations = []
    for position, issue in enumerate(issues):
        operations.append(
            {
                "operation": "replace" if position == 0 else "add",
                "issue_index": issue["issue_index"],
                "assertion_index": 0 if position == 0 else len(assertions),
                "assertion": assertions[0],
            }
        )
    return {
        "operations": operations,
        "expected_no_history_failed_check_index": 0,
    }


def _release_fixture(
    root: Path,
    *,
    source_candidate: Path,
    source_gate: Path,
    source_plan: Path,
) -> tuple[Path, Path, SimpleNamespace, SimpleNamespace]:
    published = root / "tests" / "morgan" / "001.yaml"
    published.parent.mkdir(parents=True)
    published.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))

    batch = root / "accepted_batch"
    (batch / "candidates").mkdir(parents=True)
    accepted_candidate = batch / "candidates" / "001.yaml"
    accepted_candidate.write_bytes(published.read_bytes())
    planning = batch / "planning_batch.json"
    planning.write_text(
        json.dumps(
            {
                "persona": "morgan",
                "checkpoint_identity": "checkpoint",
                "evaluation_date": "2026-09-14",
                "tasks": [_task().model_dump(mode="json")],
            }
        )
    )

    review_dir = root / "source_review"
    review_dir.mkdir()
    review_inputs = review_dir / "review_inputs.json"
    review_inputs.write_text(
        json.dumps(
            {
                "evidence": {
                    "001": {
                        "candidate": str(source_candidate.resolve()),
                        "gate": str(source_gate.resolve()),
                        "plan": str(source_plan.resolve()),
                        "published": str(published.resolve()),
                    }
                }
            }
        )
    )
    source_manifest = root / "source_manifest.json"
    source_manifest.write_text(
        json.dumps(
            {
                "persona": "morgan",
                "checkpoint_identity": "checkpoint",
                "review_dir": str(review_dir.resolve()),
                "review_inputs_sha256": hashlib.sha256(review_inputs.read_bytes()).hexdigest(),
            }
        )
    )
    (source_candidate.parents[2] / "run_inputs.json").write_text(
        json.dumps(
            {
                "selected_test_ids": [1],
                "selected_plan_positions": [1],
                "plan_path": str(source_plan.resolve()),
            }
        )
    )
    provenance = batch / "provenance.json"
    provenance.write_text(
        json.dumps(
            {
                "checkpoint_identity": "checkpoint",
                "accepted_test_ids": [1],
                "source_manifest": str(source_manifest.resolve()),
                "results": [
                    {
                        "test_id": 1,
                        "status": "accepted_initial_final_review",
                        "candidate": str(accepted_candidate.resolve()),
                        "candidate_sha256": hashlib.sha256(
                            accepted_candidate.read_bytes()
                        ).hexdigest(),
                    }
                ],
            }
        )
    )
    release_dir = root / "authoring" / "release_manifests"
    release_dir.mkdir(parents=True)
    (release_dir / "morgan.json").write_text(
        json.dumps(
            {
                "persona": "morgan",
                "checkpoint_identity": "checkpoint",
                "evaluation_date": "2026-09-14",
                "batches": [
                    {
                        "directory": str(batch.resolve()),
                        "planning_batch_sha256": hashlib.sha256(
                            planning.read_bytes()
                        ).hexdigest(),
                        "provenance_sha256": hashlib.sha256(
                            provenance.read_bytes()
                        ).hexdigest(),
                        "accepted_test_ids": [1],
                    }
                ],
            }
        )
    )
    config = SimpleNamespace(
        persona="morgan",
        evaluation_date="2026-09-14",
        designer_model="designer",
        query_model="query",
        rejected_fact_ids=[],
    )
    context = SimpleNamespace(
        checkpoint_identity="checkpoint",
        checkpoint_path=root / "checkpoint",
        persona="morgan",
        facts_by_id={1: {}},
        active_fact_ids={1},
        tools={"send_email": {}},
    )
    return published, accepted_candidate, context, config


class PublishedTestRepairTests(unittest.TestCase):
    def _setup(
        self,
        root: Path,
        decisions: dict[int, FinalTraceDecision],
        *,
        published_ids: list[int] | None = None,
        selected_ids: list[int] | None = None,
    ) -> tuple[Path, Path, Path]:
        tests = root / "tests"
        tests.mkdir()
        hashes: dict[str, str] = {}
        evidence: dict[str, dict[str, str]] = {}
        published_ids = published_ids or sorted(decisions)
        evidence_ids = selected_ids if selected_ids is not None else published_ids
        for test_id in sorted(published_ids):
            path = tests / f"{test_id:03d}.yaml"
            path.write_text(yaml.safe_dump(_candidate(test_id), sort_keys=False))
            hashes[f"{test_id:03d}"] = hashlib.sha256(path.read_bytes()).hexdigest()
            if test_id not in evidence_ids:
                continue
            evidence[f"{test_id:03d}"] = {
                "candidate": str(path),
                "gate": str(root / "unused_gate.json"),
                "plan": str(root / "unused_plan.json"),
                "published": str(path),
            }
        review = root / "review"
        review.mkdir()
        review_inputs = {"test_sha256": hashes, "evidence": evidence}
        if selected_ids is not None:
            review_inputs["selected_test_ids"] = selected_ids
        (review / "review_inputs.json").write_text(
            json.dumps(review_inputs)
        )
        review_manifest = {
            "decisions": {
                f"{key:03d}": value.model_dump(mode="json")
                for key, value in decisions.items()
            }
        }
        if selected_ids is not None:
            review_manifest["selected_test_ids"] = selected_ids
        (review / "manifest.json").write_text(
            json.dumps(review_manifest)
        )
        for test_id in sorted(evidence_ids):
            request_dir = review / "final_trace_reviews" / f"{test_id:03d}"
            request_dir.mkdir(parents=True)
            (request_dir / "request.json").write_text(
                json.dumps({"source_evidence": [], "executions": []})
            )
        return tests, review, root / "out"

    @staticmethod
    def _decision(test_id: int, *, accept: bool, part: str = "none") -> FinalTraceDecision:
        return FinalTraceDecision(
            test_id=test_id,
            accept=accept,
            specific_problem="" if accept else "The old check is wrong.",
            part_to_correct=part,
            required_correction="" if accept else "Correct only the owning part.",
        )

    @staticmethod
    def _grade(passed: bool) -> dict:
        """Return the complete result shape required from a local regrade."""
        return {
            "passed": passed,
            "assertions": [
                {
                    "passed": passed,
                    "explanation": "fixture grading evidence",
                }
            ],
        }

    def _run(self, *, root: Path, decisions: dict[int, FinalTraceDecision], **kwargs: object) -> dict:
        published_ids = kwargs.pop("published_ids", None)
        selected_ids = kwargs.pop("selected_ids", None)
        tests, review, out = self._setup(
            root,
            decisions,
            published_ids=published_ids,
            selected_ids=selected_ids,
        )
        task = _task()
        source = root / "source"
        source.mkdir()
        candidate = source / "candidate.yaml"
        candidate.write_text((tests / "001.yaml").read_text())
        gate = source / "gate.json"
        _gate(gate)
        record = {"candidate": candidate, "gate": gate, "run_dir": source, "first_pass": True}
        config = SimpleNamespace(
            persona="morgan", designer_model="designer", query_model="query", evaluation_date="2026-09-14"
        )
        context = SimpleNamespace(
            checkpoint_identity="checkpoint", checkpoint_path=root / "checkpoint", persona="morgan"
        )
        concurrency = int(kwargs.pop("concurrency", 1))
        reviewer = kwargs.pop(
            "reviewer",
            lambda **call: self._decision(int(call["test_id"]), accept=True),
        )
        with (
            patch.object(module, "load_config", return_value=config),
            patch.object(module, "load_checkpoint_context", return_value=context),
            patch.object(module, "_planned_task_from_review_evidence", return_value=(record, task)),
            patch.object(module, "AzureJsonClient", return_value=SimpleNamespace()),
        ):
            return module.repair_published_tests(
                config_path=root / "config.yaml",
                tests_dir=tests,
                review_dir=review,
                evidence_root=root,
                out=out,
                concurrency=concurrency,
                expected_count=len(published_ids or decisions),
                confirm_paid_calls=True,
                reviewer=reviewer,
                regrader=kwargs.pop(
                    "regrader", lambda _candidate, _trace: self._grade(True)
                ),
                certifier=kwargs.pop("certifier", lambda *_args, **_kwargs: {"valid": True, "shots": []}),
                grading_corrector=kwargs.pop(
                    "grading_corrector",
                    lambda **call: _grading_patch(call["request"]),
                ),
                **kwargs,
            )

    def test_hash_mismatch_refuses_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = {index: self._decision(index, accept=True) for index in (1, 2)}
            tests, review, out = self._setup(root, decisions)
            changed = (tests / "001.yaml").read_text() + "\n"
            (tests / "001.yaml").write_text(changed)
            config = SimpleNamespace(persona="morgan")
            with patch.object(module, "load_config", return_value=config), patch.object(
                module, "load_checkpoint_context", return_value=SimpleNamespace()
            ):
                with self.assertRaisesRegex(ValueError, "changed after its review"):
                    module.repair_published_tests(
                        config_path=root / "config.yaml",
                        tests_dir=tests,
                        review_dir=review,
                        evidence_root=root,
                        out=out,
                        concurrency=1,
                        expected_count=2,
                        confirm_paid_calls=True,
                    )
            self.assertFalse(out.exists())

    def test_targeted_review_hashes_every_test_but_calls_reviewer_only_for_selected_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            for test_id in (1, 2, 3):
                (tests / f"{test_id:03d}.yaml").write_text(
                    yaml.safe_dump(_candidate(test_id), sort_keys=False)
                )
            config = SimpleNamespace(persona="morgan", designer_model="designer")
            context = SimpleNamespace(
                checkpoint_identity="checkpoint", persona="morgan"
            )
            record = {
                "candidate": root / "candidate.yaml",
                "gate": root / "gate.json",
                "plan": root / "plan.json",
                "published": tests / "001.yaml",
            }
            calls: list[int] = []

            def reviewer(**kwargs: object) -> FinalTraceDecision:
                calls.append(int(kwargs["test_id"]))
                return self._decision(int(kwargs["test_id"]), accept=True)

            with (
                patch.object(module, "load_config", return_value=config),
                patch.object(module, "load_checkpoint_context", return_value=context),
                patch.object(module, "_saved_review_record", return_value=(record, _task())),
            ):
                manifest = module.review_published_tests(
                    config_path=root / "config.yaml",
                    tests_dir=tests,
                    evidence_root=root,
                    out=root / "review_out",
                    concurrency=1,
                    expected_count=3,
                    confirm_paid_calls=True,
                    reviewer=reviewer,
                    selected_test_ids=[1, 3],
                )

            review_inputs = json.loads((root / "review_out" / "review_inputs.json").read_text())
            self.assertEqual(calls, [1, 3])
            self.assertEqual(manifest["selected_test_ids"], [1, 3])
            self.assertEqual(manifest["published_test_count"], 3)
            self.assertEqual(sorted(review_inputs["test_sha256"]), ["001", "002", "003"])
            self.assertEqual(sorted(review_inputs["evidence"]), ["001", "003"])
            self.assertEqual(review_inputs["selected_test_ids"], [1, 3])

    def test_full_review_without_selection_still_reviews_every_test(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            for test_id in (1, 2):
                (tests / f"{test_id:03d}.yaml").write_text(
                    yaml.safe_dump(_candidate(test_id), sort_keys=False)
                )
            config = SimpleNamespace(persona="morgan", designer_model="designer")
            context = SimpleNamespace(checkpoint_identity="checkpoint", persona="morgan")
            calls: list[int] = []

            def reviewer(**kwargs: object) -> FinalTraceDecision:
                test_id = int(kwargs["test_id"])
                calls.append(test_id)
                return self._decision(test_id, accept=True)

            with (
                patch.object(module, "load_config", return_value=config),
                patch.object(module, "load_checkpoint_context", return_value=context),
                patch.object(
                    module,
                    "_saved_review_record",
                    return_value=(
                        {
                            "candidate": root / "candidate.yaml",
                            "gate": root / "gate.json",
                            "plan": root / "plan.json",
                            "published": tests / "001.yaml",
                        },
                        _task(),
                    ),
                ),
            ):
                manifest = module.review_published_tests(
                    config_path=root / "config.yaml",
                    tests_dir=tests,
                    evidence_root=root,
                    out=root / "review_out",
                    concurrency=1,
                    expected_count=2,
                    confirm_paid_calls=True,
                    reviewer=reviewer,
                )

            self.assertEqual(calls, [1, 2])
            self.assertEqual(manifest["selected_test_ids"], [1, 2])
            self.assertEqual(manifest["test_count"], 2)

    def test_targeted_review_rejects_unknown_ids_before_reviewer_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            for test_id in (1, 2):
                (tests / f"{test_id:03d}.yaml").write_text(
                    yaml.safe_dump(_candidate(test_id), sort_keys=False)
                )
            config = SimpleNamespace(persona="morgan")
            with patch.object(module, "load_config", return_value=config), patch.object(
                module, "load_checkpoint_context", return_value=SimpleNamespace()
            ):
                with self.assertRaisesRegex(ValueError, "unknown test IDs"):
                    module.review_published_tests(
                        config_path=root / "config.yaml",
                        tests_dir=tests,
                        evidence_root=root,
                        out=root / "review_out",
                        concurrency=1,
                        expected_count=2,
                        confirm_paid_calls=True,
                        selected_test_ids=[3],
                    )

    def test_cli_accepts_test_ids_with_published_review(self) -> None:
        with patch.object(
            module.sys,
            "argv",
            [
                "create_tests",
                "--config",
                "config.yaml",
                "--out",
                "review_out",
                "--review-published-tests",
                "tests/morgan",
                "--test-ids",
                "1,3",
            ],
        ):
            args = module.parse_args()
        self.assertEqual(args.test_ids, [1, 3])
        self.assertEqual(args.review_published_tests, Path("tests/morgan"))

    def test_targeted_repair_detects_tampering_in_unselected_test(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = {1: self._decision(1, accept=True)}
            tests, review, out = self._setup(
                root, decisions, published_ids=[1, 2, 3], selected_ids=[1]
            )
            (tests / "002.yaml").write_text((tests / "002.yaml").read_text() + "\n")
            config = SimpleNamespace(persona="morgan")
            with patch.object(module, "load_config", return_value=config), patch.object(
                module, "load_checkpoint_context", return_value=SimpleNamespace()
            ):
                with self.assertRaisesRegex(ValueError, "published test 002 changed"):
                    module.repair_published_tests(
                        config_path=root / "config.yaml",
                        tests_dir=tests,
                        review_dir=review,
                        evidence_root=root,
                        out=out,
                        concurrency=1,
                        expected_count=3,
                        confirm_paid_calls=True,
                    )
            self.assertFalse(out.exists())

    def test_public_repair_rejects_missing_or_broken_gate_python_before_output(self) -> None:
        for broken_kind in ("missing", "broken"):
            with self.subTest(broken_kind=broken_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                executable = root / ("missing-python" if broken_kind == "missing" else "broken-python")
                if broken_kind == "broken":
                    executable.write_text("#!/bin/sh\nexit 1\n")
                    executable.chmod(0o755)
                out = root / "out"
                with (
                    patch.dict(os.environ, {"DOLPHINBENCH_GATE_PYTHON": str(executable)}),
                    patch.object(module.sys, "argv", [
                        "create_tests",
                        "--config", str(root / "config.yaml"),
                        "--out", str(out),
                        "--repair-published-tests", str(root / "tests"),
                        "--review-dir", str(root / "review"),
                        "--confirm-paid-calls",
                    ]),
                    patch.object(module, "repair_published_tests") as repair,
                ):
                    with self.assertRaisesRegex(RuntimeError, "certification Python|DOLPHINBENCH_GATE_PYTHON"):
                        module.main()
                repair.assert_not_called()
                self.assertFalse(out.exists())

    def test_targeted_repair_processes_only_selected_tests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._run(
                root=root,
                decisions={1: self._decision(1, accept=True)},
                published_ids=[1, 2, 3],
                selected_ids=[1],
            )
            self.assertEqual(manifest["selected_test_ids"], [1])
            self.assertEqual([result["id"] for result in manifest["results"]], [1])
            batch = Path(manifest["accepted_batches"][0]) / "candidates"
            self.assertEqual([path.name for path in batch.glob("*.yaml")], ["001.yaml"])
            self.assertFalse((root / "out" / "repair" / "002").exists())
            self.assertFalse((root / "out" / "repair" / "003").exists())

    def test_continuation_exception_is_saved_and_raised_without_changing_prior_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decision = self._decision(1, accept=False, part="user_request")
            tests, review, out = self._setup(root, {1: decision})
            result_path = out / "repair" / "001" / "result.json"
            result_path.parent.mkdir(parents=True)
            prior = {
                "id": 1,
                "status": "unresolved_after_continuation",
                "planned_task": _task().model_dump(mode="json"),
            }
            result_path.write_text(json.dumps(prior))
            prior_bytes = result_path.read_bytes()
            config = SimpleNamespace(
                persona="morgan",
                designer_model="designer",
                query_model="query",
                evaluation_date="2026-09-14",
            )
            context = SimpleNamespace(
                checkpoint_identity="checkpoint", checkpoint_path=root, persona="morgan"
            )
            (out / "repair_inputs.json").write_text(
                json.dumps(
                    {
                        "checkpoint_identity": "checkpoint",
                        "persona": "morgan",
                        "tests_dir": str(tests.resolve()),
                        "review_dir": str(review.resolve()),
                        "evidence_root": str(root.resolve()),
                        "selected_test_ids": [1],
                        "review_manifest_sha256": hashlib.sha256(
                            (review / "manifest.json").read_bytes()
                        ).hexdigest(),
                        "review_inputs_sha256": hashlib.sha256(
                            (review / "review_inputs.json").read_bytes()
                        ).hexdigest(),
                        "replacement_plan": None,
                        "replacement_plan_sha256": None,
                    }
                )
            )
            with (
                patch.object(module, "load_config", return_value=config),
                patch.object(module, "load_checkpoint_context", return_value=context),
                patch.object(
                    module,
                    "_continue_unresolved_published_test",
                    side_effect=RuntimeError("forced continuation failure"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "saved exception evidence"):
                    module.repair_published_tests(
                        config_path=root / "config.yaml",
                        tests_dir=tests,
                        review_dir=review,
                        evidence_root=root,
                        out=out,
                        concurrency=1,
                        expected_count=1,
                        confirm_paid_calls=True,
                        continuation_test_ids=[1],
                    )

            self.assertEqual(result_path.read_bytes(), prior_bytes)
            evidence = list((result_path.parent / "continuation_exceptions").glob("*.json"))
            self.assertEqual(len(evidence), 1)
            saved = json.loads(evidence[0].read_text())
            self.assertEqual(saved["exception_type"], "RuntimeError")
            self.assertEqual(saved["message"], "forced continuation failure")
            self.assertEqual(saved["source_result_sha256"], hashlib.sha256(prior_bytes).hexdigest())
            self.assertIn("forced continuation failure", saved["traceback"])

    def test_review_finds_an_authenticated_repair_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            published = root / "tests" / "001.yaml"
            published.parent.mkdir()
            published.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))

            original_run = root / "original"
            original_candidate = original_run / "proposals" / "001" / "initial_candidate.yaml"
            original_candidate.parent.mkdir(parents=True)
            original = _candidate(1)
            original["test"] = "The earlier accepted request."
            original_candidate.write_text(yaml.safe_dump(original, sort_keys=False))
            original_gate = original_candidate.with_name("initial_gate.json")
            _gate(original_gate, original_candidate)
            original_plan = original_run / "planning_batch.json"
            original_plan.write_text(json.dumps({"tasks": [_task().model_dump(mode="json")]}))

            old_review = root / "old_review"
            old_review.mkdir()
            old_review_inputs = old_review / "review_inputs.json"
            old_review_inputs.write_text(
                json.dumps(
                    {
                        "evidence": {
                            "001": {
                                "candidate": str(original_candidate.resolve()),
                                "gate": str(original_gate.resolve()),
                                "plan": str(original_plan.resolve()),
                                "published": str(published.resolve()),
                            }
                        }
                    }
                )
            )

            repair_root = root / "repair_run"
            repair_dir = repair_root / "repair" / "001"
            repair_dir.mkdir(parents=True)
            repaired_candidate = repair_dir / "grading_checks_candidate.yaml"
            repaired_candidate.write_text(published.read_text())
            repaired_gate = repair_dir / "grading_checks_gate.json"
            _gate(repaired_gate, repaired_candidate)
            result = {
                "id": 1,
                "status": "accepted",
                "candidate": str(repaired_candidate.resolve()),
                "planned_task": _task().model_dump(mode="json"),
            }
            (repair_dir / "result.json").write_text(json.dumps(result))
            (repair_root / "repair_inputs.json").write_text(
                json.dumps(
                    {
                        "review_dir": str(old_review.resolve()),
                        "review_inputs_sha256": hashlib.sha256(
                            old_review_inputs.read_bytes()
                        ).hexdigest(),
                    }
                )
            )

            config = SimpleNamespace(persona="morgan", evaluation_date="2026-09-14")
            context = SimpleNamespace(persona="morgan")
            with patch.object(module, "_validate_tasks"):
                record, task = module._saved_review_record(
                    test_id=1,
                    published_path=published,
                    evidence_root=root,
                    context=context,
                    config=config,
                )

            self.assertEqual(record["candidate"], repaired_candidate.resolve())
            self.assertEqual(record["gate"], repaired_gate.resolve())
            self.assertEqual(record["source_candidate"], original_candidate.resolve())
            self.assertEqual(task, _task())

    def test_saved_review_uses_release_provenance_when_historical_scan_has_no_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run = root / "source_run" / "proposals" / "001"
            source_run.mkdir(parents=True)
            source_candidate = source_run / "initial_candidate.yaml"
            source_candidate.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))
            source_gate = source_run / "initial_gate.json"
            _gate(source_gate, source_candidate)
            source_plan = source_run.parents[1] / "planning_batch.json"
            source_plan.write_text(
                json.dumps(
                    {
                        "persona": "morgan",
                        "checkpoint_identity": "checkpoint",
                        "evaluation_date": "2026-09-14",
                        "tasks": [_task().model_dump(mode="json")],
                    }
                )
            )
            published, _, context, config = _release_fixture(
                root,
                source_candidate=source_candidate,
                source_gate=source_gate,
                source_plan=source_plan,
            )

            original_normalized = module._normalized_test

            def fail_for_historical_scan(path: Path) -> str:
                if path.resolve() not in {published.resolve(), source_candidate.resolve()}:
                    raise AssertionError("historical scan should not run")
                return original_normalized(path)

            with patch.object(module, "ROOT", root), patch.object(
                module, "_validate_tasks"
            ), patch.object(module, "_normalized_test", side_effect=fail_for_historical_scan):
                record, task = module._saved_review_record(
                    test_id=1,
                    published_path=published,
                    evidence_root=root / "empty_evidence_root",
                    context=context,
                    config=config,
                )

            self.assertEqual(record["candidate"], source_candidate.resolve())
            self.assertEqual(record["gate"], source_gate.resolve())
            self.assertEqual(task, _task())

    def test_saved_review_uses_accepted_hash_to_disambiguate_historical_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run = root / "source_run" / "proposals" / "001"
            source_run.mkdir(parents=True)
            source_candidate = source_run / "initial_candidate.yaml"
            source_value = _candidate(1)
            source_value["test"] = "The earlier request had different wording."
            source_candidate.write_text(yaml.safe_dump(source_value, sort_keys=True))
            source_gate = source_run / "initial_gate.json"
            _gate(source_gate, source_candidate)
            source_plan = source_run.parents[1] / "planning_batch.json"
            source_plan.write_text(
                json.dumps(
                    {
                        "persona": "morgan",
                        "checkpoint_identity": "checkpoint",
                        "evaluation_date": "2026-09-14",
                        "tasks": [_task().model_dump(mode="json")],
                    }
                )
            )
            published, _, context, config = _release_fixture(
                root,
                source_candidate=source_candidate,
                source_gate=source_gate,
                source_plan=source_plan,
            )
            exact_run = root / "exact_run" / "proposals" / "001"
            exact_run.mkdir(parents=True)
            exact_candidate = exact_run / "initial_candidate.yaml"
            exact_candidate.write_bytes(published.read_bytes())
            exact_gate = exact_run / "initial_gate.json"
            _gate(exact_gate, exact_candidate)
            duplicate_run = root / "duplicate_run" / "proposals" / "001"
            duplicate_run.mkdir(parents=True)
            duplicate_candidate = duplicate_run / "initial_candidate.yaml"
            duplicate_candidate.write_text(yaml.safe_dump(_candidate(1), sort_keys=True))
            duplicate_gate = duplicate_run / "initial_gate.json"
            _gate(duplicate_gate, duplicate_candidate)
            for run, plan_path in (
                (exact_run.parents[1], exact_run.parents[1] / "planning_batch.json"),
                (duplicate_run.parents[1], duplicate_run.parents[1] / "planning_batch.json"),
            ):
                plan_path.write_text(
                    json.dumps(
                        {
                            "persona": "morgan",
                            "checkpoint_identity": "checkpoint",
                            "evaluation_date": "2026-09-14",
                            "tasks": [_task().model_dump(mode="json")],
                        }
                    )
                )
                (run / "run_inputs.json").write_text(
                    json.dumps(
                        {
                            "selected_test_ids": [1],
                            "selected_plan_positions": [1],
                            "plan_path": str(plan_path.resolve()),
                        }
                    )
                )

            with patch.object(module, "ROOT", root), patch.object(module, "_validate_tasks"):
                record, task = module._saved_review_record(
                    test_id=1,
                    published_path=published,
                    evidence_root=root,
                    context=context,
                    config=config,
                )

            self.assertEqual(record["candidate"], exact_candidate.resolve())
            self.assertEqual(record["gate"], exact_gate.resolve())
            self.assertEqual(task, _task())

    def test_saved_review_falls_back_when_release_source_has_no_review_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run = root / "source_run" / "proposals" / "001"
            source_run.mkdir(parents=True)
            source_candidate = source_run / "initial_candidate.yaml"
            source_candidate.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))
            source_gate = source_run / "initial_gate.json"
            _gate(source_gate, source_candidate)
            source_plan = source_run.parents[1] / "planning_batch.json"
            source_plan.write_text(
                json.dumps(
                    {
                        "persona": "morgan",
                        "checkpoint_identity": "checkpoint",
                        "evaluation_date": "2026-09-14",
                        "tasks": [_task().model_dump(mode="json")],
                    }
                )
            )
            published, _, context, config = _release_fixture(
                root,
                source_candidate=source_candidate,
                source_gate=source_gate,
                source_plan=source_plan,
            )
            (root / "source_manifest.json").write_text(
                json.dumps(
                    {
                        "persona": "morgan",
                        "checkpoint_identity": "checkpoint",
                    }
                )
            )

            with patch.object(module, "ROOT", root), patch.object(module, "_validate_tasks"):
                record, task = module._saved_review_record(
                    test_id=1,
                    published_path=published,
                    evidence_root=root,
                    context=context,
                    config=config,
                )

            self.assertEqual(record["candidate"], source_candidate.resolve())
            self.assertEqual(record["gate"], source_gate.resolve())
            self.assertEqual(task, _task())

    def test_repaired_evidence_can_use_saved_authoring_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "authoring_run"
            run_dir.mkdir()
            candidate = root / "repaired_candidate.yaml"
            candidate.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))
            gate = root / "repaired_gate.json"
            _gate(gate, candidate)
            result = root / "result.json"
            result.write_text(
                json.dumps({"planned_task": _task().model_dump(mode="json")})
            )
            evidence = {
                "candidate": str(candidate),
                "gate": str(gate),
                "plan": str(result),
                "published": str(candidate),
                "run_dir": str(run_dir),
                "first_pass": "False",
            }

            with patch.object(module, "_validate_tasks"):
                record, task = module._planned_task_from_review_evidence(
                    test_id=1,
                    evidence=evidence,
                    context=SimpleNamespace(),
                    config=SimpleNamespace(),
                )

            self.assertEqual(record["run_dir"], run_dir)
            self.assertFalse(record["first_pass"])
            self.assertEqual(task, _task())

    def test_accepted_file_stays_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = {index: self._decision(index, accept=True) for index in (1, 2)}
            manifest = self._run(root=root, decisions=decisions)
            first = Path(manifest["accepted_batches"][0]) / "candidates" / "001.yaml"
            self.assertEqual(first.read_bytes(), (root / "tests" / "001.yaml").read_bytes())

    def test_grader_correction_cannot_change_another_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.yaml"
            source.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))
            destination = root / "candidate.yaml"
            with self.assertRaisesRegex(ValueError, "named assertion operations"):
                module._corrected_grading_candidate(
                    source=source,
                    destination=destination,
                    correction={
                        "assertions": _candidate(1)["grade"]["config"]["assertions"],
                        "expected_no_history_failed_check_index": 0,
                        "test": "attempted overwrite",
                    },
                    decision=self._decision(1, accept=False, part="grading_checks"),
                )
            self.assertFalse(destination.exists())


    def test_request_correction_reuses_existing_design(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = {
                1: self._decision(1, accept=False, part="user_request"),
                2: self._decision(2, accept=True),
            }
            attempt_calls: list[dict] = []

            def attempt(**kwargs: object):
                attempt_calls.append(kwargs)
                directory = kwargs["directory"]
                candidate = Path(directory) / "user_request_candidate.yaml"
                candidate.write_text((root / "tests" / "001.yaml").read_text())
                gate = Path(directory) / "user_request_gate.json"
                _gate(gate, candidate)
                return {"candidate": True}, {"stage": "accepted"}

            with patch.object(module, "_load_saved_authoring_artifacts", return_value=(SimpleNamespace(), {}, {})), patch.object(module, "_attempt", side_effect=attempt):
                self._run(root=root, decisions=decisions)
            self.assertEqual(len(attempt_calls), 1)
            self.assertIsNotNone(attempt_calls[0]["reuse_design"])

    def test_execution_reruns_the_unchanged_test(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = {
                1: self._decision(1, accept=False, part="execution"),
                2: self._decision(2, accept=True),
            }
            seen: list[bytes] = []

            def certifier(_persona: str, path: Path, **_kwargs: object) -> dict:
                seen.append(path.read_bytes())
                return {"valid": True, "shots": []}

            self._run(root=root, decisions=decisions, certifier=certifier)
            self.assertEqual(seen, [(root / "tests" / "001.yaml").read_bytes()])

    def test_planning_failure_remains_unresolved_when_redesign_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = {
                1: self._decision(1, accept=False, part="planning"),
                2: self._decision(2, accept=True),
            }
            manifest = self._run(root=root, decisions=decisions)
            self.assertEqual(manifest["unresolved"], 1)
            from authoring.propose import load_rejected_tests

            rejected = load_rejected_tests([Path(manifest["unresolved_tests_path"])])
            self.assertEqual(rejected[0]["test_id"], 1)
            self.assertEqual(rejected[0]["candidate_plan"], _task().model_dump(mode="json"))

    def test_published_grading_repair_reruns_when_review_also_reports_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decision = FinalTraceDecision.model_validate(
                {
                    "test_id": 1,
                    "accept": False,
                    "issues": [
                        {
                            "owning_step": "grading_checks",
                            "specific_problem": "A passing run omits required content.",
                            "required_correction": "Require the missing content.",
                        },
                        {
                            "owning_step": "execution",
                            "specific_problem": "The corrected checks need new executions.",
                            "required_correction": "Run the test again after correcting the checks.",
                        },
                    ],
                }
            )
            outcomes = iter([True, True, False, False])
            certifications: list[dict] = []

            def certifier(*_args: object, **kwargs: object) -> dict:
                certifications.append(kwargs)
                return {"valid": True, "shots": []}

            manifest = self._run(
                root=root,
                decisions={1: decision},
                regrader=lambda *_args: self._grade(next(outcomes)),
                certifier=certifier,
            )

            self.assertEqual(manifest["accepted"], 1)
            self.assertEqual(len(certifications), 1)

    def test_one_failed_correction_remains_unresolved_when_redesign_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = {
                1: self._decision(1, accept=False, part="grading_checks"),
                2: self._decision(2, accept=True),
            }
            manifest = self._run(
                root=root,
                decisions=decisions,
                reviewer=lambda **kwargs: self._decision(int(kwargs["test_id"]), accept=False, part="grading_checks"),
            )
            self.assertEqual(manifest["unresolved"], 1)

    def test_rejected_tests_use_requested_concurrency_and_sorted_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = {
                test_id: self._decision(
                    test_id, accept=False, part="grading_checks"
                )
                for test_id in range(1, 5)
            }
            barrier = threading.Barrier(4)
            lock = threading.Lock()
            active = 0
            maximum_active = 0

            def grading_corrector(**_kwargs: object) -> dict:
                nonlocal active, maximum_active
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                try:
                    barrier.wait(timeout=5)
                finally:
                    with lock:
                        active -= 1
                return _grading_patch(_kwargs["request"])

            manifest = self._run(
                root=root,
                decisions=decisions,
                concurrency=4,
                grading_corrector=grading_corrector,
                regrader=lambda _candidate, trace: self._grade(
                    trace["tool_calls"][0]["args"]["to"] == "person@example.com"
                ),
            )

            self.assertEqual(maximum_active, 4)
            self.assertEqual(manifest["accepted"], 4)
            self.assertEqual(
                [result["id"] for result in manifest["results"]],
                [1, 2, 3, 4],
            )
            batch = Path(manifest["accepted_batches"][0]) / "candidates"
            self.assertEqual(
                [path.name for path in sorted(batch.glob("*.yaml"))],
                ["001.yaml", "002.yaml", "003.yaml", "004.yaml"],
            )
            self.assertTrue(
                all(
                    (root / "out" / "repair" / f"{test_id:03d}" / "result.json").is_file()
                    for test_id in range(1, 5)
                )
            )

    def _saved_unresolved(
        self, *, root: Path, test_id: int, part: str
    ) -> tuple[Path, dict, dict]:
        work = root / "out" / "repair" / f"{test_id:03d}"
        work.mkdir(parents=True)
        candidate = work / "same_facts_redesign_candidate.yaml"
        candidate.write_text(yaml.safe_dump(_candidate(test_id), sort_keys=False))
        gate = work / "same_facts_redesign_gate.json"
        _gate(gate, candidate)
        request = work / "same_facts_redesign_final_review" / f"{test_id:03d}"
        request.mkdir(parents=True)
        (request / "request.json").write_text(json.dumps({"source_evidence": []}))
        decision = self._decision(test_id, accept=False, part=part)
        result = {
            "id": test_id,
            "status": "unresolved_after_redesign",
            "initial_review": decision.model_dump(mode="json"),
            "planned_task": _task().model_dump(mode="json"),
            "same_facts_redesign": {"initial": {"design": {}}},
            "same_facts_redesign_final_review": decision.model_dump(mode="json"),
        }
        (work / "result.json").write_text(json.dumps(result))
        record = {
            "candidate": candidate,
            "gate": gate,
            "plan": work / "source_plan.json",
            "published": candidate,
            "run_dir": root / "source",
            "first_pass": False,
        }
        return work, result, record

    def _continuation_identity_fixture(
        self,
        *,
        root: Path,
        test_id: int = 1,
        failure_hash: str = "a" * 64,
        saved_result: dict | None = None,
    ) -> tuple[Path, Path, dict, dict]:
        work = root / "repair" / f"{test_id:03d}"
        work.mkdir(parents=True)
        result_path = work / "result.json"
        saved = saved_result or {
            "id": test_id,
            "status": "unresolved_after_redesign",
            "planned_task": _task().model_dump(mode="json"),
        }
        result_path.write_text(json.dumps(saved))
        candidate = work / "source_candidate.yaml"
        candidate.write_text(yaml.safe_dump(_candidate(test_id), sort_keys=False))
        gate = work / "source_gate.json"
        _gate(gate, candidate)
        snapshot = module._snapshot_previous_repair_result(
            result_path=result_path, saved=saved
        )
        identity = module._continuation_inputs_identity(
            test_id=test_id,
            result_path=result_path,
            snapshot=snapshot,
            failure_hash=failure_hash,
            planned_task=_task(),
            source_candidate=candidate,
            source_gate=gate,
            correction_operation="grading_checks",
        )
        return work, result_path, saved, identity

    def _write_legacy_grading_partial_evidence(
        self,
        *,
        work: Path,
        result_path: Path,
        continuation: Path,
        identity: dict,
        reviewer_decision: dict,
    ) -> Path:
        grading = continuation / "grading_checks"
        (grading / "work").mkdir(parents=True)
        candidate = Path(identity["source_candidate"])
        gate = json.loads(Path(identity["source_gate"]).read_text())
        (grading / "grading_correction_request.json").write_text(
            json.dumps(
                {
                    "current_test": yaml.safe_load(candidate.read_text()),
                    "source_evidence": [],
                    "executions": gate["result"]["shots"],
                    "reviewer_decision": reviewer_decision,
                }
            )
        )
        (grading / "grading_correction_schema.json").write_text(json.dumps({}))
        (grading / "grading_correction_response.json").write_text(json.dumps({}))
        (grading / "grading_correction_system.txt").write_text("Correct the grading checks.\n")
        (grading / "work" / "grading_correction_response_cache.json").write_text(
            json.dumps({})
        )
        review_request = work / "source_review_request.json"
        review_request.write_text(json.dumps({"planned_work": identity["planned_task"]}))
        exception = {
            "test_id": identity["test_id"],
            "source_result": str(result_path.resolve()),
            "source_result_sha256": identity["source_result_sha256"],
            "exception_type": "ValueError",
            "message": "grading assertion 3 has no pattern",
            "traceback": (
                "Traceback (most recent call last):\n"
                "  File \"authoring/create_tests.py\", in _corrected_grading_candidate\n"
                "  File \"authoring/create_tests.py\", in _validate_grading_assertions\n"
                "ValueError: grading assertion 3 has no pattern\n"
            ),
        }
        exceptions = work / "continuation_exceptions"
        exceptions.mkdir()
        (exceptions / f"{module._json_sha256(exception)}.json").write_text(
            json.dumps(exception)
        )
        return review_request

    def test_fresh_continuation_directory_writes_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, result_path, saved, identity = self._continuation_identity_fixture(
                root=root
            )
            continuation = work / "continuations" / identity["source_failure_sha256"]

            module._prepare_continuation_directory(
                test_id=1,
                continuation_dir=continuation,
                identity=identity,
                result_path=result_path,
                saved_result=saved,
            )

            self.assertEqual(
                json.loads((continuation / "continuation_inputs.json").read_text()),
                identity,
            )

    def test_exact_continuation_retry_reuses_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, result_path, saved, identity = self._continuation_identity_fixture(
                root=root
            )
            continuation = work / "continuations" / identity["source_failure_sha256"]
            module._prepare_continuation_directory(
                test_id=1,
                continuation_dir=continuation,
                identity=identity,
                result_path=result_path,
                saved_result=saved,
            )
            marker = continuation / "preserved.txt"
            marker.write_text("existing work")

            module._prepare_continuation_directory(
                test_id=1,
                continuation_dir=continuation,
                identity=identity,
                result_path=result_path,
                saved_result=saved,
            )

            self.assertEqual(marker.read_text(), "existing work")

    def test_continuation_retry_refuses_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, result_path, saved, identity = self._continuation_identity_fixture(
                root=root
            )
            continuation = work / "continuations" / identity["source_failure_sha256"]
            module._prepare_continuation_directory(
                test_id=1,
                continuation_dir=continuation,
                identity=identity,
                result_path=result_path,
                saved_result=saved,
            )
            changed = {**identity, "correction_operation": "execution"}

            with self.assertRaisesRegex(ValueError, "continuation inputs changed"):
                module._prepare_continuation_directory(
                    test_id=1,
                    continuation_dir=continuation,
                    identity=changed,
                    result_path=result_path,
                    saved_result=saved,
                )

    def test_legacy_partial_is_adopted_from_saved_validation_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test_id = 7
            decision = self._decision(
                test_id, accept=False, part="grading_checks"
            ).model_dump(mode="json")
            work, result_path, saved, identity = self._continuation_identity_fixture(
                root=root,
                test_id=test_id,
                failure_hash=module._json_sha256(decision),
            )
            continuation = work / "continuations" / identity["source_failure_sha256"]
            review_request = self._write_legacy_grading_partial_evidence(
                work=work,
                result_path=result_path,
                continuation=continuation,
                identity=identity,
                reviewer_decision=decision,
            )

            module._prepare_continuation_directory(
                test_id=test_id,
                continuation_dir=continuation,
                identity=identity,
                result_path=result_path,
                saved_result=saved,
                source_review_request=review_request,
            )

            self.assertEqual(
                json.loads((continuation / "continuation_inputs.json").read_text()),
                identity,
            )

    def test_legacy_partial_without_saved_validation_evidence_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, result_path, saved, identity = self._continuation_identity_fixture(
                root=root
            )
            continuation = work / "continuations" / identity["source_failure_sha256"]
            (continuation / "grading_checks").mkdir(parents=True)

            with self.assertRaisesRegex(ValueError, "saved grading-validation failure"):
                module._prepare_continuation_directory(
                    test_id=1,
                    continuation_dir=continuation,
                    identity=identity,
                    result_path=result_path,
                    saved_result=saved,
                )

    def test_legacy_partial_refuses_a_saved_continuation_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved = {
                "id": 7,
                "status": "unresolved_after_continuation",
                "planned_task": _task().model_dump(mode="json"),
                "continuation": {"source_failure_sha256": "older-failure"},
            }
            work, result_path, saved, identity = self._continuation_identity_fixture(
                root=root, test_id=7, saved_result=saved
            )
            continuation = work / "continuations" / identity["source_failure_sha256"]
            (continuation / "grading_checks").mkdir(parents=True)

            with self.assertRaisesRegex(ValueError, "already has a saved result"):
                module._prepare_continuation_directory(
                    test_id=7,
                    continuation_dir=continuation,
                    identity=identity,
                    result_path=result_path,
                    saved_result=saved,
                )

    def _continuation_gate(self, candidate: Path, gate: Path) -> dict:
        _gate(gate, candidate)
        return json.loads(gate.read_text())["result"]

    @staticmethod
    def _remove_saved_issues(saved: dict, stage: str) -> None:
        decision = dict(saved[stage])
        decision.pop("issues", None)
        saved[stage] = decision

    def test_old_saved_decision_is_reviewed_before_correction_and_can_be_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, saved, record = self._saved_unresolved(
                root=root, test_id=1, part="grading_checks"
            )
            self._remove_saved_issues(saved, "same_facts_redesign_final_review")
            (work / "result.json").write_text(json.dumps(saved))
            (
                work
                / "same_facts_redesign_final_review"
                / "001"
                / "request.json"
            ).unlink()
            published = root / "published.yaml"
            published.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))
            review_calls: list[dict] = []

            def reviewer(**kwargs: object) -> FinalTraceDecision:
                review_calls.append(dict(kwargs))
                request = Path(kwargs["out"]) / "001"
                request.mkdir(parents=True, exist_ok=True)
                (request / "request.json").write_text(json.dumps({}))
                return self._decision(1, accept=True)

            def unexpected_correction(**_kwargs: object) -> dict:
                raise AssertionError("an accepted refreshed review must not run a correction")

            with (
                patch.object(module, "_planned_task_from_review_evidence", return_value=(record, _task())),
                patch.object(module, "_validate_tasks"),
            ):
                result, task, candidate, status = module._continue_unresolved_published_test(
                    test_id=1,
                    published_path=published,
                    evidence={},
                    review_dir=root / "review",
                    out=root / "out",
                    context=SimpleNamespace(checkpoint_path=root / "checkpoint"),
                    config=SimpleNamespace(
                        persona="morgan", designer_model="designer", query_model="query"
                    ),
                    grading_corrector=unexpected_correction,
                    regrader=lambda *_args: self._grade(True),
                    certifier=lambda *_args, **_kwargs: {"valid": True},
                    reviewer=reviewer,
                )

            self.assertEqual(len(review_calls), 1)
            self.assertEqual(status, "accepted_after_continuation_review")
            self.assertEqual(candidate, record["candidate"])
            self.assertEqual(task, _task())
            self.assertEqual(result["action"], "accepted_after_old_decision_review")
            self.assertEqual(result["continuation"]["changed_step"], "none")
            self.assertEqual(result["continuation"]["candidate"], str(record["candidate"].resolve()))
            self.assertEqual(result["continuation"]["gate"], str(record["gate"].resolve()))
            refreshed = result["continuation"]["refreshed_final_review"]
            self.assertNotIn("issues", refreshed["source_decision"])
            self.assertTrue(refreshed["decision"]["accept"])
            self.assertTrue(Path(result["previous_result"]).is_file())

    def test_old_saved_decision_uses_refreshed_issues_for_one_correction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, saved, record = self._saved_unresolved(
                root=root, test_id=1, part="grading_checks"
            )
            self._remove_saved_issues(saved, "same_facts_redesign_final_review")
            (work / "result.json").write_text(json.dumps(saved))
            published = root / "published.yaml"
            published.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))
            calls: list[str] = []
            refreshed = FinalTraceDecision.model_validate(
                {
                    "test_id": 1,
                    "accept": False,
                    "issues": [
                        {
                            "owning_step": "user_request",
                            "specific_problem": "The request reveals a required value.",
                            "required_correction": "Remove that value from the request.",
                        },
                        {
                            "owning_step": "grading_checks",
                            "specific_problem": "The checks require an optional phrase.",
                            "required_correction": "Grade only the required meaning.",
                        },
                    ],
                }
            )

            def reviewer(**kwargs: object) -> FinalTraceDecision:
                calls.append("review")
                request = Path(kwargs["out"]) / "001"
                request.mkdir(parents=True, exist_ok=True)
                (request / "request.json").write_text(json.dumps({}))
                return refreshed if calls.count("review") == 1 else self._decision(1, accept=True)

            def redesign(**kwargs: object):
                calls.append("redesign")
                self.assertEqual(len(kwargs["failure"]["issues"]), 2)
                destination = Path(kwargs["work_dir"])
                candidate = destination / "same_facts_redesign_candidate.yaml"
                candidate.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))
                gate = destination / "same_facts_redesign_gate.json"
                _gate(gate, candidate)
                return {"candidate": True}, {"stage": "accepted"}

            with (
                patch.object(module, "_planned_task_from_review_evidence", return_value=(record, _task())),
                patch.object(module, "_validate_tasks"),
                patch.object(module, "AzureJsonClient", return_value=SimpleNamespace()),
                patch.object(module, "_redesign_from_same_facts", side_effect=redesign),
            ):
                result, _task_out, candidate, status = module._continue_unresolved_published_test(
                    test_id=1,
                    published_path=published,
                    evidence={},
                    review_dir=root / "review",
                    out=root / "out",
                    context=SimpleNamespace(checkpoint_path=root / "checkpoint"),
                    config=SimpleNamespace(
                        persona="morgan", designer_model="designer", query_model="query"
                    ),
                    grading_corrector=lambda **_: (_ for _ in ()).throw(
                        AssertionError("mixed request and grading issues must route to design")
                    ),
                    regrader=lambda *_args: self._grade(True),
                    certifier=lambda *_args, **_kwargs: {"valid": True},
                    reviewer=reviewer,
                )

            self.assertEqual(calls, ["review", "redesign", "review"])
            self.assertEqual(result["action"], "redesigned_same_facts")
            self.assertEqual(result["continuation"]["source_failure_stage"], "refreshed_final_review")
            self.assertEqual(result["continuation"]["changed_step"], "test_design")
            self.assertEqual(len(result["continuation"]["source_failure"]["issues"]), 2)
            self.assertEqual(status, "accepted_after_continuation_review")
            self.assertIsNotNone(candidate)

    def test_new_saved_decision_does_not_add_a_review_before_correction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, _saved, record = self._saved_unresolved(
                root=root, test_id=1, part="grading_checks"
            )
            published = root / "published.yaml"
            published.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))
            calls: list[str] = []

            def grading_corrector(**_kwargs: object) -> dict:
                calls.append("correct")
                return _grading_patch(_kwargs["request"])

            def reviewer(**kwargs: object) -> FinalTraceDecision:
                calls.append("review")
                request = Path(kwargs["out"]) / "001"
                request.mkdir(parents=True, exist_ok=True)
                (request / "request.json").write_text(json.dumps({}))
                return self._decision(1, accept=True)

            with (
                patch.object(module, "_planned_task_from_review_evidence", return_value=(record, _task())),
                patch.object(module, "_validate_tasks"),
                patch.object(module, "AzureJsonClient", return_value=SimpleNamespace()),
            ):
                result, _task_out, _candidate_out, status = module._continue_unresolved_published_test(
                    test_id=1,
                    published_path=published,
                    evidence={},
                    review_dir=root / "review",
                    out=root / "out",
                    context=SimpleNamespace(checkpoint_path=root / "checkpoint"),
                    config=SimpleNamespace(
                        persona="morgan", designer_model="designer", query_model="query"
                    ),
                    grading_corrector=grading_corrector,
                    regrader=lambda _candidate, trace: self._grade(
                        trace["tool_calls"][0]["args"]["to"] == "person@example.com"
                    ),
                    certifier=lambda *_args, **_kwargs: {"valid": True},
                    reviewer=reviewer,
                )

            self.assertEqual(calls, ["correct", "review"])
            self.assertNotIn("refreshed_final_review", result["continuation"])
            self.assertEqual(status, "accepted_after_continuation_review")

    def test_continuation_routes_each_explicit_failure_owner(self) -> None:
        for part, expected_action in (
            ("grading_checks", "corrected_grading_checks"),
            ("execution", "reran_certification"),
            ("user_request", "rewrote_user_request"),
            ("starting_app_data", "redesigned_same_facts"),
            ("test_design", "redesigned_same_facts"),
            ("planning", "replanned_same_facts"),
        ):
            with self.subTest(part=part), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                work, _saved, record = self._saved_unresolved(
                    root=root, test_id=1, part=part
                )
                published = root / "published.yaml"
                published.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))
                calls: list[str] = []

                def certify(*_args: object, **kwargs: object) -> dict:
                    calls.append("certify")
                    return {"valid": True}

                def attempt(**kwargs: object):
                    calls.append(str(kwargs["attempt_name"]))
                    directory = Path(kwargs["directory"])
                    name = str(kwargs["attempt_name"])
                    candidate = directory / f"{name}_candidate.yaml"
                    candidate.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))
                    gate = directory / f"{name}_gate.json"
                    _gate(gate, candidate)
                    return {"candidate": True}, {"stage": "accepted"}

                def complete(**kwargs: object):
                    calls.append("complete_design")
                    directory = Path(kwargs["work_dir"])
                    candidate = directory / "same_facts_redesign_candidate.yaml"
                    candidate.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))
                    gate = directory / "same_facts_redesign_gate.json"
                    _gate(gate, candidate)
                    return {"candidate": True}, {"initial": {}}, candidate, gate

                def redesign(**kwargs: object):
                    calls.append("redesign")
                    directory = Path(kwargs["work_dir"])
                    candidate = directory / "same_facts_redesign_candidate.yaml"
                    candidate.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))
                    gate = directory / "same_facts_redesign_gate.json"
                    _gate(gate, candidate)
                    return {"candidate": True}, {"stage": "accepted"}

                def certify_cached(**kwargs: object) -> dict:
                    candidate = Path(kwargs["candidate_path"])
                    gate = Path(kwargs["result_path"])
                    _gate(gate, candidate)
                    calls.append("certify")
                    return {"valid": True}

                def reviewer(**kwargs: object) -> FinalTraceDecision:
                    request = Path(kwargs["out"]) / "001"
                    request.mkdir(parents=True, exist_ok=True)
                    (request / "request.json").write_text(json.dumps({}))
                    return self._decision(1, accept=True)

                with (
                    patch.object(module, "_planned_task_from_review_evidence", return_value=(record, _task())),
                    patch.object(module, "_validate_tasks"),
                    patch.object(module, "design_payload", return_value={}),
                    patch.object(module, "AzureJsonClient", return_value=SimpleNamespace()),
                    patch.object(
                        module,
                        "_saved_design_for_continuation",
                        return_value=SimpleNamespace(model_dump=lambda **_: {}),
                    ),
                    patch.object(module, "_attempt", side_effect=attempt),
                    patch.object(module, "_replan_from_same_facts", return_value=_task()),
                    patch.object(module, "_complete_same_facts_design", side_effect=complete),
                    patch.object(module, "_redesign_from_same_facts", side_effect=redesign),
                    patch.object(module, "_certify_cached", side_effect=certify_cached),
                ):
                    result, _task_out, candidate, status = module._continue_unresolved_published_test(
                        test_id=1,
                        published_path=published,
                        evidence={},
                        review_dir=root / "review",
                        out=root / "out",
                        context=SimpleNamespace(
                            checkpoint_path=root / "checkpoint", tools={"send_email": {}}
                        ),
                        config=SimpleNamespace(persona="morgan", designer_model="designer", query_model="query"),
                        grading_corrector=lambda **call: _grading_patch(call["request"]),
                        regrader=lambda _candidate, trace: self._grade(
                            trace["tool_calls"][0]["args"]["to"] == "person@example.com"
                        ),
                        certifier=certify,
                        reviewer=reviewer,
                    )
                self.assertEqual(result["action"], expected_action)
                self.assertEqual(status, "accepted_after_continuation_review")
                self.assertIsNotNone(candidate)
                self.assertTrue((work / "previous_results").is_dir())
                if part == "execution":
                    self.assertIn("certify", calls)
                if part == "planning":
                    self.assertIn("complete_design", calls)

    def test_continuation_authenticates_gate_and_uses_named_stage_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, result, record = self._saved_unresolved(
                root=root, test_id=1, part="grading_checks"
            )
            candidate, gate, request = module._latest_saved_candidate_and_gate(
                test_id=1, result=result, work_dir=work, fallback=record
            )
            self.assertEqual(candidate, work / "same_facts_redesign_candidate.yaml")
            self.assertEqual(gate, work / "same_facts_redesign_gate.json")
            self.assertEqual(request, work / "same_facts_redesign_final_review" / "001" / "request.json")
            broken = json.loads(gate.read_text())
            broken["candidate_sha256"] = "wrong"
            gate.write_text(json.dumps(broken))
            with self.assertRaisesRegex(ValueError, "does not match candidate"):
                module._latest_saved_candidate_and_gate(
                    test_id=1, result=result, work_dir=work, fallback=record
                )

    def test_grading_correction_uses_a_fresh_path_for_recertification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, _saved, record = self._saved_unresolved(
                root=root, test_id=1, part="grading_checks"
            )
            published = root / "published.yaml"
            published.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))
            certified_paths: list[Path] = []

            def certify_cached(**kwargs: object) -> dict:
                candidate = Path(kwargs["candidate_path"])
                gate = Path(kwargs["result_path"])
                certified_paths.append(gate)
                _gate(gate, candidate)
                return {"valid": True}

            def reviewer(**kwargs: object) -> FinalTraceDecision:
                request = Path(kwargs["out"]) / "001"
                request.mkdir(parents=True, exist_ok=True)
                (request / "request.json").write_text(json.dumps({}))
                return self._decision(1, accept=True)

            with (
                patch.object(module, "_planned_task_from_review_evidence", return_value=(record, _task())),
                patch.object(module, "_validate_tasks"),
                patch.object(module, "AzureJsonClient", return_value=SimpleNamespace()),
                patch.object(module, "_certify_cached", side_effect=certify_cached),
            ):
                result, _task_out, _candidate_out, status = module._continue_unresolved_published_test(
                    test_id=1,
                    published_path=published,
                    evidence={},
                    review_dir=root / "review",
                    out=root / "out",
                    context=SimpleNamespace(checkpoint_path=root / "checkpoint"),
                    config=SimpleNamespace(
                        persona="morgan", designer_model="designer", query_model="query"
                    ),
                    grading_corrector=lambda **call: _grading_patch(call["request"]),
                    regrader=lambda *_args: self._grade(False),
                    certifier=lambda *_args, **_kwargs: {"valid": True},
                    reviewer=reviewer,
                )

            continuation = Path(result["continuation"]["candidate"]).parent
            self.assertEqual(status, "accepted_after_continuation_review")
            self.assertEqual(certified_paths, [continuation / "grading_checks_gate.json"])
            self.assertTrue((continuation / "grading_checks_regrade.json").is_file())
            self.assertTrue((continuation / "grading_checks_gate.json").is_file())

    def test_continuation_reruns_when_grading_and_execution_are_both_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, saved, record = self._saved_unresolved(
                root=root, test_id=1, part="grading_checks"
            )
            decision = FinalTraceDecision.model_validate(
                {
                    "test_id": 1,
                    "accept": False,
                    "issues": [
                        {
                            "owning_step": "grading_checks",
                            "specific_problem": "The checks accept output without the required role.",
                            "required_correction": "Require the role.",
                        },
                        {
                            "owning_step": "execution",
                            "specific_problem": "The saved agent runs do not prove the corrected checks work.",
                            "required_correction": "Run the corrected test again.",
                        },
                    ],
                }
            )
            saved["same_facts_redesign_final_review"] = decision.model_dump(mode="json")
            (work / "result.json").write_text(json.dumps(saved))
            published = root / "published.yaml"
            published.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))
            outcomes = iter([True, True, False, False])
            certified_paths: list[Path] = []

            def certify_cached(**kwargs: object) -> dict:
                candidate = Path(kwargs["candidate_path"])
                gate = Path(kwargs["result_path"])
                certified_paths.append(gate)
                _gate(gate, candidate)
                return {"valid": True}

            def reviewer(**kwargs: object) -> FinalTraceDecision:
                request = Path(kwargs["out"]) / "001"
                request.mkdir(parents=True, exist_ok=True)
                (request / "request.json").write_text(json.dumps({}))
                return self._decision(1, accept=True)

            with (
                patch.object(module, "_planned_task_from_review_evidence", return_value=(record, _task())),
                patch.object(module, "_validate_tasks"),
                patch.object(module, "AzureJsonClient", return_value=SimpleNamespace()),
                patch.object(module, "_certify_cached", side_effect=certify_cached),
            ):
                result, _task_out, _candidate_out, status = module._continue_unresolved_published_test(
                    test_id=1,
                    published_path=published,
                    evidence={},
                    review_dir=root / "review",
                    out=root / "out",
                    context=SimpleNamespace(checkpoint_path=root / "checkpoint"),
                    config=SimpleNamespace(
                        persona="morgan", designer_model="designer", query_model="query"
                    ),
                    grading_corrector=lambda **call: _grading_patch(call["request"]),
                    regrader=lambda *_args: self._grade(next(outcomes)),
                    certifier=lambda *_args, **_kwargs: {"valid": True},
                    reviewer=reviewer,
                )

            continuation = Path(result["continuation"]["candidate"]).parent
            self.assertEqual(status, "accepted_after_continuation_review")
            self.assertTrue(result["certification_rerun"])
            self.assertEqual(certified_paths, [continuation / "grading_checks_gate.json"])

    def test_continuation_preserves_result_and_refuses_repeating_same_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, _saved, record = self._saved_unresolved(
                root=root, test_id=1, part="grading_checks"
            )
            published = root / "published.yaml"
            published.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))

            def reviewer(**kwargs: object) -> FinalTraceDecision:
                request = Path(kwargs["out"]) / "001"
                request.mkdir(parents=True, exist_ok=True)
                (request / "request.json").write_text(json.dumps({}))
                return self._decision(1, accept=False, part="grading_checks")

            with (
                patch.object(module, "_planned_task_from_review_evidence", return_value=(record, _task())),
                patch.object(module, "_validate_tasks"),
                patch.object(module, "AzureJsonClient", return_value=SimpleNamespace()),
            ):
                module._continue_unresolved_published_test(
                    test_id=1, published_path=published, evidence={}, review_dir=root / "review", out=root / "out",
                    context=SimpleNamespace(checkpoint_path=root / "checkpoint"),
                    config=SimpleNamespace(persona="morgan", designer_model="designer", query_model="query"),
                    grading_corrector=lambda **call: _grading_patch(call["request"]),
                    regrader=lambda _candidate, trace: self._grade(
                        trace["tool_calls"][0]["args"]["to"] == "person@example.com"
                    ),
                    certifier=lambda *_args, **_kwargs: {"valid": True}, reviewer=reviewer,
                )
                with self.assertRaisesRegex(ValueError, "refusing to repeat"):
                    module._continue_unresolved_published_test(
                        test_id=1, published_path=published, evidence={}, review_dir=root / "review", out=root / "out",
                        context=SimpleNamespace(checkpoint_path=root / "checkpoint"),
                        config=SimpleNamespace(persona="morgan", designer_model="designer", query_model="query"),
                        grading_corrector=lambda **_: {},
                        regrader=lambda *_: self._grade(True),
                        certifier=lambda *_args, **_kwargs: {"valid": True}, reviewer=reviewer,
                    )
            snapshots = list((work / "previous_results").glob("*.json"))
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(json.loads(snapshots[0].read_text())["status"], "unresolved_after_redesign")

    def test_continuation_uses_saved_local_validation_failure_before_old_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, saved, record = self._saved_unresolved(
                root=root, test_id=1, part="starting_app_data"
            )
            exact_error = (
                "AuthoringValidationError: new record state_key 'pull_requests' "
                "is not discoverable through a read tool"
            )
            saved.update(
                status="unresolved_after_continuation",
                continuation={
                    "changed_step": "starting_app_data",
                    "source_failure": saved["same_facts_redesign_final_review"],
                    "source_failure_sha256": "old-review-hash",
                },
                attempt={
                    "stage": "local_validation",
                    "reason": exact_error,
                    "design": {},
                },
            )
            (work / "result.json").write_text(json.dumps(saved))
            published = root / "published.yaml"
            published.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))
            captured: dict = {}

            def redesign(**kwargs: object):
                captured.update(kwargs["failure"])
                directory = Path(kwargs["work_dir"])
                candidate = directory / "same_facts_redesign_candidate.yaml"
                candidate.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))
                gate = directory / "same_facts_redesign_gate.json"
                _gate(gate, candidate)
                return {"candidate": True}, {"stage": "accepted"}

            def reviewer(**kwargs: object) -> FinalTraceDecision:
                request = Path(kwargs["out"]) / "001"
                request.mkdir(parents=True, exist_ok=True)
                (request / "request.json").write_text(json.dumps({}))
                return self._decision(1, accept=True)

            with (
                patch.object(module, "_planned_task_from_review_evidence", return_value=(record, _task())),
                patch.object(module, "_validate_tasks"),
                patch.object(module, "AzureJsonClient", return_value=SimpleNamespace()),
                patch.object(module, "_redesign_from_same_facts", side_effect=redesign),
            ):
                result, _task_out, _candidate_out, status = module._continue_unresolved_published_test(
                    test_id=1, published_path=published, evidence={}, review_dir=root / "review", out=root / "out",
                    context=SimpleNamespace(
                        checkpoint_path=root / "checkpoint",
                        persona="morgan",
                        tools={"send_email": {}},
                        readable_state={},
                    ),
                    config=SimpleNamespace(persona="morgan", designer_model="designer", query_model="query"),
                    grading_corrector=lambda **_: {},
                    regrader=lambda *_: self._grade(True),
                    certifier=lambda *_args, **_kwargs: {"valid": True}, reviewer=reviewer,
                )
            self.assertEqual(result["action"], "redesigned_same_facts")
            self.assertEqual(status, "accepted_after_continuation_review")
            self.assertEqual(captured["stage"], "local_validation")
            self.assertEqual(captured["part_to_correct"], "starting_app_data")
            self.assertEqual(captured["reason"], exact_error)
            self.assertTrue(Path(result["previous_result"]).is_file())

    def test_approved_corrections_apply_only_the_named_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.yaml"
            source.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))

            grading = _candidate(1)["grade"]["config"]["assertions"][0]
            replacement = {**grading, "path": "args"}
            grading_out = root / "grading.yaml"
            module._apply_approved_correction(
                source=source,
                destination=grading_out,
                correction={
                    "part_to_correct": "grading_checks",
                    "change": {
                        "assertion_index": 0,
                        "expected_assertion_sha256": module._json_sha256(grading),
                        "replacement_assertion": replacement,
                    },
                },
            )
            grading_value = yaml.safe_load(grading_out.read_text())
            expected = _candidate(1)
            expected["grade"]["config"]["assertions"][0] = replacement
            self.assertEqual(grading_value, expected)

            request_out = root / "request.yaml"
            module._apply_approved_correction(
                source=source,
                destination=request_out,
                correction={
                    "part_to_correct": "user_request",
                    "change": {"replacement_request": "Please send the corrected update."},
                },
            )
            request_value = yaml.safe_load(request_out.read_text())
            expected = _candidate(1)
            expected["test"] = "Please send the corrected update."
            self.assertEqual(request_value, expected)

    def test_approved_corrections_authenticate_saved_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "out"
            result = out / "repair" / "001" / "result.json"
            result.parent.mkdir(parents=True)
            result.write_text(json.dumps({"id": 1, "status": "unresolved_after_continuation"}))
            candidate = root / "candidate.yaml"
            candidate.write_text(yaml.safe_dump(_candidate(1), sort_keys=False))
            gate = root / "gate.json"
            _gate(gate, candidate)
            assertion = _candidate(1)["grade"]["config"]["assertions"][0]
            correction_path = root / "corrections.json"
            correction = {
                "test_id": 1,
                "source_result_sha256": hashlib.sha256(result.read_bytes()).hexdigest(),
                "part_to_correct": "grading_checks",
                "specific_problem": "The check examines the wrong field.",
                "required_correction": "Examine the complete tool arguments.",
                "source_candidate": str(candidate),
                "source_candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                "source_gate": str(gate),
                "source_gate_sha256": hashlib.sha256(gate.read_bytes()).hexdigest(),
                "change": {
                    "assertion_index": 0,
                    "expected_assertion_sha256": module._json_sha256(assertion),
                    "replacement_assertion": {**assertion, "path": "args"},
                },
            }
            correction_path.write_text(json.dumps({"corrections": [correction]}))
            loaded = module._load_approved_corrections(
                path=correction_path, expected_test_ids={1}, out=out
            )
            self.assertEqual(set(loaded), {1})

            design_correction = {
                **correction,
                "part_to_correct": "test_design",
                "required_correction": "Redesign the same test from the same fact.",
                "change": {
                    "redesign_instruction": "Ask for a corrected historical caption."
                },
            }
            correction_path.write_text(json.dumps({"corrections": [design_correction]}))
            loaded = module._load_approved_corrections(
                path=correction_path, expected_test_ids={1}, out=out
            )
            self.assertEqual(loaded[1]["part_to_correct"], "test_design")

            result.write_text(json.dumps({"id": 1, "status": "changed"}))
            with self.assertRaisesRegex(ValueError, "source result changed"):
                module._load_approved_corrections(
                    path=correction_path, expected_test_ids={1}, out=out
                )


if __name__ == "__main__":
    unittest.main()
