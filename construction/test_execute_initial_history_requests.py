from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from construction import execute_initial_history_requests as subject


class ExecuteInitialHistoryRequestsTest(unittest.TestCase):
    def make_request(
        self,
        run: Path,
        filename: str = "01_entity_inventory.json",
        *,
        stage: str = "entity_inventory",
        **overrides: object,
    ) -> None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        }
        envelope: dict[str, object] = {
            "stage": stage,
            "model": "gpt-5.5",
            "system": "Return the value.",
            "response_schema": schema,
            "input": {"stage": stage},
        }
        envelope.update(overrides)
        path = run / "requests" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(envelope), encoding="utf-8")

    def test_requires_explicit_confirmation_without_calling_client(self) -> None:
        with patch.object(subject, "cached_client_complete") as complete:
            with self.assertRaisesRegex(ValueError, "confirm-paid-calls"):
                subject.execute(
                    run=Path("unused"),
                    requests=["01_entity_inventory.json"],
                    confirmed=False,
                )
        complete.assert_not_called()

    def test_rejects_traversal_request_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "run"
            with self.assertRaisesRegex(ValueError, "plain JSON filename"):
                subject.execute(
                    run=run,
                    requests=["../01_entity_inventory.json"],
                    confirmed=True,
                )

    def test_rejects_invalid_envelope_before_calling_client(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "run"
            self.make_request(run, response_schema=["not", "an", "object"])
            with patch.object(subject, "cached_client_complete") as complete:
                with self.assertRaisesRegex(ValueError, "response_schema must be an object"):
                    subject.execute(
                        run=run,
                        requests=["01_entity_inventory.json"],
                        confirmed=True,
                    )
            complete.assert_not_called()

    def test_executes_one_requested_envelope_sequentially_with_cache_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "run"
            self.make_request(run)

            def complete(path, system, payload, client, **kwargs):
                self.assertEqual(path, run / "cache" / "entity_inventory.json")
                self.assertEqual(system, "Return the value.")
                self.assertEqual(payload, {"stage": "entity_inventory"})
                self.assertEqual(kwargs["response_schema_name"], "entity_inventory")
                return {
                    "value": "accepted",
                    "_response_id": "response-1",
                    "_usage": {"total_tokens": 10},
                }

            with patch.object(subject, "AzureJsonClient") as client, patch.object(
                subject, "cached_client_complete", side_effect=complete
            ) as complete_mock:
                result = subject.execute(
                    run=run,
                    requests=["01_entity_inventory.json"],
                    confirmed=True,
                )

            client.assert_called_once_with(model="gpt-5.5", reasoning_effort="high")
            complete_mock.assert_called_once()
            self.assertEqual(result, [{
                "stage": "entity_inventory",
                "response": str(run / "responses" / "entity_inventory.json"),
                "validated": True,
            }])
            self.assertEqual(
                json.loads((run / "responses" / "entity_inventory.json").read_text()),
                {"value": "accepted"},
            )
            receipt = json.loads((run / "receipts" / "entity_inventory.json").read_text())
            self.assertEqual(receipt["response_id"], "response-1")
            self.assertEqual(receipt["usage"], {"total_tokens": 10})
            self.assertTrue(receipt["validated"])

    def test_rejects_duplicate_filename_and_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "run"
            self.make_request(run)
            with self.assertRaisesRegex(ValueError, "each request"):
                subject.execute(
                    run=run,
                    requests=["01_entity_inventory.json", "01_entity_inventory.json"],
                    confirmed=True,
                )

            self.make_request(run, "02_existing_fact_subjects.json", stage="entity_inventory")
            with self.assertRaisesRegex(ValueError, "each stage"):
                subject.execute(
                    run=run,
                    requests=["01_entity_inventory.json", "02_existing_fact_subjects.json"],
                    confirmed=True,
                )


if __name__ == "__main__":
    unittest.main()
