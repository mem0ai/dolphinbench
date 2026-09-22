from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import ValidationError

from authoring import run as run_module
from authoring import create_tests as create_tests_module
from authoring import final_trace_review as final_trace_review_module
from authoring.finalize_batch import _failure_reason, write_completed_batch
from authoring.final_trace_review import FinalTraceDecision, FinalTraceReviewResponse
from authoring.models import (
    DesignResponse,
    FactApplicationExplanationOnEvaluationDate,
    PlannedTask,
    RequiredResult,
)


class FinalReviewCorrectionTests(unittest.TestCase):
    @staticmethod
    def _audit() -> dict[str, object]:
        return {
            "request_quality": {
                "reader_and_goal": "Morgan needs an investor email with current funding context.",
                "work_is_plausible": {
                    "passes": True, "explanation": "Investors need the company's funding update.", "issue_indexes": [],
                },
                "scope_is_necessary": {
                    "passes": True, "explanation": "The request asks for an update, not unrelated reporting.", "issue_indexes": [],
                },
                "request_is_natural": {
                    "passes": True, "explanation": "Send the investor update names the work without listing remembered values.", "issue_indexes": [],
                },
            },
            "user_request_requirements": [
                {"requirement": "Send the update.", "assertion_indexes": [0],
                 "grading_role": "final_effect", "grading_reason": "The content check proves the email was sent."}
            ],
            "planned_required_results": [
                {
                    "planned_result_index": 0,
                    "requirement": "Include the funding context.",
                    "why_required_for_work": "The investor update would omit the requested funding context.",
                    "assertion_indexes": [0],
                }
            ],
            "grading_assertions": [
                {
                    "assertion_index": 0,
                    "normal_correct_value": "The funding context is included.",
                    "incorrect_or_incomplete_value": "The funding context is missing.",
                    "result_quality": {
                        "passes": True, "explanation": "The body check measures required funding content and proves the email was sent; no other check duplicates it.", "issue_indexes": [],
                    },
                }
            ],
            "source_and_date_checks": [
                {
                    "fact_id": 1,
                    "source_supports_required_result": True,
                    "source_evidence": "The cited message states the funding context.",
                    "applies_on_evaluation_date": True,
                    "date_evidence": "No later message replaces it.",
                }
            ],
            "tool_capability_checks": [
                {
                    "tool_name": "send_email",
                    "needed_capability": "Send an investor email.",
                    "contract_supports_capability": True,
                    "contract_evidence": "The tool accepts an email body.",
                }
            ],
        }

    @staticmethod
    def _planned_task() -> PlannedTask:
        return PlannedTask(
            task_description="Send a current investor update.",
            fact_ids=[1],
            required_results=[
                RequiredResult(result="Include the funding context.", fact_ids=[1])
            ],
            expected_tools=["send_email"],
            action_without_memory="The email omits the funding context.",
            why_memory_changes_result="Fact 1 supplies required email content.",
            fact_application_explanations_on_evaluation_date=[
                FactApplicationExplanationOnEvaluationDate(
                    fact_id=1,
                    why_fact_still_applies_on_evaluation_date="No later fact replaces it.",
                )
            ],
            why_listed_tools_can_complete_requested_work=(
                "send_email accepts a recipient, subject, and body."
            ),
        )

    def _decision(
        self, *, accept: bool, part: str = "none"
    ) -> FinalTraceDecision:
        return FinalTraceDecision(
            test_id=121,
            accept=accept,
            specific_problem="" if accept else "The previous request states a hidden date.",
            part_to_correct=part,
            required_correction="" if accept else "Do not state the hidden date.",
        )

    def _record(self, root: Path) -> dict[str, object]:
        proposal_dir = root / "authoring" / "part_01" / "proposals" / "121"
        proposal_dir.mkdir(parents=True)
        (proposal_dir / "initial_candidate.yaml").write_text(
            """id: '121'
narrative_anchor_date: '2026-09-14'
test: Send the investor update.
load_bearing_facts: [1]
expected_tool_calls: [send_email]
grade:
  type: tool_trace
  config:
    assertions:
      - type: field_llm_judge
        tool: send_email
        path: args.body
        criterion: Does the email include the funding context?
mock_state: {}
"""
        )
        (proposal_dir / "initial_gate.json").write_text("{}\n")
        return {
            "candidate": proposal_dir / "initial_candidate.yaml",
            "gate": proposal_dir / "initial_gate.json",
            "run_dir": root / "authoring" / "part_01",
            "first_pass": True,
        }

    def test_old_saved_single_issue_decision_loads_as_one_issue(self) -> None:
        decision = FinalTraceDecision.model_validate(
            {
                "test_id": 121,
                "accept": False,
                "specific_problem": "The request exposes the date.",
                "part_to_correct": "user_request",
                "required_correction": "Remove the date from the request.",
            }
        )

        self.assertEqual(decision.part_to_correct, "user_request")
        self.assertEqual(len(decision.issues), 1)
        self.assertEqual(decision.issues[0].owning_step, "user_request")

    def test_model_call_schema_requires_every_property_and_has_no_legacy_fields(self) -> None:
        schema = FinalTraceReviewResponse.model_json_schema()

        def assert_all_properties_are_required(value: object) -> None:
            if isinstance(value, dict):
                properties = value.get("properties")
                if isinstance(properties, dict):
                    self.assertEqual(set(value.get("required") or []), set(properties))
                for child in value.values():
                    assert_all_properties_are_required(child)
            elif isinstance(value, list):
                for child in value:
                    assert_all_properties_are_required(child)

        assert_all_properties_are_required(schema)
        self.assertEqual(set(schema["properties"]), {"test_id", "accept", "issues", "audit"})
        self.assertNotIn("specific_problem", schema["properties"])
        self.assertNotIn("part_to_correct", schema["properties"])
        self.assertNotIn("required_correction", schema["properties"])

    def test_model_call_response_requires_issues_to_match_acceptance(self) -> None:
        accepted = FinalTraceReviewResponse.model_validate(
            {"test_id": 121, "accept": True, "issues": [], "audit": self._audit()}
        )
        self.assertTrue(accepted.accept)

        issue = {
            "owning_step": "grading_checks",
            "specific_problem": "The check accepts missing content.",
            "required_correction": "Require that content.",
            "request_requirement_quote": "",
            "source_session_id": "1",
            "source_text_quote": "The cited message states the funding context.",
        }
        with self.assertRaisesRegex(ValidationError, "accepted review must have no issues"):
            FinalTraceReviewResponse.model_validate(
                {"test_id": 121, "accept": True, "issues": [issue], "audit": self._audit()}
            )
        with self.assertRaisesRegex(ValidationError, "rejected review must have at least one issue"):
            FinalTraceReviewResponse.model_validate(
                {"test_id": 121, "accept": False, "issues": [], "audit": self._audit()}
            )

    def test_execute_final_review_uses_strict_response_then_builds_saved_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            raw = {
                "test_id": 121,
                "accept": False,
                "issues": [
                    {
                        "owning_step": "user_request",
                        "specific_problem": "The request reveals the answer.",
                        "required_correction": "Remove the answer.",
                        "request_requirement_quote": "Send the update.",
                        "source_citations": [],
                    }
                ],
                "audit": self._audit(),
            }
            with patch.object(
                final_trace_review_module,
                "cached_client_complete",
                return_value=raw,
            ) as complete:
                decision = final_trace_review_module.execute_final_trace_review(
                    request={
                        "test_id": 121,
                        "user_request": "Send the update.",
                        "planned_work": {
                            "required_results": [
                                {"result": "Include the funding context."}
                            ]
                        },
                        "grading_checks": [{"type": "field_llm_judge"}],
                        "source_evidence": [{"fact_id": 1, "source_sessions": []}],
                        "selected_tool_contracts": {"send_email": {"arguments": ["body"]}},
                    },
                    out=out,
                    client=SimpleNamespace(),
                    confirm_paid_calls=True,
                )

            schema = complete.call_args.kwargs["response_schema"]
            self.assertEqual(set(schema["properties"]), {"test_id", "accept", "issues", "audit"})
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
            self.assertEqual(decision.part_to_correct, "user_request")
            self.assertEqual(decision.specific_problem, "The request reveals the answer.")
            saved_response = json.loads((out / "response.json").read_text())
            saved_decision = json.loads((out / "decision.json").read_text())
            self.assertEqual(set(saved_response), {"test_id", "accept", "issues", "audit"})
            self.assertEqual(saved_decision["part_to_correct"], "user_request")

    def test_multiple_issues_are_kept_and_routed_to_one_design_correction(self) -> None:
        decision = FinalTraceDecision.model_validate(
            {
                "test_id": 121,
                "accept": False,
                "issues": [
                    {
                        "owning_step": "user_request",
                        "specific_problem": "The request reveals a remembered date.",
                        "required_correction": "Remove the date from the request.",
                    },
                    {
                        "owning_step": "grading_checks",
                        "specific_problem": "The checks require an optional phrase.",
                        "required_correction": "Grade the required meaning instead.",
                    },
                ],
            }
        )

        self.assertEqual(decision.part_to_correct, "test_design")
        self.assertEqual(
            [issue.owning_step for issue in decision.issues],
            ["user_request", "grading_checks"],
        )
        self.assertIn("The request reveals", decision.specific_problem)
        self.assertIn("The checks require", decision.specific_problem)

    def test_test_16_mixed_grading_and_execution_routing_preserves_both_owners(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decision = FinalTraceDecision.model_validate(
                {
                    "test_id": 121,
                    "accept": False,
                    "issues": [
                        {
                            "owning_step": "grading_checks",
                            "specific_problem": "The first prose check accepts a missing role.",
                            "required_correction": "Require the role in the prose check.",
                        },
                        {
                            "owning_step": "grading_checks",
                            "specific_problem": "The second prose check accepts a missing date.",
                            "required_correction": "Require the date in the prose check.",
                        },
                        {
                            "owning_step": "execution",
                            "specific_problem": "The saved runs did not show the corrected checks working.",
                            "required_correction": "Run the corrected test again.",
                        },
                    ],
                }
            )
            corrector = Mock(return_value={"assertions": [], "expected_no_history_failed_check_index": 0})
            certify = Mock(return_value={"valid": True})
            review_dir = root / "review"
            review_dir.mkdir()
            (review_dir / "request.json").write_text(
                json.dumps(
                    {
                        "planned_work": {"required_results": [{"result": "funding context"}]},
                        "user_request": "Send the investor update.",
                        "starting_app_data": {"emails": []},
                        "selected_tool_contracts": {"send_email": {"arguments": ["to", "body"]}},
                        "source_evidence": [{"fact_id": 1}],
                        "executions": [],
                    }
                )
            )
            with (
                patch.object(
                    create_tests_module,
                    "_load_saved_authoring_artifacts",
                    return_value=(self._design(), {}, {}),
                ),
                patch.object(create_tests_module, "_corrected_grading_candidate", return_value=0),
                patch.object(
                    create_tests_module,
                    "_regraded_gate",
                    return_value={"result": {"g1_pass_count": 2, "g2_pass_count": 0}},
                ),
                patch.object(create_tests_module, "_certify_cached", side_effect=certify),
            ):
                candidate, evidence_path, correction = create_tests_module._correct_after_final_review(
                    decision=decision,
                    record=self._record(root),
                    planned_task=self._planned_task(),
                    context=SimpleNamespace(checkpoint_path=root / "checkpoint"),
                    config=SimpleNamespace(persona="morgan"),
                    test_id=121,
                    review_dir=review_dir,
                    design_client=SimpleNamespace(),
                    query_client=SimpleNamespace(),
                    certifier=Mock(),
                    grading_corrector=corrector,
                    regrader=Mock(),
                )

        self.assertIsNotNone(candidate)
        self.assertEqual(
            evidence_path,
            root
            / "authoring"
            / "part_01"
            / "proposals"
            / "121"
            / "final_review_correction_gate.json",
        )
        self.assertEqual(correction["part_to_correct"], "grading_checks")
        self.assertTrue(correction["certification_rerun"])
        self.assertEqual(
            correction["issue_owners"], ["grading_checks", "execution"]
        )
        self.assertTrue(correction["execution_rerun_required"])
        corrector.assert_called_once()
        sent = corrector.call_args.kwargs["request"]["reviewer_decision"]
        self.assertEqual(len(sent["issues"]), 3)
        request = corrector.call_args.kwargs["request"]
        self.assertEqual(request["planned_work"]["required_results"][0]["result"], "funding context")
        self.assertEqual(request["final_user_request"], "Send the investor update.")
        self.assertEqual(request["starting_app_data"], {"emails": []})
        self.assertIn("send_email", request["selected_tool_contracts"])
        self.assertIn("anyOf", request["supported_assertion_shapes"]["items"])
        certify.assert_called_once()

    def test_grading_correction_reuses_complete_regrade_when_four_saved_runs_still_certify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decision = self._decision(accept=False, part="grading_checks")
            review_dir = root / "review"
            review_dir.mkdir()
            (review_dir / "request.json").write_text(
                json.dumps({"source_evidence": [], "executions": []})
            )
            certify = Mock()

            with (
                patch.object(
                    create_tests_module,
                    "_load_saved_authoring_artifacts",
                    return_value=(self._design(), {}, {}),
                ),
                patch.object(
                    create_tests_module,
                    "_corrected_grading_candidate",
                    return_value=0,
                ),
                patch.object(
                    create_tests_module,
                    "_regraded_gate",
                    return_value={"result": {"g1_pass_count": 2, "g2_pass_count": 0}},
                ),
            ):
                candidate, evidence_path, correction = create_tests_module._correct_after_final_review(
                    decision=decision,
                    record=self._record(root),
                    planned_task=self._planned_task(),
                    context=SimpleNamespace(checkpoint_path=root / "checkpoint"),
                    config=SimpleNamespace(persona="morgan"),
                    test_id=121,
                    review_dir=review_dir,
                    design_client=SimpleNamespace(),
                    query_client=SimpleNamespace(),
                    certifier=certify,
                    grading_corrector=Mock(
                        return_value={
                            "assertions": [],
                            "expected_no_history_failed_check_index": 0,
                        }
                    ),
                    regrader=Mock(),
                )

        self.assertIsNotNone(candidate)
        self.assertEqual(
            evidence_path,
            root
            / "authoring"
            / "part_01"
            / "proposals"
            / "121"
            / "final_review_correction_regrade.json",
        )
        self.assertFalse(correction["certification_rerun"])
        certify.assert_not_called()

    def test_regraded_gate_replaces_stale_per_check_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.yaml"
            candidate.write_text("id: 121\n")
            saved_gate = root / "gate.json"
            shots = []
            for with_memory, passed in ((True, True), (True, True), (False, False), (False, False)):
                shots.append(
                    {
                        "with_memory": with_memory,
                        "passed": passed,
                        "tool_calls": [],
                        "response_text": "done",
                        "grade": {
                            "passed": passed,
                            "details": [{"name": "old assertion", "ok": passed}],
                        },
                    }
                )
            saved_gate.write_text(
                json.dumps(
                    {
                        "result": {
                            "shots": shots,
                            "g1_pass_count": 2,
                            "g2_pass_count": 0,
                            "valid": True,
                        }
                    }
                )
            )

            def regrader(_candidate: dict, oracle_result: dict) -> dict:
                return {
                    "passed": oracle_result["response_text"] == "done",
                    "details": [{"name": "new assertion", "ok": True}],
                }

            regraded = create_tests_module._regraded_gate(
                candidate_path=candidate,
                saved_gate_path=saved_gate,
                regrader=regrader,
            )

        for shot in regraded["result"]["shots"]:
            self.assertEqual(shot["grade"]["details"], [{"name": "new assertion", "ok": True}])
            self.assertEqual(shot["passed"], shot["grade"]["passed"])

    def _run_reviews(
        self,
        *,
        root: Path,
        decisions: list[FinalTraceDecision],
        attempt_result: tuple[dict[str, object] | None, dict[str, object]] | None = None,
        attempt_results: list[tuple[dict[str, object] | None, dict[str, object]]] | None = None,
    ) -> tuple[dict[int, Path], list[dict[str, object]], object]:
        review_calls: list[dict[str, object]] = []

        def reviewer(**kwargs: object) -> FinalTraceDecision:
            review_calls.append(kwargs)
            return decisions.pop(0)

        design = self._design()
        attempt = attempt_result or ({"id": 121}, {"stage": "accepted"})
        config = SimpleNamespace(
            planner_model="planner-model",
            designer_model="designer-model",
            evaluation_date="2026-09-14",
            persona="morgan",
        )
        def write_attempt(**kwargs: object) -> tuple[dict[str, object] | None, dict[str, object]]:
            value = attempt_results.pop(0) if attempt_results is not None else attempt
            if value[0] is not None:
                proposal_dir = kwargs["directory"]
                assert isinstance(proposal_dir, Path)
                candidate = proposal_dir / f"{kwargs['attempt_name']}_candidate.yaml"
                candidate.write_text("id: '121'\n")
                (proposal_dir / f"{kwargs['attempt_name']}_gate.json").write_text(
                    json.dumps({"result": {"valid": True}})
                )
            return value

        with (
            patch.object(
                create_tests_module,
                "_load_saved_authoring_artifacts",
                return_value=(design, {"saved": "design"}, {"query": "saved"}),
            ),
            patch.object(create_tests_module, "design_payload", return_value={"source": "input"}),
            patch.object(
                create_tests_module,
                "_attempt",
                side_effect=write_attempt,
            ) as attempt_call,
        ):
            accepted, results = create_tests_module._review_four_run_candidates(
                reviewable={121: self._record(root)},
                tasks_by_id={121: SimpleNamespace()},
                context=SimpleNamespace(),
                config=config,
                out=root,
                concurrency=1,
                design_client=SimpleNamespace(name="designer"),
                query_client=SimpleNamespace(name="writer"),
                certifier=lambda *args, **kwargs: {"valid": True},
                reviewer=reviewer,
            )
        self.assertTrue(review_calls)
        self.assertTrue(all(call["model"] == "designer-model" for call in review_calls))
        return accepted, results, (attempt_call, review_calls)

    def test_user_request_reuses_design_and_calls_only_the_request_writer_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            accepted, results, (attempt, review_calls) = self._run_reviews(
                root=root,
                decisions=[
                    self._decision(accept=False, part="user_request"),
                    self._decision(accept=True),
                ],
            )

            self.assertEqual(results[0]["status"], "accepted_after_final_review_correction")
            self.assertIn(121, accepted)
            self.assertEqual(len(review_calls), 2)
            self.assertEqual(attempt.call_count, 1)
            kwargs = attempt.call_args.kwargs
            self.assertIsNotNone(kwargs["reuse_design"])
            self.assertEqual(
                kwargs["query_final_review_feedback"],
                {
                    "specific_problem": "The previous request states a hidden date.",
                    "required_correction": "Do not state the hidden date.",
                },
            )
            saved = json.loads(
                (root / "final_trace_reviews" / "121" / "correction.json").read_text()
            )
            self.assertEqual(saved["initial_final_review"]["part_to_correct"], "user_request")
            self.assertTrue(saved["corrected_final_review"]["accept"])

    def test_failed_grading_correction_stops_without_rewriting_the_test(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            accepted, results, (attempt, review_calls) = self._run_reviews(
                root=Path(directory),
                decisions=[
                    self._decision(accept=False, part="grading_checks"),
                    self._decision(accept=True),
                ],
            )

            self.assertNotIn(121, accepted)
            self.assertEqual(
                results[0]["status"], "replacement_required_after_one_correction"
            )
            self.assertEqual(len(review_calls), 1)
            self.assertEqual(attempt.call_count, 0)

    def test_test_58_one_scope_check_routes_to_grading_only_correction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            record["authoring_mode"] = "unified"
            corrected_candidate = root / "corrected.yaml"
            corrected_gate = root / "corrected_gate.json"
            corrected_candidate.write_text("id: 121\n")
            corrected_gate.write_text(json.dumps({"result": {"valid": True}}))
            decisions = [
                self._decision(accept=False, part="grading_checks"),
                self._decision(accept=True),
            ]

            def reviewer(**_kwargs: object) -> FinalTraceDecision:
                return decisions.pop(0)

            with (
                patch.object(
                    create_tests_module,
                    "_correct_after_final_review",
                    return_value=(
                        corrected_candidate,
                        corrected_gate,
                        {"status": "candidate_certified"},
                    ),
                ) as targeted,
                patch.object(
                    create_tests_module,
                    "_unified_final_review_correction",
                ) as complete_rewrite,
            ):
                accepted, results = create_tests_module._review_four_run_candidates(
                    reviewable={121: record},
                    tasks_by_id={121: self._planned_task()},
                    context=SimpleNamespace(),
                    config=SimpleNamespace(designer_model="designer"),
                    out=root,
                    concurrency=1,
                    design_client=SimpleNamespace(),
                    query_client=SimpleNamespace(),
                    certifier=Mock(),
                    reviewer=reviewer,
                )

            self.assertEqual(accepted[121], corrected_candidate)
            self.assertEqual(
                results[0]["status"], "accepted_after_final_review_correction"
            )
            targeted.assert_called_once()
            complete_rewrite.assert_not_called()

    def test_planning_rejection_stops_for_a_new_approved_idea(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            accepted, results, (attempt, review_calls) = self._run_reviews(
                root=Path(directory),
                decisions=[
                    self._decision(accept=False, part="planning"),
                    self._decision(accept=True),
                ],
            )

            self.assertNotIn(121, accepted)
            self.assertEqual(results[0]["status"], "replacement_required")
            self.assertEqual(attempt.call_count, 0)
            self.assertEqual(len(review_calls), 1)

    def test_initial_final_review_acceptance_does_not_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            accepted, results, (attempt, review_calls) = self._run_reviews(
                root=Path(directory), decisions=[self._decision(accept=True)]
            )

            self.assertIn(121, accepted)
            self.assertEqual(results[0]["status"], "accepted_initial_final_review")
            self.assertEqual(attempt.call_count, 0)
            self.assertEqual(len(review_calls), 1)

    def test_accepted_candidate_is_not_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            candidate_path = record["candidate"]
            assert isinstance(candidate_path, Path)
            original = candidate_path.read_bytes()
            reviewer = Mock(return_value=self._decision(accept=True))

            accepted, results = create_tests_module._review_four_run_candidates(
                reviewable={121: record},
                tasks_by_id={121: self._planned_task()},
                context=SimpleNamespace(),
                config=SimpleNamespace(designer_model="designer"),
                out=root,
                concurrency=1,
                design_client=SimpleNamespace(),
                query_client=SimpleNamespace(),
                certifier=Mock(),
                reviewer=reviewer,
            )

            self.assertEqual(accepted[121], candidate_path)
            self.assertEqual(candidate_path.read_bytes(), original)
            self.assertEqual(results[0]["status"], "accepted_initial_final_review")

    def test_failed_certification_cannot_be_accepted_without_correction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            record["certification_valid"] = False
            reviewer = Mock(return_value=self._decision(accept=True))

            accepted, results = create_tests_module._review_four_run_candidates(
                    reviewable={121: record},
                    tasks_by_id={121: self._planned_task()},
                    context=SimpleNamespace(),
                    config=SimpleNamespace(designer_model="designer"),
                    out=root,
                    concurrency=1,
                    design_client=SimpleNamespace(),
                    query_client=SimpleNamespace(),
                    certifier=Mock(),
                    reviewer=reviewer,
            )
            self.assertFalse(accepted)
            self.assertEqual(results[0]["status"], "review_pending")
            self.assertIn("four-run certification failed", results[0]["reason"])

    def test_no_second_redesign_after_one_correction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            accepted, results, (attempt, review_calls) = self._run_reviews(
                root=Path(directory),
                decisions=[
                    self._decision(accept=False, part="starting_app_data"),
                    self._decision(accept=False, part="grading_checks"),
                    self._decision(accept=True),
                ],
            )

            self.assertNotIn(121, accepted)
            self.assertEqual(
                results[0]["status"], "replacement_required_after_one_correction"
            )
            self.assertEqual(attempt.call_count, 1)
            self.assertEqual(len(review_calls), 2)

    def test_failed_targeted_correction_stops_without_another_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            accepted, results, (attempt, review_calls) = self._run_reviews(
                root=Path(directory),
                decisions=[
                    self._decision(accept=False, part="user_request"),
                    self._decision(accept=True),
                ],
                attempt_results=[
                    (None, {"stage": "certification", "reason": "invalid"}),
                    ({"id": 121}, {"stage": "accepted"}),
                ],
            )

            self.assertNotIn(121, accepted)
            self.assertEqual(
                results[0]["status"], "replacement_required_after_one_correction"
            )
            self.assertEqual(attempt.call_count, 1)
            self.assertEqual(len(review_calls), 1)

    def test_reused_design_attempt_never_calls_the_designer(self) -> None:
        design = self._design()
        model_calls: list[dict[str, object]] = []
        certifier_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def fake_model_call(**kwargs: object) -> dict[str, str]:
            model_calls.append(kwargs)
            return {"query": "Please draft the investor update."}

        def certifier(*args: object, **kwargs: object) -> dict[str, object]:
            certifier_calls.append((args, kwargs))
            return {"valid": True, "verdict": "pass"}

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(run_module, "_model_call", side_effect=fake_model_call),
                patch.object(run_module, "assemble_candidate", return_value={"id": 121}),
                patch.object(run_module, "_grading_design_request", return_value={}),
                patch.object(run_module, "_parse_grading_design", return_value=design),
            ):
                candidate, result = run_module._attempt(
                    directory=Path(directory),
                    attempt_name="final_review_correction",
                    design_system="must not be called",
                    design_request={},
                    planned_task=SimpleNamespace(),
                    context=SimpleNamespace(checkpoint_path=Path(directory), tools={}),
                    config=SimpleNamespace(evaluation_date="2026-09-14", persona="morgan"),
                    test_id=121,
                    design_client=SimpleNamespace(name="designer"),
                    query_client=SimpleNamespace(name="writer"),
                    certifier=certifier,
                    reuse_design=design,
                    query_final_review_feedback={
                        "specific_problem": "The request states a hidden date.",
                        "required_correction": "Do not state the date.",
                    },
                )

        self.assertEqual(candidate, {"id": 121})
        self.assertEqual(result["stage"], "accepted")
        self.assertEqual(
            [call["name"] for call in model_calls],
            ["final_review_correction_blind_query", "final_review_correction_grading"],
        )
        self.assertIn("The request states a hidden date.", model_calls[0]["system"])
        self.assertIn("Do not state the date.", model_calls[0]["system"])
        self.assertEqual(len(certifier_calls), 1)
        self.assertTrue(certifier_calls[0][1]["confirm_paid_calls"])

    def test_create_tests_manifest_distinguishes_each_final_review_outcome(self) -> None:
        from authoring.test_four_stage_authoring import idea

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text("persona: morgan\n")
            (root / "plan.json").write_text("{}\n")
            initial_candidate = root / "initial.yaml"
            corrected_candidate = root / "corrected.yaml"
            initial_candidate.write_text("id: 121\n")
            corrected_candidate.write_text("id: 122\n")
            review_results = [
                {"id": 121, "status": "accepted_initial_final_review"},
                {"id": 122, "status": "accepted_after_final_review_correction"},
                {"id": 123, "status": "replacement_required"},
            ]
            config = SimpleNamespace(
                checkpoint_identity="checkpoint-id",
                persona="morgan",
                evaluation_date="2026-09-14",
                planner_model="review-model",
                designer_model="designer-model",
                query_model="writer-model",
            )
            context = SimpleNamespace(
                checkpoint_identity="checkpoint-id",
                persona="morgan",
            )
            task = idea()
            with (
                patch.object(create_tests_module, "load_config", return_value=config),
                patch.object(create_tests_module, "load_checkpoint_context", return_value=context),
                patch.object(
                    create_tests_module,
                    "load_authoring_tasks",
                    return_value=[task.model_copy(update={"idea_id": index + 1}, deep=True) for index in range(7)],
                ),
                patch.object(create_tests_module, "_run_authoring_parts", return_value=[]),
                patch.object(
                    create_tests_module,
                    "_reviewable_outputs",
                    return_value=({}, [{"id": 121}]),
                ),
                patch.object(
                    create_tests_module,
                    "_review_four_run_candidates",
                    return_value=({121: initial_candidate, 122: corrected_candidate}, review_results),
                ),
                patch.object(
                    create_tests_module,
                    "AzureJsonClient",
                    side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
                ),
                patch.object(
                    create_tests_module, "record_accepted_batch"
                ) as record_accepted,
            ):
                manifest = create_tests_module.create_tests(
                    config_path=root / "config.yaml",
                    plan_path=root / "plan.json",
                    out=root / "test creation",
                    start_id=None,
                    test_ids=[121, 122, 123, 124, 125, 127, 130],
                    concurrency=1,
                    confirm_paid_calls=True,
                )

            self.assertEqual(manifest["accepted_initial_final_review"], 1)
            self.assertEqual(manifest["accepted_after_final_review_correction"], 1)
            self.assertEqual(manifest["replacement_required_after_final_review"], 1)
            self.assertEqual(manifest["final_review_accepted"], 2)
            self.assertEqual(manifest["candidate_count"], 7)
            self.assertEqual(
                manifest["test_ids"], [121, 122, 123, 124, 125, 127, 130]
            )
            record_accepted.assert_called_once_with(
                config=config,
                context=context,
                directory=root / "test creation" / "accepted_batch",
            )

    def test_all_rejected_batch_preserves_rejections_without_an_accepted_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "run"
            out.mkdir()
            accepted_paths, provenance = write_completed_batch(
                out=out,
                plan_path=Path(directory) / "plan.json",
                checkpoint_identity="checkpoint", persona="morgan", evaluation_date="2026-09-14",
                tasks_by_id={1: self._planned_task()},
                accepted={},
                authoring_results=[
                    {"id": 1, "status": "replacement_required", "reason": "Bad request."}
                ],
                final_review_results=[],
            )

            self.assertEqual(accepted_paths, [])
            self.assertEqual(provenance, [])
            self.assertFalse((out / "accepted_batch").exists())
            rejected = json.loads((out / "rejected_tests.json").read_text())
            self.assertEqual(rejected["rejected_tests"][0]["failure_reason"], "Bad request.")

    @staticmethod
    def _design() -> DesignResponse:
        return DesignResponse.model_validate(
            {
                "outcome": "design",
                "rejection_reason": "",
                "source_message_meaning": "Morgan established the funding context.",
                "reason_it_applies_on_test_date": "No later message changed it.",
                "present_situation": "An investor update is due today.",
                "requested_work": "Please draft the investor update.",
                "missing_information": "The funding context to include.",
                "information_the_request_must_not_reveal": "The funding amount.",
                "existing_records": [],
                "new_records": [],
                "expected_tool_calls": ["send_email"],
                "grading_checks": [
                    {
                        "fact_ids": [1],
                        "assertion": {
                            "type": "field_equals",
                            "tool": "send_email",
                            "path": "args.to",
                            "value": "investor@example.com",
                        },
                    }
                ],
                "answers_that_must_not_appear": ["$18M"],
                "with_history_result": "Draft the update with funding context.",
                "without_history_result": "The funding context is missing.",
                "without_memory_failed_check": 0,
            }
        )


class RejectionFeedbackTests(unittest.TestCase):
    def test_specific_and_latest_failure_reasons_are_preserved(self):
        reason = "The approved plan turns a concise routing DM into an exhaustive operational-rule recap."
        cases = (
            ({"reason": "proposal_rejected", "status": "replacement_required",
              "first_failure": {"reason": reason}, "proposal_rejected": {"reason": reason}}, reason),
            ({"status": "replacement_required", "first_failure": {"reason": "The first attempt failed."},
              "redesign_failure": {"reason": "The corrected attempt still failed."}},
             "The corrected attempt still failed."),
        )
        for result, expected in cases:
            with self.subTest(result=result):
                self.assertEqual(_failure_reason(
                    authoring_result=result, final_review_result=None), expected)


if __name__ == "__main__":
    unittest.main()
