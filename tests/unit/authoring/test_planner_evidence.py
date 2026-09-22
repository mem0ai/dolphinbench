"""Tests for the source-checking call in the public proposal flow."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from authoring.context import _related_supersession_chain_facts

from authoring.models import EvidenceDecisionBatch, SituationProposalBatch, evidence_decision_batch_schema
from authoring.propose import _candidate_batch, _validate_evidence_response


def _frozen() -> SituationProposalBatch:
    return SituationProposalBatch.model_validate(
        {
            "checkpoint_identity": "checkpoint",
            "persona": "morgan",
            "evaluation_date": "2026-09-14",
            "proposals": [
                {
                    "proposal_id": 1,
                    "new_situation": "An investor asked Morgan for a current company update.",
                    "requested_work": "Send an investor email with the company's latest wins and funding context.",
                    "work_items": [
                        {
                            "work_item_id": "1.1",
                            "work": "Write and send the investor email.",
                        }
                    ],
                    "candidate_fact_ids": [431],
                    "tool_names": ["send_email"],
                    "without_history": "The email can describe current wins but omits the funding context.",
                    "why_listed_tools_can_complete_requested_work": "send_email can deliver the completed email.",
                }
            ],
        }
    )


def _request(source: str = "We closed an $18M Series B led by Northstar.") -> dict[str, object]:
    return {
        "selected_facts": [
            {
                "proposal_id": 1,
                "fact_id": 431,
                "source_session_ids": ["funding-1"],
                "exact_source_messages": [
                    {
                        "source_session_id": "funding-1",
                        "source_date": "2025-03-02T09:00:00-08:00",
                        "exact_user_message_text": source,
                    }
                ],
            }
        ]
    }


def _evidence(
    *,
    work_item_id: str = "1.1",
    work_item_text: str = "Write and send the investor email.",
    quote: str = "closed an $18M Series B led by Northstar",
) -> EvidenceDecisionBatch:
    return EvidenceDecisionBatch.model_validate(
        {
            "checkpoint_identity": "checkpoint",
            "persona": "morgan",
            "evaluation_date": "2026-09-14",
            "decisions": [
                {
                    "proposal_id": 1,
                    "outcome": "accept",
                    "rejection_reason": "",
                    "remembered_results": [
                        {
                            "work_item_id": work_item_id,
                            "exact_work_item_text": work_item_text,
                            "fact_id": 431,
                            "required_result": "State the $18M Series B led by Northstar in the funding context.",
                            "source_session_id": "funding-1",
                            "exact_supporting_source_text": quote,
                            "why_source_supports_required_result": "The message states the funding round, amount, and lead investor.",
                            "why_required_for_work": "The investor update needs the funding context to be complete.",
                            "why_fact_still_applies_on_evaluation_date": "No later fact in this request replaces the funding result.",
                        }
                    ],
                }
            ],
        }
    )


class PlannerEvidenceTests(unittest.TestCase):
    def test_current_planning_requires_and_preserves_necessity(self):
        frozen, evidence = _frozen(), _evidence()
        request = dict(_request(), necessity_evidence_version=2)
        _validate_evidence_response(response=evidence, frozen_proposal=frozen, request=request)
        task = _candidate_batch(frozen_proposal=frozen, response=evidence).tasks[0]
        self.assertIn("funding context", task.why_memory_changes_result)
        evidence.decisions[0].remembered_results[0].why_required_for_work = ""
        validated = _validate_evidence_response(
            response=evidence, frozen_proposal=frozen, request=request
        )
        self.assertEqual(validated.decisions[0].outcome, "reject")
        self.assertIn(
            "why the requested work needs it",
            validated.decisions[0].rejection_reason,
        )
        schema = evidence_decision_batch_schema(1)["$defs"]["RememberedResultEvidence"]
        self.assertIn("why_required_for_work", schema["required"])

    def test_related_facts_keep_only_explicit_supersession_chain(self) -> None:
        context = SimpleNamespace(
            facts=[
                {"id": 8, "subjects": ["organization:pinecone"], "supersedes": [10]},
                {"id": 9, "subjects": ["organization:pinecone"], "supersedes": []},
                {"id": 10, "subjects": ["organization:pinecone"], "supersedes": [9]},
                {"id": 11, "subjects": ["organization:pinecone"], "supersedes": []},
                {"id": 12, "subjects": ["organization:scaffold"], "supersedes": []},
            ]
        )

        related = _related_supersession_chain_facts(context, [10])

        self.assertEqual([fact["id"] for fact in related], [8, 9])
        related[0]["subjects"].append("mutated")
        self.assertEqual(context.facts[0]["subjects"], ["organization:pinecone"])

    def assert_local_rejection(self, evidence: EvidenceDecisionBatch, message: str) -> None:
        validated = _validate_evidence_response(
            response=evidence,
            frozen_proposal=_frozen(),
            request=_request(),
        )
        decision = validated.decisions[0]
        self.assertEqual(decision.outcome, "reject")
        self.assertIn(message, decision.rejection_reason)
        self.assertEqual(decision.remembered_results, [])

    def test_exact_source_passage_and_work_item_are_accepted(self) -> None:
        frozen = _frozen()
        evidence = _evidence()
        _validate_evidence_response(
            response=evidence,
            frozen_proposal=frozen,
            request=_request(),
        )
        task = _candidate_batch(frozen_proposal=frozen, response=evidence).tasks[0]
        self.assertEqual(task.new_situation, frozen.proposals[0].new_situation)
        self.assertEqual(task.requested_work, frozen.proposals[0].requested_work)
        self.assertEqual(task.required_results[0].work_item_id, "1.1")
        self.assertIn("$18M Series B led by Northstar", task.required_results[0].result)

    def test_evidence_cannot_attach_to_a_work_item_that_was_not_frozen(self) -> None:
        self.assert_local_rejection(
            _evidence(work_item_id="1.2"), "missing work_item_id"
        )

    def test_evidence_must_quote_the_supplied_source_message(self) -> None:
        self.assert_local_rejection(
            _evidence(quote="Northstar funded the company later."),
            "not an exact supplied source passage",
        )

    def test_evidence_must_copy_the_frozen_work_item_text(self) -> None:
        self.assert_local_rejection(
            _evidence(work_item_text="Add a page, FAQ, live discussion, and cleanup."),
            "frozen work item text",
        )


if __name__ == "__main__":
    unittest.main()
