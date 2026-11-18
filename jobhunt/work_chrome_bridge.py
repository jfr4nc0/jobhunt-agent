"""JSON-lines bridge to the existing Chrome profile via Chrome DevTools MCP.

This keeps the MCP connection alive while Codex uses background tabs. Chrome must
have remote debugging enabled through chrome://inspect/#remote-debugging.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .paths import ROOT

ALLOWED = {
    "list_pages", "new_page", "select_page", "navigate_page", "take_snapshot",
    "take_screenshot", "fill", "click", "upload_file", "wait_for", "evaluate_script",
}


async def main() -> None:
    script = os.environ.get("JOBHUNT_CHROME_MCP_SCRIPT", "")
    if script:
        command = shutil.which("node") or "node"
        args = [script]
    else:
        command = shutil.which("npx") or "npx"
        args = ["-y", "chrome-devtools-mcp@1.13.0"]
    params = StdioServerParameters(
        command=command,
        args=[*args, "--autoConnect", "--no-usage-statistics", "--no-performance-crux",
              f"--filesystemRoot={ROOT}"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            while line := await asyncio.to_thread(sys.stdin.readline):
                try:
                    request = json.loads(line)
                    if request.get("tool") == "close":
                        print(json.dumps({"ok": True, "closed": True}), flush=True)
                        break
                    name = request["tool"]
                    if name not in ALLOWED:
                        raise ValueError(f"Unsupported Chrome tool: {name}")
                    result = await session.call_tool(name, request.get("args", {}))
                    output = [item.text if item.type == "text" else {"type": item.type}
                              for item in result.content]
                    print(json.dumps({"ok": not result.isError, "content": output}, ensure_ascii=False), flush=True)
                except Exception as exc:
                    print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
