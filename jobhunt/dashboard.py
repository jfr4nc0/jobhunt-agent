"""Read-only local dashboard backed directly by the jobhunt SQLite database."""

from __future__ import annotations

import json
import os
import time
from contextlib import closing
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from . import db
from .paths import DB_PATH

STATUS_LABELS = {
    "discovered": "Discovered",
    "shortlisted": "Shortlisted",
    "drafting": "Drafting",
    "ready_for_review": "Ready for review",
    "approved": "Approved",
    "submitted": "Submitted and confirmed",
    "awaiting_verification": "Awaiting verification",
    "verification_pending": "Submission unverified",
    "needs_manual": "Needs your action",
    "interviewing": "Interviewing",
    "offer": "Offer",
    "rejected": "Rejected",
    "withdrawn": "Withdrawn",
    "skipped": "Skipped",
}
DASHBOARD_TITLE = os.environ.get("JOBHUNT_DASHBOARD_TITLE", "Job Hunt Tracker")

STYLE = """
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e8edf5;background:#0d1420}
*{box-sizing:border-box}body{margin:0}main{max-width:1300px;margin:auto;padding:32px 22px 70px}
a{color:#a2d4ff;text-decoration:none}a:hover{text-decoration:underline}
header{display:flex;justify-content:space-between;gap:20px;align-items:baseline;flex-wrap:wrap;margin-bottom:24px}
h1{font-size:29px;letter-spacing:-.03em;margin:0}h2{font-size:18px;margin:25px 0 12px}
.sub,.muted{color:#aeb9c9}.sub{margin:6px 0 0}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:20px 0 27px}
.card,.panel{background:#172337;border:1px solid #2b3b54;border-radius:13px}.card{padding:15px}.card strong{display:block;font-size:27px}.card span{font-size:13px;color:#b7c4d6}
form{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}input,select,button{background:#172337;color:#edf4ff;border:1px solid #41546f;border-radius:9px;padding:10px 12px;font:inherit}
input{min-width:240px;flex:1}button{background:#246fac;border-color:#246fac;cursor:pointer}
.panel{overflow:hidden}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:11px 13px;border-bottom:1px solid #2b3b54;vertical-align:top;font-size:14px}th{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:#aeb9c9}tr:last-child td{border:0}
.badge{display:inline-block;padding:4px 8px;border-radius:30px;background:#30405a;font-size:12px;white-space:nowrap}
.badge.submitted,.badge.offer,.badge.interviewing{background:#174b3a;color:#b3f3cc}.badge.needs_manual,.badge.verification_pending,.badge.awaiting_verification{background:#5b3d22;color:#ffddad}
.live-status{position:fixed;right:16px;bottom:14px;background:#172337;border:1px solid #41546f;border-radius:30px;padding:7px 12px;color:#b7c4d6;font-size:12px;box-shadow:0 4px 18px #0006}.live-status.connected{border-color:#2d9168;color:#b3f3cc}
.pager{display:flex;gap:14px;margin:17px 0}.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:16px 0}.meta div{padding:12px;background:#172337;border:1px solid #2b3b54;border-radius:9px}
.meta small{display:block;color:#aeb9c9;margin-bottom:4px}.description,.event{white-space:pre-wrap;overflow-wrap:anywhere;padding:16px}.event{border-bottom:1px solid #2b3b54}.event:last-child{border:0}.event p{margin:7px 0 0}.empty{padding:22px;color:#b7c4d6}
@media(max-width:760px){main{padding:20px 12px}table{display:block;overflow-x:auto}th,td{min-width:110px}.cards{grid-template-columns:repeat(2,1fr)}}
"""

SCRIPT = """(() => {
  const indicator = document.getElementById('live-status');
  const source = new EventSource('/events');
  let updating = false;
  let queued = false;
  source.onopen = () => {
    indicator.textContent = '● Live';
    indicator.classList.add('connected');
  };
  source.onerror = () => {
    indicator.textContent = '○ Reconnecting…';
    indicator.classList.remove('connected');
  };
  async function update() {
    if (updating) { queued = true; return; }
    updating = true;
    try {
      const response = await fetch(location.pathname + location.search, {cache: 'no-store'});
      if (!response.ok) throw new Error('Could not refresh the dashboard');
      const next = new DOMParser().parseFromString(await response.text(), 'text/html');
      const replacement = next.getElementById('live-root');
      const current = document.getElementById('live-root');
      if (!replacement || !current) return;
      const focused = document.activeElement;
      const fieldName = focused && current.contains(focused) ? focused.name : null;
      const formValues = Object.fromEntries(new FormData(current.querySelector('form') || document.createElement('form')));
      const scroll = window.scrollY;
      current.replaceWith(replacement);
      const form = replacement.querySelector('form');
      if (form) {
        for (const [name, value] of Object.entries(formValues)) {
          const field = form.elements.namedItem(name);
          if (field) field.value = value;
        }
        if (fieldName && form.elements.namedItem(fieldName)) form.elements.namedItem(fieldName).focus();
      }
      window.scrollTo(0, scroll);
    } catch (_) {
      indicator.textContent = '○ Reconnecting…';
      indicator.classList.remove('connected');
    } finally {
      updating = false;
      if (queued) { queued = false; update(); }
    }
  }
  source.addEventListener('changed', update);
})();
"""


def page(title: str, body: str) -> bytes:
    html = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{escape(title)} · {escape(DASHBOARD_TITLE)}</title><style>{STYLE}</style></head>"
            f"<body><main><div id='live-root'>{body}</div></main>"
            "<div id='live-status' class='live-status' aria-live='polite'>○ Connecting…</div>"
            "<script src='/dashboard.js' defer></script></body></html>")
    return html.encode("utf-8")


def counts(db_path: Path = DB_PATH) -> dict[str, int]:
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        return {row["status"]: row["n"] for row in conn.execute(
            "SELECT status,COUNT(*) AS n FROM applications GROUP BY status")}


def _badge(status: str) -> str:
    label = STATUS_LABELS.get(status, status)
    return f'<span class="badge {escape(status)}">{escape(label)}</span>'


def render_index(status: str = "", query: str = "", page_no: int = 1,
                 db_path: Path = DB_PATH) -> bytes:
    status = status if status in db.STATUSES else ""
    query = query.strip()[:100]
    page_no = max(1, min(page_no, 10000))
    totals = counts(db_path)
    filters = []
    params: list[object] = []
    if status:
        filters.append("a.status=?")
        params.append(status)
    if query:
        filters.append("(c.name LIKE ? OR j.title LIKE ? OR j.location LIKE ?)")
        params.extend([f"%{query}%"] * 3)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    with db.connect(db_path) as conn:
        confirmed = conn.execute(
            "SELECT COUNT(*) FROM applications WHERE submitted_at IS NOT NULL").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM jobs j JOIN companies c ON c.id=j.company_id "
                             "JOIN applications a ON a.job_id=j.id" + where, params).fetchone()[0]
        rows = conn.execute("""SELECT j.id,c.name AS company,j.title,j.location,j.remote_scope,
            j.canonical_url,j.match_score,a.status,a.updated_at FROM jobs j
            JOIN companies c ON c.id=j.company_id JOIN applications a ON a.job_id=j.id"""
            + where + " ORDER BY CASE WHEN a.status='submitted' THEN 0 WHEN a.status IN "
            "('awaiting_verification','verification_pending','needs_manual') THEN 1 "
            "WHEN a.status='shortlisted' THEN 2 ELSE 3 END, a.updated_at DESC, j.match_score DESC LIMIT 50 OFFSET ?",
            [*params, (page_no - 1) * 50]).fetchall()
    selected = sum(totals.values())
    card_data = [
        (confirmed, "Confirmed applications"),
        (totals.get("interviewing", 0), "Interviews"),
        (totals.get("awaiting_verification", 0) + totals.get("verification_pending", 0), "Awaiting verification"),
        (totals.get("needs_manual", 0), "Needs your action"),
        (totals.get("shortlisted", 0), "Shortlisted"),
        (selected, "Tracked jobs"),
    ]
    cards = "".join(f'<div class="card"><strong>{n}</strong><span>{escape(label)}</span></div>'
                    for n, label in card_data)
    options = '<option value="">All statuses</option>' + "".join(
        f'<option value="{escape(s)}"{" selected" if s == status else ""}>{escape(STATUS_LABELS[s])}</option>'
        for s in db.STATUSES)
    table_rows = "".join(
        "<tr>" +
        f'<td><a href="/job/{row["id"]}">#{row["id"]} {escape(row["company"])}</a></td>' +
        f'<td>{escape(row["title"])}</td>' +
        f'<td>{escape(row["location"] or "—")}</td>' +
        f'<td>{_badge(row["status"])}</td>' +
        f'<td>{row["match_score"]}</td>' +
        f'<td>{escape((row["updated_at"] or "")[:16].replace("T", " "))}</td>' +
        "</tr>" for row in rows)
    def page_link(number: int) -> str:
        return "/?" + urlencode({"status": status, "q": query, "page": number})
    pager = '<nav class="pager">'
    if page_no > 1:
        pager += f'<a href="{escape(page_link(page_no - 1), quote=True)}">← Previous</a>'
    pager += f'<span class="muted">Page {page_no} · {total} results</span>'
    if page_no * 50 < total:
        pager += f'<a href="{escape(page_link(page_no + 1), quote=True)}">Next →</a>'
    pager += "</nav>"
    body = (f"<header><div><h1>{escape(DASHBOARD_TITLE)}</h1>"
            "<p class='sub'>SQLite source of truth · Unverified attempts are not counted as submitted.</p></div>"
            "<span class='muted'>Local view · updates automatically</span></header>"
            f'<div class="cards">{cards}</div>'
            f'<form method="get" action="/"><input name="q" value="{escape(query, quote=True)}" placeholder="Company, role or location">'
            f'<select name="status">{options}</select><button type="submit">Filter</button></form>'
            '<div class="panel"><table><thead><tr><th>Company</th><th>Role</th><th>Location</th>'
            '<th>Status</th><th>Score</th><th>Updated</th></tr></thead><tbody>'
            + (table_rows or '<tr><td colspan="6" class="empty">No results for this filter.</td></tr>')
            + f'</tbody></table></div>{pager}')
    return page("Applications", body)


def render_job(job_id: int, db_path: Path = DB_PATH) -> bytes:
    job = db.get_job(job_id, db_path)
    messages = db.list_mail_for_job(job_id, db_path)
    mail = "".join(
        f'<div class="event"><strong>{escape(message["subject"] or "(no subject)")}</strong> '
        f'<span class="muted">{escape((message["message_at"] or message["created_at"])[:19].replace("T", " "))}</span>'
        f'<p>{escape(message["direction"])} · {escape(message["sender"])} → {escape(message["recipient"])}</p>'
        f'<p>{escape(message["snippet"] or "")}</p></div>' for message in messages)
    events = "".join(
        f'<div class="event"><strong>{escape(event["event_type"])}</strong> '
        f'<span class="muted">{escape(event["created_at"].replace("T", " ")[:19])}</span>'
        f'<p>{escape(json.dumps(event["details"], ensure_ascii=False, indent=2))}</p></div>'
        for event in reversed(job["events"]))
    body = (f'<a href="/">← All jobs</a><h1>{escape(job["company"])} · {escape(job["title"])}</h1>'
            f'<p>{_badge(job["status"])} · ID #{job_id}</p>'
            '<div class="meta">'
            f'<div><small>Location</small>{escape(job["location"] or "—")}</div>'
            f'<div><small>Published salary</small>{escape(job["salary"] or "Not published")}</div>'
            f'<div><small>Score</small>{job["match_score"]}</div>'
            f'<div><small>Follow-up</small>{escape(job["follow_up_at"] or "Not scheduled")}</div>'
            '</div>'
            f'<p><a href="{escape(job["canonical_url"], quote=True)}" target="_blank" rel="noopener noreferrer">Open original posting ↗</a></p>'
            f'<p class="muted">Resume: {escape(job["cv_path"] or "Not attached yet")}<br>'
            f'Notes: {escape(job["notes"] or "—")}</p>'
            f'<h2>Description</h2><div class="panel description">{escape(job["description"] or "No saved description")}</div>'
            f'<h2>Linked email</h2><div class="panel">{mail or "<div class=empty>No linked messages.</div>"}</div>'
            f'<h2>History</h2><div class="panel">{events or "<div class=empty>No events.</div>"}</div>')
    return page(f'{job["company"]} · {job["title"]}', body)


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    database_path = DB_PATH

    def _stream_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            with closing(db.connect(self.database_path)) as conn:
                version = conn.execute("PRAGMA data_version").fetchone()[0]
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                seconds = 0
                while True:
                    time.sleep(1)
                    current = conn.execute("PRAGMA data_version").fetchone()[0]
                    if current != version:
                        version = current
                        self.wfile.write(b"event: changed\ndata: {}\n\n")
                        self.wfile.flush()
                    seconds += 1
                    if seconds >= 20:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        seconds = 0
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/events":
            self._stream_events()
            return
        try:
            if parsed.path == "/":
                query = parse_qs(parsed.query)
                try:
                    page_no = int(query.get("page", ["1"])[0])
                except ValueError:
                    page_no = 1
                content = render_index(query.get("status", [""])[0], query.get("q", [""])[0],
                                       page_no, self.database_path)
                status = 200
            elif parsed.path.startswith("/job/") and parsed.path[5:].isdigit():
                content = render_job(int(parsed.path[5:]), self.database_path)
                status = 200
            elif parsed.path == "/dashboard.js":
                content = SCRIPT.encode("utf-8")
                status = 200
            elif parsed.path == "/health":
                content = b"ok"
                status = 200
            else:
                content = b"Not found"
                status = 404
        except KeyError:
            content = b"Not found"
            status = 404
        self.send_response(status)
        content_type = ("application/javascript; charset=utf-8" if parsed.path == "/dashboard.js"
                        else "text/html; charset=utf-8" if status == 200
                        else "text/plain; charset=utf-8")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(content)


def serve(port: int = 8765) -> None:
    if not 1024 <= port <= 65535:
        raise ValueError("Port must be between 1024 and 65535")
    db.init_db()
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"{DASHBOARD_TITLE}: http://127.0.0.1:{port}/", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
