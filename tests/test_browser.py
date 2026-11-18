from __future__ import annotations

import asyncio
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jobhunt import browser as browser_module


class FormHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/thanks"):
            html = b"<!doctype html><title>Confirmation</title><body>Thank you for applying</body>"
        else:
            html = b"""<!doctype html><title>Test application</title><body>
              <h1>Example job</h1><form action='/thanks' method='get'>
              <label>Name <input id='name' name='name' required></label>
              <button type='submit'>Submit application</button></form></body>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, *_args):
        pass


@pytest.mark.asyncio
async def test_browser_inspection_and_submission_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBHUNT_HEADLESS", "1")
    monkeypatch.setattr(browser_module, "BROWSER_PROFILE_DIR", tmp_path / "chrome")
    monkeypatch.setattr(browser_module, "SCREENSHOT_DIR", tmp_path / "screenshots")
    server = ThreadingHTTPServer(("127.0.0.1", 0), FormHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = browser_module.BrowserSession()
    try:
        result = await session.open_url(f"http://127.0.0.1:{server.server_port}/")
        assert result["title"] == "Test application"
        filled = await session.fill("#name", "Example Candidate")
        assert any(f["value"] == "Example Candidate" for f in filled["snapshot"]["fields"])
        with pytest.raises(ValueError, match="Final submission is blocked"):
            await session.click("button[type=submit]")
        saved = []
        cv_path = tmp_path / "cv.pdf"
        cv_path.write_bytes(b"test pdf bytes")
        monkeypatch.setattr(browser_module, "APPLICATION_CV_PATH", cv_path)
        monkeypatch.setattr(browser_module.profile, "submission_issues", lambda: [])
        cv_hash = hashlib.sha256(cv_path.read_bytes()).hexdigest()
        monkeypatch.setattr(browser_module.db, "get_job", lambda job_id: {
            "status": "approved", "draft": {"cv_sha256": cv_hash}})
        monkeypatch.setattr(browser_module.db, "record_submission",
                            lambda job_id, confirmed, url, evidence: saved.append((job_id, confirmed, url)))
        session._job_id = 123
        result = await session.submit(123, "button[type=submit]")
        assert result["status"] == "submitted"
        assert saved[0][0:2] == (123, True)
        assert (tmp_path / "screenshots").exists()
    finally:
        await session.close()
        server.shutdown()
        thread.join(timeout=2)
