"""Codex-facing MCP tools for discovery, tracking and browser-assisted applications."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import db, profile, salary, sources
from .browser import BrowserSession

mcp = FastMCP("jobhunt")
browser = BrowserSession()


@mcp.tool()
def candidate_readiness() -> dict:
    """Check whether the factual candidate profile and updated CV are ready for real applications."""
    issues = profile.submission_issues()
    return {"ready": not issues, "issues": issues}


@mcp.tool()
def add_job_board(provider: str, slug: str, company_name: str) -> dict:
    """Add a public Greenhouse, Lever or Ashby board discovered from a company career page."""
    return {"board_id": db.add_board(provider, slug, company_name)}


@mcp.tool()
def discover_board_jobs() -> dict:
    """Fetch current published jobs from registered public company boards and update the local database."""
    return sources.discover()


@mcp.tool()
def add_job(company: str, title: str, url: str, location: str = "", description: str = "",
            apply_url: str = "", source: str = "web") -> dict:
    """Track a vacancy found through Codex web search, LinkedIn, or another career site."""
    return {"job_id": sources.add_external_job(company, title, url, location, description, apply_url or None, source)}


@mcp.tool()
def list_tracked_jobs(status: str = "", limit: int = 50) -> list[dict]:
    """List and rank tracked vacancies; statuses include discovered, ready_for_review and submitted."""
    return db.list_jobs(status or None, limit)


@mcp.tool()
def get_tracked_job(job_id: int) -> dict:
    """Read a vacancy, its application draft, status, and complete event history."""
    return db.get_job(job_id)


@mcp.tool()
def suggest_salary_for_job(job_id: int, country_code: str = "", role_key: str = "",
                           location: str = "national") -> dict:
    """Suggest pay from a dated local role median; require country and role context."""
    return salary.for_job(job_id, country_code, role_key, location)


@mcp.tool()
def record_salary_benchmark(country_code: str, role_key: str, currency: str,
                            median_annual_gross: float, source_url: str,
                            observed_at: str, location: str = "national") -> dict:
    """Persist a verified gross annual local salary median with its source and date."""
    return {"benchmark_id": db.add_salary_benchmark(country_code, role_key, currency,
        median_annual_gross, source_url, observed_at, location)}


@mcp.tool()
def update_application_status(job_id: int, status: str, note: str = "") -> dict:
    """Record a later employer response or a manual decision. Never mark a submission without evidence."""
    if status in {"approved", "submitted", "verification_pending", "ready_for_review"}:
        raise ValueError("These states are controlled by the review/submission workflow")
    db.set_status(job_id, status, {"note": note, "manual": True})
    return {"job_id": job_id, "status": status}


@mcp.tool()
def schedule_follow_up(job_id: int, date: str, note: str = "") -> dict:
    """Set an ISO date or datetime to follow up on this application."""
    db.set_follow_up(job_id, date, note)
    return {"job_id": job_id, "follow_up_at": date}


@mcp.tool()
def record_application_event(job_id: int, event_type: str, details: dict) -> dict:
    """Add a dated event to the history, such as recruiter contact, interview, or offer details."""
    db.add_event(job_id, event_type, details)
    return {"job_id": job_id, "event_type": event_type}


@mcp.tool()
async def browser_open_login(url: str) -> dict:
    """Open a login page in the local Chrome profile. The user enters credentials directly in Chrome."""
    return await browser.open_url(url)


@mcp.tool()
async def browser_open_job(job_id: int) -> dict:
    """Open the tracked vacancy/application page in the persistent local Chrome session."""
    return await browser.open_job(job_id)


@mcp.tool()
async def browser_inspect() -> dict:
    """Inspect the current page's visible text and form controls, with a local screenshot path."""
    return await browser.snapshot()


@mcp.tool()
def browser_diagnostics() -> dict:
    """Report recent failed browser requests and HTTP errors for application troubleshooting."""
    return browser.diagnostics()


@mcp.tool()
async def browser_fill(selector: str, value: str, frame_selector: str = "") -> dict:
    """Fill one form field using a CSS selector and a fact supported by the candidate profile or user."""
    return await browser.fill(selector, value, frame_selector)


@mcp.tool()
async def browser_select(selector: str, value: str, frame_selector: str = "") -> dict:
    """Choose a select option by its HTML value."""
    return await browser.select(selector, value, frame_selector)


@mcp.tool()
async def browser_check(selector: str, checked: bool = True, frame_selector: str = "") -> dict:
    """Set a checkbox after verifying what it means."""
    return await browser.check(selector, checked, frame_selector)


@mcp.tool()
async def browser_upload_cv(selector: str, frame_selector: str = "", variant: str = "default") -> dict:
    """Upload a PDF variant configured in the private candidate profile."""
    return await browser.upload_cv(selector, frame_selector, variant)


@mcp.tool()
async def browser_click_next(selector: str, frame_selector: str = "") -> dict:
    """Click a non-final control. Submission controls are blocked until the application is reviewed."""
    return await browser.click(selector, frame_selector)


@mcp.tool()
async def prepare_application_review(job_id: int, agent_notes: str = "") -> dict:
    """Save the filled draft and return its field summary for the user's per-vacancy review."""
    return await browser.prepare_review(job_id, agent_notes)


@mcp.tool()
async def resume_application_draft(job_id: int) -> dict:
    """Reopen and refill a saved review draft after a session restart; inspect it before submitting."""
    return await browser.resume_review(job_id)


@mcp.tool()
def approve_application(job_id: int) -> dict:
    """Approve a completed draft under the user's current application mode."""
    mode = profile.load_profile().get("application_mode", "review")
    if mode != "autonomous":
        raise ValueError("Per-vacancy review mode is active; the user must approve this job in chat")
    db.approve(job_id, basis="autonomous_authorization")
    return {"job_id": job_id, "status": "approved", "basis": "autonomous_authorization"}


@mcp.tool()
async def submit_reviewed_application(job_id: int, selector: str, frame_selector: str = "") -> dict:
    """Submit a reviewed and explicitly approved draft once; verify the result and persist it."""
    return await browser.submit(job_id, selector, frame_selector)


@mcp.tool()
async def browser_close() -> dict:
    """Close the browser process while preserving its separate local login profile."""
    await browser.close()
    return {"closed": True}


def main() -> None:
    db.init_db()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
