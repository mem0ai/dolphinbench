"""Local transport and recovery tests for the reviewed input presentation."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from authoring.input_rendering import render_authoring_input, render_evidence_input, render_planner_input
from authoring.progress import authoring_artifacts, cached_authoring_response
from authoring.run import _model_call
from construction.runtime_model_calls import request_hash


def restore_input(text: str) -> dict:
    """Recombine disjoint field sections to detect missing or duplicated data."""
    result = {}
    def merge(target, source):
        for key, value in source.items():
            if key not in target:
                target[key] = value
            elif isinstance(value, dict) and isinstance(target[key], dict):
                merge(target[key], value)
            else:
                raise AssertionError(f"duplicated input field: {key}")
    for block in re.findall(r"^```json\n(.*?)\n```$", text, re.M | re.S):
        merge(result, json.loads(block))
    return result


class InputRenderingTests(unittest.TestCase):
    def payload(self):
        return {
            "persona": "alex", "evaluation_date": "2028-01-01", "authoring_schema_version": 2,
            "planned_task": {
                "new_situation": "The on-call team needs a guide.", "requested_work": "Write the guide.",
                "work_items": [{"work_item_id": "1.1", "work": "Write the guide."}],
                "required_results": [{"result": "Use the supported limit.", "fact_ids": [539]}],
                "task_description": "Original description.",
            },
            "selected_fact_evidence": [{"source": "Exact text: \"value\"\n```\n## Not a section", "fact_id": 539}],
            "current_facts_that_may_change_the_meaning": [{"id": 540}],
            "tools": {"create_runbook_entry": {"arguments": {"body": "string"}}},
            "current_readable_app_state": {"runbooks": [{"id": "existing", "body": "Original record"}]},
            "future_field": {"untouched": True},
        }

    def test_authoring_sections_are_lossless_and_do_not_mutate_input(self):
        payload = self.payload()
        before = copy.deepcopy(payload)
        rendered = render_authoring_input(payload)
        self.assertEqual(restore_input(rendered), payload)
        self.assertEqual(payload, before)
        self.assertLess(rendered.index("Person and present work"), rendered.index("Approved results"))
        self.assertLess(rendered.index("Approved results"), rendered.index("Source evidence"))
        self.assertEqual(rendered, render_authoring_input(dict(reversed(list(payload.items())))))

    def test_planner_and_evidence_preserve_all_records(self):
        for renderer in (render_planner_input, render_evidence_input):
            payload = {
                "checkpoint": {"proposals_to_return": 1}, "current_facts": [{"id": 1}, {"id": 2}],
                "all_current_facts": [{"id": 1}, {"id": 2}], "prior_work": {"accepted": [2]},
                "frozen_proposal": {"work_items": ["unchanged"]},
                "selected_facts": [{"exact_source_messages": ["Exact\nsource"]}],
                "later_facts_about_same_subjects": [{"id": 2}], "tools": [{"tool_name": "send_email"}],
            }
            with self.subTest(renderer=renderer.__name__):
                self.assertEqual(restore_input(renderer(payload)), payload)

    def test_correction_keeps_original_input_and_allowed_fields_separate(self):
        payload = {
            "original_authoring_input": self.payload(), "previous_authoring": {"query": "Keep this message."},
            "allowed_correction_fields": ["prose_requirements"], "failure_to_fix": {"reason": "One check is wrong."},
        }
        rendered = render_authoring_input(payload)
        self.assertEqual(restore_input(rendered), payload)
        self.assertIn("Original input: Person and present work", rendered)
        for empty in ({"planned_task": {}}, {"planned_task": None}, {"original_authoring_input": {}}):
            self.assertEqual(restore_input(render_authoring_input(empty)), empty)

    def test_writer_sends_and_hashes_the_saved_rendered_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            payload = self.payload()
            client = Mock(model="local-test", complete=Mock(return_value={"query": "Written request"}))
            args = dict(directory=directory, name="initial_authoring", system="Instructions", payload=payload,
                        client=client, response_schema={"type": "object"}, response_schema_name="dolphinbench_complete_test")
            _model_call(**args)
            rendered = (directory / "initial_authoring_request.md").read_text()
            self.assertEqual(client.complete.call_args.args[1], rendered)
            self.assertEqual(restore_input(rendered), json.loads((directory / "initial_authoring_request.json").read_text()))
            cache = json.loads((directory / "work/initial_authoring_response_cache.json").read_text())
            self.assertEqual(cache["request_sha256"], request_hash("Instructions", rendered,
                response_schema=args["response_schema"], response_schema_name=args["response_schema_name"]))
            _model_call(**args)
            self.assertEqual(client.complete.call_count, 1)
            files = {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in authoring_artifacts(directory, "initial")}
            self.assertEqual(cached_authoring_response(files, "initial")[0], {"query": "Written request"})
            del files[str((directory / "initial_authoring_request.md").resolve())]
            with self.assertRaisesRegex(ValueError, "authenticated rendered request"):
                cached_authoring_response(files, "initial")
            (directory / "initial_authoring_request.md").unlink()
            with self.assertRaisesRegex(ValueError, "does not match its authenticated request"):
                cached_authoring_response(files, "initial")

    def test_writer_cache_tracks_presentation_changes_and_recovery_uses_saved_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            client = Mock(model="local-test", complete=Mock(return_value={"query": "Written request"}))
            args = dict(directory=directory, name="initial_authoring", system="Instructions", payload=self.payload(),
                        client=client, response_schema={"type": "object"}, response_schema_name="dolphinbench_complete_test")
            _model_call(**args)
            rendered = (directory / "initial_authoring_request.md").read_text()
            files = {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in authoring_artifacts(directory, "initial")}
            with patch("authoring.run.render_authoring_input", return_value=rendered + "\n"):
                self.assertEqual(cached_authoring_response(files, "initial")[0], {"query": "Written request"})
                _model_call(**args)
            self.assertEqual(client.complete.call_count, 2)
            self.assertEqual(client.complete.call_args.args[1], rendered + "\n")
            (directory / "initial_authoring_request.md").write_text("Changed input")
            with self.assertRaisesRegex(ValueError, "does not match its authenticated request"):
                cached_authoring_response(files, "initial")

    def test_old_json_call_format_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = Mock(complete=Mock(return_value={}))
            _model_call(directory=Path(temporary), name="legacy", system="Legacy", payload={"data": 1}, client=client)
            self.assertEqual(client.complete.call_args.args[1], {"data": 1})
            self.assertFalse((Path(temporary) / "legacy_request.md").exists())


if __name__ == "__main__":
    unittest.main()
