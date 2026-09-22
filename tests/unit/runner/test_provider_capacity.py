import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harness.provider_capacity import _rate_state, _retry_delay, _safe_headers, chat_completion, provider_call


class ProviderCapacityTests(unittest.TestCase):
    def test_success_headers_are_allowlisted_without_changing_the_response(self):
        response = object()
        client = Mock()
        client.with_raw_response.chat.completions.create.return_value = SimpleNamespace(
            headers={"x-ratelimit-limit-tokens": "1000000", "set-cookie": "secret"}, parse=lambda: response)
        transport = {}
        with patch.dict(os.environ, {"DOLPHINBENCH_AUTHORING_PROVIDER_CAPACITY": "enabled"}):
            self.assertIs(chat_completion(client, {"model": "fixture"}, transport), response)
        self.assertEqual(transport["rate_limit_headers"], {"x-ratelimit-limit-tokens": "1000000"})
        client.chat.completions.create.assert_not_called()

    def test_shared_pacing_and_cooldown(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "DOLPHINBENCH_AUTHORING_PROVIDER_TPM": "300000", "DOLPHINBENCH_AUTHORING_PROVIDER_RPM": "60"}):
            root = Path(directory)
            with patch("harness.provider_capacity.time.time", return_value=100.):
                self.assertEqual(_rate_state(root, "primary", 50000), 0)
                self.assertEqual(_rate_state(root, "primary", 50000), 10)
                self.assertEqual(_rate_state(root, "baseline", 50000), 0)
                _rate_state(root, "primary", 0, cooldown=90)
            with patch("harness.provider_capacity.time.time", return_value=130.):
                self.assertEqual(_rate_state(root, "primary", 50000), 60)

    def test_retry_headers_and_secrets(self):
        exc = SimpleNamespace(response=SimpleNamespace(headers={"Retry-After": "12", "Authorization": "secret"}))
        self.assertEqual(_safe_headers(exc), {"retry-after": "12"})
        self.assertEqual(_retry_delay({"retry-after-ms": "2000"}, 0), 2)
        self.assertEqual(_retry_delay({"retry-after": "Thu, 01 Jan 1970 00:02:00 GMT"}, 100), 20)
        self.assertEqual(_retry_delay({}, 100), 60)

    def test_429_records_cooldown_and_releases_slot(self):
        class RateError(Exception):
            status_code = 429
            headers = {"retry-after": "30"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "DOLPHINBENCH_AUTHORING_PROVIDER_CAPACITY": directory, "DOLPHINBENCH_AUTHORING_PROVIDER_SLOTS": "1",
                "DOLPHINBENCH_AUTHORING_TRANSPORT_LEDGER": directory + "/ledger.jsonl"}):
            with self.assertRaises(RateError), provider_call(model="primary", request={}, kind="test"):
                raise RateError()
            row = json.loads((Path(directory) / "ledger.jsonl").read_text())
            self.assertEqual(row["cooldown_seconds"], 30)
            self.assertEqual(row["error_type"], "RateError")
            self.assertGreater(_rate_state(Path(directory), "primary", 1), 29)


class ProviderRateTests(unittest.TestCase):
    def test_header_feedback_reserves_inflight_calls_and_keeps_cooldown(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "DOLPHINBENCH_AUTHORING_PROVIDER_TPM": "450000", "DOLPHINBENCH_AUTHORING_PROVIDER_RPM": "120",
                "DOLPHINBENCH_AUTHORING_PROVIDER_HEADER_FEEDBACK": "1"}):
            root = Path(directory)
            headers = {"x-ratelimit-limit-tokens": "250000", "x-ratelimit-remaining-tokens": "250000",
                       "x-ratelimit-reset-tokens": "0"}
            with patch("harness.provider_capacity.time.time", return_value=100.):
                self.assertEqual(_rate_state(root, "baseline", 100000, ticket="first"), 0)
                _rate_state(root, "baseline", 0, ticket="first", cancel=True, headers=headers, observed_start=100)
            with patch("harness.provider_capacity.time.time", return_value=101.):
                self.assertEqual(_rate_state(root, "baseline", 150000, ticket="second"), 0)
            with patch("harness.provider_capacity.time.time", return_value=102.):
                self.assertGreater(_rate_state(root, "baseline", 100000, ticket="third"), 0)
                _rate_state(root, "baseline", 0, cooldown=60)
                _rate_state(root, "baseline", 0, ticket="second", cancel=True, headers=headers, observed_start=101)
                self.assertGreaterEqual(_rate_state(root, "baseline", 100000, ticket="third"), 60)

    def test_fifo_prevents_new_requests_overtaking_waiting_work(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "DOLPHINBENCH_AUTHORING_PROVIDER_TPM": "300000", "DOLPHINBENCH_AUTHORING_PROVIDER_RPM": "60"}):
            root = Path(directory)
            with patch("harness.provider_capacity.time.time", return_value=100.):
                _rate_state(root, "primary", 5000, ticket="first", enqueue_only=True)
                _rate_state(root, "primary", 5000, ticket="second", enqueue_only=True)
                self.assertGreater(_rate_state(root, "primary", 5000, ticket="second"), 0)
                self.assertEqual(_rate_state(root, "primary", 5000, ticket="first"), 0)
                self.assertEqual(_rate_state(root, "primary", 5000, ticket="second"), 1)
            with patch("harness.provider_capacity.time.time", return_value=101.):
                _rate_state(root, "primary", 5000, ticket="third", enqueue_only=True)
                self.assertGreater(_rate_state(root, "primary", 5000, ticket="third"), 0)
                self.assertEqual(_rate_state(root, "primary", 5000, ticket="second"), 0)

    def test_cancelled_and_dead_waiters_do_not_block_the_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _rate_state(root, "primary", 1, ticket="cancelled", enqueue_only=True)
            _rate_state(root, "primary", 1, ticket="cancelled", cancel=True)
            _rate_state(root, "primary", 1, ticket="dead", enqueue_only=True)
            with patch("harness.provider_capacity._alive", return_value=False):
                self.assertEqual(_rate_state(root, "primary", 1, ticket="live"), 0)

    def test_cooldown_preserves_fifo(self):
        with tempfile.TemporaryDirectory() as directory, patch("harness.provider_capacity.time.time", return_value=100.):
            root = Path(directory)
            _rate_state(root, "primary", 1, ticket="first", enqueue_only=True)
            _rate_state(root, "primary", 0, cooldown=60)
            self.assertEqual(_rate_state(root, "primary", 1, ticket="first"), 60)


if __name__ == "__main__":
    unittest.main()
