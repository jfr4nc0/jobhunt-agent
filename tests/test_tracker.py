import json
from pathlib import Path

import pytest

from jobhunt import db, sources


def job(url="https://jobs.lever.co/acme/abc?utm_source=linkedin", source="lever"):
    return {
        "company": "Acme", "title": "AI Software Engineer", "source": source,
        "source_id": "abc" if source == "lever" else None,
        "url": url, "apply_url": "https://jobs.lever.co/acme/abc/apply",
        "location": "Remote LATAM", "description": "Python, LangGraph and Kubernetes",
        "remote_scope": "home_ok", "match_score": 85,
    }


def test_job_dedup_and_application_history(tmp_path: Path):
    db_path = tmp_path / "applications.sqlite3"
    first = db.upsert_job(job(), db_path)
    second = db.upsert_job(job("https://jobs.lever.co/acme/abc?utm_campaign=another"), db_path)
    third = db.upsert_job(job("https://linkedin.com/jobs/view/999", "linkedin"), db_path)
    assert first == second == third
    assert len(db.list_jobs(db_path=db_path)) == 1
    assert db.get_job(first, db_path)["status"] == "discovered"
    db.save_review(first, {"url": "https://jobs.lever.co/acme/abc/apply", "fields": []},
                   "/tmp/current-cv.pdf", db_path)
    with pytest.raises(ValueError):
        db.record_submission(first, True, "https://example.com/thanks", "test", db_path)
    db.approve(first, db_path)
    db.record_submission(first, False, "https://example.com/unknown", "no confirmation", db_path)
    saved = db.get_job(first, db_path)
    assert saved["status"] == "verification_pending"
    assert saved["submitted_at"] is None
    assert saved["confirmation_url"] is None
    assert [e["event_type"] for e in saved["events"]] == [
        "discovered", "ready_for_review", "approved", "verification_pending"]
    db.record_failed_attempt(first, "CV failed to upload", "test screenshot", db_path)
    corrected = db.get_job(first, db_path)
    assert corrected["status"] == "needs_manual"
    assert corrected["submitted_at"] is None
    assert corrected["events"][-1]["event_type"] == "submission_failed"
    db.save_partial_draft(first, {"url": "https://example.com/apply", "missing": ["traffic"]},
                          "/tmp/current-cv.pdf", "Missing traffic figure", db_path)
    partial = db.get_job(first, db_path)
    assert partial["status"] == "needs_manual"
    assert partial["draft"]["missing"] == ["traffic"]
    assert partial["notes"] == "Missing traffic figure"


def test_board_normalization_and_match(monkeypatch, tmp_path):
    candidate = tmp_path / "profile.json"
    candidate.write_text(json.dumps({
        "home_search_aliases": ["Chile", "LATAM", "Latin America", "South America"],
        "target_location_aliases": ["Germany", "Berlin", "Australia", "Sydney", "Netherlands", "Amsterdam", "Singapore"],
    }), encoding="utf-8")
    monkeypatch.setattr(sources, "PROFILE_PATH", candidate)
    monkeypatch.setattr(sources, "fetch_json", lambda url: {
        "jobs": [{"id": 123, "title": "Backend AI Engineer",
                  "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/123",
                  "location": {"name": "Remote - Latin America"},
                  "content": "<p>Build agents with Python and Kubernetes.</p>"}]})
    found = sources.fetch_board("greenhouse", "acme", "Acme")
    assert len(found) == 1
    assert found[0]["remote_scope"] == "home_ok"
    assert found[0]["match_score"] >= 55
    assert "Python" in found[0]["description"]
    assert sources.board_from_url("https://jobs.ashbyhq.com/acme/123") == ("ashby", "acme")
    assert sources.board_from_url("https://jobs.lever.co/acme/abc") == ("lever", "acme")
    assert sources.infer_remote_scope("Remote - US only", "Python") == "excluded"
    assert sources.infer_remote_scope("Australia", "Work with teams in Latin America", True, "Remote") == "excluded"
    assert sources.infer_remote_scope("Brazil; Chile", "Python", True, "Remote") == "home_ok"
    assert sources.infer_remote_scope("Berlin; Germany", "Visa sponsorship and relocation support are available.",
                                      False, "OnSite") == "sponsored_abroad"
    assert sources.infer_remote_scope("Berlin; Germany", "Visa sponsorship is not available.",
                                      False, "OnSite") == "excluded"
    assert sources.infer_remote_scope("Sydney; Australia", "x" * 6000 +
                                      " Visa sponsorship. For engineers relocating to Australia.",
                                      False, "OnSite") == "sponsored_abroad"
    assert sources.infer_remote_scope("Amsterdam office; Remote (EU)",
                                      "Relocation support, visa sponsorship and 30% ruling support",
                                      None, "Hybrid") == "sponsored_abroad"
    assert sources.infer_remote_scope("Singapore; Remote (Asia)",
                                      "Visa Sponsorship: We provide support for relocation and visas for full-time candidates.",
                                      False, "Hybrid") == "sponsored_abroad"
    assert sources.sponsorship_advertised("Visa: Employer sponsors the Dutch highly skilled migrant visa.")
    assert not sources.sponsorship_advertised("Visa: We do not sponsor work visas.")
    assert sources.infer_remote_scope("Sydney; Australia", "We cannot offer visa sponsorship.",
                                      False, "OnSite") == "excluded"
    assert sources.infer_remote_scope("Global - Remote", "Python") == "home_ok"
    assert sources.infer_remote_scope("São Paulo, Brasil", "Remote team with LATAM colleagues") == "excluded"
    monkeypatch.setattr(sources, "fetch_json", lambda url: {"jobs": [{
        "id": "au-1", "title": "AI Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/au-1",
        "location": "Sydney", "address": {"postalAddress": {"addressCountry": "Australia"}},
        "secondaryLocations": [], "isRemote": True, "workplaceType": "Remote",
        "descriptionPlain": "We collaborate globally with Latin America.",
    }]})
    assert sources.fetch_board("ashby", "acme", "Acme")[0]["remote_scope"] == "excluded"
    assert sources.manual_only("Recruiter and AI submissions will be rejected.")


def test_follow_up_is_persistent(tmp_path: Path):
    db_path = tmp_path / "applications.sqlite3"
    job_id = db.upsert_job(job(), db_path)
    db.set_status(job_id, "shortlisted", {"note": "Good fit"}, db_path)
    db.set_follow_up(job_id, "2026-10-15", "Send note", db_path)
    db.add_event(job_id, "recruiter_reply", {"contact": "Example", "note": "Asked for availability"}, db_path)
    saved = db.get_job(job_id, db_path)
    assert saved["notes"] == "Send note"
    assert saved["follow_up_at"] == "2026-10-15"
    assert saved["events"][-2]["event_type"] == "follow_up_set"
    assert saved["events"][-1]["details"]["contact"] == "Example"


def test_linkedin_tracking_links_canonicalize_to_one_job():
    assert db.canonical_url("https://linkedin.com/jobs/view/123/?trackingId=abc") == \
           db.canonical_url("https://www.linkedin.com/jobs/view/123?refId=xyz")


def test_email_verification_precedes_confirmed_submission(tmp_path: Path):
    db_path = tmp_path / "applications.sqlite3"
    job_id = db.upsert_job(job(), db_path)
    db.save_review(job_id, {"url": "https://jobs.lever.co/acme/abc/apply"}, "/tmp/cv.pdf", db_path)
    db.approve(job_id, db_path, basis="autonomous_authorization")
    db.record_verification_required(job_id, "email_code", db_path=db_path)
    assert db.get_job(job_id, db_path)["submitted_at"] is None
    db.record_submission(job_id, True, "https://example.com/confirmation", "thank you", db_path)
    assert db.get_job(job_id, db_path)["status"] == "submitted"
