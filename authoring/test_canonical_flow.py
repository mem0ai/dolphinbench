from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from authoring import propose as propose_module
from authoring import run as run_module
from authoring.context import load_checkpoint_context, load_config, planner_action_inventory
from authoring.final_trace_review import FinalTraceDecision
from authoring.models import (
    CandidateTaskBatch,
    DesignResponse,
    EvidenceDecisionBatch,
    SituationProposal,
    SituationProposalBatch,
)
from authoring.pipeline import AuthoringValidationError, _validate_answer_absence
from authoring.prompts import TEST_IDEA_PLANNER_SYSTEM
from authoring.propose import (
    execute_proposals,
    load_rejected_tests,
    prepare_proposals,
    proposal_request,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "authoring" / "configs" / "morgan.yaml"
ACCEPTED_BATCHES: list[Path] = []
REJECTED_TESTS = ROOT / "authoring" / "testdata" / "rejected_tests.json"


class CanonicalFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(CONFIG)
        cls.context = load_checkpoint_context(cls.config)

    def _proposal(self) -> SituationProposal:
        return SituationProposal(
            proposal_id=1,
            candidate_fact_ids=[142],
            new_situation="A vendor asks Morgan to schedule a routine call next week.",
            requested_work="Send a concise reply with a weekday time next week.",
            tool_names=["send_email"],
            work_items=[
                {
                    "work_item_id": "1.1",
                    "work": "Write and send the vendor reply.",
                }
            ],
            without_history="The assistant could offer a routine Friday vendor call.",
            why_listed_tools_can_complete_requested_work=(
                "send_email accepts the vendor recipient, subject, and body and "
                "writes the completed reply to sent email."
            ),
        )

    def _frozen(self, count: int = 1) -> SituationProposalBatch:
        proposals = []
        for proposal_id in range(1, count + 1):
            proposal = self._proposal().model_copy(deep=True)
            proposal.proposal_id = proposal_id
            proposal.work_items[0].work_item_id = f"{proposal_id}.1"
            proposals.append(proposal)
        return SituationProposalBatch(
            checkpoint_identity=self.context.checkpoint_identity,
            persona=self.context.persona,
            evaluation_date=self.config.evaluation_date,
            proposals=proposals,
        )

    def _evidence(self, frozen: SituationProposalBatch) -> EvidenceDecisionBatch:
        return EvidenceDecisionBatch.model_validate(
            {
                "checkpoint_identity": frozen.checkpoint_identity,
                "persona": frozen.persona,
                "evaluation_date": frozen.evaluation_date,
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
                    for proposal in frozen.proposals
                ],
            }
        )

    def _state_file(self, root: Path, *, rejected: list[Path] = ()) -> Path:
        def digest(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        path = root / "persona-state.json"
        release = root / "release.json"
        release.write_text(
            json.dumps(
                {
                    "persona": self.context.persona,
                    "checkpoint_identity": self.context.checkpoint_identity,
                    "evaluation_date": self.config.evaluation_date,
                    "batches": [],
                }
            )
            + "\n"
        )
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "persona": self.context.persona,
                    "checkpoint_identity": self.context.checkpoint_identity,
                    "evaluation_date": self.config.evaluation_date,
                    "release_manifest": {
                        "path": str(release.resolve()),
                        "sha256": digest(release),
                    },
                    "approved_plans": [],
                    "rejected_test_files": [
                        {"path": str(item.resolve()), "sha256": digest(item)}
                        for item in rejected
                    ],
                }
            )
            + "\n"
        )
        return path

    def test_final_review_accepts_plain_none_as_empty(self) -> None:
        decision = FinalTraceDecision.model_validate(
            {
                "test_id": 130,
                "accept": True,
                "specific_problem": "none",
                "part_to_correct": "none",
                "required_correction": "n/a",
            }
        )
        self.assertEqual(decision.specific_problem, "")
        self.assertEqual(decision.required_correction, "")

    def test_situation_planner_receives_all_current_facts_without_source_messages(self) -> None:
        accepted = {
            "test_id": 21,
            "fact_ids": [142],
            "requested_work": "Send a concise reply with a weekday time next week.",
            "tool_names": ["send_email"],
            "memory_dependent_results": ["Avoid offering a Friday vendor call."],
        }
        with tempfile.TemporaryDirectory() as directory:
            state = self._state_file(Path(directory))
            with patch.object(
                propose_module, "_accepted_test_summaries", return_value=[accepted]
            ):
                request = proposal_request(
                    config=self.config,
                    context=self.context,
                    persona_state_path=state,
                )
        self.assertTrue(request["current_facts"])
        self.assertGreater(len(request["prior_work"]["accepted"]), 0)
        self.assertEqual(request["checkpoint"]["proposals_to_return"], 10)
        self.assertTrue(all(set(row) == {"id", "statement", "applies_when", "subjects", "latest_source_date", "used_by_test_ids", "supersedes"} for row in request["current_facts"]))
        expected_usage: dict[int, list[int]] = {}
        for test in request["prior_work"]["accepted"]:
            for fact_id in test["fact_ids"]:
                expected_usage.setdefault(int(fact_id), []).append(int(test["test_id"]))
        self.assertTrue(expected_usage)
        self.assertEqual(
            {int(row["id"]): row["used_by_test_ids"] for row in request["current_facts"]},
            {
                int(row["id"]): sorted(expected_usage.get(int(row["id"]), []))
                for row in request["current_facts"]
            },
        )
        self.assertNotIn("source_sessions", request)
        self.assertNotIn("current_readable_app_state", request)
        self.assertNotIn("new_action_this_fact_can_change", request["current_facts"][0])

    def test_rejected_test_file_wiring_and_manifest_identity_are_reproducible(self) -> None:
        rejected = load_rejected_tests([REJECTED_TESTS])
        self.assertEqual([row["test_id"] for row in rejected], [121])
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "prepared"
            rejected_copy = Path(directory) / "rejected-tests.json"
            rejected_copy.write_bytes(REJECTED_TESTS.read_bytes())
            state = self._state_file(Path(directory), rejected=[rejected_copy])
            manifest = prepare_proposals(
                config_path=CONFIG,
                persona_state_path=state,
                out=out,
            )
            request = json.loads((out / "planner_request.json").read_text())
            self.assertEqual(request["previous_work"]["rejected"][0]["test_id"], rejected[0]["test_id"])
            self.assertEqual(manifest["persona_state"]["path"], str(state.resolve()))
            rejected_copy.write_text(rejected_copy.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "persona state rejected tests hash"):
                execute_proposals(
                    config_path=CONFIG,
                    persona_state_path=state,
                    out=out,
                    client=SimpleNamespace(model=self.config.planner_model),
                    confirm_paid_calls=True,
                )

    def test_every_rejected_item_reaches_the_planner_request(self) -> None:
        rejected = load_rejected_tests([REJECTED_TESTS])
        request = proposal_request(
            config=self.config,
            context=self.context,
            persona_state_path=self._state_file(Path(tempfile.mkdtemp()), rejected=[REJECTED_TESTS]),
        )
        self.assertEqual(request["prior_work"]["rejected"][0]["test_id"], rejected[0]["test_id"])

    def test_rejections_from_multiple_batches_all_reach_the_planner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            second = Path(directory) / "second.json"
            payload = json.loads(REJECTED_TESTS.read_text())
            payload["rejected_tests"][0]["failure_reason"] = "A different failed idea."
            second.write_text(json.dumps(payload))

            rejected = load_rejected_tests([REJECTED_TESTS, second])
            self.assertEqual(len(rejected), 2)
            self.assertEqual(
                [row["failure_reason"] for row in rejected],
                [
                    "The proposed request revealed the remembered answer.",
                    "A different failed idea.",
                ],
            )

    def test_rejected_test_file_requires_the_strict_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps({"rejected_tests": [{"test_id": 121}]}))
            with self.assertRaisesRegex(ValueError, "must contain exactly"):
                load_rejected_tests([path])

    def test_planner_actions_describe_only_recovered_tool_contracts(self) -> None:
        actions = planner_action_inventory(self.context.tools)
        self.assertTrue(
            all(set(action) == {"tool_name", "description"} for action in actions)
        )
        self.assertTrue(
            all(
                all(label in action["description"] for label in (
                    "Accepts:", "Returns:", "Reads:", "Writes:", "Cannot:"
                ))
                for action in actions
            )
        )
        by_name = {action["tool_name"]: action["description"] for action in actions}
        self.assertIn(
            "arguments to, subject, body, cc; required arguments to, subject, body",
            by_name["send_email"],
        )
        self.assertIn("Writes: sent_emails", by_name["send_email"])
        self.assertIn("No prior lookup is required", by_name["send_email"])
        self.assertNotIn("does_not_", by_name["send_email"])
        self.assertIn("cannot target an existing thread or reply context", by_name["send_discord_message"])

    def test_proposal_can_use_remembered_content_in_the_same_tool(self) -> None:
        proposal = self._proposal()
        self.assertIn("The tool itself need not change", " ".join(TEST_IDEA_PLANNER_SYSTEM.split()))
        self.assertEqual(proposal.candidate_fact_ids, [142])
        self.assertEqual(proposal.tool_names, ["send_email"])
        self.assertNotIn("Friday", proposal.requested_work)

    def test_proposal_validation_requires_fact_and_tool_explanations(self) -> None:
        proposal = self._proposal().model_dump()
        proposal["work_items"][0]["work"] = " "
        with self.assertRaisesRegex(ValidationError, "work item text must not be empty"):
            SituationProposal.model_validate(proposal)
        proposal = self._proposal().model_dump()
        proposal["requested_work"] = " "
        with self.assertRaisesRegex(ValidationError, "proposal text must not be empty"):
            SituationProposal.model_validate(proposal)

    def test_model_routing_keeps_blind_query_writer_on_gpt_5_4(self) -> None:
        self.assertEqual(self.config.planner_model, "gpt-5.6-sol")
        self.assertEqual(self.config.designer_model, "gpt-5.6-sol")
        self.assertEqual(self.config.query_model, "gpt-5.4")

        class FakeClient:
            def __init__(self, *, model: str, reasoning_effort: str) -> None:
                self.model = model
                self.reasoning_effort = reasoning_effort

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "propose",
                        "--config",
                        str(CONFIG),
                        "--out",
                        str(out),
                        "--confirm-paid-calls",
                    ],
                ),
                patch.object(propose_module, "prepare_proposals") as prepare,
                patch.object(
                    propose_module,
                    "execute_proposals",
                    return_value=SimpleNamespace(tasks=[]),
                ) as execute,
                patch.object(propose_module, "AzureJsonClient", FakeClient),
                patch("builtins.print"),
            ):
                propose_module.main()
            self.assertEqual(execute.call_args.kwargs["client"].model, "gpt-5.6-sol")
            self.assertNotIn("rejected_tests_paths", prepare.call_args.kwargs)
            self.assertNotIn("rejected_tests_paths", execute.call_args.kwargs)

            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "run",
                        "--config",
                        str(CONFIG),
                        "--plan",
                        str(out / "plan.json"),
                        "--out",
                        str(out / "run"),
                        "--start-id",
                        "1",
                        "--confirm-paid-calls",
                    ],
                ),
                patch.object(run_module.importlib.util, "find_spec", return_value=object()),
                patch.object(
                    run_module, "run_authoring", return_value={"summary": {}}
                ) as run_authoring,
                patch.object(run_module, "AzureJsonClient", FakeClient),
                patch("builtins.print"),
            ):
                run_module.main()
            self.assertEqual(
                run_authoring.call_args.kwargs["design_client"].model, "gpt-5.6-sol"
            )
            self.assertEqual(
                run_authoring.call_args.kwargs["query_client"].model, "gpt-5.4"
            )

    def test_answer_values_cannot_appear_in_query_or_state(self) -> None:
        with self.assertRaisesRegex(AuthoringValidationError, "reveals answer-bearing"):
            _validate_answer_absence(
                query="Send the update and say the round was $18M.",
                state={},
                answers=["$18M"],
            )
        _validate_answer_absence(
            query="Send the investor a concise update.",
            state={},
            answers=["$18M"],
        )

    def test_ten_proposals_convert_to_one_small_candidate_plan(self) -> None:
        proposal = self._proposal()
        response = self._frozen(10)
        evidence = self._evidence(response)
        plan = CandidateTaskBatch(
            checkpoint_identity=self.context.checkpoint_identity,
            persona=self.context.persona,
            evaluation_date=self.config.evaluation_date,
            tasks=[
                {
                    "new_situation": proposal.new_situation,
                    "requested_work": proposal.requested_work,
                    "work_items": [item.model_dump(mode="json") for item in proposal.work_items],
                    "fact_ids": proposal.candidate_fact_ids,
                    "required_results": [
                        {
                            "result": evidence.decisions[index].remembered_results[0].required_result,
                            "fact_ids": [142],
                            "work_item_id": proposal.work_items[0].work_item_id,
                        }
                    ],
                    "expected_tools": proposal.tool_names,
                    "action_without_memory": proposal.without_history,
                    "why_memory_changes_result": "The cited fact changes the email.",
                    "fact_application_explanations_on_evaluation_date": [
                        {
                            "fact_id": 142,
                            "required_result": evidence.decisions[index].remembered_results[0].required_result,
                            "source_session_id": "000091",
                            "exact_supporting_source_text": "Stop putting vendor calls on Fridays.",
                            "why_source_supports_required_result": "The source states the restriction.",
                            "why_fact_still_applies_on_evaluation_date": "No later fact replaces it.",
                        }
                    ],
                    "why_listed_tools_can_complete_requested_work": proposal.why_listed_tools_can_complete_requested_work,
                }
                for index, proposal in enumerate(response.proposals)
            ],
        )
        self.assertEqual(len(plan.tasks), 10)

    def test_execute_passes_rejected_tests_and_explanations_to_the_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "prepared"
            state = self._state_file(Path(directory), rejected=[REJECTED_TESTS])
            prepare_proposals(
                config_path=CONFIG, persona_state_path=state, out=out, count=1
            )
            frozen = self._frozen()
            proposal = frozen.proposals[0]
            response = {"ideas": [{
                "situation": proposal.new_situation,
                "work": proposal.requested_work,
                "fact_ids": proposal.candidate_fact_ids,
                "expected_tools": proposal.tool_names,
                "history_needed": "The source supplies a necessary scheduling restriction.",
                "likely_without_history": proposal.without_history,
            }], "shortfall_reason": ""}
            fake_client = SimpleNamespace(model=self.config.planner_model)
            with patch.object(
                propose_module,
                "cached_client_complete",
                return_value=response,
            ) as complete:
                batch = execute_proposals(
                    config_path=CONFIG,
                    persona_state_path=state,
                    out=out,
                    client=fake_client,
                    confirm_paid_calls=True,
                    count=1,
                )
            complete.assert_called_once()
            request = complete.call_args.args[2]
            self.assertEqual(request["previous_work"]["rejected"][0]["test_id"], 121)
            self.assertNotIn("selected_facts", request)
            self.assertEqual(batch.tasks[0].work, proposal.requested_work)

    def test_new_design_shape_uses_plain_field_names(self) -> None:
        self.assertIn("source_message_meaning", DesignResponse.model_fields)
        self.assertIn("reason_it_applies_on_test_date", DesignResponse.model_fields)
        self.assertIn("answers_that_must_not_appear", DesignResponse.model_fields)
        self.assertNotIn(
            "why_this_can_affect_an_action_on_the_test_date",
            DesignResponse.model_fields,
        )


if __name__ == "__main__":
    unittest.main()
