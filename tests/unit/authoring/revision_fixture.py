import copy
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import yaml

from tests.unit.authoring import test_revision_review as fixtures
from authoring.revision_workflow import validate_revision_test


class RevisionFixture(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RevisionReviewTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.candidate = self.fixture.out / "candidate.yaml"
        self.candidate.write_text(yaml.safe_dump(self.fixture.candidate))
        self.out = self.fixture.out / "validation"
        self.client = SimpleNamespace(
            model="test", reasoning_effort="medium", max_output_tokens=1000,
            complete=Mock(side_effect=[self.fixture.response().model_dump(mode="json"),
                                       {"decision": "approve", "issues": [], "examples": None}]))
        self.grader = Mock(side_effect=self.grade)
        self.certifier = Mock(side_effect=self.certify)
        self.request = copy.deepcopy(self.fixture.request)
        self.request["legacy_execution_note"] = {
            "approved": True, "test_ids": ["001"], "limitation": "Missing old initial transcripts."}
        self.shots = [{"with_memory": flag, "passed": flag, "response_text": "done",
                       "tool_calls": [{"tool": "send_email", "args": {
                           "to": "Anna", "subject": "Update", "body": "right" if flag else "wrong"}}]}
                      for flag in [True, True, False, False]]
        self.gate = {"result": {"test_id": "001", "shots": self.shots}}

    def grade(self, calls, config, **kwargs):
        passed = calls[0]["args"]["body"] in {"right", "equivalent"}
        return {"passed": passed, "details": [{"assertion": a, "ok": passed}
                                               for a in config["assertions"]]}

    def certify(self, *args, **kwargs):
        shots = copy.deepcopy(self.shots)
        state_hash = hashlib.sha256(json.dumps({}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        for shot in shots:
            shot["grade"] = self.grade(shot["tool_calls"], self.fixture.candidate["grade"]["config"])
            shot["agent_input_id"] = "initial"
            shot["tool_results"] = [{"call_index": i, **call, "result": "sent"}
                                    for i, call in enumerate(shot["tool_calls"])]
        return {"test_id": "001", "shots": shots, "valid": True, "g1_pass_count": 2, "g2_pass_count": 0,
                "agent_inputs": {"initial": {"tools": [], "starting_state_sha256": state_hash,
                    "messages": [{"role": "system", "content": "assistant"},
                                 {"role": "user", "content": self.request["user_request"]}]}}}

    def run_workflow(self, **overrides):
        args = dict(candidate_path=self.candidate, request=self.request, saved_gate=self.gate,
                    oracle_input=None, out=self.out, checkpoint=self.fixture.out, persona="morgan",
                    client=self.client, grader=self.grader, certifier=self.certifier,
                    execution_identity={"runtime": "test-runtime"}, confirm_paid_calls=True)
        return validate_revision_test(**{**args, **overrides})
