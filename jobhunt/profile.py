"""Candidate facts and a simple, selectable-text PDF resume generator."""

from __future__ import annotations

import json
import subprocess
from html import escape
from pathlib import Path

from .paths import APPLICATION_CV_PATH, CV_DIR, PROFILE_PATH, UPDATED_CV_PATH


def load_profile(path: Path = PROFILE_PATH) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Profile missing: {path}. See README.md")
    return json.loads(path.read_text(encoding="utf-8"))


def cv_path_for_variant(variant: str = "default", candidate: dict | None = None) -> Path:
    candidate = candidate or load_profile()
    filenames = candidate.get("cv_variants") or {"default": "resume.pdf"}
    if variant not in filenames:
        raise ValueError(f"Unknown resume variant: {variant}")
    path = (CV_DIR / filenames[variant]).resolve()
    if not path.is_relative_to(CV_DIR.resolve()) or path.suffix.lower() != ".pdf":
        raise ValueError("Resume variant must be a PDF inside cv/")
    return path


def readiness(profile: dict | None = None, cv_path: Path = APPLICATION_CV_PATH) -> list[str]:
    profile = profile or load_profile()
    issues: list[str] = []
    for field in ("name", "email", "summary"):
        if not profile.get(field):
            issues.append(f"Profile field {field} is required")
    if not cv_path.is_file():
        issues.append(f"Resume PDF not found: {cv_path}")
        return issues
    if cv_path.suffix.lower() != ".pdf" or not cv_path.read_bytes().startswith(b"%PDF"):
        issues.append("Resume must be a PDF")
        return issues
    try:
        text = subprocess.run(["pdftotext", str(cv_path), "-"], check=True,
                              capture_output=True, text=True).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        issues.append("Install poppler-utils so the resume text can be checked")
        return issues
    for field in ("name", "email"):
        if profile.get(field) and profile[field].casefold() not in text.casefold():
            issues.append(f"Resume PDF does not contain the profile {field}")
    return issues


def submission_issues(profile: dict | None = None, cv_path: Path = APPLICATION_CV_PATH) -> list[str]:
    return readiness(profile, cv_path)


def build_cv(profile_path: Path = PROFILE_PATH, output_path: Path = UPDATED_CV_PATH) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer

    profile = load_profile(profile_path)
    if not profile.get("name") or not profile.get("email"):
        raise ValueError("Profile name and email are required before building a resume")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(output_path), pagesize=(8.5 * inch, 11 * inch),
                            rightMargin=0.62 * inch, leftMargin=0.62 * inch,
                            topMargin=0.53 * inch, bottomMargin=0.5 * inch)
    base = getSampleStyleSheet()
    navy = colors.HexColor("#17324d")
    styles = {
        "name": ParagraphStyle("Name", parent=base["Title"], fontName="Helvetica-Bold", fontSize=16,
                               leading=19, textColor=navy, alignment=TA_CENTER, spaceAfter=2),
        "headline": ParagraphStyle("Headline", parent=base["Normal"], fontName="Helvetica", fontSize=10,
                                   leading=13, alignment=TA_CENTER, spaceAfter=3),
        "contact": ParagraphStyle("Contact", parent=base["Normal"], fontSize=8, leading=10,
                                  alignment=TA_CENTER, spaceAfter=8),
        "section": ParagraphStyle("Section", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=9,
                                  leading=12, textColor=navy, spaceBefore=9, spaceAfter=4),
        "role": ParagraphStyle("Role", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=9,
                               leading=12, spaceBefore=4, spaceAfter=2),
        "body": ParagraphStyle("Body", parent=base["Normal"], fontSize=8.2, leading=11.2, spaceAfter=3),
        "bullet": ParagraphStyle("Bullet", parent=base["Normal"], fontSize=8.1, leading=10.8,
                                 leftIndent=10, firstLineIndent=-6, spaceAfter=2),
    }
    story = [Paragraph(escape(profile["name"]), styles["name"]),
             Paragraph(escape(profile["headline"]), styles["headline"])]
    contact = " | ".join(str(profile.get(k, "")) for k in ("location", "email", "phone") if profile.get(k))
    story.append(Paragraph(escape(contact), styles["contact"]))
    links = profile.get("links", {})
    story.append(Paragraph(escape(" | ".join(links.values())), styles["contact"]))
    story.append(Paragraph("PROFILE", styles["section"]))
    story.append(Paragraph(escape(profile["summary"]), styles["body"]))
    story.append(Paragraph("EXPERIENCE", styles["section"]))
    for exp in profile["experience"]:
        period = f"{exp['start']} – {exp.get('end') or 'Present'}"
        block = [Paragraph(f"{escape(exp['title'])} | {escape(exp['company'])} | {escape(period)}", styles["role"])]
        block.extend(Paragraph("• " + escape(bullet), styles["bullet"]) for bullet in exp.get("bullets", []))
        if exp.get("stack"):
            block.append(Paragraph("<b>Stack:</b> " + escape(", ".join(exp["stack"])), styles["body"]))
        story.append(KeepTogether(block))
    story.append(Paragraph("SKILLS", styles["section"]))
    story.append(Paragraph(escape(", ".join(profile.get("skills", []))), styles["body"]))
    story.append(Paragraph("EDUCATION", styles["section"]))
    for edu in profile.get("education", []):
        story.append(Paragraph(escape(f"{edu['institution']} — {edu['program']} ({edu['period']})"), styles["body"]))
    if profile.get("certifications"):
        story.append(Paragraph("CERTIFICATIONS", styles["section"]))
        story.append(Paragraph(escape(" · ".join(profile["certifications"])), styles["body"]))
    story.append(Spacer(1, 3))
    story.append(Paragraph(escape("Languages: " + ", ".join(profile.get("languages", []))), styles["body"]))
    doc.build(story)
    return output_path
