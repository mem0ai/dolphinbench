from __future__ import annotations

import unittest

from harness.costing import compute_call_cost


class CostingTests(unittest.TestCase):
    def test_selects_generic_long_context_rates_for_all_token_types(self) -> None:
        pricing = {
            "models": {
                "example": {
                    "input_cost_per_token": 1.0,
                    "output_cost_per_token": 2.0,
                    "cache_read_input_token_cost": 0.5,
                    "cache_creation_input_token_cost": 1.25,
                    "input_cost_per_token_above_272k_tokens": 2.0,
                    "output_cost_per_token_above_272k_tokens": 3.0,
                    "cache_read_input_token_cost_above_272k_tokens": 1.0,
                    "cache_creation_input_token_cost_above_272k_tokens": 2.5,
                }
            }
        }
        usage = {
            "input_tokens": 272001,
            "output_tokens": 2,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 4,
        }
        self.assertEqual(
            compute_call_cost(usage, "example", pricing),
            272001 * 2.0 + 2 * 3.0 + 3 * 1.0 + 4 * 2.5,
        )

    def test_uses_base_rates_at_or_below_threshold(self) -> None:
        pricing = {"models": {"example": {
            "input_cost_per_token": 1.0,
            "output_cost_per_token": 2.0,
            "input_cost_per_token_above_272k_tokens": 2.0,
            "output_cost_per_token_above_272k_tokens": 3.0,
        }}}
        self.assertEqual(
            compute_call_cost({"input_tokens": 272000, "output_tokens": 2}, "example", pricing),
            272004.0,
        )

    def test_separate_small_requests_do_not_trigger_long_context_pricing(self) -> None:
        pricing = {"models": {"example": {
            "input_cost_per_token": 1.0,
            "output_cost_per_token": 2.0,
            "input_cost_per_token_above_272k_tokens": 2.0,
            "output_cost_per_token_above_272k_tokens": 3.0,
        }}}
        calls = [
            {"input_tokens": 100_000, "output_tokens": 1},
            {"input_tokens": 100_000, "output_tokens": 1},
            {"input_tokens": 100_000, "output_tokens": 1},
        ]

        self.assertEqual(
            sum(compute_call_cost(call, "example", pricing) for call in calls),
            300_006.0,
        )


if __name__ == "__main__":
    unittest.main()
