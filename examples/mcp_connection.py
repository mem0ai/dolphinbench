"""Connect a harness to runner-supplied apps, or check local MCP discovery."""

import argparse
import asyncio
import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@asynccontextmanager
async def connect_apps(apps):
    parameters = StdioServerParameters(
        command=apps["command"], args=apps["args"], env=apps["env"],
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def inspect_apps(persona):
    from examples.offline_adapter import OfflineAdapter
    from harness.runner import ROOT, Runner

    with tempfile.TemporaryDirectory(prefix="dolphinbench-app-check-") as temporary:
        directory = Path(temporary)
        runner = Runner({}, directory, OfflineAdapter({}, directory),
                        identity={"local_app_check": True}, release_root=ROOT)
        apps = runner._apps("tests", persona, "connection-check", {"mock_state": {}})
        async with connect_apps(apps) as session:
            response = await session.list_tools()
            names = [tool.name for tool in response.tools]
            if not names:
                raise ValueError("The app server exposed no tools")
    return {"persona": persona, "tools": names, "model_calls": 0, "tool_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--persona", choices=("morgan", "alex", "riley"), default="morgan")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(inspect_apps(args.persona)), indent=2))


if __name__ == "__main__":
    main()
