"""Public job-board discovery and deterministic first-pass matching."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import unescape
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from . import db
from .paths import PROFILE_PATH


def plain_text(value: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", unescape(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def fetch_json(url: str) -> dict | list:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "jobhunt-agent/0.1"})
    with urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return json.load(response)


def lever_created_at(value: int | None) -> str | None:
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="seconds") if value else None


def ashby_salary(value: dict | str | None) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    summary = value.get("scrapeableCompensationSalarySummary") or value.get("compensationTierSummary")
    if summary:
        return str(summary)
    tiers = value.get("compensationTiers") or value.get("summaryComponents")
    return json.dumps(tiers, ensure_ascii=False) if tiers else None


def _profile_aliases(key: str) -> tuple[str, ...]:
    if not PROFILE_PATH.is_file():
        return ()
    try:
        values = json.loads(PROFILE_PATH.read_text(encoding="utf-8")).get(key, [])
    except (OSError, ValueError):
        return ()
    if not isinstance(values, list):
        return ()
    return tuple(str(value).strip().lower() for value in values if str(value).strip())


def _mentions(text: str, aliases: tuple[str, ...]) -> bool:
    return any(re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text) for alias in aliases)


def infer_remote_scope(location: str, description: str, is_remote: bool | None = None,
                       workplace_type: str = "") -> str:
    loc, body = location.lower(), description[:20000].lower()
    workplace = workplace_type.lower()
    home_aliases = _profile_aliases("home_search_aliases")
    target_aliases = _profile_aliases("target_location_aliases")
    if _mentions(loc, target_aliases) and sponsorship_advertised(body):
        return "sponsored_abroad"
    if workplace in {"onsite", "on-site", "hybrid"} or is_remote is False:
        return "excluded"
    if re.search(r"\b(us only|usa only|united states only|canada only|eu only|europe only|uk only|within the us|u\.s\. only)\b", f"{loc} {body}"):
        return "excluded"
    remote = is_remote is True or workplace == "remote" or bool(re.search(
        r"\b(remote|remoto|work from anywhere)\b", f"{loc} {body}"))
    if not remote:
        return "unknown"
    if _mentions(loc, home_aliases) or re.search(r"\b(worldwide|anywhere|global)\b", loc):
        return "home_ok"
    if loc and not re.fullmatch(r"\s*(remote|remoto)\s*", loc):
        return "excluded"
    if re.search(r"\b(work from anywhere|worldwide remote|global remote)\b", body):
        return "home_ok"
    return "unknown"


def score_job(title: str, location: str, description: str, remote_scope: str) -> tuple[int, str]:
    title_lower = title.lower()
    keywords = [
        (r"\b(ai|artificial intelligence|llm|machine learning|ml|agentic|agents)\b", 35, "AI/ML"),
        (r"\b(back.?end|software engineer|product engineer|platform engineer|full.?stack engineer)\b", 28, "software/product/backend"),
        (r"\b(python|java|kotlin|typescript)\b", 8, "core stack"),
    ]
    points = 0
    reasons = []
    for pattern, weight, reason in keywords:
        if re.search(pattern, title_lower):
            points += weight
            reasons.append(reason)
    stack_hits = [s for s in ("python", "typescript", "java", "kotlin", "langgraph", "kubernetes", "mcp")
                  if re.search(r"\b" + re.escape(s) + r"\b", description.lower())]
    points += min(len(stack_hits), 5) * 4
    if stack_hits:
        reasons.append("stack: " + ", ".join(stack_hits[:5]))
    if remote_scope == "home_ok":
        points += 20
        reasons.append("remote accepts home location/region")
    elif remote_scope == "sponsored_abroad":
        points += 25
        reasons.append("visa sponsorship advertised abroad")
    elif remote_scope == "excluded":
        points -= 50
        reasons.append("remote location excludes configured home location")
    else:
        reasons.append("remote eligibility unconfirmed")
    if re.search(r"\b(intern|internship|sales|recruiter|marketing)\b", title_lower):
        points -= 45
        reasons.append("different role family")
    if re.search(r"\b(manager|operations|onboarding|frontend|front-end)\b", title_lower) and not re.search(
        r"\b(engineer|developer|architect)\b", title_lower):
        points -= 35
        reasons.append("outside target engineering role")
    if re.search(r"\b(frontend|front-end)\b", title_lower):
        points -= 25
        reasons.append("frontend is a secondary target")
    if manual_only(description):
        points -= 70
        reasons.append("posting rejects AI-assisted submissions; apply manually")
    return max(0, min(100, points)), "; ".join(reasons)


def manual_only(description: str) -> bool:
    return bool(re.search(r"\b(ai submissions will be rejected|no ai.generated applications|do not use ai to apply)\b",
                          description, re.I))


def sponsorship_advertised(description: str) -> bool:
    text = description.lower()
    if re.search(r"\b(no visa sponsorship|visa sponsorship (?:is )?not available|"
                 r"(?:cannot|can't|unable to|not in a position to|do not|don't)\s+(?:offer|provide|support)?\s*visa sponsorship|"
                 r"do not sponsor visas)\b", text):
        return False
    return bool(re.search(r"\b(visa sponsorship (?:is )?available|visa sponsorship and relocation|"
                          r"relocation support(?: and|,)\s*visa sponsorship|including visa sponsorship|"
                          r"visa sponsorship as a|visa sponsorship where necessary|"
                          r"sponsor visas|visa sponsorship offered|visa sponsorship\.\s*(?:for\b|$))", text))


def _enrich(job: dict) -> dict:
    job["description"] = plain_text(job.get("description"))
    job["remote_scope"] = infer_remote_scope(
        job.get("location") or "", job["description"],
        job.get("is_remote"), job.get("workplace_type") or "")
    job["match_score"], job["match_reason"] = score_job(
        job["title"], job.get("location") or "", job["description"], job["remote_scope"])
    job["manual_only"] = manual_only(job["description"])
    return job


def fetch_board(provider: str, slug: str, company: str) -> list[dict]:
    escaped = quote(slug, safe="")
    if provider == "greenhouse":
        payload = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{escaped}/jobs?content=true")
        jobs = payload.get("jobs", [])
        return [_enrich({
            "source": "greenhouse", "source_id": str(item["id"]), "company": company,
            "url": item["absolute_url"], "apply_url": item["absolute_url"],
            "title": item["title"], "location": (item.get("location") or {}).get("name", ""),
            "description": item.get("content", ""),
        }) for item in jobs if item.get("absolute_url")]
    if provider == "lever":
        payload = fetch_json(f"https://api.lever.co/v0/postings/{escaped}?mode=json")
        return [_enrich({
            "source": "lever", "source_id": str(item["id"]), "company": company,
            "url": item.get("hostedUrl") or item.get("applyUrl"),
            "apply_url": item.get("applyUrl") or item.get("hostedUrl"),
            "title": item["text"], "location": (item.get("categories") or {}).get("location", ""),
            "description": item.get("descriptionPlain") or item.get("description", ""),
            "workplace_type": item.get("workplaceType", ""),
            "posted_at": lever_created_at(item.get("createdAt")),
        }) for item in payload if item.get("hostedUrl") or item.get("applyUrl")]
    if provider == "ashby":
        payload = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{escaped}?includeCompensation=true")
        jobs = payload.get("jobs", [])

        def locations(item: dict) -> str:
            places = [item] + item.get("secondaryLocations", [])
            labels = [place.get("location", "") for place in places]
            countries = [(place.get("address") or {}).get("postalAddress", {}).get("addressCountry", "")
                         for place in places]
            return "; ".join(dict.fromkeys(label for label in labels + countries if label))

        return [_enrich({
            "source": "ashby", "source_id": str(item.get("id") or item.get("jobUrl")),
            "company": company, "url": item.get("jobUrl") or item.get("applyUrl"),
            "apply_url": item.get("applyUrl") or item.get("jobUrl"),
            "title": item["title"], "location": locations(item),
            "description": item.get("descriptionPlain") or item.get("descriptionHtml", ""),
            "is_remote": item.get("isRemote"), "workplace_type": item.get("workplaceType", ""),
            "salary": ashby_salary(item.get("compensation")),
            "posted_at": item.get("publishedAt"),
        }) for item in jobs if item.get("jobUrl") or item.get("applyUrl")]
    raise ValueError(f"Unsupported provider: {provider}")


def discover(db_path=db.DB_PATH) -> dict:
    results = {"boards": 0, "jobs_seen": 0, "errors": []}
    for board in db.list_boards(db_path):
        try:
            listings = fetch_board(board["provider"], board["slug"], board["company_name"])
            job_ids = db.upsert_jobs(listings, db_path)
            for job, job_id in zip(listings, job_ids):
                if job["manual_only"] and db.get_job(job_id, db_path)["status"] == "discovered":
                    db.set_status(job_id, "needs_manual", {"reason": "Posting rejects AI-assisted submissions"}, db_path)
            results["boards"] += 1
            results["jobs_seen"] += len(listings)
        except Exception as exc:
            results["errors"].append(f"{board['provider']}/{board['slug']}: {exc}")
    return results


def board_from_url(url: str) -> tuple[str, str] | None:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        return None
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        return "greenhouse", segments[0]
    if host in {"jobs.lever.co", "jobs.eu.lever.co"}:
        return "lever", segments[0]
    if host == "jobs.ashbyhq.com":
        return "ashby", segments[0]
    return None


def add_external_job(company: str, title: str, url: str, location: str = "",
                     description: str = "", apply_url: str | None = None,
                     source: str = "web", db_path=db.DB_PATH) -> int:
    job = _enrich({"source": source, "company": company, "title": title,
                   "url": url, "apply_url": apply_url or url, "location": location,
                   "description": description})
    job_id = db.upsert_job(job, db_path)
    if job["manual_only"] and db.get_job(job_id, db_path)["status"] == "discovered":
        db.set_status(job_id, "needs_manual", {"reason": "Posting rejects AI-assisted submissions"}, db_path)
    return job_id
