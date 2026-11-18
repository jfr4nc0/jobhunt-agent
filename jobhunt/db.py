"""SQLite persistence for companies, vacancies, applications and their history."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .paths import DB_PATH

STATUSES = (
    "discovered",
    "shortlisted",
    "drafting",
    "ready_for_review",
    "approved",
    "submitted",
    "awaiting_verification",
    "verification_pending",
    "needs_manual",
    "interviewing",
    "offer",
    "rejected",
    "withdrawn",
    "skipped",
)
_initialized_paths: set[str] = set()
_init_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalized_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text.strip().lower())


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in {"https", "http"} or not parts.netloc:
        raise ValueError(f"Invalid HTTP URL: {url!r}")
    host = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    if host in {"linkedin.com", "www.linkedin.com"} and re.fullmatch(r"/jobs/view/\d+", path):
        return f"https://www.linkedin.com{path}"
    if host in {"jobs.lever.co", "jobs.eu.lever.co", "jobs.ashbyhq.com",
                "boards.greenhouse.io", "job-boards.greenhouse.io"}:
        return urlunsplit(("https", host, path, "", ""))
    keep = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in {
        "ref", "source", "trk", "trackingid", "originalsubdomain",
    } and not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(keep), ""))


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    key = str(db_path.resolve())
    if key in _initialized_paths and db_path.is_file():
        return
    with _init_lock:
        if key in _initialized_paths and db_path.is_file():
            return
        with connect(db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL UNIQUE,
                website TEXT,
                linkedin_url TEXT
            );
            CREATE TABLE IF NOT EXISTS boards (
                id INTEGER PRIMARY KEY,
                provider TEXT NOT NULL CHECK(provider IN ('greenhouse','lever','ashby')),
                slug TEXT NOT NULL,
                company_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                UNIQUE(provider, slug)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id),
                source TEXT NOT NULL,
                source_id TEXT,
                canonical_url TEXT NOT NULL UNIQUE,
                apply_url TEXT,
                title TEXT NOT NULL,
                location TEXT,
                remote_scope TEXT NOT NULL DEFAULT 'unknown',
                description TEXT,
                salary TEXT,
                posted_at TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                match_score INTEGER NOT NULL DEFAULT 0,
                match_reason TEXT,
                UNIQUE(source, source_id)
            );
            CREATE INDEX IF NOT EXISTS jobs_company_idx ON jobs(company_id);
            CREATE INDEX IF NOT EXISTS jobs_match_idx ON jobs(match_score DESC);
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY,
                job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
                status TEXT NOT NULL DEFAULT 'discovered',
                cv_path TEXT,
                draft_json TEXT,
                submitted_at TEXT,
                confirmation_url TEXT,
                follow_up_at TEXT,
                notes TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS applications_status_idx ON applications(status);
            CREATE TABLE IF NOT EXISTS application_events (
                id INTEGER PRIMARY KEY,
                job_id INTEGER NOT NULL REFERENCES jobs(id),
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_job_idx ON application_events(job_id, created_at);
            CREATE TABLE IF NOT EXISTS mail_messages (
                id INTEGER PRIMARY KEY,
                account TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'gmail',
                message_id TEXT NOT NULL,
                thread_id TEXT,
                job_id INTEGER REFERENCES jobs(id),
                direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                snippet TEXT NOT NULL DEFAULT '',
                message_at TEXT,
                classification TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(account, message_id)
            );
            CREATE INDEX IF NOT EXISTS mail_messages_job_idx ON mail_messages(job_id, message_at DESC);
            CREATE INDEX IF NOT EXISTS mail_messages_thread_idx ON mail_messages(account, thread_id);
            CREATE TABLE IF NOT EXISTS salary_benchmarks (
                id INTEGER PRIMARY KEY,
                country_code TEXT NOT NULL,
                role_key TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT 'national',
                currency TEXT NOT NULL,
                median_annual_gross REAL NOT NULL CHECK(median_annual_gross > 0),
                source_url TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(country_code, role_key, location, source_url, observed_at)
            );
            CREATE INDEX IF NOT EXISTS salary_benchmarks_lookup_idx
                ON salary_benchmarks(country_code, role_key, location, observed_at DESC);
            PRAGMA user_version=3;
            """)
        _initialized_paths.add(key)


def _event(conn: sqlite3.Connection, job_id: int, event_type: str, details: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO application_events(job_id,event_type,details_json,created_at) VALUES (?,?,?,?)",
        (job_id, event_type, json.dumps(details or {}, ensure_ascii=False), now()),
    )


def add_board(provider: str, slug: str, company_name: str, db_path: Path = DB_PATH) -> int:
    provider, slug = provider.strip().lower(), slug.strip()
    slug_pattern = (r"[A-Za-z0-9_-]+(?:[ .][A-Za-z0-9_-]+)*" if provider == "ashby"
                    else r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
    if provider not in {"greenhouse", "lever", "ashby"} or not re.fullmatch(slug_pattern, slug):
        raise ValueError("Provider must be greenhouse, lever or ashby and slug must be a safe board name")
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute("INSERT INTO boards(provider,slug,company_name) VALUES (?,?,?) "
                     "ON CONFLICT(provider,slug) DO UPDATE SET company_name=excluded.company_name,enabled=1",
                     (provider, slug, company_name.strip()))
        return conn.execute("SELECT id FROM boards WHERE provider=? AND slug=?", (provider, slug)).fetchone()[0]


def list_boards(db_path: Path = DB_PATH) -> list[dict]:
    init_db(db_path)
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM boards WHERE enabled=1 ORDER BY company_name")]


def add_salary_benchmark(country_code: str, role_key: str, currency: str,
                         median_annual_gross: float, source_url: str, observed_at: str,
                         location: str = "national", db_path: Path = DB_PATH) -> int:
    country_code, currency = country_code.strip().upper(), currency.strip().upper()
    role_key, location = role_key.strip().lower(), location.strip().lower()
    if not re.fullmatch(r"[A-Z]{2}", country_code) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Use ISO country and currency codes")
    if not re.fullmatch(r"[a-z0-9_-]+", role_key) or not location:
        raise ValueError("Role key and location are required")
    if median_annual_gross <= 0 or not source_url.startswith("https://"):
        raise ValueError("Positive median and HTTPS source URL are required")
    try:
        datetime.fromisoformat(observed_at).date()
    except ValueError as exc:
        raise ValueError("observed_at must be an ISO date") from exc
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute("""INSERT INTO salary_benchmarks
            (country_code,role_key,location,currency,median_annual_gross,source_url,observed_at,created_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(country_code,role_key,location,source_url,observed_at)
            DO UPDATE SET currency=excluded.currency,median_annual_gross=excluded.median_annual_gross""",
            (country_code, role_key, location, currency, median_annual_gross,
             source_url, observed_at, now()))
        return conn.execute("""SELECT id FROM salary_benchmarks WHERE country_code=? AND role_key=?
            AND location=? AND source_url=? AND observed_at=?""",
            (country_code, role_key, location, source_url, observed_at)).fetchone()[0]


def get_salary_benchmark(country_code: str, role_key: str, location: str = "national",
                         db_path: Path = DB_PATH) -> dict | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute("""SELECT * FROM salary_benchmarks WHERE country_code=? AND role_key=?
            AND location IN (?, 'national') ORDER BY (location=?) DESC, observed_at DESC, id DESC LIMIT 1""",
            (country_code.strip().upper(), role_key.strip().lower(), location.strip().lower(),
             location.strip().lower())).fetchone()
        return dict(row) if row else None


def upsert_job(job: dict, db_path: Path = DB_PATH) -> int:
    init_db(db_path)
    with connect(db_path) as conn:
        return _upsert_job_in_conn(conn, job)


def upsert_jobs(jobs: list[dict], db_path: Path = DB_PATH) -> list[int]:
    """Import a board's vacancies in one SQLite transaction."""
    init_db(db_path)
    with connect(db_path) as conn:
        return [_upsert_job_in_conn(conn, job) for job in jobs]


def _upsert_job_in_conn(conn: sqlite3.Connection, job: dict) -> int:
    company = job["company"].strip()
    title = job["title"].strip()
    if not company or not title:
        raise ValueError("Company and title are required")
    url = canonical_url(job["url"])
    apply_url = canonical_url(job["apply_url"]) if job.get("apply_url") else None
    source, source_id = job["source"].strip().lower(), str(job.get("source_id") or "") or None
    stamp = now()
    key = normalized_name(company)
    conn.execute("INSERT INTO companies(name,name_key) VALUES (?,?) ON CONFLICT(name_key) DO NOTHING", (company, key))
    company_id = conn.execute("SELECT id FROM companies WHERE name_key=?", (key,)).fetchone()[0]
    existing = conn.execute("SELECT id FROM jobs WHERE canonical_url=?", (url,)).fetchone()
    if not existing and apply_url:
        existing = conn.execute("SELECT id FROM jobs WHERE apply_url=?", (apply_url,)).fetchone()
    if not existing and source_id:
        existing = conn.execute("SELECT id FROM jobs WHERE source=? AND source_id=?", (source, source_id)).fetchone()
    if existing:
        job_id = existing[0]
        conn.execute("""UPDATE jobs SET last_seen_at=?, location=COALESCE(?,location),
            description=COALESCE(?,description),
            salary=COALESCE(?,salary), posted_at=COALESCE(?,posted_at),
            apply_url=COALESCE(?,apply_url), match_score=?, match_reason=?, remote_scope=?
            WHERE id=?""", (stamp, job.get("location"), job.get("description"), job.get("salary"), job.get("posted_at"),
                          apply_url, job.get("match_score", 0), job.get("match_reason"),
                          job.get("remote_scope", "unknown"), job_id))
        return job_id
    cursor = conn.execute("""INSERT INTO jobs(company_id,source,source_id,canonical_url,apply_url,title,
        location,remote_scope,description,salary,posted_at,first_seen_at,last_seen_at,match_score,match_reason)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (company_id, source, source_id, url, apply_url, title, job.get("location"),
         job.get("remote_scope", "unknown"), job.get("description"), job.get("salary"),
         job.get("posted_at"), stamp, stamp, job.get("match_score", 0), job.get("match_reason")))
    job_id = cursor.lastrowid
    conn.execute("INSERT INTO applications(job_id,status,updated_at) VALUES (?,?,?)",
                 (job_id, "discovered", stamp))
    _event(conn, job_id, "discovered", {"source": source, "url": url})
    return job_id


def get_job(job_id: int, db_path: Path = DB_PATH) -> dict:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute("""SELECT j.*, c.name AS company, a.status, a.cv_path, a.draft_json,
            a.submitted_at, a.confirmation_url, a.follow_up_at, a.notes, a.updated_at
            FROM jobs j JOIN companies c ON c.id=j.company_id
            JOIN applications a ON a.job_id=j.id WHERE j.id=?""", (job_id,)).fetchone()
        if not row:
            raise KeyError(f"Job {job_id} not found")
        result = dict(row)
        result["draft"] = json.loads(result.pop("draft_json") or "null")
        result["events"] = [dict(e) for e in conn.execute(
            "SELECT event_type,details_json,created_at FROM application_events WHERE job_id=? ORDER BY id", (job_id,))]
        for e in result["events"]:
            e["details"] = json.loads(e.pop("details_json"))
        return result


def list_jobs(status: str | None = None, limit: int = 50, db_path: Path = DB_PATH) -> list[dict]:
    init_db(db_path)
    if status and status not in STATUSES:
        raise ValueError(f"Unknown status: {status}")
    with connect(db_path) as conn:
        sql = """SELECT j.id,c.name AS company,j.title,j.location,j.remote_scope,j.source,
                 j.canonical_url,j.match_score,a.status,a.updated_at FROM jobs j
                 JOIN companies c ON c.id=j.company_id JOIN applications a ON a.job_id=j.id"""
        params: list = []
        if status:
            sql += " WHERE a.status=?"
            params.append(status)
        sql += " ORDER BY j.match_score DESC, j.first_seen_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        return [dict(r) for r in conn.execute(sql, params)]


def set_status(job_id: int, status: str, details: dict | None = None, db_path: Path = DB_PATH) -> None:
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status}")
    init_db(db_path)
    with connect(db_path) as conn:
        current = conn.execute("SELECT status FROM applications WHERE job_id=?", (job_id,)).fetchone()
        if not current:
            raise KeyError(f"Job {job_id} not found")
        conn.execute("UPDATE applications SET status=?,notes=COALESCE(?,notes),updated_at=? WHERE job_id=?",
                     (status, (details or {}).get("note") or None, now(), job_id))
        _event(conn, job_id, status, details)


def set_follow_up(job_id: int, date_value: str, note: str = "", db_path: Path = DB_PATH) -> None:
    try:
        datetime.fromisoformat(date_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Follow-up must be an ISO date or datetime") from exc
    init_db(db_path)
    with connect(db_path) as conn:
        if not conn.execute("SELECT 1 FROM applications WHERE job_id=?", (job_id,)).fetchone():
            raise KeyError(f"Job {job_id} not found")
        conn.execute("UPDATE applications SET follow_up_at=?,notes=COALESCE(?,notes),updated_at=? WHERE job_id=?",
                     (date_value, note or None, now(), job_id))
        _event(conn, job_id, "follow_up_set", {"date": date_value, "note": note})


def add_event(job_id: int, event_type: str, details: dict, db_path: Path = DB_PATH) -> None:
    if not event_type.strip() or not isinstance(details, dict):
        raise ValueError("Event type and JSON object details are required")
    init_db(db_path)
    with connect(db_path) as conn:
        if not conn.execute("SELECT 1 FROM applications WHERE job_id=?", (job_id,)).fetchone():
            raise KeyError(f"Job {job_id} not found")
        _event(conn, job_id, event_type.strip(), details)
        conn.execute("UPDATE applications SET updated_at=? WHERE job_id=?", (now(), job_id))


def record_mail_message(account: str, message_id: str, direction: str, sender: str,
                        recipient: str, subject: str = "", snippet: str = "",
                        thread_id: str = "", message_at: str = "", job_id: int | None = None,
                        classification: str = "", db_path: Path = DB_PATH) -> int:
    """Store Gmail metadata once, without message bodies or credentials."""
    if direction not in {"inbound", "outbound"}:
        raise ValueError("Mail direction must be inbound or outbound")
    if not account.strip() or not message_id.strip() or not sender.strip() or not recipient.strip():
        raise ValueError("Mail account, message ID, sender and recipient are required")
    if message_at:
        try:
            datetime.fromisoformat(message_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Mail timestamp must be ISO date or datetime") from exc
    init_db(db_path)
    with connect(db_path) as conn:
        if job_id is not None and not conn.execute("SELECT 1 FROM applications WHERE job_id=?", (job_id,)).fetchone():
            raise KeyError(f"Job {job_id} not found")
        result = conn.execute("""INSERT INTO mail_messages
            (account,provider,message_id,thread_id,job_id,direction,sender,recipient,
             subject,snippet,message_at,classification,created_at)
            VALUES (?,'gmail',?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account,message_id) DO NOTHING""",
            (account.strip().lower(), message_id.strip(), thread_id or None, job_id,
             direction, sender.strip(), recipient.strip(), subject.strip(), snippet.strip(),
             message_at or None, classification or None, now()))
        row = conn.execute("SELECT id,job_id FROM mail_messages WHERE account=? AND message_id=?",
                           (account.strip().lower(), message_id.strip())).fetchone()
        if not result.rowcount and job_id is not None and row["job_id"] is None:
            conn.execute("UPDATE mail_messages SET job_id=? WHERE id=?", (job_id, row["id"]))
        if result.rowcount and job_id is not None:
            _event(conn, job_id, "mail_" + direction, {"message_id": message_id, "subject": subject,
                                                       "classification": classification})
            conn.execute("UPDATE applications SET updated_at=? WHERE job_id=?", (now(), job_id))
        return int(row["id"])


def list_mail_for_job(job_id: int, db_path: Path = DB_PATH) -> list[dict]:
    init_db(db_path)
    with connect(db_path) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM mail_messages WHERE job_id=? ORDER BY COALESCE(message_at,created_at) DESC, id DESC",
            (job_id,))]


def save_review(job_id: int, draft: dict, cv_path: str, db_path: Path = DB_PATH) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        current = conn.execute("SELECT status FROM applications WHERE job_id=?", (job_id,)).fetchone()
        if not current:
            raise KeyError(f"Job {job_id} not found")
        if current[0] in {"submitted", "interviewing", "offer", "rejected", "withdrawn"}:
            raise ValueError("This application is already submitted or closed")
        conn.execute("UPDATE applications SET status='ready_for_review',draft_json=?,cv_path=?,updated_at=? WHERE job_id=?",
                     (json.dumps(draft, ensure_ascii=False), cv_path, now(), job_id))
        _event(conn, job_id, "ready_for_review", {"url": draft.get("url")})


def save_partial_draft(job_id: int, draft: dict, cv_path: str, reason: str,
                       db_path: Path = DB_PATH) -> None:
    """Persist a form that cannot be submitted until missing facts are supplied."""
    if not reason.strip():
        raise ValueError("A reason is required for a partial draft")
    init_db(db_path)
    with connect(db_path) as conn:
        current = conn.execute("SELECT status FROM applications WHERE job_id=?", (job_id,)).fetchone()
        if not current:
            raise KeyError(f"Job {job_id} not found")
        if current[0] in {"submitted", "interviewing", "offer", "rejected", "withdrawn"}:
            raise ValueError("This application is already submitted or closed")
        conn.execute("""UPDATE applications SET status='needs_manual',draft_json=?,cv_path=?,
            notes=?,updated_at=? WHERE job_id=?""",
            (json.dumps(draft, ensure_ascii=False), cv_path, reason.strip(), now(), job_id))
        _event(conn, job_id, "needs_manual", {"reason": reason.strip(), "url": draft.get("url")})


def approve(job_id: int, db_path: Path = DB_PATH, basis: str = "per_vacancy") -> None:
    if get_job(job_id, db_path)["status"] != "ready_for_review":
        raise ValueError("Application must be ready for review")
    set_status(job_id, "approved", {"basis": basis}, db_path=db_path)


def record_submission(job_id: int, confirmed: bool, url: str, evidence: str,
                      db_path: Path = DB_PATH) -> None:
    if get_job(job_id, db_path)["status"] not in {"approved", "awaiting_verification"}:
        raise ValueError("Application must be approved before submission")
    status = "submitted" if confirmed else "verification_pending"
    with connect(db_path) as conn:
        conn.execute("""UPDATE applications SET status=?,submitted_at=?,confirmation_url=?,updated_at=?
            WHERE job_id=?""", (status, now() if confirmed else None, url if confirmed else None, now(), job_id))
        _event(conn, job_id, status, {"url": url, "evidence": evidence})


def record_failed_attempt(job_id: int, reason: str, evidence: str = "",
                          db_path: Path = DB_PATH) -> None:
    """Correct an attempted submission that the site explicitly rejected."""
    init_db(db_path)
    with connect(db_path) as conn:
        if not conn.execute("SELECT 1 FROM applications WHERE job_id=?", (job_id,)).fetchone():
            raise KeyError(f"Job {job_id} not found")
        conn.execute("""UPDATE applications SET status='needs_manual',submitted_at=NULL,
            confirmation_url=NULL,updated_at=? WHERE job_id=?""", (now(), job_id))
        _event(conn, job_id, "submission_failed", {"reason": reason, "evidence": evidence})


def record_verification_required(job_id: int, kind: str, evidence: str = "",
                                 db_path: Path = DB_PATH) -> None:
    """Track a required human verification before the application is sent."""
    init_db(db_path)
    with connect(db_path) as conn:
        if not conn.execute("SELECT 1 FROM applications WHERE job_id=?", (job_id,)).fetchone():
            raise KeyError(f"Job {job_id} not found")
        conn.execute("""UPDATE applications SET status='awaiting_verification',submitted_at=NULL,
            confirmation_url=NULL,updated_at=? WHERE job_id=?""", (now(), job_id))
        _event(conn, job_id, "verification_required", {"kind": kind, "evidence": evidence})
