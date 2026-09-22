import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from authoring.revision_inputs import authenticate_legacy_execution, load_review_context
from tests.unit.authoring import test_revision_execution


class RevisionInputsTest(unittest.TestCase):
    setUp = test_revision_execution.RevisionExecutionTest.setUp
    bound_gate = test_revision_execution.RevisionExecutionTest.bound_gate

    def record(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        return {"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def evidence(self):
        bound = self.bound_gate()
        self.shots = json.loads(bound["source_gate"].read_text())["result"]["shots"]
        self.contract = {"arguments": ["to", "body"], "required_arguments": ["to", "body"]}
        self.sessions = [{"id": "source", "messages": [{"role": "user", "content": "original"}]}]
        self.request = {"user_request": self.candidate["test"], "starting_app_data": {},
                        "evaluation_date": self.candidate["narrative_anchor_date"],
                        "executions": self.shots,
                        "source_evidence": [{"fact_id": 1, "source_sessions": self.sessions}],
                        "selected_tool_contracts": {"send_email": self.contract}}
        self.context = SimpleNamespace(checkpoint_identity="current", tools={"send_email": self.contract})
        return {"candidate": {"path": "candidate.yaml", "sha256": bound["source_candidate_sha256"]},
                "gate": {"path": "gate.json", "sha256": bound["source_gate_sha256"]},
                "linked_review_request": self.record("review.json", self.request),
                "selected_tool_contract_differences_from_current": [],
                "checkpoint_correction_chains": [{
                    "input_record": {**self.record("inputs.json", {"checkpoint_identity": "old"}),
                                     "checkpoint_identity": "old"},
                    "history_unchanged_correction_chain": [self.record("correction.json", {
                        "old_checkpoint_identity": "old", "new_checkpoint_identity": "current",
                        "history_changed": False, "corrected_fact_ids": [2],
                    })],
                }]}

    def authenticate(self, evidence):
        with patch("authoring.revision_inputs._fact_evidence", return_value={"source_sessions": self.sessions}):
            return authenticate_legacy_execution(
                root=self.root, original=self.candidate, candidate=copy.deepcopy(self.candidate),
                evidence=evidence, context=self.context)

    def test_legacy_evidence_accepts_matching_sources(self):
        self.assertEqual(self.authenticate(self.evidence())["result"]["shots"], self.shots)

    def test_legacy_review_mismatch_is_not_hidden_by_trace_claims(self):
        for field, value in [("user_request", "different"), ("starting_app_data", {"leak": True}),
                             ("evaluation_date", "2026-09-15"), ("executions", [])]:
            with self.subTest(field=field):
                evidence = self.evidence()
                self.request[field] = value
                evidence["linked_review_request"] = self.record("review.json", self.request)
                with self.assertRaisesRegex(ValueError, "original task"):
                    self.authenticate(evidence)

    def test_legacy_full_source_change_blocks_reuse(self):
        evidence = self.evidence()
        self.sessions = [{"id": "source", "messages": [{"role": "user", "content": "newer"}]}]
        with self.assertRaisesRegex(ValueError, "complete sources"):
            self.authenticate(evidence)

    def test_legacy_corrected_selected_fact_blocks_reuse(self):
        evidence = self.evidence()
        evidence["checkpoint_correction_chains"][0]["history_unchanged_correction_chain"] = [
            self.record("correction.json", {"old_checkpoint_identity": "old",
                "new_checkpoint_identity": "current", "history_changed": False, "corrected_fact_ids": [1]})]
        with self.assertRaisesRegex(ValueError, "changes the execution inputs"):
            self.authenticate(evidence)

    def test_legacy_missing_checkpoint_binding_blocks_reuse(self):
        evidence = self.evidence()
        evidence["checkpoint_correction_chains"] = []
        with self.assertRaisesRegex(ValueError, "no authenticated checkpoint"):
            self.authenticate(evidence)

    def test_legacy_unapproved_tool_difference_blocks_reuse(self):
        evidence = self.evidence()
        self.context.tools = {"send_email": {**self.contract, "description": "changed"}}
        with self.assertRaisesRegex(ValueError, "differences changed"):
            self.authenticate(evidence)


class RevisionReviewContextTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.approval = self.root / "approval.json"
        self.approval.write_text("{}")
        self.path = self.root / "context.json"
        self.raw = {
            "version": 1,
            "approval_sha256": hashlib.sha256(self.approval.read_bytes()).hexdigest(),
            "tests": {"009": {"source_session_ids": ["000231"]}},
        }
        self.context = SimpleNamespace(history=[{
            "id": "000231", "narrative_date": "2023-03-13",
            "messages": ["Original first message", "Original second message"],
        }])

    def load(self):
        self.path.write_text(json.dumps(self.raw))
        return load_review_context(path=self.path, approval_path=self.approval,
                                   context=self.context, changed_ids={"009"})

    def test_preserves_complete_original_messages_and_dates(self):
        result = self.load()["009"]
        self.assertIn("not added to assistant certification", result["purpose"])
        self.assertEqual(result["original_messages"], [
            {"source_session_id": "000231", "message_id": "000231:message:0",
             "date": "2023-03-13", "text": "Original first message"},
            {"source_session_id": "000231", "message_id": "000231:message:1",
             "date": "2023-03-13", "text": "Original second message"},
        ])

    def test_no_manifest_leaves_inputs_unchanged(self):
        self.assertEqual(load_review_context(path=None, approval_path=self.approval,
                                            context=self.context, changed_ids={"009"}), {})

    def test_changed_approval_is_rejected(self):
        self.approval.write_text('{"changed": true}')
        with self.assertRaisesRegex(ValueError, "bind the approved"):
            self.load()

    def test_unapproved_test_is_rejected(self):
        self.raw["tests"]["010"] = self.raw["tests"].pop("009")
        with self.assertRaisesRegex(ValueError, "selected IDs"):
            self.load()

    def test_missing_or_repeated_sources_are_rejected(self):
        for ids in ([], ["missing"], ["000231", "000231"]):
            with self.subTest(ids=ids):
                self.raw["tests"]["009"]["source_session_ids"] = ids
                with self.assertRaisesRegex(ValueError, "missing or repeated"):
                    self.load()

    def test_arbitrary_context_and_candidate_changes_are_rejected(self):
        for key in ("text", "mock_state", "test", "grading"):
            with self.subTest(key=key):
                self.raw["tests"]["009"][key] = "injected"
                with self.assertRaisesRegex(ValueError, "only existing original"):
                    self.load()
                del self.raw["tests"]["009"][key]

    def test_scope_only_repair_adds_no_source_messages(self):
        self.raw["tests"]["009"] = {
            "source_session_ids": [], "review_scope": "approved_delta_v1"}
        result = self.load()["009"]
        self.assertEqual(result["original_messages"], [])
        self.assertEqual(result["review_scope"], "approved_delta_v1")

    def test_unknown_scope_and_unstructured_sources_are_rejected(self):
        for entry in ({"source_session_ids": [], "review_scope": "rewrite"},
                      {"source_session_ids": [{}]}, None):
            with self.subTest(entry=entry):
                self.raw["tests"]["009"] = entry
                with self.assertRaises(ValueError):
                    self.load()


if __name__ == "__main__":
    unittest.main()
