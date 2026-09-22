from __future__ import annotations

import hashlib
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from authoring.create_tests import resume_new_test_run
from authoring.context import dump_json
from authoring.reconcile_review_status import reconcile_pending_reviews
from tests.unit.authoring.creation_fixture import writer
from tests.unit.authoring import staged_fixture as staged_fixtures
from authoring.final_trace_review import FinalTraceDecision, build_final_trace_review_request
from authoring.models import (
    FactApplicationExplanationOnEvaluationDate,
    PlannedTask,
    RequiredResult,
)


class NewTestResumeTests(unittest.TestCase):
    def test_resume_uses_bounded_workers_and_preserves_other_results_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._fixture(root)
            ids = list(range(1, 9))
            records = {
                test_id: {
                    "resume_kind": "saved_stage", "progress": {"plan_position": 1},
                    "model_corrections_used": 1, "execution_reruns_used": 1,
                } for test_id in ids
            }
            barrier = threading.Barrier(4, timeout=5)
            lock = threading.Lock()
            active = peak = 0
            completed = []

            def resume_stage(*, test_id, **kwargs):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    barrier.wait()
                    if test_id == 2:
                        raise ValueError("isolated review failure")
                    with lock:
                        completed.append(test_id)
                    candidate = fixture["candidate"] if test_id == 1 else None
                    return candidate, {"id": test_id, "status": "completed"}, {"id": test_id}
                finally:
                    with lock:
                        active -= 1

            with (
                patch("authoring.create_tests.load_config", return_value=fixture["config"]),
                patch("authoring.create_tests.load_checkpoint_context", return_value=fixture["context"]),
                patch("authoring.create_tests.load_authoring_tasks", return_value=[fixture["task"]]),
                patch("authoring.create_tests._progress_resume_inputs", return_value=(
                    records, dict.fromkeys(ids, fixture["task"]), {"authenticated_files": {}},
                )),
                patch("authoring.create_tests._continue_saved_stage", side_effect=resume_stage),
                patch("authoring.create_tests.write_completed_batch", return_value=([], [])),
                patch("authoring.create_tests.record_accepted_batch"),
            ):
                result = resume_new_test_run(
                    config_path=fixture["config_path"], plan_path=fixture["plan_path"],
                    source_run=fixture["source"], out=root / "parallel", test_ids=ids,
                    concurrency=4, confirm_paid_calls=True,
                )
            self.assertEqual(peak, 4)
            self.assertEqual(sorted(completed), [1, 3, 4, 5, 6, 7, 8])
            self.assertEqual(result["final_review_accepted"], 1)
            self.assertEqual([row["id"] for row in result["authoring_results"]], ids)
            failure = result["authoring_results"][1]
            self.assertEqual(failure["status"], "resume_pending")
            self.assertEqual(failure["model_corrections_used"], 1)
            self.assertEqual(failure["execution_reruns_used"], 1)

    @staticmethod
    def _json_hash(value: object) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _task(self) -> PlannedTask:
        return PlannedTask(
            task_description="Send the account update.",
            fact_ids=[1],
            required_results=[
                RequiredResult(result="Use the remembered account name.", fact_ids=[1])
            ],
            expected_tools=["update_crm"],
            action_without_memory="The assistant cannot identify the account.",
            why_memory_changes_result="The account name changes the record updated.",
            fact_application_explanations_on_evaluation_date=[
                FactApplicationExplanationOnEvaluationDate(
                    fact_id=1,
                    why_fact_still_applies_on_evaluation_date="No later message changes it.",
                )
            ],
            why_listed_tools_can_complete_requested_work=(
                "update_crm accepts the record name and status."
            ),
        )

    def _fixture(self, root: Path) -> dict[str, object]:
        config_path = root / "config.yaml"
        config_path.write_text("persona: morgan\n")
        plan_path = root / "candidate_plan.json"
        plan_path.write_text('{"tasks": [{"fixture": true}]}\n')
        source = root / "source"
        proposal = source / "authoring" / "part_01" / "proposals" / "001"
        review = source / "final_trace_reviews" / "001"
        proposal.mkdir(parents=True)
        review.mkdir(parents=True)

        candidate = proposal / "initial_candidate.yaml"
        candidate_value = {
            "id": "001",
            "narrative_anchor_date": "2026-09-14",
            "test": "Send the update.",
            "load_bearing_facts": [1],
            "expected_tool_calls": ["update_crm"],
            "grade": {
                "type": "tool_trace",
                "config": {
                    "assertions": [
                        {
                            "type": "field_llm_judge",
                            "tool": "update_crm",
                            "path": "args.name",
                            "criterion": "Does the update use the right account name?",
                        },
                        {
                            "type": "field_equals",
                            "tool": "update_crm",
                            "path": "args.status",
                            "value": "active",
                        },
                    ]
                },
            },
            "mock_state": {},
        }
        candidate.write_text(yaml.safe_dump(candidate_value, sort_keys=False))
        candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        gate = proposal / "initial_gate.json"
        shots = [
            {
                "with_memory": True,
                "response_text": "good history result",
                "tool_calls": [{"name": "update_crm", "arguments": {}}],
                "passed": False,
                "grade": {"passed": False},
            },
            {
                "with_memory": True,
                "response_text": "good history result",
                "tool_calls": [{"name": "update_crm", "arguments": {}}],
                "passed": False,
                "grade": {"passed": False},
            },
            {
                "with_memory": False,
                "response_text": "no history result",
                "tool_calls": [{"name": "update_crm", "arguments": {}}],
                "passed": False,
                "grade": {"passed": False},
            },
            {
                "with_memory": False,
                "response_text": "no history result",
                "tool_calls": [{"name": "update_crm", "arguments": {}}],
                "passed": False,
                "grade": {"passed": False},
            },
        ]
        gate.write_text(
            json.dumps(
                {
                    "candidate_sha256": candidate_hash,
                    "result": {
                        "valid": False,
                        "verdict": "unsolvable_with_memory",
                        "g1_pass_count": 0,
                        "g2_pass_count": 0,
                        "shots": shots,
                    },
                },
                indent=2,
            )
            + "\n"
        )

        task = self._task()
        context = SimpleNamespace(
            checkpoint_identity="checkpoint-1",
            checkpoint_path=root / "checkpoint",
            persona="morgan",
            facts_by_id={
                1: {
                    "statement": "Use Northstar Ventures.",
                    "applies_when": "Current",
                    "supersedes": [],
                    "source_session_ids": ["session-1"],
                }
            },
            sessions_by_id={
                "session-1": {"session_id": "session-1", "message": "Use Northstar Ventures."}
            },
            tools={
                "update_crm": {
                    "description": "Update one CRM record.",
                    "arguments": ["name", "status"],
                }
            },
        )
        config = SimpleNamespace(
            persona="morgan",
            evaluation_date="2026-09-14",
            designer_model="designer",
            query_model="query",
        )
        request = build_final_trace_review_request(
            candidate_path=candidate,
            certification_gate_path=gate,
            planned_task=task,
            context=context,
        )
        (review / "request.json").write_text(json.dumps(request, indent=2) + "\n")
        decision = FinalTraceDecision.model_validate(
            {
                "test_id": 1,
                "accept": False,
                "issues": [
                    {
                        "owning_step": "grading_checks",
                        "specific_problem": "The first check is too vague.",
                        "required_correction": "Require the remembered account name.",
                        "request_requirement_quote": "Send the update.",
                        "source_session_id": "",
                        "source_text_quote": "",
                    }
                ],
            }
        )
        decision_json = decision.model_dump(mode="json")
        (review / "decision.json").write_text(json.dumps(decision_json, indent=2) + "\n")

        row = {
            "id": 1,
            "fact_ids": [1],
            "status": "review_required",
            "candidate": str(candidate.resolve()),
            "gate": str(gate.resolve()),
            "authoring_mode": "unified",
        }
        part_manifest = {
            "checkpoint_identity": context.checkpoint_identity,
            "persona": context.persona,
            "evaluation_date": config.evaluation_date,
            "config_path": str(config_path.resolve()),
            "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "plan_path": str(plan_path.resolve()),
            "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            "selected_test_ids": [1],
            "selected_plan_positions": [1],
            "results": [row],
        }
        (source / "authoring" / "part_01" / "manifest.json").write_text(
            json.dumps(part_manifest, indent=2) + "\n"
        )
        source_manifest = {
            "checkpoint_identity": context.checkpoint_identity,
            "plan_path": str(plan_path.resolve()),
            "start_id": None,
            "test_ids": [1],
            "candidate_count": 1,
            "authoring_results": [row],
            "final_review_results": [
                {
                    "id": 1,
                    "initial_final_review": decision_json,
                    "status": "replacement_required_after_one_correction",
                }
            ],
        }
        (source / "manifest.json").write_text(json.dumps(source_manifest, indent=2) + "\n")
        return {
            "config_path": config_path,
            "plan_path": plan_path,
            "source": source,
            "candidate": candidate,
            "gate": gate,
            "decision": review / "decision.json",
            "config": config,
            "context": context,
            "task": task,
        }

    @staticmethod
    def _correction(**_: object) -> dict[str, object]:
        return {
            "operations": [
                {
                    "operation": "replace",
                    "issue_index": 0,
                    "assertion_index": 0,
                    "assertion": {
                        "type": "field_llm_judge",
                        "tool": "update_crm",
                        "path": "args.name",
                        "value": None,
                        "pattern": None,
                        "criterion": "Does the update name Northstar Ventures?",
                        "count": None,
                    },
                }
            ],
            "expected_no_history_failed_check_index": 0,
        }

    @staticmethod
    def _regrader(_candidate: object, result: dict[str, object]) -> dict[str, object]:
        passed = str(result["response_text"]).startswith("good")
        return {"passed": passed, "assertions": []}

    def _run(self, fixture: dict[str, object], out: Path, **kwargs: object) -> dict[str, object]:
        reviewer = kwargs.pop("reviewer", None) or Mock(
            return_value=FinalTraceDecision(test_id=1, accept=True)
        )
        with (
            patch("authoring.create_tests.load_config", return_value=fixture["config"]),
            patch(
                "authoring.create_tests.load_checkpoint_context",
                return_value=fixture["context"],
            ),
            patch(
                "authoring.create_tests.load_authoring_tasks",
                return_value=[fixture["task"]],
            ),
            patch("authoring.create_tests.AzureJsonClient", return_value=Mock()),
            patch("authoring.create_tests._run_authoring_parts") as authoring,
            patch("authoring.create_tests._author_new_test_attempt") as author_test,
            patch("authoring.create_tests._attempt") as attempt,
        ):
            manifest = resume_new_test_run(
                config_path=fixture["config_path"],
                plan_path=fixture["plan_path"],
                source_run=fixture["source"],
                out=out,
                test_ids=[1],
                concurrency=1,
                confirm_paid_calls=True,
                grading_corrector=self._correction,
                regrader=self._regrader,
                certifier=kwargs.pop("certifier", Mock(side_effect=AssertionError("Hermes ran"))),
                reviewer=reviewer,
                **kwargs,
            )
            authoring.assert_not_called()
            author_test.assert_not_called()
            attempt.assert_not_called()
            return manifest

    def _approved_correction(
        self, fixture: dict[str, object], path: Path, *, multiple: bool = False
    ) -> Path:
        candidate = fixture["candidate"]
        gate = fixture["gate"]
        source_manifest = fixture["source"] / "manifest.json"
        candidate_value = yaml.safe_load(candidate.read_text())
        original = candidate_value["grade"]["config"]["assertions"][0]
        replacement = dict(original)
        replacement["criterion"] = "Does the update name Northstar Ventures?"
        change: dict[str, object] = {
            "assertion_index": 0,
            "expected_assertion_sha256": self._json_hash(original),
            "replacement_assertion": replacement,
        }
        if multiple:
            second = candidate_value["grade"]["config"]["assertions"][1]
            second_replacement = dict(second)
            second_replacement["value"] = "inactive"
            change = {
                "assertion_replacements": [
                    change,
                    {
                        "assertion_index": 1,
                        "expected_assertion_sha256": self._json_hash(second),
                        "replacement_assertion": second_replacement,
                    },
                ]
            }
        value = {
            "corrections": [
                {
                    "test_id": 1,
                    "source_result_sha256": hashlib.sha256(
                        source_manifest.read_bytes()
                    ).hexdigest(),
                    "part_to_correct": "grading_checks",
                    "specific_problem": "The first check is too vague.",
                    "required_correction": "Require the remembered account name.",
                    "source_candidate": str(candidate.resolve()),
                    "source_candidate_sha256": hashlib.sha256(
                        candidate.read_bytes()
                    ).hexdigest(),
                    "source_gate": str(gate.resolve()),
                    "source_gate_sha256": hashlib.sha256(gate.read_bytes()).hexdigest(),
                    "change": change,
                }
            ]
        }
        path.write_text(json.dumps(value, indent=2) + "\n")
        return path

    def test_applies_approved_grading_change_and_reuses_saved_executions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root)
            correction = self._approved_correction(fixture, root / "correction.json")
            source_hashes = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in fixture["source"].rglob("*")
                if path.is_file()
            }
            reviewer = Mock(return_value=FinalTraceDecision(test_id=1, accept=True))

            manifest = self._run(
                fixture,
                root / "resume",
                reviewer=reviewer,
                approved_corrections_path=correction,
            )

            self.assertEqual(manifest["final_review_accepted"], 1)
            reviewer.assert_called_once()
            accepted = yaml.safe_load(Path(manifest["accepted_candidates"][0]).read_text())
            source = yaml.safe_load(fixture["candidate"].read_text())
            accepted_assertions = accepted["grade"]["config"]["assertions"]
            source_assertions = source["grade"]["config"]["assertions"]
            self.assertEqual(
                accepted_assertions[0]["criterion"],
                "Does the update name Northstar Ventures?",
            )
            self.assertEqual(accepted_assertions[1:], source_assertions[1:])
            regrade = json.loads(
                (
                    root
                    / "resume"
                    / "authoring"
                    / "part_01"
                    / "proposals"
                    / "001"
                    / "approved_grading_correction_regrade.json"
                ).read_text()
            )
            self.assertEqual(regrade["result"]["g1_pass_count"], 2)
            self.assertEqual(regrade["result"]["g2_pass_count"], 0)
            self.assertEqual(
                source_hashes,
                {
                    path: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in fixture["source"].rglob("*")
                    if path.is_file()
                },
            )

    def test_applies_several_approved_grading_changes_in_one_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root)
            correction = self._approved_correction(
                fixture, root / "correction.json", multiple=True
            )

            manifest = self._run(
                fixture,
                root / "resume",
                approved_corrections_path=correction,
            )

            self.assertEqual(manifest["final_review_accepted"], 1)
            accepted = yaml.safe_load(Path(manifest["accepted_candidates"][0]).read_text())
            assertions = accepted["grade"]["config"]["assertions"]
            self.assertEqual(
                assertions[0]["criterion"], "Does the update name Northstar Ventures?"
            )
            self.assertEqual(assertions[1]["value"], "inactive")

    def test_approved_grading_change_allows_no_final_review_after_failed_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root)
            manifest_path = fixture["source"] / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["final_review_results"] = []
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
            correction = self._approved_correction(fixture, root / "correction.json")

            result = self._run(
                fixture,
                root / "resume",
                approved_corrections_path=correction,
            )

            self.assertEqual(result["final_review_accepted"], 1)
            self.assertEqual(
                result["final_review_results"][0]["status"],
                "accepted_after_final_review_correction",
            )

    def test_approved_grading_change_rejects_changed_hashes(self) -> None:
        changes = {
            "source manifest": lambda fixture, correction: (
                fixture["source"] / "manifest.json"
            ).write_text("{}\n"),
            "candidate": lambda fixture, correction: fixture["candidate"].write_text(
                "changed\n"
            ),
            "gate": lambda fixture, correction: fixture["gate"].write_text("{}\n"),
            "assertion": lambda fixture, correction: (
                lambda value: correction.write_text(json.dumps(value, indent=2) + "\n")
            )(
                {
                    **json.loads(correction.read_text()),
                    "corrections": [
                        {
                            **json.loads(correction.read_text())["corrections"][0],
                            "change": {
                                **json.loads(correction.read_text())["corrections"][0][
                                    "change"
                                ],
                                "expected_assertion_sha256": "0" * 64,
                            },
                        }
                    ],
                }
            ),
        }
        for label, change in changes.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = self._fixture(root)
                correction = self._approved_correction(fixture, root / "correction.json")
                change(fixture, correction)
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    self._run(
                        fixture,
                        root / "resume",
                        approved_corrections_path=correction,
                    )
                self.assertFalse((root / "resume").exists())

    def test_reuses_saved_executions_without_authoring_or_hermes_and_accepts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root)
            source_hashes = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in fixture["source"].rglob("*")
                if path.is_file()
            }
            certifier = Mock(side_effect=AssertionError("Hermes ran"))
            reviewer = Mock(return_value=FinalTraceDecision(test_id=1, accept=True))
            with patch(
                "authoring.create_tests.record_accepted_batch"
            ) as record_accepted:
                manifest = self._run(
                    fixture,
                    root / "resume",
                    certifier=certifier,
                    reviewer=reviewer,
                )

            self.assertEqual(manifest["final_review_accepted"], 1)
            certifier.assert_not_called()
            reviewer.assert_called_once()
            correction = manifest["final_review_results"][0]["correction"]
            self.assertFalse(correction["certification_rerun"])
            self.assertEqual(correction["status"], "candidate_certified")
            accepted = Path(manifest["accepted_candidates"][0])
            self.assertTrue(accepted.is_file())
            self.assertTrue((root / "resume" / "accepted_batch" / "planning_batch.json").is_file())
            provenance = json.loads(
                (root / "resume" / "accepted_batch" / "provenance.json").read_text()
            )
            self.assertEqual(provenance["accepted_test_ids"], [1])
            self.assertEqual(
                source_hashes,
                {
                    path: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in fixture["source"].rglob("*")
                    if path.is_file()
                },
            )
            record_accepted.assert_called_once_with(
                config=fixture["config"],
                context=fixture["context"],
                directory=root / "resume" / "accepted_batch",
            )

    def test_rejects_changed_candidate_gate_plan_or_decision(self) -> None:
        changes = {
            "candidate": lambda fixture: fixture["candidate"].write_text("changed\n"),
            "gate": lambda fixture: fixture["gate"].write_text("{}\n"),
            "plan": lambda fixture: fixture["plan_path"].write_text('{"changed": true}\n'),
            "decision": lambda fixture: fixture["decision"].write_text("{}\n"),
        }
        for label, change in changes.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = self._fixture(root)
                change(fixture)
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    self._run(fixture, root / "resume")
                self.assertFalse((root / "resume").exists())

    def test_rejects_a_non_grading_saved_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root)
            decision = FinalTraceDecision.model_validate(
                {
                    "test_id": 1,
                    "accept": False,
                    "issues": [
                        {
                            "owning_step": "user_request",
                            "specific_problem": "The request reveals the answer.",
                            "required_correction": "Remove the answer.",
                            "request_requirement_quote": "Send the update.",
                            "source_session_id": "",
                            "source_text_quote": "",
                        }
                    ],
                }
            ).model_dump(mode="json")
            fixture["decision"].write_text(json.dumps(decision) + "\n")
            manifest_path = fixture["source"] / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["final_review_results"][0]["initial_final_review"] = decision
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

            with self.assertRaisesRegex(ValueError, "grading-check final review"):
                self._run(fixture, root / "resume")
            self.assertFalse((root / "resume").exists())


class HistoricalAcceptanceResumeTests(unittest.TestCase):
    def _fixture(self, root: Path) -> dict[str, object]:
        from authoring.context import dump_json
        from authoring.final_trace_review import FinalTraceReviewResponse
        from construction.runtime_model_calls import request_hash

        fixture = NewTestResumeTests()._fixture(root)
        source = fixture["source"]
        (source / "manifest.json").unlink()
        gate = json.loads(fixture["gate"].read_text())
        gate["result"].update(valid=True, verdict="valid", g1_pass_count=2)
        for shot in gate["result"]["shots"]:
            shot["passed"] = shot["with_memory"]
            shot["grade"]["passed"] = shot["with_memory"]
        dump_json(fixture["gate"], gate)
        part = source / "authoring" / "part_01" / "manifest.json"
        manifest = json.loads(part.read_text())
        manifest["results"][0]["status"] = "certified_initial"
        dump_json(part, manifest)
        request = build_final_trace_review_request(
            candidate_path=fixture["candidate"], certification_gate_path=fixture["gate"],
            planned_task=fixture["task"], context=fixture["context"],
        )
        raw = {
            "test_id": 1, "accept": True, "issues": [],
            "audit": {
                "user_request_requirements": [{"requirement": "Send the update.", "assertion_indexes": [0, 1]}],
                "planned_required_results": [{"planned_result_index": 0, "requirement": "Use the account name.", "assertion_indexes": [0]}],
                "grading_assertions": [
                    {"assertion_index": index, "normal_correct_value": "Correct value.", "incorrect_or_incomplete_value": "Wrong value."}
                    for index in range(2)
                ],
                "source_and_date_checks": [{
                    "fact_id": 1, "source_supports_required_result": True,
                    "source_evidence": "The source names Northstar Ventures.",
                    "applies_on_evaluation_date": True, "date_evidence": "No later replacement.",
                }],
                "tool_capability_checks": [{
                    "tool_name": "update_crm", "needed_capability": "Update an account.",
                    "contract_supports_capability": True, "contract_evidence": "Accepts name and status.",
                }],
            },
        }
        review = fixture["decision"].parent
        schema = FinalTraceReviewResponse.model_json_schema()
        (review / "system.txt").write_text("saved system\n")
        dump_json(review / "request.json", request)
        dump_json(review / "schema.json", schema)
        dump_json(review / "response.json", raw)
        dump_json(fixture["decision"], FinalTraceDecision.model_validate(raw).model_dump(mode="json"))
        dump_json(review / "work" / "response_cache.json", {
            "request_sha256": request_hash("saved system", request, response_schema=schema, response_schema_name="enact_final_trace_review"),
            "response": raw,
        })
        return fixture

    def test_historical_acceptance_is_recovered_without_review_or_agent_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root)
            before = {str(path): path.read_bytes() for path in fixture["source"].rglob("*") if path.is_file()}
            reviewer = Mock(side_effect=AssertionError("review repeated"))
            manifest = NewTestResumeTests()._run(fixture, root / "resume", reviewer=reviewer)
            reviewer.assert_not_called()
            self.assertEqual(manifest["final_review_accepted"], 1)
            self.assertEqual(len(manifest["accepted_candidates"]), 1)
            self.assertEqual(Path(manifest["accepted_candidates"][0]).read_bytes(), fixture["candidate"].read_bytes())
            self.assertEqual(before, {str(path): path.read_bytes() for path in fixture["source"].rglob("*") if path.is_file()})

    def test_historical_acceptance_rejects_changed_evidence_before_output(self) -> None:
        for mutation in ("cache", "decision", "request", "gate"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = self._fixture(root)
                review = fixture["decision"].parent
                if mutation == "cache":
                    path = review / "work" / "response_cache.json"
                    value = json.loads(path.read_text())
                    value["request_sha256"] = "changed"
                elif mutation == "decision":
                    path = fixture["decision"]
                    value = json.loads(path.read_text())
                    value["test_id"] = 2
                elif mutation == "request":
                    path = review / "request.json"
                    value = json.loads(path.read_text())
                    value["user_request"] = "Different work."
                else:
                    path = fixture["gate"]
                    value = json.loads(path.read_text())
                    value["result"]["valid"] = False
                path.write_text(json.dumps(value))
                reviewer = Mock(side_effect=AssertionError("review ran"))
                with self.assertRaises(ValueError):
                    NewTestResumeTests()._run(fixture, root / "resume", reviewer=reviewer)
                reviewer.assert_not_called()
                self.assertFalse((root / "resume").exists())


class ReconcileReviewStatusTests(unittest.TestCase):
    def setUp(self):
        self.fixture = staged_fixtures.StagedFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def source(self, decision="pending"):
        draft = writer()
        draft["quality_audit"] = None
        issue = {"part": "planning", "check_ids": [], "source_message_ids": [],
                 "problem": "The saved evidence needs inspection.", "required_change": "Keep pending without another call."}
        self.fixture._run_stages([draft], [{"decision": decision, "issues": [issue]}], workflow="production")
        source = self.root / "staged"
        progress_path = source / "progress/001.json"
        row = json.loads(progress_path.read_text())
        row["status"] = "replacement_required"
        dump_json(progress_path, row)
        manifest = json.loads((source / "manifest.json").read_text())
        manifest["authoring_results"] = [row]
        dump_json(source / "manifest.json", manifest)
        from authoring.finalize_batch import write_completed_batch
        write_completed_batch(out=source, plan_path=self.fixture.plan, tasks_by_id={1: self.fixture.idea},
                              checkpoint_identity=self.fixture.context.checkpoint_identity,
                              persona=self.fixture.context.persona, evaluation_date=self.fixture.config.evaluation_date,
                              accepted={}, authoring_results=[row], final_review_results=[])
        state_path = self.root / "persona_state.json"
        state = json.loads(state_path.read_text())
        state["rejected_test_files"] = [{"path": str(source / "rejected_tests.json"),
                                        "sha256": hashlib.sha256((source / "rejected_tests.json").read_bytes()).hexdigest()}]
        dump_json(state_path, state)
        return source

    def reconcile(self, source):
        with (patch("authoring.reconcile_review_status.load_checkpoint_context", return_value=self.fixture.context),
              patch("authoring.propose.load_persona_state", return_value={
                  "approved_plan_paths": [self.fixture.plan], "accepted_batch_dirs": []}),
              patch("construction.llm.AzureJsonClient.complete", side_effect=AssertionError("no provider calls")) as model,
              patch("authoring.staged_creation.certify_candidate", side_effect=AssertionError("no oracle")) as oracle):
            result = reconcile_pending_reviews(config_path=self.fixture.config_path, plan_path=self.fixture.plan,
                                               source_run=source, out=self.root / "reconciled", test_ids=[1])
        model.assert_not_called()
        oracle.assert_not_called()
        return result

    def test_reconcile_only_planning_classification_and_preserve_original_records(self):
        source = self.source()
        hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob("*") if p.is_file()}
        state_before = json.loads((self.root / "persona_state.json").read_text())
        result = self.reconcile(source)
        self.assertEqual(result["provider_calls"], 0)
        self.assertEqual(result["final_review_accepted"], 0)
        self.assertEqual(result["authoring_results"][0]["status"], "review_pending")
        for path, digest in hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        state = json.loads((self.root / "persona_state.json").read_text())
        self.assertEqual({k: v for k, v in state.items() if k != "rejected_test_files"},
                         {k: v for k, v in state_before.items() if k != "rejected_test_files"})
        self.assertEqual(json.loads((self.root / "reconciled/rejected_tests.json").read_text()), {"rejected_tests": []})
        from authoring.progress import load_progress
        progress, _ = load_progress(source_run=self.root / "reconciled", test_id=1,
                                   config_path=self.fixture.config_path, plan_path=self.fixture.plan,
                                   config=self.fixture.config, context=self.fixture.context, tasks=[self.fixture.idea])
        self.assertEqual(progress["status"], "review_pending")

    def test_real_rejection_cannot_be_reclassified(self):
        with self.assertRaisesRegex(ValueError, "does not establish"):
            self.reconcile(self.source(decision="reject"))
        self.assertFalse((self.root / "reconciled").exists())

    def test_changed_saved_decision_is_refused_before_writing(self):
        source = self.source()
        path = source / "final_trace_reviews/001/initial/decision.json"
        path.write_text(path.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            self.reconcile(source)
        self.assertFalse((self.root / "reconciled").exists())

    def test_acceptance_in_another_batch_is_protected(self):
        source = self.source()
        with (patch("authoring.propose._accepted_test_summaries", return_value=[{"test_id": 1}]),
              self.assertRaisesRegex(ValueError, "accepted in another batch")):
            self.reconcile(source)
        self.assertFalse((self.root / "reconciled").exists())

    def test_public_command_dispatches_local_only_mode(self):
        from authoring.create_tests import main
        output = io.StringIO()
        with (patch("sys.argv", ["create_tests", "--config", str(self.fixture.config_path),
                                "--plan", str(self.fixture.plan), "--resume-new-test-run", str(self.root / "source"),
                                "--test-ids", "1", "--out", str(self.root / "new"), "--reconcile-review-statuses"]),
              patch("authoring.reconcile_review_status.reconcile_pending_reviews", return_value={"test_ids": [1]}) as local,
              redirect_stdout(output)):
            main()
        local.assert_called_once()
        self.assertEqual(json.loads(output.getvalue())["provider_calls"], 0)

    def test_public_status_reconciliation_refuses_paid_confirmation(self):
        from authoring.create_tests import main
        with (patch("sys.argv", ["create_tests", "--config", str(self.fixture.config_path),
                                "--plan", str(self.fixture.plan), "--resume-new-test-run", str(self.root / "source"),
                                "--test-ids", "1", "--out", str(self.root / "new"),
                                "--reconcile-review-statuses", "--confirm-paid-calls"]),
              self.assertRaisesRegex(ValueError, "forbids paid")):
            main()


if __name__ == "__main__":
    unittest.main()
