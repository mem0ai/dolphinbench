"""Focused regression coverage for the two-stage proposal planner."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from authoring.models import EvidenceDecisionBatch, SituationProposalBatch
from authoring.prompts import (
    AUTHOR_TEST_SYSTEM,
    EVIDENCE_PLANNER_SYSTEM,
    TEST_IDEA_PLANNER_SYSTEM,
    evidence_planner_system,
    test_idea_planner_system as idea_planner_system,
)
from authoring.propose import (
    _candidate_batch,
    _validate_evidence_response,
    load_persona_state,
    record_accepted_batch,
    record_approved_plan,
    record_rejected_tests,
)


def _frozen(*, investor_email: bool = False) -> SituationProposalBatch:
    if investor_email:
        situation = "An investor asked Morgan for a current company update."
        requested_work = "Send an investor email with the company's latest wins and funding context."
        work = "Write and send the investor email."
        without_history = "The email can describe current wins but omits the funding context."
    else:
        situation = "The product launch team needs a visual briefing for a customer meeting."
        requested_work = "Prepare the agreed screenshots and one-page overview."
        work = "Prepare the screenshots and one-page overview."
        without_history = "The materials use a generic description of the product."
    return SituationProposalBatch.model_validate(
        {
            "checkpoint_identity": "checkpoint",
            "persona": "morgan",
            "evaluation_date": "2026-09-14",
            "proposals": [
                {
                    "proposal_id": 1,
                    "new_situation": situation,
                    "requested_work": requested_work,
                    "work_items": [{"work_item_id": "1.1", "work": work}],
                    "candidate_fact_ids": [1],
                    "tool_names": ["send_email"],
                    "without_history": without_history,
                    "why_listed_tools_can_complete_requested_work": "send_email can deliver the completed email.",
                }
            ],
        }
    )


def _evidence_request(source: str) -> dict[str, object]:
    return {
        "selected_facts": [
            {
                "proposal_id": 1,
                "fact_id": 1,
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


def _accepted_evidence(
    result: str,
    *,
    work_item_text: str = "Prepare the screenshots and one-page overview.",
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
                            "work_item_id": "1.1",
                            "exact_work_item_text": work_item_text,
                            "fact_id": 1,
                            "required_result": result,
                            "source_session_id": "funding-1",
                            "exact_supporting_source_text": "closed an $18M Series B led by Northstar",
                            "why_source_supports_required_result": "The message states the funding round, amount, and lead investor.",
                            "why_required_for_work": "The investor update needs the funding context to be complete.",
                            "why_fact_still_applies_on_evaluation_date": "No later fact in this request replaces the funding result.",
                        }
                    ],
                }
            ],
        }
    )


class TwoStagePlanningTests(unittest.TestCase):
    @staticmethod
    def _state_batch(root: Path, name: str) -> Path:
        directory = root / name
        directory.mkdir()
        (directory / "planning_batch.json").write_text('{"batch": true}\n')
        (directory / "provenance.json").write_text('{"accepted": true}\n')
        return directory

    @staticmethod
    def _batch_entry(directory: Path) -> dict[str, str]:
        return {
            "directory": str(directory.resolve()),
            "planning_batch_sha256": hashlib.sha256(
                (directory / "planning_batch.json").read_bytes()
            ).hexdigest(),
            "provenance_sha256": hashlib.sha256(
                (directory / "provenance.json").read_bytes()
            ).hexdigest(),
        }

    def test_evidence_cannot_edit_a_frozen_screenshots_and_one_pager_item(self) -> None:
        frozen = _frozen()
        raw = _accepted_evidence("State the agreed product boundary in the materials.").model_dump()
        raw["decisions"][0]["work_items"] = [
            {
                "work_item_id": "1.2",
                "work": "Add a generic page, FAQ, live discussion, and scope hygiene review.",
            }
        ]
        with self.assertRaises(ValidationError):
            EvidenceDecisionBatch.model_validate(raw)

        changed_text = _accepted_evidence(
            "State the agreed product boundary in the materials."
        ).model_dump()
        changed_text["decisions"][0]["remembered_results"][0][
            "exact_work_item_text"
        ] = "Add a page, FAQ, live discussion, and general cleanup."
        validated = _validate_evidence_response(
            response=EvidenceDecisionBatch.model_validate(changed_text),
            frozen_proposal=frozen,
            request=_evidence_request(
                "We closed an $18M Series B led by Northstar."
            ),
        )
        self.assertEqual(validated.decisions[0].outcome, "reject")
        self.assertIn(
            "frozen work item text", validated.decisions[0].rejection_reason
        )

        source = "We closed an $18M Series B led by Northstar."
        evidence = _accepted_evidence("State the agreed product boundary in the materials.")
        _validate_evidence_response(
            response=evidence,
            frozen_proposal=frozen,
            request=_evidence_request(source),
        )
        task = _candidate_batch(frozen_proposal=frozen, response=evidence).tasks[0]
        self.assertEqual(task.new_situation, frozen.proposals[0].new_situation)
        self.assertEqual(task.requested_work, frozen.proposals[0].requested_work)
        self.assertEqual([item.work_item_id for item in task.work_items], ["1.1"])
        self.assertNotIn("task_description", task.model_dump(mode="json", exclude_none=True))

    def test_exact_evidence_and_series_b_investor_email_still_validate(self) -> None:
        frozen = _frozen(investor_email=True)
        source = "We closed an $18M Series B led by Northstar."
        evidence = _accepted_evidence(
            "State the $18M Series B led by Northstar in the funding context.",
            work_item_text="Write and send the investor email.",
        )
        _validate_evidence_response(
            response=evidence,
            frozen_proposal=frozen,
            request=_evidence_request(source),
        )
        task = _candidate_batch(frozen_proposal=frozen, response=evidence).tasks[0]
        self.assertEqual(task.required_results[0].work_item_id, "1.1")
        self.assertIn("$18M Series B led by Northstar", task.required_results[0].result)

    def test_persona_state_remembers_approved_and_rejected_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "release.json"
            release.write_text(
                json.dumps(
                    {
                        "persona": "morgan",
                        "checkpoint_identity": "checkpoint",
                        "evaluation_date": "2026-09-14",
                        "batches": [],
                    }
                )
            )
            state = root / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "persona": "morgan",
                        "checkpoint_identity": "checkpoint",
                        "evaluation_date": "2026-09-14",
                        "release_manifest": {
                            "path": str(release),
                            "sha256": hashlib.sha256(release.read_bytes()).hexdigest(),
                        },
                        "approved_plans": [],
                        "rejected_test_files": [],
                    }
                )
            )
            plan = root / "candidate_plan.json"
            plan.write_text('{"approved": true}\n')
            rejected = root / "rejected_tests.json"
            rejected.write_text(
                json.dumps(
                    {
                        "rejected_tests": [
                            {
                                "test_id": 3,
                                "candidate_plan": {"fact_ids": [1]},
                                "failure_reason": "The requested work was unsupported.",
                                "candidate_plan_path": str(plan),
                                "failure_manifest_path": str(root / "manifest.json"),
                            }
                        ]
                    }
                )
            )
            config = SimpleNamespace(
                persona="morgan",
                evaluation_date="2026-09-14",
                persona_state=str(state),
            )
            context = SimpleNamespace(
                persona="morgan", checkpoint_identity="checkpoint"
            )

            record_approved_plan(config=config, context=context, plan_path=plan)
            record_rejected_tests(
                config=config, context=context, rejected_path=rejected
            )
            loaded = load_persona_state(config=config, context=context)

            self.assertEqual(loaded["approved_plan_paths"], [plan])
            self.assertEqual(loaded["rejected_tests_paths"], [rejected])

    def test_accepted_batch_registration_is_idempotent_and_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "persona": "morgan",
                        "checkpoint_identity": "checkpoint",
                        "evaluation_date": "2026-09-14",
                        "batches": [],
                        "approved_plans": [],
                        "rejected_test_files": [],
                    }
                )
                + "\n"
            )
            batch = self._state_batch(root, "accepted")
            config = SimpleNamespace(
                persona="morgan",
                evaluation_date="2026-09-14",
                persona_state=str(state),
            )
            context = SimpleNamespace(
                persona="morgan", checkpoint_identity="checkpoint"
            )

            with patch(
                "authoring.propose._accepted_test_summaries",
                return_value=[{"test_id": 1}],
            ) as validate:
                record_accepted_batch(
                    config=config, context=context, directory=batch
                )
                first = state.read_bytes()
                record_accepted_batch(
                    config=config, context=context, directory=batch
                )

            self.assertEqual(state.read_bytes(), first)
            self.assertEqual(validate.call_count, 2)
            self.assertEqual(
                load_persona_state(config=config, context=context)[
                    "accepted_batch_dirs"
                ],
                [batch],
            )
            self.assertEqual(
                json.loads(state.read_text())["batches"],
                [self._batch_entry(batch)],
            )

            (batch / "provenance.json").write_text('{"changed": true}\n')
            with self.assertRaisesRegex(ValueError, "batch hash does not match"):
                load_persona_state(config=config, context=context)

    def test_working_batches_are_loaded_after_frozen_release_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frozen_batch = self._state_batch(root, "frozen")
            working_batch = self._state_batch(root, "working")
            release = root / "release.json"
            release.write_text(
                json.dumps(
                    {
                        "persona": "morgan",
                        "checkpoint_identity": "checkpoint",
                        "evaluation_date": "2026-09-14",
                        "batches": [self._batch_entry(frozen_batch)],
                    }
                )
                + "\n"
            )
            state = root / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "persona": "morgan",
                        "checkpoint_identity": "checkpoint",
                        "evaluation_date": "2026-09-14",
                        "release_manifest": {
                            "path": str(release),
                            "sha256": hashlib.sha256(
                                release.read_bytes()
                            ).hexdigest(),
                        },
                        "batches": [self._batch_entry(working_batch)],
                        "approved_plans": [],
                        "rejected_test_files": [],
                    }
                )
                + "\n"
            )
            config = SimpleNamespace(
                persona="morgan",
                evaluation_date="2026-09-14",
                persona_state=str(state),
            )
            context = SimpleNamespace(
                persona="morgan", checkpoint_identity="checkpoint"
            )

            loaded = load_persona_state(config=config, context=context)

            self.assertEqual(
                loaded["accepted_batch_dirs"], [frozen_batch, working_batch]
            )

    def test_registration_rejects_a_test_id_already_in_prior_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing_batch = self._state_batch(root, "existing")
            new_batch = self._state_batch(root, "new")
            state = root / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "persona": "morgan",
                        "checkpoint_identity": "checkpoint",
                        "evaluation_date": "2026-09-14",
                        "batches": [self._batch_entry(existing_batch)],
                        "approved_plans": [],
                        "rejected_test_files": [],
                    }
                )
                + "\n"
            )
            original_state = state.read_bytes()
            config = SimpleNamespace(
                persona="morgan",
                evaluation_date="2026-09-14",
                persona_state=str(state),
            )
            context = SimpleNamespace(
                persona="morgan", checkpoint_identity="checkpoint"
            )

            with patch(
                "authoring.propose._accepted_test_summaries",
                side_effect=ValueError("duplicate accepted test ID 1"),
            ) as validate:
                with self.assertRaisesRegex(
                    ValueError, "duplicate accepted test ID 1"
                ):
                    record_accepted_batch(
                        config=config, context=context, directory=new_batch
                    )

            self.assertEqual(state.read_bytes(), original_state)
            self.assertEqual(
                validate.call_args.kwargs["accepted_batch_dirs"],
                [existing_batch, new_batch],
            )


class PlannerPromptTests(unittest.TestCase):
    def test_active_prompts_load_from_files(self):
        directory = Path(__file__).parent / "prompt_text"
        self.assertEqual(TEST_IDEA_PLANNER_SYSTEM, (directory / "planner.md").read_text().strip())
        self.assertEqual(EVIDENCE_PLANNER_SYSTEM, (directory / "evidence_checker.md").read_text().strip())
        self.assertEqual(AUTHOR_TEST_SYSTEM, "\n\n".join(
            (directory / name).read_text().strip() for name in ("writer.md", "writer_output_reference.md")
        ))

    def test_prompt_factories_reject_invalid_proposal_counts(self):
        for factory in (idea_planner_system, evidence_planner_system):
            for invalid in (0, 26, True, "1", 1.5):
                with self.subTest(factory=factory.__name__, invalid=invalid):
                    with self.assertRaises(ValueError):
                        factory(invalid)


if __name__ == "__main__":
    unittest.main()
