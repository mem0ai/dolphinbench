import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from authoring import create_tests as module
from authoring.final_trace_review import FinalTraceDecision, FinalTraceIssue
from authoring.models import CandidateTaskBatch, PlannedTask
from harness.test_spec_schema import TestSpec


class ApprovedRevisionTests(unittest.TestCase):
    def _task(self, fact_id: int = 1) -> PlannedTask:
        return PlannedTask.model_validate(
            {
                "task_description": "Send a fact-dependent message.",
                "fact_ids": [fact_id],
                "required_results": [
                    {"result": "the message contains the remembered detail", "fact_ids": [fact_id]}
                ],
                "expected_tools": ["send_message"],
                "action_without_memory": "The message omits the remembered detail.",
                "why_memory_changes_result": "The message needs the remembered detail.",
                "fact_application_explanations_on_evaluation_date": [
                    {"fact_id": fact_id, "why_fact_still_applies_on_evaluation_date": "It remains current."}
                ],
                "why_listed_tools_can_complete_requested_work": "send_message can send the message.",
            }
        )

    def _fixture(self, root: Path, *, candidate_facts: list[int] | None = None) -> dict[str, object]:
        published_root = root / "tests" / "morgan"
        published_root.mkdir(parents=True)
        corrections = root / "corrections"
        corrections.mkdir()
        config_path = root / "config.yaml"
        config_path.write_text("config")
        plan_path = corrections / "approved_plan.json"
        plan_path.write_text(json.dumps({"tasks": []}))
        candidate = {
            "id": 3,
            "narrative_anchor_date": "2026-09-14",
            "test": "Send the message.",
            "load_bearing_facts": candidate_facts or [1],
            "expected_tool_calls": ["send_message"],
            "grade": {
                "type": "tool_trace",
                "config": {
                    "today": "2026-09-14",
                    "assertions": [{"type": "tool_called", "tool": "send_message"}],
                },
            },
            "mock_state": {},
        }
        candidate_path = corrections / "003.yaml"
        candidate_path.write_text(yaml.safe_dump(candidate, sort_keys=False))
        published = published_root / "003.yaml"
        published.write_text(yaml.safe_dump({**candidate, "test": "old message"}, sort_keys=False))
        manifest_path = corrections / "approved_revisions.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "approved_plan": "approved_plan.json",
                    "candidates": {
                        "003": {
                            "path": "003.yaml",
                            "sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
                            "published_sha256": hashlib.sha256(published.read_bytes()).hexdigest(),
                        }
                    },
                }
            )
        )
        config = SimpleNamespace(
            persona="morgan", evaluation_date="2026-09-14", designer_model="review-model"
        )
        context = SimpleNamespace(
            persona="morgan",
            checkpoint_identity="checkpoint-id",
            checkpoint_path=root / "checkpoint",
            tools={"send_message": {"arguments": ["body"], "state_effect": {"external_effect": True}}},
        )
        return {
            "config_path": config_path,
            "manifest_path": manifest_path,
            "plan_path": plan_path,
            "candidate_path": candidate_path,
            "published_path": published,
            "config": config,
            "context": context,
        }

    def _load_patches(self, fixture: dict[str, object], task: PlannedTask):
        return patch.multiple(
            module,
            load_config=Mock(return_value=fixture["config"]),
            load_checkpoint_context=Mock(return_value=fixture["context"]),
            load_authoring_tasks=Mock(return_value=[task]),
            record_approved_plan=Mock(),
            record_rejected_tests=Mock(),
            verify_gate_python=Mock(),
            ROOT=Path(fixture["config_path"]).parent,
        )

    def _certifier(self, valid: bool):
        def certify(*_args: object, **_kwargs: object) -> dict[str, object]:
            shots = [
                {"with_memory": True, "passed": valid, "grade": {"passed": valid, "checks": [{"reason": "with"}]}},
                {"with_memory": True, "passed": valid, "grade": {"passed": valid, "checks": [{"reason": "with"}]}},
                {"with_memory": False, "passed": False, "grade": {"passed": False, "checks": [{"reason": "without"}]}},
                {"with_memory": False, "passed": False, "grade": {"passed": False, "checks": [{"reason": "without"}]}},
            ]
            return {
                "valid": valid,
                "verdict": "valid" if valid else "unsolvable_with_memory",
                "g1_pass_count": 2 if valid else 1,
                "g2_pass_count": 0,
                "shots": shots,
            }

        return certify

    def _reviewer(self, decisions: dict[int, bool], calls: list[int]):
        def review(*, test_id: int, out: Path, **_kwargs: object) -> FinalTraceDecision:
            calls.append(test_id)
            review_dir = out / f"{test_id:03d}"
            (review_dir / "work").mkdir(parents=True, exist_ok=True)
            (review_dir / "request.json").write_text("{}")
            raw = {
                "test_id": test_id,
                "accept": decisions[test_id],
                "issues": [],
                "_response_id": f"response-{test_id}",
                "_usage": {"reasoning_tokens": 7},
            }
            (review_dir / "response.json").write_text(json.dumps(raw))
            (review_dir / "work" / "response_cache.json").write_text(
                json.dumps({"response": raw})
            )
            decision = (
                FinalTraceDecision(test_id=test_id, accept=True)
                if decisions[test_id]
                else FinalTraceDecision(
                    test_id=test_id,
                    accept=False,
                    specific_problem="the candidate is not valid",
                    part_to_correct="grading_checks",
                    required_correction="reject the incomplete result",
                    issues=[
                        FinalTraceIssue(
                            owning_step="grading_checks",
                            specific_problem="the candidate is not valid",
                            required_correction="reject the incomplete result",
                        )
                    ],
                )
            )
            (review_dir / "decision.json").write_text(
                json.dumps(decision.model_dump(mode="json"))
            )
            return decision

        return review

    def test_plan_mismatch_is_rejected_before_certification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory), candidate_facts=[2])
            certifier = Mock(side_effect=AssertionError("paid call"))
            with self._load_patches(fixture, self._task()), patch.object(module, "_certify_cached", certifier):
                with self.assertRaisesRegex(ValueError, "facts do not match"):
                    module.create_approved_revisions(
                        config_path=fixture["config_path"],
                        approved_revisions_path=fixture["manifest_path"],
                        out=Path(directory) / "out",
                        concurrency=1,
                        confirm_paid_calls=True,
                        certifier=certifier,
                    )
            certifier.assert_not_called()

    def test_certification_requires_final_review_and_preserves_check_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            certifier = self._certifier(True)
            review_calls: list[int] = []
            with self._load_patches(fixture, self._task()):
                manifest = module.create_approved_revisions(
                    config_path=fixture["config_path"],
                    approved_revisions_path=fixture["manifest_path"],
                    out=Path(directory) / "out",
                    concurrency=1,
                    confirm_paid_calls=True,
                    certifier=certifier,
                    reviewer=self._reviewer({3: True}, review_calls),
                )
            self.assertEqual(review_calls, [3])
            self.assertEqual(manifest["certification_results"][0]["certification"]["shots"][0]["grade"]["checks"][0]["reason"], "with")
            evidence = manifest["final_review_results"][0]["final_review_evidence"]
            self.assertEqual(evidence["model_call_metadata"]["reasoning_tokens"], 7)
            self.assertTrue(evidence["raw_response"])

    def test_failed_candidate_is_excluded_and_accepted_batch_is_consumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root)
            candidate_2 = fixture["candidate_path"].with_name("004.yaml")
            candidate_2.write_bytes(fixture["candidate_path"].read_bytes().replace(b"id: 3", b"id: 4"))
            published_2 = fixture["published_path"].with_name("004.yaml")
            published_2.write_bytes(fixture["published_path"].read_bytes().replace(b"id: 3", b"id: 4"))
            raw = json.loads(fixture["manifest_path"].read_text())
            raw["candidates"]["004"] = {
                "path": "004.yaml",
                "sha256": hashlib.sha256(candidate_2.read_bytes()).hexdigest(),
                "published_sha256": hashlib.sha256(published_2.read_bytes()).hexdigest(),
            }
            fixture["manifest_path"].write_text(json.dumps(raw))
            tasks = [self._task(), self._task()]
            certifier = Mock(side_effect=[self._certifier(True)(None), self._certifier(False)(None)])
            review_calls: list[int] = []
            with self._load_patches(fixture, tasks[0]), patch.object(module, "load_authoring_tasks", return_value=tasks):
                manifest = module.create_approved_revisions(
                    config_path=fixture["config_path"],
                    approved_revisions_path=fixture["manifest_path"],
                    out=root / "out",
                    concurrency=1,
                    confirm_paid_calls=True,
                    certifier=certifier,
                    reviewer=self._reviewer({3: True, 4: False}, review_calls),
                )
            self.assertEqual(manifest["final_review_accepted"], 1)
            self.assertEqual(review_calls, [3, 4])
            batch = Path(manifest["accepted_batch"]) / "planning_batch.json"
            loaded_batch = CandidateTaskBatch.model_validate(json.loads(batch.read_text()))
            self.assertEqual(len(loaded_batch.tasks), 1)
            accepted = Path(manifest["accepted_candidates"][0])
            self.assertEqual(accepted.read_bytes(), fixture["candidate_path"].read_bytes())
            TestSpec.model_validate(yaml.safe_load(accepted.read_text()))
            self.assertEqual(manifest["certification_results"][1]["certification"]["valid"], False)

    def _save_first_run(self, root, fixture):
        module.create_approved_revisions(
            config_path=fixture["config_path"],
            approved_revisions_path=fixture["manifest_path"],
            out=root / "first", concurrency=1, confirm_paid_calls=True,
            certifier=self._certifier(True), reviewer=self._reviewer({3: True}, []),
        )
        source = root / "first" / "manifest.json"
        raw = json.loads(fixture["manifest_path"].read_text())
        raw["candidates"]["003"].update(
            saved_run=str(source), saved_run_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
        )
        return raw

    def test_grading_edit_reuses_all_four_executions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root)
            with self._load_patches(fixture, self._task()):
                raw = self._save_first_run(root, fixture)
                candidate = yaml.safe_load(fixture["candidate_path"].read_text())
                candidate["grade"]["config"]["assertions"].append({
                    "type": "field_equals", "tool": "send_message", "path": "args.body", "value": "remembered"
                })
                fixture["candidate_path"].write_text(yaml.safe_dump(candidate))
                raw["candidates"]["003"]["sha256"] = hashlib.sha256(fixture["candidate_path"].read_bytes()).hexdigest()
                fixture["manifest_path"].write_text(json.dumps(raw))
                certifier = Mock(side_effect=AssertionError("must not rerun agent"))
                regrader = Mock(side_effect=[{"passed": p, "details": [{"reason": "new check"}]} for p in [True, True, False, False]])
                result = module.create_approved_revisions(
                    config_path=fixture["config_path"], approved_revisions_path=fixture["manifest_path"],
                    out=root / "second", concurrency=1, confirm_paid_calls=True,
                    certifier=certifier, reviewer=self._reviewer({3: True}, []), regrader=regrader,
                )
                certifier.assert_not_called()
                self.assertEqual(regrader.call_count, 4)
                self.assertEqual(result["final_review_accepted"], 1)
                shot = result["certification_results"][0]["certification"]["shots"][0]
                self.assertEqual(shot["grade"]["details"][0]["reason"], "new check")

    def test_explicit_review_only_reuses_grades_but_runs_reviewer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._fixture(root)
            with self._load_patches(fixture, self._task()):
                raw = self._save_first_run(root, fixture)
                raw["candidates"]["003"]["review_only"] = True
                fixture["manifest_path"].write_text(json.dumps(raw))
                no_call = Mock(side_effect=AssertionError("no agent or grader call"))
                reviews = []
                result = module.create_approved_revisions(
                    config_path=fixture["config_path"], approved_revisions_path=fixture["manifest_path"],
                    out=root / "second", concurrency=1, confirm_paid_calls=True,
                    certifier=no_call, regrader=no_call, reviewer=self._reviewer({3: True}, reviews),
                )
                no_call.assert_not_called()
                self.assertEqual(reviews, [3])
                self.assertEqual(result["final_review_accepted"], 1)

    def test_saved_outputs_rejected_when_agent_inputs_changed(self):
        for changed in ("test", "mock_state", "load_bearing_facts"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = self._fixture(root)
                with self._load_patches(fixture, self._task()):
                    raw = self._save_first_run(root, fixture)
                    candidate = yaml.safe_load(fixture["candidate_path"].read_text())
                    candidate[changed] = {"test": "A different request", "mock_state": {"records": []}, "load_bearing_facts": [2]}[changed]
                    fixture["candidate_path"].write_text(yaml.safe_dump(candidate))
                    raw["candidates"]["003"]["sha256"] = hashlib.sha256(fixture["candidate_path"].read_bytes()).hexdigest()
                    fixture["manifest_path"].write_text(json.dumps(raw))
                    no_call = Mock(side_effect=AssertionError("paid call"))
                    with self.assertRaises(ValueError):
                        module.create_approved_revisions(
                            config_path=fixture["config_path"], approved_revisions_path=fixture["manifest_path"],
                            out=root / "second", concurrency=1, confirm_paid_calls=True,
                            certifier=no_call, reviewer=no_call, regrader=no_call,
                        )
                    no_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
