from jobhunt import dashboard, db


def test_mail_metadata_links_to_job_once_and_appears_safely_in_dashboard(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    job_id = db.upsert_job({
        "company": "Example", "title": "Backend Engineer", "source": "web",
        "url": "https://example.org/jobs/1",
    }, db_path)
    fields = {
        "account": "alex@example.org",
        "message_id": "gmail-123",
        "thread_id": "thread-1",
        "job_id": job_id,
        "direction": "inbound",
        "sender": "recruiter@example.org",
        "recipient": "alex@example.org",
        "subject": "Interview <script>alert(1)</script>",
        "snippet": "Please choose a time",
        "message_at": "2026-11-18T14:00:00Z",
        "classification": "interview_invitation",
    }
    first = db.record_mail_message(**fields, db_path=db_path)
    second = db.record_mail_message(**fields, db_path=db_path)
    assert first == second
    assert len(db.list_mail_for_job(job_id, db_path)) == 1
    assert [e["event_type"] for e in db.get_job(job_id, db_path)["events"]] == ["discovered", "mail_inbound"]
    detail = dashboard.render_job(job_id, db_path).decode()
    assert "Interview &lt;script&gt;alert(1)&lt;/script&gt;" in detail
    assert "<script>alert(1)</script>" not in detail
