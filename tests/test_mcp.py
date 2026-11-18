import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.asyncio
async def test_mcp_starts_and_exposes_review_barrier():
    params = StdioServerParameters(command=sys.executable, args=["-m", "jobhunt.mcp_server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
    assert {"discover_board_jobs", "browser_inspect", "prepare_application_review",
            "approve_application", "submit_reviewed_application"} <= names
