import http.client
import threading
from http.server import ThreadingHTTPServer

from jobhunt import dashboard, db


def test_dashboard_reads_live_status_and_escapes_untrusted_job_text(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    job_id = db.upsert_job({
        "company": "<script>alert(1)</script>",
        "title": "Backend <img src=x onerror=alert(1)>",
        "source": "web",
        "url": "https://example.org/jobs/1",
        "location": "Berlin",
        "description": "<svg onload=alert(1)>",
        "match_score": 90,
    }, db_path)
    db.set_status(job_id, "needs_manual", {"note": "CAPTCHA <blocked>"}, db_path)

    listing = dashboard.render_index(db_path=db_path).decode()
    assert "Needs your action" in listing
    assert "<strong>1</strong>" in listing
    assert "<script>" not in listing
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in listing
    assert "<img src=x" not in listing

    detail = dashboard.render_job(job_id, db_path).decode()
    assert "&lt;svg onload=alert(1)&gt;" in detail
    assert "CAPTCHA &lt;blocked&gt;" in detail
    assert "<svg onload" not in detail


def test_confirmed_submission_remains_counted_during_interviews(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    job_id = db.upsert_job({
        "company": "Example", "title": "AI Engineer", "source": "web",
        "url": "https://example.org/jobs/2", "match_score": 80,
    }, db_path)
    db.save_review(job_id, {"url": "https://example.org/apply"}, "/tmp/cv.pdf", db_path)
    db.approve(job_id, db_path)
    db.record_submission(job_id, True, "https://example.org/thanks", "confirmation", db_path)
    db.set_status(job_id, "interviewing", db_path=db_path)

    html = dashboard.render_index(db_path=db_path).decode()
    assert "<strong>1</strong><span>Confirmed applications</span>" in html
    assert "<strong>1</strong><span>Interviews</span>" in html


def test_live_stream_notifies_when_sqlite_changes(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    db.init_db(db_path)

    class TestHandler(dashboard.DashboardHandler):
        database_path = db_path

    server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        client.request("GET", "/events")
        response = client.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
        assert response.readline() == b": connected\n"
        assert response.readline() == b"\n"
        db.upsert_job({
            "company": "Example", "title": "Backend Engineer", "source": "web",
            "url": "https://example.org/jobs/live",
        }, db_path)
        assert response.readline() == b"event: changed\n"
        assert response.readline() == b"data: {}\n"
    finally:
        client.close()
        server.shutdown()
        server.server_close()
