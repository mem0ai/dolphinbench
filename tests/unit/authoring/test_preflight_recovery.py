"""Local integration coverage for early review and authenticated recovery."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from authoring import create_tests, preflight, run
from authoring.context import dump_json
from authoring.final_trace_review import FinalTraceDecision, build_final_trace_review_request
from authoring.progress import cached_authoring_response, load_progress, save_progress
from graders.mechanical import grade_tool_trace
import tests.unit.authoring.test_final_review_correction as review_fixture
import tests.unit.authoring.test_new_test_resume as resume_fixture
import tests.unit.authoring.test_unified_authoring as system_fixture


class PreflightIntegrationTests(unittest.TestCase):
    def test_rejected_reviews_still_require_audit_examples(self) -> None:
        self.assertIn("Omit only these executable fixtures", preflight.PREFLIGHT_SYSTEM)
        self.assertIn("including assertions you reject", preflight.PREFLIGHT_SYSTEM)
        raw = dict(self.raw)
        raw["audit"]["grading_assertions"][0]["normal_correct_value"] = ""
        with self.assertRaisesRegex(ValueError, "normal correct value must not be empty"):
            preflight.PreflightResponse.model_validate(raw)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture = system_fixture.RequirementPreservationTests()
        fixture.setUp()
        self.context, self.task, self.authored = fixture.context, fixture.task, fixture.raw
        self.candidate = self.root / "candidate.yaml"
        dump_json(self.candidate, {
            "id": "121", "narrative_anchor_date": "2028-01-01",
            "test": "Send the investor update.", "load_bearing_facts": [1],
            "expected_tool_calls": ["send_email"], "mock_state": {},
            "grade": {"type": "tool_trace", "config": {"assertions": [{
                "type": "field_equals", "tool": "send_email", "path": "args.body",
                "value": "$18M Series B led by Northstar",
            }]}},
        })
        self.raw = {
            "test_id": 121, "accept": True, "issues": [],
            "audit": review_fixture.FinalReviewCorrectionTests._audit(),
            "reference_tool_calls_json": json.dumps([{
                "tool": "send_email", "args": {"to": "investor@example.com", "body": "$18M Series B led by Northstar"},
            }]),
            "grading_probes": [{
                "assertion_index": 0, "reference_call_index": 0,
                "correct_value_json": json.dumps("$18M Series B led by Northstar"),
                "incorrect_value_json": json.dumps("A funding round happened."),
            }],
        }

    def execute(self, *, raw=None, out=None, **kwargs):
        return preflight.execute_preflight(
            candidate_path=self.candidate, planned_task=self.task, context=self.context,
            authoring_evidence={}, out=out or self.root / "review",
            client=Mock(complete=Mock(return_value=raw or self.raw)),
            confirm_paid_calls=True, **kwargs,
        )

    def test_real_request_builder_and_mechanical_probes_save_per_case_evidence(self) -> None:
        result = self.execute()
        self.assertTrue(result["passed"])
        cases = json.loads(Path(result["grading_examples"]).read_text())["cases"]
        self.assertEqual([case["grade"]["passed"] for case in cases], [True, True, False])
        self.assertTrue(all(case["matches_expected"] for case in cases))
        self.assertTrue(all("judge_config" in case["inputs"] for case in cases))

    def test_saved_report_without_scope_gets_current_review_not_silent_acceptance(self) -> None:
        old = json.loads(json.dumps(self.raw))
        for row in old["audit"]["user_request_requirements"]:
            row.pop("grading_role")
            row.pop("grading_reason")
        for row in old["audit"]["planned_required_results"]:
            row.pop("why_required_for_work")
        request = preflight.build_test_review_request(
            candidate_path=self.candidate, planned_task=self.task, context=self.context,
        )
        request["authoring_evidence"] = {}
        with patch("authoring.preflight.cached_client_complete", return_value=self.raw) as review:
            result = self.execute(saved_response=old, saved_request=request)
        review.assert_called_once()
        self.assertTrue(result["passed"])

    def test_malformed_probe_never_becomes_a_reusable_review(self) -> None:
        self.raw["grading_probes"][0]["incorrect_value_json"] = "not JSON"
        grader = Mock(side_effect=AssertionError("grader must not run"))
        with self.assertRaises(ValueError):
            self.execute(grader=grader)
        grader.assert_not_called()
        self.assertFalse((self.root / "review" / "response.json").exists())

    def test_old_quality_incomplete_report_stops_before_grading(self) -> None:
        del self.raw["audit"]["request_quality"]
        grader = Mock(side_effect=AssertionError("grader must not run"))
        with self.assertRaises(ValueError):
            self.execute(grader=grader)
        grader.assert_not_called()
        self.assertFalse((self.root / "review" / "decision.json").exists())

    def test_scope_rejection_stops_before_grading_and_certification(self) -> None:
        def reject_scope(*, request, **kwargs):
            raw = json.loads(json.dumps(self.raw))
            raw.update(accept=False, reference_tool_calls_json="[]", grading_probes=[])
            raw["issues"] = [{
                "owning_step": "planning",
                "specific_problem": "The approved work includes an unnecessary reporting requirement.",
                "required_correction": "Require a newly approved idea with a justified reporting scope.",
                "request_requirement_quote": request["user_request"],
                "source_citations": [], "planned_work_quote": "",
            }]
            raw["audit"]["request_quality"]["scope_is_necessary"] = {
                "passes": False, "explanation": "The approved work adds reporting unrelated to the stated goal.", "issue_indexes": [0],
            }
            # Match the real writer fixture, which includes recipient and content checks.
            assertion = raw["audit"]["grading_assertions"][0]
            raw["audit"]["grading_assertions"] = [
                dict(assertion, assertion_index=index)
                for index in range(len(request["grading_checks"]))
            ]
            return raw

        def review(**kwargs):
            request = preflight.build_test_review_request(
                candidate_path=kwargs["candidate_path"], planned_task=kwargs["planned_task"], context=kwargs["context"],
            )
            grader = Mock(side_effect=AssertionError("rejected test must not reach grading"))
            with patch.object(preflight, "cached_client_complete", return_value=reject_scope(request=request)):
                result = preflight.execute_preflight(**kwargs, grader=grader)
            grader.assert_not_called()
            return result

        certifier = Mock(side_effect=AssertionError("rejected test must not reach certification"))
        with patch.object(run, "execute_preflight", side_effect=review):
            candidate, result = run._author_new_test_attempt(
                directory=self.root / "attempt" / "proposals" / "121", attempt_name="initial",
                system="unused", payload={"authoring_schema_version": 2},
                planned_task=self.task, context=self.context,
                config=SimpleNamespace(persona="morgan", evaluation_date="2028-01-01"), test_id=121,
                client=Mock(complete=Mock(side_effect=AssertionError("writer must not run"))),
                certifier=certifier, reuse_authoring=self.authored,
            )
        self.assertIsNone(candidate)
        self.assertEqual(result["stage"], "preflight_rejected")
        self.assertEqual(result["preflight"]["decision"]["part_to_correct"], "planning")
        self.assertEqual(result["allowed_correction_fields"], [])
        certifier.assert_not_called()

    def test_duplicate_fact_audit_is_a_report_error(self) -> None:
        self.raw["audit"]["source_and_date_checks"] *= 2
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.execute()
        self.assertFalse((self.root / "review" / "decision.json").exists())

    def test_transport_failure_preserves_completed_probes_for_resume(self) -> None:
        calls = []
        def interrupted(trace, config, **kwargs):
            calls.append(trace)
            if len(calls) == 2:
                raise RuntimeError("judge unavailable")
            return grade_tool_trace(trace, config, **kwargs)
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.execute(grader=interrupted)
        out = self.root / "review"
        request = json.loads((out / "request.json").read_text())
        grader = Mock(wraps=grade_tool_trace)
        with patch.object(preflight, "cached_client_complete", side_effect=AssertionError("review repeated")):
            result = self.execute(saved_response=self.raw, saved_request=request, grader=grader)
        self.assertTrue(result["passed"])
        self.assertEqual(grader.call_count, 2)

    def test_judge_configuration_change_invalidates_probe_results(self) -> None:
        self.execute()
        from graders import llm_judge
        grader = Mock(wraps=grade_tool_trace)
        with patch.object(llm_judge, "AZURE_DEPLOYMENT", "different-judge"):
            self.execute(grader=grader)
        self.assertEqual(grader.call_count, 3)

    def test_incorrect_example_accepted_by_grader_blocks_certification(self) -> None:
        result = self.execute(grader=Mock(return_value={"passed": True}))
        self.assertFalse(result["passed"])
        self.assertTrue(result["decision"]["accept"])
        self.assertIn("disagrees", result["reason"])

    def test_changed_request_does_not_reuse_a_saved_review(self) -> None:
        self.execute()
        old_request = json.loads((self.root / "review" / "request.json").read_text())
        old_request["user_request"] = "A different task."
        client_call = Mock(return_value=self.raw)
        with patch.object(preflight, "cached_client_complete", client_call):
            self.execute(out=self.root / "new-review", saved_response=self.raw, saved_request=old_request)
        client_call.assert_called_once()

    def test_preflight_precedes_certification_and_errors_do_not_trigger_rewriting(self) -> None:
        for error in (False, True):
            with self.subTest(error=error):
                events = []
                def review(**kwargs):
                    events.append("preflight")
                    self.assertTrue(kwargs["candidate_path"].is_file())
                    if error:
                        raise ValueError("malformed review")
                    return {"passed": True}
                def certify(*args, **kwargs):
                    events.append("certify")
                    return {"valid": True}
                with patch.object(run, "execute_preflight", side_effect=review):
                    candidate, result = run._author_new_test_attempt(
                        directory=self.root / str(error) / "proposals" / "121",
                        attempt_name="initial", system="unused", payload={"authoring_schema_version": 2},
                        planned_task=self.task, context=self.context,
                        config=SimpleNamespace(persona="morgan", evaluation_date="2028-01-01"),
                        test_id=121, client=Mock(complete=Mock(side_effect=AssertionError("writer repeated"))),
                        certifier=certify, reuse_authoring=self.authored,
                    )
                self.assertEqual(events, ["preflight"] if error else ["preflight", "certify"])
                self.assertEqual(result["stage"], "preflight_pending" if error else "accepted")
                self.assertEqual(candidate is None, error)


class RecoveryIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = resume_fixture.NewTestResumeTests()._fixture(self.root)
        self.source = self.fixture["source"]
        self.proposal = self.fixture["candidate"].parent
        self.inputs = self.proposal.parents[1] / "run_inputs.json"
        self.task = self.fixture["task"]
        self.config = self.fixture["config"]
        self.context = self.fixture["context"]
        dump_json(self.inputs, {
            "config_path": str(self.fixture["config_path"]),
            "config_sha256": self.sha(self.fixture["config_path"]),
            "plan_path": str(self.fixture["plan_path"]),
            "plan_sha256": self.sha(self.fixture["plan_path"]),
            "checkpoint_identity": self.context.checkpoint_identity,
            "persona": self.config.persona, "evaluation_date": self.config.evaluation_date,
            "selected_test_ids": [1, 2], "selected_plan_positions": [1, 2],
        })

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def save(self, *, status="review_pending", count=0, accepted_decision=False):
        gate_path = self.fixture["gate"]
        gate = json.loads(gate_path.read_text())
        gate["result"].update(valid=True, verdict="pass", g1_pass_count=2)
        for shot in gate["result"]["shots"]:
            shot["passed"] = shot["with_memory"]
            shot["grade"]["passed"] = shot["with_memory"]
        dump_json(gate_path, gate)
        request = self.source / "final_trace_reviews" / "001" / "request.json"
        dump_json(request, build_final_trace_review_request(
            candidate_path=self.fixture["candidate"], certification_gate_path=gate_path,
            planned_task=self.task, context=self.context,
        ))
        result = {
            "id": 1, "status": status, "fact_ids": [1], "model_corrections_used": count,
            "candidate": str(self.fixture["candidate"]), "gate": str(gate_path),
        }
        if accepted_decision:
            result["initial_final_review"] = FinalTraceDecision(test_id=1, accept=True).model_dump(mode="json")
        return save_progress(
            path=request.parent / "progress.json", result=result, inputs_path=self.inputs,
            artifact_paths=[self.fixture["candidate"], gate_path, request],
        )

    def load(self):
        return load_progress(
            source_run=self.source, test_id=1, config_path=self.fixture["config_path"],
            plan_path=self.fixture["plan_path"], config=self.config, context=self.context,
            tasks=[self.task, self.task],
        )

    def resume(self, reviewer):
        with (
            patch.object(create_tests, "load_config", return_value=self.config),
            patch.object(create_tests, "load_checkpoint_context", return_value=self.context),
            patch.object(create_tests, "load_authoring_tasks", return_value=[self.task, self.task]),
            patch.object(create_tests, "AzureJsonClient", return_value=Mock()),
            patch.object(create_tests, "_create_one_test", side_effect=AssertionError("writer repeated")),
        ):
            return create_tests.resume_new_test_run(
                config_path=self.fixture["config_path"], plan_path=self.fixture["plan_path"],
                source_run=self.source, out=self.root / "resumed", test_ids=[1], concurrency=1,
                confirm_paid_calls=True, reviewer=reviewer,
                certifier=Mock(side_effect=AssertionError("agents repeated")),
            )

    def test_public_resume_reuses_accepted_review_and_preserves_source_files(self) -> None:
        self.save(accepted_decision=True)
        original = {str(path): path.read_bytes() for path in self.source.rglob("*") if path.is_file()}
        reviewer = Mock(side_effect=AssertionError("accepting review repeated"))
        manifest = self.resume(reviewer)
        self.assertEqual(manifest["final_review_accepted"], 1)
        self.assertEqual(manifest["test_ids"], [1])
        reviewer.assert_not_called()
        self.assertTrue(Path(manifest["accepted_batch"]).is_dir())
        self.assertEqual(original, {str(path): path.read_bytes() for path in self.source.rglob("*") if path.is_file()})

    def test_public_resume_retries_only_a_missing_review(self) -> None:
        self.save()
        reviewer = Mock(return_value=FinalTraceDecision(test_id=1, accept=True))
        manifest = self.resume(reviewer)
        self.assertEqual(manifest["final_review_accepted"], 1)
        reviewer.assert_called_once()

    def test_saved_authoring_in_second_part_is_rebuilt_without_repeating_writer(self) -> None:
        fixture = system_fixture.RequirementPreservationTests()
        fixture.setUp()
        fixture.context.checkpoint_identity = self.context.checkpoint_identity
        proposal = self.source / "authoring" / "part_02" / "proposals" / "001"
        response = proposal / "initial_authoring_response.json"
        dump_json(response, fixture.raw)
        inputs = proposal.parents[1] / "run_inputs.json"
        dump_json(inputs, json.loads(self.inputs.read_text()))
        save_progress(
            path=proposal / "progress.json", inputs_path=inputs, artifact_paths=[response],
            result={
                "id": 1, "status": "validation_pending", "fact_ids": [1],
                "attempt_name": "initial", "authoring_response": str(response),
                "model_corrections_used": 0,
            },
        )
        gate = json.loads(self.fixture["gate"].read_text())["result"]
        gate.update(valid=True, verdict="pass", g1_pass_count=2)
        for shot in gate["shots"]:
            shot["passed"] = shot["with_memory"]
            shot["grade"]["passed"] = shot["with_memory"]
        certifier = Mock(return_value=gate)
        with (
            patch.object(create_tests, "load_config", return_value=self.config),
            patch.object(create_tests, "load_checkpoint_context", return_value=fixture.context),
            patch.object(create_tests, "load_authoring_tasks", return_value=[fixture.task, fixture.task]),
            patch.object(create_tests, "design_payload", return_value={"authoring_schema_version": 2}),
            patch.object(create_tests, "AzureJsonClient", return_value=Mock()),
            patch.object(run, "_model_call", side_effect=AssertionError("writer repeated")),
            patch.object(run, "execute_preflight", return_value={"passed": True}) as early_review,
        ):
            manifest = create_tests.resume_new_test_run(
                config_path=self.fixture["config_path"], plan_path=self.fixture["plan_path"],
                source_run=self.source, out=self.root / "resumed", test_ids=[1], concurrency=1,
                confirm_paid_calls=True, certifier=certifier,
                reviewer=Mock(return_value=FinalTraceDecision(test_id=1, accept=True)),
            )
        self.assertEqual(manifest["final_review_accepted"], 1)
        certifier.assert_called_once()
        early_review.assert_called_once()
        saved = self.root / "resumed" / "authoring" / "part_01" / "proposals" / "001" / "initial_authoring_response.json"
        self.assertEqual(json.loads(saved.read_text()), fixture.raw)

    def test_review_error_remains_pending_and_can_resume_again(self) -> None:
        self.save(count=1)
        manifest = self.resume(Mock(side_effect=ValueError("malformed audit")))
        self.assertEqual(manifest["final_review_accepted"], 0)
        self.assertEqual(manifest["final_review_results"][0]["status"], "review_pending")
        loaded = load_progress(
            source_run=self.root / "resumed", test_id=1, config_path=self.fixture["config_path"],
            plan_path=self.fixture["plan_path"], config=self.config, context=self.context,
            tasks=[self.task, self.task],
        )
        self.assertEqual(loaded[0]["model_corrections_used"], 1)
        rejected = json.loads((self.root / "resumed" / "rejected_tests.json").read_text())
        self.assertEqual(rejected["rejected_tests"], [])

    def test_changed_candidate_or_plan_is_rejected_before_any_resume_call(self) -> None:
        for key in ("candidate", "plan_path"):
            with self.subTest(key=key):
                self.save()
                path = self.fixture[key]
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                with self.assertRaisesRegex(ValueError, "changed|approved inputs"):
                    self.load()
                path.write_bytes(original)

    def test_saved_correction_limit_prevents_a_second_writer_call(self) -> None:
        self.save(count=1)
        decision = FinalTraceDecision(
            test_id=1, accept=False, part_to_correct="user_request",
            specific_problem="The request is wrong.", required_correction="Correct the request.",
        )
        with patch.object(create_tests, "_unified_final_review_correction", side_effect=AssertionError("second correction")):
            manifest = self.resume(Mock(return_value=decision))
        self.assertEqual(manifest["final_review_results"][0]["status"], "replacement_required_after_one_correction")

    def test_one_review_error_does_not_discard_another_accepted_test(self) -> None:
        self.save()
        record = {
            "candidate": self.fixture["candidate"], "gate": self.fixture["gate"],
            "run_dir": self.proposal.parents[1], "first_pass": True,
            "certification_valid": True, "model_corrections_used": 0,
        }
        def reviewer(**kwargs):
            if kwargs["test_id"] == 2:
                raise ValueError("duplicate fact audit")
            return FinalTraceDecision(test_id=1, accept=True)
        out = self.root / "batch-review"
        accepted, results = create_tests._review_four_run_candidates(
            reviewable={1: dict(record), 2: dict(record)}, tasks_by_id={1: self.task, 2: self.task},
            context=self.context, config=self.config, out=out, concurrency=2,
            design_client=Mock(), query_client=Mock(), certifier=Mock(), reviewer=reviewer,
        )
        self.assertEqual(set(accepted), {1})
        self.assertEqual(results[1]["status"], "review_pending")
        candidates, _ = create_tests.write_completed_batch(
            out=out, plan_path=self.fixture["plan_path"], tasks_by_id={1: self.task, 2: self.task},
            accepted=accepted, authoring_results=[], final_review_results=results,
            checkpoint_identity=self.context.checkpoint_identity, persona=self.config.persona,
            evaluation_date=self.config.evaluation_date,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(json.loads((out / "rejected_tests.json").read_text())["rejected_tests"], [])
        self.assertEqual(json.loads((out / "pending_tests.json").read_text())["pending_tests"][0]["id"], 2)

    def test_cached_response_recovery_requires_matching_authenticated_request(self) -> None:
        from construction.runtime_model_calls import request_hash
        schema = {"response_schema": {"type": "object"}, "response_schema_name": "test"}
        files = {
            self.proposal / "initial_authoring_request.json": {"task": "approved"},
            self.proposal / "initial_authoring_schema.json": schema,
            self.proposal / "work" / "initial_authoring_response_cache.json": {
                "request_sha256": request_hash("system", {"task": "approved"}, **schema),
                "response": {"query": "saved response"},
            },
        }
        for path, value in files.items():
            dump_json(path, value)
        system = self.proposal / "initial_authoring_system.txt"
        system.write_text("system\n")
        hashes = {str(path): self.sha(path) for path in [*files, system]}
        recovered = cached_authoring_response(hashes, "initial")
        self.assertEqual(recovered[0], {"query": "saved response"})
        dump_json(self.proposal / "initial_authoring_request.json", {"task": "different"})
        with self.assertRaisesRegex(ValueError, "does not match"):
            cached_authoring_response(hashes, "initial")


if __name__ == "__main__":
    unittest.main()
