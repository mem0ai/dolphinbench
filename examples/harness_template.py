"""Connect your existing agent by implementing the five TODO methods below."""

import asyncio

from examples.mcp_connection import connect_apps
from harness.adapter import InteractionRecord


class BenchmarkHarness:
    def __init__(self, options, work_dir):
        self.options = options
        self.work_dir = work_dir
        # Construction must not call models or memory services.

    def identity(self):
        """TODO: Return code/dependency versions and model/memory settings as JSON, without credentials."""
        raise NotImplementedError("Implement identity() in my_harness.py")

    def run_interaction(self, request):
        return asyncio.run(self._run(request))

    async def _run(self, request):
        async with connect_apps(request.apps) as apps:
            tools = (await apps.list_tools()).tools
            return await self.run_agent(request, tools, apps.call_tool)

    async def run_agent(self, request, tools, call_app) -> InteractionRecord:
        """TODO: Run your agent and return InteractionRecord(settings, messages).

        Send request.dated_message in a fresh conversation. Use request.persona's
        memory: writable for phase "ingestion", read-only for phase "tests".
        Route app tools through await call_app(name, arguments).
        Record the full conversation and API-reported usage for each response.
        """
        raise NotImplementedError("Implement run_agent() in my_harness.py")

    def freeze(self, persona):
        """TODO: Wait for memory writes, preserve completed memory, and return its JSON identity."""
        raise NotImplementedError("Implement freeze() in my_harness.py")

    def verify_checkpoint(self, persona, checkpoint):
        """TODO: Raise if the stored memory is missing or no longer matches checkpoint."""
        raise NotImplementedError("Implement verify_checkpoint() in my_harness.py")

    def total_cost_usd(self, phase):
        """TODO: Return this phase's agent and memory-processing cost for all personas.

        phase is "ingestion" or "tests". Include retries and completed background
        work; exclude grading and infrastructure. Read durable usage records so
        this method works after a restart. Raise if the total cannot be reported;
        do not use zero for missing costs or run the agent again to collect costs.
        """
        raise NotImplementedError("Implement total_cost_usd(phase) in my_harness.py")
