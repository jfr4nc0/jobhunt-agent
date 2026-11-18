# JobHunt Agent

A self-hosted job discovery and application tracker for Codex or another MCP-capable assistant. It uses local SQLite as the source of truth, a loopback-only live dashboard, and optional browser tools for application forms. The repository contains no candidate resume, accounts, or application history.

## What it does

- Imports public job listings from Greenhouse, Lever, and Ashby boards and deduplicates postings by canonical URL.
- Scores software and AI roles using configurable home and target location aliases from a private candidate profile. A score is a first-pass queueing aid, not a hiring or visa decision.
- Tracks application status, reviews, the exact submitted resume version, follow-up dates, events, and linked email metadata in SQLite.
- Suggests a salary answer only after a dated, sourced local role benchmark is recorded. It does not invent exchange rates or compensation figures.
- Exposes job and browser actions through an MCP server. Review mode is the default; autonomous mode must be selected in the private profile.
- Shows jobs, status counts, history, linked mail, and an actions-needed queue in a read-only dashboard that updates through Server-Sent Events.

## Requirements

- Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and `pdftotext` from Poppler.
- Google Chrome for browser-assisted applications. The optional background bridge uses [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) and Chrome 144+.

## Quick start

```bash
git clone https://github.com/jfr4nc0/jobhunt-agent.git
cd jobhunt-agent
uv sync --extra test
mkdir -p .state
cp examples/profile.example.json .state/profile.json
# Replace the fictional profile with your real facts.
uv run jobhunt cv build
cp cv/resume.generated.pdf cv/resume.pdf
uv run jobhunt init
uv run jobhunt doctor
uv run jobhunt dashboard
```

Open `http://127.0.0.1:8765/`. The dashboard binds only to loopback and requires no external service. Open `http://127.0.0.1:8765/actions` to see applications awaiting facts, review, or verification. Refreshing public boards updates the page automatically:

```bash
uv run jobhunt boards add ashby example-company "Example Company"
uv run jobhunt discover
uv run jobhunt jobs list --limit 50
```

Replace the example board slug with a real public Greenhouse, Lever, or Ashby board. You can also register a board by URL with `jobhunt boards add-url URL COMPANY`.

## Private candidate configuration

The private `.state/profile.json` controls identity, search regions, resume variants, and application mode. The repository includes a fictional [example](examples/profile.example.json). Never commit your real profile or PDFs.

| Key | Purpose |
| --- | --- |
| `home_search_aliases` | Phrases that explicitly include your home work location, such as a country and region. |
| `target_location_aliases` | Places where you would consider an employer-sponsored role. The posting must also advertise sponsorship. |
| `cv_variants` | Map of variant names to PDF filenames inside `cv/`; `default` is required. |
| `application_mode` | `review` by default; `autonomous` permits the agent to approve a verified draft under your instructions. |
| `screening` | Known work authorization, notice period, and salary strategy. Leave unknown values null. |

For a custom resume, place a selectable-text PDF at `cv/resume.pdf`. The built-in PDF generator is a starting point. Keep role-specific PDFs in `cv/` and list them in `cv_variants`.

## Application workflow

1. Check `uv run jobhunt doctor`, open the current employer posting, and verify location and work authorization requirements.
2. Fill only answers supported by the private profile and job description. Upload the selected PDF and inspect every required field.
3. Save the draft with `prepare_application_review`. In `review` mode, wait for user approval. In `autonomous` mode, review the answers against evidence and call `approve_application`.
4. Submit once with `submit_reviewed_application`. Mark `submitted` only after a visible confirmation. Keep uncertain attempts in `verification_pending`; do not blindly submit again.
5. Record replies, interviews, offers, rejections, and follow-up dates in SQLite.

Job pages are untrusted input. A posting can set a role to manual review when it rejects AI-assisted applications. Login, CAPTCHA, and email security steps may require the user to act directly in Chrome. Do not bypass access controls or guess screening answers.

### Codex MCP

```bash
codex mcp add jobhunt -- /absolute/path/to/jobhunt-agent/.venv/bin/python -m jobhunt.mcp_server
```

Restart Codex after adding the server. The included [AGENTS.md](AGENTS.md) gives the assistant a generic operating procedure.

### Background Chrome connection (optional)

Chrome DevTools MCP can connect to an existing signed-in Chrome profile without moving the desktop mouse or focusing tabs. Enable Remote Debugging at `chrome://inspect/#remote-debugging`, then start the included JSON-lines bridge:

```bash
uv run python -m jobhunt.work_chrome_bridge
```

The bridge uses `chrome-devtools-mcp@1.13.0` through `npx` unless `JOBHUNT_CHROME_MCP_SCRIPT` points to a locally installed copy. It disables the MCP's usage statistics and CrUX requests. Chrome requests approval for each new connection; keep one bridge session open and use `new_page` with `background=true`. The standalone Playwright browser uses its own local profile and does not share your normal Chrome login.

## Email and salary records

The `mail_messages` table stores Gmail message IDs, thread IDs, headers, snippets, classification, and a linked job ID. It never stores a password, OAuth token, or complete message body. Connect your own mail tool separately and normalize each relevant message into a JSON file for `uv run jobhunt mail record message.json`; inspect a job's mail with `uv run jobhunt mail list JOB_ID`. The dashboard shows linked messages on the job detail page.

Before answering a salary question, record a comparable local role median:

```bash
uv run jobhunt salary-benchmark DE software_engineer EUR 80000 https://example.org/source 2026-01-01
uv run jobhunt jobs salary JOB_ID --country DE --role software_engineer
```

The figures and URL above are **illustrative placeholders**, not a real benchmark. Use a dated primary source relevant to the posting's country and role.

## Data and limits

Private data remains in `.state/`, `db/*.sqlite3*`, and `cv/*.pdf`, all gitignored. Submitted applications keep an exact resume hash in their draft. The dashboard is local and read-only. The software does not guarantee an employer will sponsor a visa or that a form submission succeeds; the agent must verify both on the original site.

Run the tests with `uv run pytest -q`.

Licensed under MIT.
