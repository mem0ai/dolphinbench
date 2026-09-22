import copy
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harness.paid_budget import PaidBudget, PaidBudgetError, budgeted_completion


class PaidBudgetTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.approval = {
            "version": 1, "approved": True, "work_sha256": "test-work",
            "rate_source": "test fixture, not provider pricing", "usd_limit": "0.0612",
            "max_provider_requests": 10, "models": {"test-model": {
                "input_usd_per_million": "2", "output_usd_per_million": "4",
                "max_input_tokens": 10000, "max_output_tokens": 100}}}
        self.write_approval()
        self.budget = PaidBudget(self.directory)
        self.request = {"model": "test-model", "messages": [], "max_completion_tokens": 100}

    def write_approval(self):
        (self.directory / "approval.json").write_text(json.dumps(self.approval))

    def ledger(self):
        return json.loads((self.directory / "ledger.json").read_text())

    def test_concurrent_reservations_cannot_overspend(self):
        def reserve(_):
            try:
                return self.budget.reserve("test-model", self.request)
            except PaidBudgetError:
                return None
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(reserve, range(20)))
        self.assertEqual(sum(result is not None for result in results), 3)
        self.assertEqual(len(self.ledger()["calls"]), 3)

    def test_actual_usage_releases_unused_reservation_without_cache_discount(self):
        call = self.budget.reserve("test-model", self.request)
        self.budget.settle(call, {"prompt_tokens": 100, "completion_tokens": 10,
                                  "prompt_tokens_details": {"cached_tokens": 100}})
        self.assertEqual(self.ledger()["calls"][0]["charged_usd"], "0.00024")

    def test_missing_usage_and_uncertain_calls_retain_full_reservation(self):
        for usage in [None, {"prompt_tokens": 10}, {"prompt_tokens": True, "completion_tokens": 2}]:
            call = self.budget.reserve("test-model", self.request)
            self.budget.settle(call, usage)
        with self.assertRaisesRegex(PaidBudgetError, "exhausted"):
            self.budget.reserve("test-model", self.request)
        self.assertTrue(all(row["charged_usd"] == "0.0204" for row in self.ledger()["calls"]))

    def test_unknown_model_is_blocked(self):
        with self.assertRaisesRegex(PaidBudgetError, "no approved pricing"):
            self.budget.reserve("unknown", self.request)

    def test_unbounded_or_oversized_requests_are_blocked(self):
        for extra in [{"max_completion_tokens": None}, {"max_completion_tokens": 101},
                      {"messages": [{"content": "x" * 10000}]}]:
            with self.assertRaises(PaidBudgetError):
                self.budget.reserve("test-model", {**self.request, **extra})
        self.assertFalse((self.directory / "ledger.json").exists())

    def test_changed_approval_cannot_reset_existing_budget(self):
        self.budget.reserve("test-model", self.request)
        self.approval["usd_limit"] = "100"
        self.write_approval()
        with self.assertRaisesRegex(PaidBudgetError, "changed during execution"):
            self.budget.reserve("test-model", self.request)
        with self.assertRaisesRegex(PaidBudgetError, "cannot be reset"):
            PaidBudget(self.directory).reserve("test-model", self.request)

    def test_provider_bound_violation_blocks_further_requests(self):
        call = self.budget.reserve("test-model", self.request)
        with self.assertRaisesRegex(PaidBudgetError, "exceeded"):
            self.budget.settle(call, {"prompt_tokens": 10001, "completion_tokens": 10})
        with self.assertRaisesRegex(PaidBudgetError, "exhausted"):
            self.budget.reserve("test-model", self.request)

    def test_completed_request_cannot_be_settled_twice(self):
        call = self.budget.reserve("test-model", self.request)
        self.budget.settle(call, None)
        with self.assertRaisesRegex(PaidBudgetError, "already accounted"):
            self.budget.settle(call, {"prompt_tokens": 0, "completion_tokens": 0})

    def test_sdk_retries_are_disabled_and_output_is_bounded(self):
        client = Mock()
        client.with_options.return_value.chat.completions.create.return_value = SimpleNamespace(
            usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 100, "completion_tokens": 10}))
        request = {"model": "test-model", "messages": []}
        budgeted_completion(client, request, self.budget)
        client.with_options.assert_called_once_with(max_retries=0)
        self.assertEqual(client.with_options.return_value.chat.completions.create.call_args.kwargs[
            "max_completion_tokens"], 100)
        self.assertNotIn("max_completion_tokens", request)

    def test_failed_sdk_request_keeps_its_reservation(self):
        client = Mock()
        client.with_options.return_value.chat.completions.create.side_effect = RuntimeError("transport")
        with self.assertRaises(RuntimeError):
            budgeted_completion(client, self.request, self.budget)
        self.assertEqual(self.ledger()["calls"][0]["charged_usd"], "0.0204")

    def test_judge_transport_retry_counts_both_provider_requests(self):
        from graders import llm_judge
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"usage": {"prompt_tokens": 100, "completion_tokens": 10}}).encode()
        request = urllib.request.Request("https://example.invalid", data=json.dumps(self.request).encode())
        with patch.dict("os.environ", {"DOLPHINBENCH_PAID_BUDGET": str(self.directory)}), \
                patch.object(llm_judge.urllib.request, "urlopen", side_effect=[urllib.error.URLError("timeout"), response]), \
                patch.object(llm_judge.time, "sleep"):
            llm_judge._urlopen_json(request, 1, model="test-model")
        self.assertEqual(len(self.ledger()["calls"]), 2)
        self.assertEqual(self.ledger()["calls"][0]["charged_usd"], "0.0204")
        self.assertEqual(self.ledger()["calls"][1]["charged_usd"], "0.00024")

    def test_oracle_retry_uses_the_same_shared_budget(self):
        from harness import oracle
        client = Mock()
        response = SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: {
            "prompt_tokens": 100, "completion_tokens": 10}))
        client.with_options.return_value.chat.completions.create.side_effect = [RuntimeError("timeout"), response]
        with patch.dict("os.environ", {"DOLPHINBENCH_PAID_BUDGET": str(self.directory)}), \
                patch.object(oracle.time, "sleep"):
            self.assertIs(oracle._create_with_retry(client, self.request), response)
        self.assertEqual(len(self.ledger()["calls"]), 2)
        client.chat.completions.create.assert_not_called()

    def test_reviewer_client_uses_the_same_shared_budget(self):
        from construction import llm
        client = Mock()
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=10,
                                model_dump=lambda: {"prompt_tokens": 100, "completion_tokens": 10})
        client.with_options.return_value.chat.completions.create.return_value = SimpleNamespace(
            usage=usage, id="test-response", choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"decision": "approve"}'))])
        with patch.dict("os.environ", {"DOLPHINBENCH_PAID_BUDGET": str(self.directory),
                                       "AZURE_OPENAI_API_KEY": "test-key",
                                       "AZURE_OPENAI_ENDPOINT": "https://example.invalid"}), \
                patch.object(llm, "AzureOpenAI", return_value=client):
            result = llm.AzureJsonClient("test-model").complete("instructions", {})
        self.assertEqual(result["decision"], "approve")
        self.assertEqual(len(self.ledger()["calls"]), 1)
        client.chat.completions.create.assert_not_called()

    def test_invalid_rates_and_limits_are_blocked(self):
        original = copy.deepcopy(self.approval)
        for value in ["NaN", "Infinity", "-1", "0"]:
            self.approval = copy.deepcopy(original)
            self.approval["models"]["test-model"]["input_usd_per_million"] = value
            self.write_approval()
            with self.assertRaises(PaidBudgetError):
                PaidBudget(self.directory)


if __name__ == "__main__":
    unittest.main()
