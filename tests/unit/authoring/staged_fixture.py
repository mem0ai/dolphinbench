"""Offline regressions for the approved staged workflow and evidence closure."""

import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from authoring.context import CheckpointContext, dump_json
from authoring.evidence_packets import prepare_evidence
from authoring.complete_test import certification_input, writer_payload
from authoring.review import ReviewPendingError, ReviewResponse, validate_review
from authoring.runtime_policy import CreationPolicy
from tests.unit.authoring.creation_fixture import CreationFixture, ScriptedClient, approving_review, writer


class StagedFixture(unittest.TestCase):
    def setUp(self):
        self.fixture = CreationFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for name in ("context", "root", "config", "config_path", "idea", "plan", "payload"):
            setattr(self, name, getattr(self.fixture, name))
        self.context = CheckpointContext(**vars(self.context), metadata={}, entities=[])

    def _run_stages(self, drafts, reviews, *, workflow="staged", transform_gate=None, fresh=False):
        from authoring.staged_creation import resume_staged
        source = self.root / "source"
        source.mkdir()
        policy_path = self.root / "policy.json"
        dump_json(policy_path, {"version": 1, "workflow": workflow, "preparation_workers": 1, "writing_workers": 1,
                               "preflight_workers": 1, "certification_workers": 1, "review_workers": 1})
        records = {1: {"progress": {"plan_position": 1, "status": "input_pending"},
                       "authenticated_files": {}, "model_corrections_used": 0, "execution_reruns_used": 0,
                       "test_overrides": {}}}
        writer_client = ScriptedClient(*drafts)
        review_client = ScriptedClient(*reviews)

        def certifier(*args, **kwargs):
            kwargs.pop("max_technical_retries", None)
            gate = self.fixture.certify(*args, **kwargs)
            return transform_gate(gate) if transform_gate else gate

        with (patch("authoring.staged_creation.load_checkpoint_context", return_value=self.context),
              patch("authoring.propose.load_persona_state", return_value={"approved_plan_paths": [self.plan], "accepted_batch_dirs": []}),
              patch("authoring.create_tests._progress_resume_inputs", return_value=(records, {1: self.idea}, {"authenticated_files": {}})),
              patch.object(CreationPolicy, "client", side_effect=lambda model, writing=False: writer_client if writing else review_client),
              patch("authoring.staged_creation.certify_candidate", side_effect=certifier),
              patch("authoring.propose.record_approved_plan") as approved,
              patch("authoring.propose.record_accepted_batch") as registered):
            if fresh:
                from authoring.create_tests import create_tests
                with patch("authoring.create_tests.load_checkpoint_context", return_value=self.context):
                    result = create_tests(config_path=self.config_path, plan_path=self.plan,
                                          out=self.root / "staged", start_id=301, concurrency=1,
                                          runtime_policy_path=policy_path, confirm_paid_calls=True)
                approved.assert_called_once()
            else:
                result = resume_staged(config_path=self.config_path, plan_path=self.plan,
                                       source_run=source, out=self.root / "staged", test_ids=[1],
                                       policy_path=policy_path, confirm_paid_calls=True)
        return result, writer_client, review_client, registered
