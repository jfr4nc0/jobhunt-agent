"""Persistent local browser that Codex can operate through the MCP server."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import BrowserContext, Page, Playwright, TimeoutError as PlaywrightTimeoutError, async_playwright

from . import db, profile, sources
from .paths import APPLICATION_CV_PATH, BROWSER_PROFILE_DIR, SCREENSHOT_DIR

FINAL_ACTION = re.compile(
    r"\b(submit( application)?|send application|complete application|finalizar postulación|"
    r"enviar (solicitud|postulación|candidatura)|confirmar postulación|apply now|postularme)\b", re.I,
)
CONFIRMATION = re.compile(
    r"thank you for applying|application (has been )?submitted|application sent|"
    r"applied successfully|successfully applied|solicitud enviada|postulación enviada|"
    r"candidatura enviada|gracias por postularte|hemos recibido tu (solicitud|postulación)", re.I,
)
UPLOAD_ERROR = re.compile(r"failed to upload|file upload failed|could not upload|error uploading", re.I)
EMAIL_VERIFICATION = re.compile(r"verification code was sent|enter the 8-character code|security code", re.I)


def safe_url(url: str) -> str:
    parts = urlsplit(url)
    path_parts = [part for part in parts.path.split("/") if part]
    short_path = "/" + "/".join(path_parts[:3]) if path_parts else "/"
    return urlunsplit((parts.scheme, parts.netloc, short_path, "", ""))


def captcha_challenge_open(page: Page) -> bool:
    return any("frame=challenge" in frame.url and ("hcaptcha" in frame.url or "recaptcha" in frame.url)
               or "recaptcha/api2/bframe" in frame.url for frame in page.frames)


class BrowserSession:
    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._job_id: int | None = None
        self._actions: list[dict] = []
        self._network_errors: list[dict] = []
        self._lock = asyncio.Lock()

    async def _ensure_page(self) -> Page:
        if self._page and not self._page.is_closed():
            return self._page
        browser_profile = Path(os.environ.get("JOBHUNT_BROWSER_PROFILE_DIR", str(BROWSER_PROFILE_DIR)))
        browser_profile.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        headless = os.environ.get("JOBHUNT_HEADLESS", "0") == "1"
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(browser_profile), channel="chrome",
                headless=headless,
                accept_downloads=True,
            )
        except Exception as exc:
            if not headless and "XServer" in str(exc):
                try:
                    self._context = await self._playwright.chromium.launch_persistent_context(
                        str(browser_profile), channel="chrome", headless=True, accept_downloads=True)
                except Exception:
                    await self._playwright.stop()
                    self._playwright = None
                    raise
            else:
                await self._playwright.stop()
                self._playwright = None
                raise
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._page.set_default_timeout(12000)
        self._page.on("requestfailed", lambda request: self._network_errors.append(
            {"url": safe_url(request.url), "failure": request.failure}))
        self._page.on("response", lambda response: self._network_errors.append(
            {"url": safe_url(response.url), "status": response.status}) if response.status >= 400 else None)
        return self._page

    async def open_job(self, job_id: int) -> dict:
        async with self._lock:
            job = db.get_job(job_id)
            if job["status"] in {"submitted", "interviewing", "offer", "rejected", "withdrawn",
                                 "awaiting_verification", "verification_pending"}:
                raise ValueError("This application is submitted, closed, or awaiting verification; inspect its history before reopening")
            url = job["apply_url"] or job["canonical_url"]
            parts = urlsplit(url)
            if parts.scheme not in {"https", "http"}:
                raise ValueError("Only HTTP(S) pages can be opened")
            page = await self._ensure_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            self._job_id = job_id
            self._actions = []
            self._network_errors = []
            db.set_status(job_id, "drafting", {"url": page.url})
            return await self._snapshot_unlocked(page)

    async def open_url(self, url: str) -> dict:
        """Open a login page without associating it with an application."""
        async with self._lock:
            parts = urlsplit(url)
            if parts.scheme not in {"https", "http"}:
                raise ValueError("Only HTTP(S) pages can be opened")
            page = await self._ensure_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            self._job_id = None
            self._actions = []
            return await self._snapshot_unlocked(page)

    async def snapshot(self) -> dict:
        async with self._lock:
            return await self._snapshot_unlocked(await self._ensure_page())

    def diagnostics(self) -> dict:
        return {"network_errors": self._network_errors[-30:]}

    async def _snapshot_unlocked(self, page: Page) -> dict:
        fields = await page.locator("input, textarea, select, button").evaluate_all("""els => els
            .filter(el => el.offsetParent !== null && el.type !== 'password' && el.type !== 'hidden')
            .slice(0, 100).map(el => ({
                tag: el.tagName.toLowerCase(), type: el.type || '', name: el.name || '',
                id: el.id || '', text: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 120),
                label: (el.labels?.[0]?.innerText || el.closest('label')?.innerText || '').trim().slice(0, 180),
                placeholder: el.getAttribute('placeholder') || '',
                html: el.outerHTML.slice(0, 700),
                value: el.id?.startsWith('security-input-') ? (el.value ? '•' : '') : (el.value || '').slice(0, 300),
                required: !!el.required,
                checked: !!el.checked,
                options: el.tagName === 'SELECT' ? [...el.options].slice(0, 40).map(o => ({value:o.value, text:o.text})) : []
            }))""")
        try:
            body = await page.locator("body").inner_text(timeout=8000)
        except Exception:
            body = ""
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        screenshot = SCREENSHOT_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}.png"
        screenshot_error = None
        try:
            await page.screenshot(path=str(screenshot), full_page=False, timeout=10000)
        except PlaywrightTimeoutError as exc:
            screenshot = None
            screenshot_error = str(exc).splitlines()[0]
        frames = []
        for frame in page.frames[1:10]:
            frame_url = safe_url(frame.url)
            if "hcaptcha" in frame.url or "recaptcha" in frame.url:
                frames.append({"url": frame_url, "kind": "captcha_challenge" if "frame=challenge" in frame.url
                               or "recaptcha/api2/bframe" in frame.url else "captcha_widget"})
                continue
            try:
                frames.append({"url": frame_url, "name": frame.name,
                               "text": (await frame.locator("body").inner_text(timeout=2000))[:4000]})
            except Exception:
                frames.append({"url": frame_url, "name": frame.name, "text": "unavailable"})
        return {"job_id": self._job_id, "url": page.url, "title": await page.title(),
                "text": body[:18000], "fields": fields, "frames": frames,
                "screenshot": str(screenshot) if screenshot else None,
                "screenshot_error": screenshot_error}

    @staticmethod
    def _target(page: Page, selector: str, frame_selector: str):
        if frame_selector:
            return page.frame_locator(frame_selector).locator(selector).first
        return page.locator(selector).first

    async def fill(self, selector: str, value: str, frame_selector: str = "") -> dict:
        async with self._lock:
            page = await self._ensure_page()
            target = self._target(page, selector, frame_selector)
            if await target.get_attribute("type") == "password":
                raise ValueError("The user must enter passwords directly in Chrome")
            await target.fill(value)
            self._actions.append({"action": "fill", "selector": selector, "value": value,
                                  "frame_selector": frame_selector})
            return {"filled": selector, "snapshot": await self._snapshot_unlocked(page)}

    async def select(self, selector: str, value: str, frame_selector: str = "") -> dict:
        async with self._lock:
            page = await self._ensure_page()
            await self._target(page, selector, frame_selector).select_option(value=value)
            self._actions.append({"action": "select", "selector": selector, "value": value,
                                  "frame_selector": frame_selector})
            return {"selected": selector, "snapshot": await self._snapshot_unlocked(page)}

    async def check(self, selector: str, checked: bool = True, frame_selector: str = "") -> dict:
        async with self._lock:
            page = await self._ensure_page()
            locator = self._target(page, selector, frame_selector)
            if checked:
                await locator.check()
            else:
                await locator.uncheck()
            self._actions.append({"action": "check", "selector": selector, "checked": checked,
                                  "frame_selector": frame_selector})
            return {"checked": selector, "snapshot": await self._snapshot_unlocked(page)}

    async def upload_cv(self, selector: str, frame_selector: str = "", variant: str = "default") -> dict:
        async with self._lock:
            cv_path = profile.cv_path_for_variant(variant)
            issues = profile.readiness(cv_path=cv_path)
            if issues:
                raise ValueError("Candidate profile or CV is not ready: " + "; ".join(issues))
            page = await self._ensure_page()
            await self._target(page, selector, frame_selector).set_input_files(str(cv_path))
            self._actions.append({"action": "upload_cv", "selector": selector,
                                  "frame_selector": frame_selector, "variant": variant})
            return {"uploaded": str(cv_path), "snapshot": await self._snapshot_unlocked(page)}

    async def click(self, selector: str, frame_selector: str = "") -> dict:
        async with self._lock:
            page = await self._ensure_page()
            locator = self._target(page, selector, frame_selector)
            descriptor = await locator.evaluate("""el => ({type: el.type || '',
                in_form: !!el.closest('form'),
                choice: el.hasAttribute('data-option') || el.getAttribute('role') === 'radio',
                text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()})""")
            label = descriptor["text"]
            visible_form_fields = await page.locator("form input:visible, form textarea:visible, form select:visible").count()
            is_initial_apply = bool(re.search(r"\b(apply now|postularme)\b", label, re.I)) and visible_form_fields == 0
            if (FINAL_ACTION.search(label) and not is_initial_apply) or (descriptor["type"] == "submit" and descriptor["in_form"] and not descriptor["choice"] and not is_initial_apply and not re.search(
                r"\b(next|continue|review|siguiente|continuar|revisar)\b", label, re.I)):
                raise ValueError("Final submission is blocked here. Prepare the review and use submit_application after explicit user approval.")
            await locator.click()
            await page.wait_for_timeout(500)
            self._actions.append({"action": "click", "selector": selector,
                                  "frame_selector": frame_selector})
            return await self._snapshot_unlocked(page)

    async def prepare_review(self, job_id: int, agent_notes: str = "") -> dict:
        async with self._lock:
            if self._job_id != job_id:
                raise ValueError("Open this job in the browser first")
            job = db.get_job(job_id)
            if sources.manual_only(job.get("description") or ""):
                db.set_status(job_id, "needs_manual", {"reason": "Posting rejects AI-assisted submissions"})
                raise ValueError("This employer rejects AI-assisted submissions; apply manually")
            issues = profile.readiness()
            if issues:
                raise ValueError("Application cannot be prepared: " + "; ".join(issues))
            page = await self._ensure_page()
            snapshot = await self._snapshot_unlocked(page)
            if UPLOAD_ERROR.search(snapshot["text"]):
                raise ValueError("The portal reports that the CV upload failed; resolve it before review")
            upload_actions = [action for action in self._actions if action["action"] == "upload_cv"]
            variant = upload_actions[-1].get("variant", "default") if upload_actions else "default"
            cv_path = profile.cv_path_for_variant(variant)
            draft = {"url": snapshot["url"], "title": snapshot["title"],
                     "fields": snapshot["fields"], "agent_notes": agent_notes,
                     "screenshot": snapshot["screenshot"], "actions": list(self._actions),
                     "cv_sha256": hashlib.sha256(cv_path.read_bytes()).hexdigest()}
            db.save_review(job_id, draft, str(cv_path))
            instruction = ("Verify every required answer against the profile and posting, then approve and submit once."
                           if profile.load_profile().get("application_mode") == "autonomous"
                           else "Show this application to the user and wait for explicit approval before submission.")
            return {"job": db.get_job(job_id), "review": draft, "instruction": instruction}

    async def resume_review(self, job_id: int) -> dict:
        """Recreate a saved draft after Codex or the browser process restarts."""
        job = db.get_job(job_id)
        if job["status"] not in {"ready_for_review", "approved"} or not job.get("draft"):
            raise ValueError("No reviewable draft exists for this job")
        draft = job["draft"]
        cv_path = Path(job.get("cv_path") or APPLICATION_CV_PATH)
        if hashlib.sha256(cv_path.read_bytes()).hexdigest() != draft.get("cv_sha256"):
            raise ValueError("The CV changed after review; prepare a new draft")
        async with self._lock:
            page = await self._ensure_page()
            await page.goto(job["apply_url"] or job["canonical_url"],
                            wait_until="domcontentloaded", timeout=30000)
            self._job_id = job_id
            self._actions = []
        for action in draft.get("actions", []):
            kind = action["action"]
            selector = action["selector"]
            frame_selector = action.get("frame_selector", "")
            if kind == "fill":
                await self.fill(selector, action["value"], frame_selector)
            elif kind == "select":
                await self.select(selector, action["value"], frame_selector)
            elif kind == "check":
                await self.check(selector, action["checked"], frame_selector)
            elif kind == "upload_cv":
                await self.upload_cv(selector, frame_selector, action.get("variant", "default"))
            elif kind == "click":
                await self.click(selector, frame_selector)
            else:
                raise ValueError(f"Unknown saved browser action: {kind}")
        return await self.snapshot()

    async def submit(self, job_id: int, selector: str, frame_selector: str = "") -> dict:
        async with self._lock:
            if self._job_id != job_id:
                raise ValueError("Open this job in the browser first")
            status = db.get_job(job_id)["status"]
            if status not in {"approved", "awaiting_verification"}:
                raise ValueError("This application has not been approved after review")
            cv_path = Path(db.get_job(job_id).get("cv_path") or APPLICATION_CV_PATH)
            issues = profile.submission_issues() if cv_path == APPLICATION_CV_PATH else profile.submission_issues(cv_path=cv_path)
            if issues:
                raise ValueError("Candidate facts need review before submission: " + "; ".join(issues))
            reviewed_hash = db.get_job(job_id)["draft"]["cv_sha256"]
            if hashlib.sha256(cv_path.read_bytes()).hexdigest() != reviewed_hash:
                raise ValueError("The CV changed after approval; prepare a new draft")
            page = await self._ensure_page()
            if status == "awaiting_verification":
                code_fields = page.locator("input[id^='security-input-']")
                if await code_fields.count() and not await code_fields.evaluate_all(
                    "els => els.length === 8 && els.every(el => el.value.length === 1)"):
                    raise ValueError("The user has not completed the email security code in Chrome")
            before_url = page.url
            await self._target(page, selector, frame_selector).click()
            for _ in range(15):
                await page.wait_for_timeout(1000)
                try:
                    current_text = await page.locator("body").inner_text(timeout=3000)
                except Exception:
                    current_text = ""
                if CONFIRMATION.search(current_text) or UPLOAD_ERROR.search(current_text) or \
                        EMAIL_VERIFICATION.search(current_text) or captcha_challenge_open(page):
                    break
            snapshot = await self._snapshot_unlocked(page)
            confirmed = bool(CONFIRMATION.search(snapshot["text"]))
            if confirmed:
                evidence = f"screenshot={snapshot['screenshot']}; before_url={before_url}; confirmation_text=True"
                db.record_submission(job_id, True, snapshot["url"], evidence)
                return {"job_id": job_id, "status": "submitted", "confirmation_detected": True,
                        "snapshot": snapshot, "diagnostics": self.diagnostics()}
            if EMAIL_VERIFICATION.search(snapshot["text"]):
                db.record_verification_required(job_id, "email_code", snapshot["screenshot"] or "")
                return {"job_id": job_id, "status": "awaiting_verification",
                        "instruction": "The user must enter the code directly in Chrome before submission can finish.",
                        "snapshot": snapshot}
            if captcha_challenge_open(page):
                reason = "A CAPTCHA challenge appeared before the application could be submitted"
                db.record_failed_attempt(job_id, reason, snapshot["screenshot"] or "")
                return {"job_id": job_id, "status": "needs_manual", "error": reason,
                        "snapshot": snapshot, "diagnostics": self.diagnostics()}
            if UPLOAD_ERROR.search(snapshot["text"]):
                reason = "The portal reported that the CV failed to upload"
                db.record_failed_attempt(job_id, reason, snapshot["screenshot"] or "")
                return {"job_id": job_id, "status": "needs_manual", "error": reason,
                        "snapshot": snapshot, "diagnostics": self.diagnostics()}
            evidence = f"screenshot={snapshot['screenshot']}; before_url={before_url}; confirmation_text=False"
            db.record_submission(job_id, False, snapshot["url"], evidence)
            return {"job_id": job_id, "status": "verification_pending",
                    "confirmation_detected": False, "snapshot": snapshot,
                    "diagnostics": self.diagnostics(),
                    "instruction": "If verification is pending, inspect the page or employer email manually. Do not click submit again."}

    async def close(self) -> None:
        async with self._lock:
            if self._context:
                await self._context.close()
            if self._playwright:
                await self._playwright.stop()
            self._context = self._page = self._playwright = None
            self._job_id = None
            self._actions = []
            self._network_errors = []
