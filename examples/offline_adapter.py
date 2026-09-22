"""Inert contract fixture, not an agent or a benchmark result."""

from harness.adapter import InteractionRecord


class OfflineAdapter:
    def total_cost_usd(self, phase):
        # This fixture makes no model or memory-service calls.
        return 0.0

    def __init__(self, options, work_dir):
        if options:
            raise ValueError("The offline fixture takes no options")

    def identity(self):
        return {"fixture": True, "version": 1}

    def run_interaction(self, request):
        return InteractionRecord({"model": "offline-contract-fixture"}, [
            {"role": "user", "content": request.dated_message},
            {"role": "assistant", "content": "Offline fixture; no action performed.",
             "usage": {"input_tokens": 0, "output_tokens": 0}},
        ])

    def freeze(self, persona):
        return {"fixture": True, "persona": persona}

    def verify_checkpoint(self, persona, checkpoint):
        if checkpoint != self.freeze(persona):
            raise ValueError("Offline checkpoint changed")
