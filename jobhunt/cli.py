"""Human-readable and scriptable CLI for the local job search."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import dashboard, db, profile, salary, sources
from .paths import APPLICATION_CV_PATH, DB_PATH, PROFILE_PATH, UPDATED_CV_PATH


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobhunt", description="Local job search and application tracker")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("doctor")
    dashboard_cmd = sub.add_parser("dashboard", help="Read-only local web view of all tracked jobs")
    dashboard_cmd.add_argument("--port", type=int, default=8765)

    board = sub.add_parser("boards")
    board_sub = board.add_subparsers(dest="action", required=True)
    board_sub.add_parser("list")
    add_board = board_sub.add_parser("add")
    add_board.add_argument("provider", choices=("greenhouse", "lever", "ashby"))
    add_board.add_argument("slug")
    add_board.add_argument("company")
    board_url = board_sub.add_parser("add-url")
    board_url.add_argument("url")
    board_url.add_argument("company")

    sub.add_parser("discover")
    mail_cmd = sub.add_parser("mail", help="Record and inspect job-search email metadata")
    mail_sub = mail_cmd.add_subparsers(dest="action", required=True)
    mail_record = mail_sub.add_parser("record")
    mail_record.add_argument("json_file", type=Path, help="JSON file with Gmail message metadata")
    mail_list = mail_sub.add_parser("list")
    mail_list.add_argument("job_id", type=int)
    jobs = sub.add_parser("jobs")
    jobs_sub = jobs.add_subparsers(dest="action", required=True)
    listing = jobs_sub.add_parser("list")
    listing.add_argument("--status", choices=db.STATUSES)
    listing.add_argument("--limit", type=int, default=50)
    show = jobs_sub.add_parser("show")
    show.add_argument("id", type=int)
    salary_cmd = jobs_sub.add_parser("salary")
    salary_cmd.add_argument("id", type=int)
    salary_cmd.add_argument("--country", default="", help="ISO country code, e.g. DE")
    salary_cmd.add_argument("--role", default="", help="Comparable role key, e.g. software_engineer")
    salary_cmd.add_argument("--location", default="national")
    add = jobs_sub.add_parser("add")
    add.add_argument("--company", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--url", required=True)
    add.add_argument("--location", default="")
    add.add_argument("--description", default="")
    add.add_argument("--apply-url")
    add.add_argument("--source", default="web")
    status = jobs_sub.add_parser("status")
    status.add_argument("id", type=int)
    status.add_argument("status", choices=db.STATUSES)
    status.add_argument("--note", default="")
    follow_up = jobs_sub.add_parser("follow-up")
    follow_up.add_argument("id", type=int)
    follow_up.add_argument("date", help="ISO date or datetime")
    follow_up.add_argument("--note", default="")
    event = jobs_sub.add_parser("event")
    event.add_argument("id", type=int)
    event.add_argument("event_type")
    event.add_argument("--details", default="{}", help="JSON object with contact, date, notes, etc.")

    benchmark = sub.add_parser("salary-benchmark")
    benchmark.add_argument("country_code")
    benchmark.add_argument("role_key")
    benchmark.add_argument("currency")
    benchmark.add_argument("median_annual_gross", type=float)
    benchmark.add_argument("source_url")
    benchmark.add_argument("observed_at", help="ISO date of the source figure")
    benchmark.add_argument("--location", default="national")

    candidate = sub.add_parser("profile")
    candidate_sub = candidate.add_subparsers(dest="action", required=True)
    candidate_sub.add_parser("check")
    cv = sub.add_parser("cv")
    cv_sub = cv.add_subparsers(dest="action", required=True)
    cv_sub.add_parser("build")

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            db.init_db()
            _print({"database": str(DB_PATH), "profile": str(PROFILE_PATH), "profile_exists": PROFILE_PATH.is_file()})
        elif args.command == "dashboard":
            dashboard.serve(args.port)
        elif args.command == "doctor":
            db.init_db()
            _print({"database": str(DB_PATH), "profile_exists": PROFILE_PATH.is_file(),
                    "application_cv": str(APPLICATION_CV_PATH),
                    "application_cv_exists": APPLICATION_CV_PATH.is_file(),
                    "readiness": profile.submission_issues() if PROFILE_PATH.is_file() else ["Profile missing"]})
        elif args.command == "boards":
            if args.action == "list":
                _print(db.list_boards())
            elif args.action == "add":
                _print({"board_id": db.add_board(args.provider, args.slug, args.company)})
            else:
                result = sources.board_from_url(args.url)
                if not result:
                    raise ValueError("URL is not a Greenhouse, Lever or Ashby board")
                _print({"board_id": db.add_board(result[0], result[1], args.company),
                        "provider": result[0], "slug": result[1]})
        elif args.command == "discover":
            _print(sources.discover())
        elif args.command == "mail":
            if args.action == "list":
                _print(db.list_mail_for_job(args.job_id))
            else:
                message = json.loads(args.json_file.read_text(encoding="utf-8"))
                allowed = {"account", "message_id", "direction", "sender", "recipient",
                           "subject", "snippet", "thread_id", "message_at", "job_id", "classification"}
                if not isinstance(message, dict) or set(message) - allowed:
                    raise ValueError("Mail metadata file contains unsupported fields")
                _print({"mail_id": db.record_mail_message(**message)})
        elif args.command == "jobs":
            if args.action == "list":
                _print(db.list_jobs(args.status, args.limit))
            elif args.action == "show":
                _print(db.get_job(args.id))
            elif args.action == "salary":
                _print(salary.for_job(args.id, args.country, args.role, args.location))
            elif args.action == "add":
                _print({"job_id": sources.add_external_job(args.company, args.title, args.url,
                    args.location, args.description, args.apply_url, args.source)})
            elif args.action == "status":
                db.set_status(args.id, args.status, {"note": args.note, "manual": True})
                _print({"job_id": args.id, "status": args.status})
            elif args.action == "follow-up":
                db.set_follow_up(args.id, args.date, args.note)
                _print({"job_id": args.id, "follow_up_at": args.date})
            else:
                db.add_event(args.id, args.event_type, json.loads(args.details))
                _print({"job_id": args.id, "event_type": args.event_type})
        elif args.command == "salary-benchmark":
            benchmark_id = db.add_salary_benchmark(args.country_code, args.role_key, args.currency,
                args.median_annual_gross, args.source_url, args.observed_at, args.location)
            _print({"benchmark_id": benchmark_id})
        elif args.command == "profile":
            _print({"readiness": profile.submission_issues()})
        elif args.command == "cv":
            _print({"cv": str(profile.build_cv())})
    except (ValueError, KeyError, FileNotFoundError, json.JSONDecodeError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
