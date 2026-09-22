"""Tests for one-through-ten proposal batches and historical plan compatibility."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from authoring import propose as propose_module
from authoring.context import load_checkpoint_context, load_config
from authoring.models import EvidenceDecisionBatch, SituationProposalBatch
from authoring.prompts import IDEA_PLANNER_SYSTEM
from authoring.propose import (
    _candidate_batch,
    _validate_evidence_response,
    evidence_request,
    execute_proposals,
    parse_args,
    prepare_proposals,
    proposal_request,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "authoring" / "configs" / "morgan.yaml"


class VariableProposalCountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(CONFIG)
        cls.context = load_checkpoint_context(cls.config)

    def _ideas(self, count: int) -> dict:
        return {
            "ideas": [{
                "situation": f"Vendor {index} asks for a call next week.",
                "work": f"Send vendor {index} a weekday time next week.",
                "fact_ids": [142],
                "expected_tools": ["send_email"],
                "history_needed": "Morgan's Friday restriction determines the offered day.",
                "likely_without_history": "The assistant offers Friday.",
            } for index in range(count)],
            "shortfall_reason": "",
        }

    def _situation(self, count: int) -> SituationProposalBatch:
        return SituationProposalBatch.model_validate(
            {
                "checkpoint_identity": self.context.checkpoint_identity,
                "persona": self.context.persona,
                "evaluation_date": self.config.evaluation_date,
                "proposals": [
                    {
                        "proposal_id": proposal_id,
                        "new_situation": "A vendor asks Morgan to schedule a routine call next week.",
                        "requested_work": "Send a concise reply with a weekday time next week.",
                        "work_items": [
                            {
                                "work_item_id": f"{proposal_id}.1",
                                "work": "Write and send the vendor reply.",
                            }
                        ],
                        "candidate_fact_ids": [142],
                        "tool_names": ["send_email"],
                        "without_history": "The assistant could offer a routine Friday vendor call.",
                        "why_listed_tools_can_complete_requested_work": "send_email can deliver the reply.",
                    }
                    for proposal_id in range(1, count + 1)
                ],
            }
        )

    def _evidence(self, situation: SituationProposalBatch) -> EvidenceDecisionBatch:
        return EvidenceDecisionBatch.model_validate(
            {
                "checkpoint_identity": situation.checkpoint_identity,
                "persona": situation.persona,
                "evaluation_date": situation.evaluation_date,
                "decisions": [
                    {
                        "proposal_id": proposal.proposal_id,
                        "outcome": "accept",
                        "rejection_reason": "",
                        "remembered_results": [
                            {
                                "work_item_id": proposal.work_items[0].work_item_id,
                                "exact_work_item_text": proposal.work_items[0].work,
                                "fact_id": 142,
                                "required_result": "Avoid offering a Friday vendor call.",
                                "source_session_id": "000091",
                                "exact_supporting_source_text": "Stop putting vendor calls on Fridays.",
                                "why_source_supports_required_result": "The source states the scheduling restriction.",
                                "why_required_for_work": "Scheduling without the restriction would choose an unavailable time.",
                                "why_fact_still_applies_on_evaluation_date": "No later fact replaces it.",
                            }
                        ],
                    }
                    for proposal in situation.proposals
                ],
            }
        )

    def _all_rejected_evidence(
        self, situation: SituationProposalBatch
    ) -> EvidenceDecisionBatch:
        return EvidenceDecisionBatch.model_validate(
            {
                "checkpoint_identity": situation.checkpoint_identity,
                "persona": situation.persona,
                "evaluation_date": situation.evaluation_date,
                "decisions": [
                    {
                        "proposal_id": proposal.proposal_id,
                        "outcome": "reject",
                        "rejection_reason": "The source messages do not support this work.",
                        "remembered_results": [],
                    }
                    for proposal in situation.proposals
                ],
            }
        )

    def _state(self, root: Path, *, accepted: list[Path] = (), approved: list[Path] = ()) -> Path:
        def digest(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        path = root / "persona-state.json"
        path.write_text(
            json.dumps(
                {
                    "persona": self.context.persona,
                    "checkpoint_identity": self.context.checkpoint_identity,
                    "evaluation_date": self.config.evaluation_date,
                    "batches": [
                        {
                            "directory": str(batch.resolve()),
                            "planning_batch_sha256": digest(batch / "planning_batch.json"),
                            "provenance_sha256": digest(batch / "provenance.json"),
                        }
                        for batch in accepted
                    ],
                    "approved_plans": [
                        {"path": str(plan.resolve()), "sha256": digest(plan)}
                        for plan in approved
                    ],
                    "rejected_test_files": [],
                }
            )
            + "\n"
        )
        return path

    def _accepted_batch(self, root: Path, test_ids: list[int]) -> Path:
        directory = root / "accepted-batch"
        directory.mkdir()
        (directory / "planning_batch.json").write_text(
            json.dumps({"checkpoint_identity": self.context.checkpoint_identity}) + "\n"
        )
        results = []
        for test_id in test_ids:
            candidate = directory / f"{test_id:03d}.yaml"
            candidate.write_text("id: %03d\n" % test_id)
            results.append({"test_id": test_id, "candidate": str(candidate.resolve())})
        (directory / "provenance.json").write_text(
            json.dumps(
                {
                    "checkpoint_identity": self.context.checkpoint_identity,
                    "accepted_test_ids": test_ids,
                    "results": results,
                }
            )
            + "\n"
        )
        return directory

    def test_default_request_is_compact_and_asks_for_ten(self) -> None:
        request = proposal_request(config=self.config, context=self.context)
        self.assertEqual(request["checkpoint"]["proposals_to_return"], 10)
        self.assertTrue(request["current_facts"])
        self.assertNotIn("source_sessions", request)
        self.assertIn("Return up to requested_count ideas", IDEA_PLANNER_SYSTEM)

    def test_cli_exposes_only_the_new_state_and_count_inputs(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["propose", "--config", "config.yaml", "--persona-state", "state.json", "--replace-test-id", "3", "--count", "7", "--out", "out"],
        ):
            args = parse_args()
        self.assertEqual(args.persona_state, Path("state.json"))
        self.assertEqual(args.replace_test_id, [3])
        self.assertEqual(args.count, 7)
        self.assertFalse(hasattr(args, "approved_plan"))
        self.assertFalse(hasattr(args, "rejected_tests"))

    def test_preparation_writes_one_request_without_a_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "prepared"
            with patch.object(propose_module, "cached_client_complete") as complete:
                manifest = prepare_proposals(config_path=CONFIG, out=out, count=7)
            complete.assert_not_called()
            self.assertEqual(manifest["proposal_count"], 7)
            self.assertEqual(manifest["model_calls_made"], 0)
            request = json.loads((out / "planner_request.json").read_text())
            schema = json.loads((out / "planner_schema.json").read_text())
            self.assertEqual(request["requested_count"], 7)
            self.assertEqual(schema["properties"]["ideas"]["maxItems"], 7)
            self.assertEqual((out / "planner_system.txt").read_text().strip(), IDEA_PLANNER_SYSTEM)
            self.assertFalse((out / "evidence_system.txt").exists())

    def test_published_tests_are_summarized_through_the_verified_state_file(self) -> None:
        release = json.loads(
            (ROOT / "authoring" / "release_manifests" / "morgan.json").read_text()
        )
        request = proposal_request(config=self.config, context=self.context, count=7)
        self.assertEqual(
            [row["test_id"] for row in request["prior_work"]["accepted"]],
            release["test_ids"],
        )

    def test_replacement_is_excluded_before_current_fact_validation(self) -> None:
        request = proposal_request(
            config=self.config, context=self.context,
            replace_test_ids=[21], count=1,
        )
        self.assertNotIn(
            21,
            [row["test_id"] for row in request["prior_work"]["accepted"]],
        )

    def test_approved_plans_are_summarized_without_removing_their_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "approved-plan.json"
            plan.write_text(
                json.dumps(
                    _candidate_batch(
                        frozen_proposal=self._situation(1),
                        response=self._evidence(self._situation(1)),
                    ).model_dump(mode="json")
                )
                + "\n"
            )
            state = self._state(root, approved=[plan])
            request = proposal_request(config=self.config, context=self.context, persona_state_path=state, count=1)
        self.assertEqual(request["prior_work"]["approved"][0]["fact_ids"], [142])
        self.assertIn(142, {int(row["id"]) for row in request["current_facts"]})

    def test_execute_makes_one_call_and_stops_for_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "prepared"
            state = self._state(Path(directory))
            before = state.read_bytes()
            prepare_proposals(config_path=CONFIG, persona_state_path=state, out=out, count=3)
            with patch.object(propose_module, "cached_client_complete", return_value=self._ideas(3)) as complete:
                batch = execute_proposals(
                    config_path=CONFIG, out=out, client=SimpleNamespace(model=self.config.planner_model),
                    confirm_paid_calls=True, persona_state_path=state, count=3,
                )
            complete.assert_called_once()
            request = json.loads((out / "planner_request.json").read_text())
            self.assertEqual(complete.call_args.args[2], request)
            self.assertNotIn("source_sessions", request)
            self.assertEqual([task.idea_id for task in batch.tasks], [1, 2, 3])
            self.assertTrue(all(task.authoring_version == 4 for task in batch.tasks))
            self.assertTrue((out / "candidate_plan.json").is_file())
            self.assertEqual(json.loads((out / "manifest.json").read_text())["status"], "awaiting_human_acceptance")
            self.assertEqual(state.read_bytes(), before)

    def test_invalid_evidence_rejects_only_its_proposal(self) -> None:
        situation = self._situation(2)
        evidence = self._evidence(situation)
        evidence.decisions[1].remembered_results[0].exact_supporting_source_text = (
            "A fabricated source passage."
        )
        validated = _validate_evidence_response(
            response=evidence,
            frozen_proposal=situation,
            request=evidence_request(
                config=self.config,
                context=self.context,
                frozen_proposal=situation,
            ),
        )
        self.assertEqual(
            [decision.outcome for decision in validated.decisions],
            ["accept", "reject"],
        )
        self.assertIn(
            "not an exact supplied source passage",
            validated.decisions[1].rejection_reason,
        )
        self.assertEqual(
            len(_candidate_batch(frozen_proposal=situation, response=validated).tasks),
            1,
        )

    def test_invalid_fact_reference_preserves_response_without_rejecting_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "prepared"
            state = self._state(Path(directory))
            before = state.read_bytes()
            prepare_proposals(config_path=CONFIG, persona_state_path=state, out=out, count=2)
            raw = self._ideas(2)
            raw["ideas"][1]["fact_ids"] = [999999]
            with patch.object(propose_module, "cached_client_complete", return_value=raw):
                with self.assertRaises(ValueError):
                    execute_proposals(
                        config_path=CONFIG, out=out, client=SimpleNamespace(model=self.config.planner_model),
                        confirm_paid_calls=True, persona_state_path=state, count=2,
                    )
            self.assertEqual(json.loads((out / "planner_raw_response.json").read_text()), raw)
            self.assertFalse((out / "rejected_tests.json").exists())
            self.assertEqual(state.read_bytes(), before)

    def test_validation_failure_preserves_completed_call_and_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "prepared"
            prepare_proposals(config_path=CONFIG, out=out, count=1)
            raw = self._ideas(1)
            raw["ideas"][0]["unexpected_field"] = "invalid"
            raw["_usage"] = {"input_tokens": 101, "output_tokens": 11}
            with patch.object(propose_module, "cached_client_complete", return_value=raw):
                with self.assertRaises(ValueError):
                    execute_proposals(
                        config_path=CONFIG, out=out, client=SimpleNamespace(model=self.config.planner_model),
                        confirm_paid_calls=True, count=1,
                    )
            manifest = json.loads((out / "manifest.json").read_text())
            self.assertEqual(manifest["model_calls_made"], 1)
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failure_stage"], "planner_validation")
            self.assertEqual(manifest["calls"]["planner"]["status"], "completed")
            self.assertEqual(manifest["calls"]["planner"]["usage"], raw["_usage"])

    def test_same_command_replays_matching_caches_without_provider_calls(self) -> None:
        class Client:
            def __init__(self, model: str, responses: list[dict] | None = None):
                self.model = model
                self.responses = iter(responses or [])
                self.call_count = 0

            def complete(self, *args, **kwargs):
                self.call_count += 1
                return next(self.responses)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = self._state(root)
            config_path = root / "morgan.yaml"
            config_path.write_text(
                CONFIG.read_text().replace(
                    "persona_state: authoring/persona_states/morgan.json",
                    f"persona_state: {state}",
                )
            )
            config = load_config(config_path)
            out = root / "prepared"
            prepare_proposals(
                config_path=config_path,
                persona_state_path=state,
                out=out,
                count=1,
            )
            first_client = Client(config.planner_model, [self._ideas(1)])
            execute_proposals(
                config_path=config_path,
                out=out,
                client=first_client,
                confirm_paid_calls=True,
                persona_state_path=state,
                count=1,
            )
            replay_client = Client(config.planner_model)
            batch = execute_proposals(
                config_path=config_path,
                out=out,
                client=replay_client,
                confirm_paid_calls=True,
                persona_state_path=state,
                count=1,
            )
            self.assertEqual(len(batch.tasks), 1)
            self.assertEqual(first_client.call_count, 1)
            self.assertEqual(replay_client.call_count, 0)
            ledger = [
                json.loads(line)
                for line in (out / "call_ledger.jsonl").read_text().splitlines()
            ]
            self.assertEqual([row["cache_hit"] for row in ledger], [False, True])

    def test_execute_rejects_changed_rendered_input_before_model_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "prepared"
            prepare_proposals(config_path=CONFIG, out=out, count=1)
            (out / "planner_request.json").write_text("{}\n")
            with patch.object(propose_module, "cached_client_complete") as complete:
                with self.assertRaisesRegex(ValueError, "no longer matches its inputs"):
                    execute_proposals(
                        config_path=CONFIG, out=out,
                        client=SimpleNamespace(model=self.config.planner_model),
                        confirm_paid_calls=True, count=1,
                    )
            complete.assert_not_called()

    def test_overlapping_approved_plans_are_summarized_once_without_changing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            situation = self._situation(1)
            batch = _candidate_batch(frozen_proposal=situation, response=self._evidence(situation))
            plans = [root / "original.json", root / "retry.json"]
            for plan in plans:
                plan.write_text(batch.model_dump_json(indent=2))
            state = self._state(root, approved=plans)
            before = {path: path.read_bytes() for path in [*plans, state]}
            request = proposal_request(config=self.config, context=self.context, persona_state_path=state, count=1)
            self.assertEqual(len(request["prior_work"]["approved"]), 1)
            self.assertEqual({path: path.read_bytes() for path in before}, before)
            self.assertIn(142, {row["id"] for row in request["current_facts"]})

    def test_changed_approved_idea_remains_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            situation = self._situation(1)
            batch = _candidate_batch(frozen_proposal=situation, response=self._evidence(situation))
            original, changed = root / "original.json", root / "changed.json"
            original.write_text(batch.model_dump_json())
            batch.tasks[0].requested_work += " Offer two time options."
            changed.write_text(batch.model_dump_json())
            state = self._state(root, approved=[original, changed])
            request = proposal_request(config=self.config, context=self.context, persona_state_path=state, count=1)
            self.assertEqual(len(request["prior_work"]["approved"]), 2)

    def test_overlapping_plan_still_requires_matching_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            situation = self._situation(1)
            batch = _candidate_batch(frozen_proposal=situation, response=self._evidence(situation))
            plans = [root / "original.json", root / "retry.json"]
            for plan in plans:
                plan.write_text(batch.model_dump_json())
            state = self._state(root, approved=plans)
            plans[1].write_text(batch.model_dump_json() + "\n")
            with self.assertRaisesRegex(ValueError, "hash does not match"):
                proposal_request(config=self.config, context=self.context, persona_state_path=state, count=1)

    def test_duplicate_path_and_duplicate_within_one_plan_still_fail(self) -> None:
        situation = self._situation(1)
        batch = _candidate_batch(frozen_proposal=situation, response=self._evidence(situation))
        with patch.object(propose_module, "load_authoring_tasks", return_value=batch.tasks):
            with self.assertRaisesRegex(ValueError, "supplied more than once"):
                propose_module._approved_idea_summaries(
                    config=self.config, context=self.context, approved_plan_paths=[Path("same.json"), Path("same.json")],
                )
        with patch.object(propose_module, "load_authoring_tasks", return_value=batch.tasks * 2):
            with self.assertRaisesRegex(ValueError, "same idea more than once"):
                propose_module._approved_idea_summaries(
                    config=self.config, context=self.context, approved_plan_paths=[Path("same.json")],
                )

    def test_empty_response_records_shortfall_without_mutating_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "prepared"
            state = self._state(Path(directory))
            before = state.read_bytes()
            prepare_proposals(config_path=CONFIG, persona_state_path=state, out=out, count=1)
            raw = {"ideas": [], "shortfall_reason": "No distinct work found."}
            with patch.object(propose_module, "cached_client_complete", return_value=raw) as complete:
                batch = execute_proposals(
                    config_path=CONFIG, out=out, client=SimpleNamespace(model=self.config.planner_model),
                    confirm_paid_calls=True, persona_state_path=state, count=1,
                )
            complete.assert_called_once()
            self.assertEqual(batch.tasks, [])
            self.assertEqual(state.read_bytes(), before)
            manifest = json.loads((out / "manifest.json").read_text())
            self.assertEqual(manifest["proposed_ideas"], 0)
            self.assertEqual(manifest["shortfall_reason"], raw["shortfall_reason"])
            self.assertFalse((out / "rejected_tests.json").exists())

    def test_execute_requires_an_explanation_for_a_shortfall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "prepared"
            prepare_proposals(config_path=CONFIG, out=out, count=7)
            with patch.object(
                propose_module,
                "cached_client_complete",
                return_value=self._ideas(6),
            ):
                with self.assertRaisesRegex(ValueError, "must explain a shortfall"):
                    execute_proposals(
                        config_path=CONFIG,
                        out=out,
                        client=SimpleNamespace(model=self.config.planner_model),
                        confirm_paid_calls=True,
                        count=7,
                    )


if __name__ == "__main__":
    unittest.main()
