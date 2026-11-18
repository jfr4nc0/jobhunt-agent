"""JSON-lines browser bridge for a Codex session that has not reloaded the new MCP."""

from __future__ import annotations

import asyncio
import json
import sys

from .browser import BrowserSession


async def main() -> None:
    browser = BrowserSession()
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            try:
                request = json.loads(line)
                action = request["action"]
                if action == "open_job":
                    result = await browser.open_job(int(request["job_id"]))
                elif action == "open_url":
                    result = await browser.open_url(request["url"])
                elif action == "snapshot":
                    result = await browser.snapshot()
                elif action == "diagnostics":
                    result = browser.diagnostics()
                elif action == "fill":
                    result = await browser.fill(request["selector"], request["value"], request.get("frame_selector", ""))
                elif action == "select":
                    result = await browser.select(request["selector"], request["value"], request.get("frame_selector", ""))
                elif action == "check":
                    result = await browser.check(request["selector"], request.get("checked", True), request.get("frame_selector", ""))
                elif action == "upload_cv":
                    result = await browser.upload_cv(request["selector"], request.get("frame_selector", ""), request.get("variant", "default"))
                elif action == "click":
                    result = await browser.click(request["selector"], request.get("frame_selector", ""))
                elif action == "prepare_review":
                    result = await browser.prepare_review(int(request["job_id"]), request.get("agent_notes", ""))
                elif action == "resume_review":
                    result = await browser.resume_review(int(request["job_id"]))
                elif action == "submit":
                    result = await browser.submit(int(request["job_id"]), request["selector"], request.get("frame_selector", ""))
                elif action == "close":
                    result = {"closed": True}
                    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False), flush=True)
                    break
                else:
                    raise ValueError(f"Unknown action: {action}")
                print(json.dumps({"ok": True, "result": result}, ensure_ascii=False), flush=True)
            except Exception as exc:
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
