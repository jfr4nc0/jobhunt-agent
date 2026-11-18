import json

import pytest

from jobhunt import profile


def test_resume_build_and_readiness_are_candidate_agnostic(tmp_path):
    candidate = {
        "name": "Alex Example", "headline": "Software Engineer", "location": "Toronto, Canada",
        "email": "alex@example.org", "phone": "", "links": {},
        "summary": "Backend engineer building developer tools.",
        "experience": [{"company": "Example Co", "title": "Engineer", "start": "2024-01",
                        "end": None, "bullets": ["Built APIs."], "stack": ["Python"]}],
        "skills": ["Python"], "education": [], "languages": ["English"],
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(candidate), encoding="utf-8")
    output = tmp_path / "resume.pdf"
    profile.build_cv(path, output)
    assert output.read_bytes().startswith(b"%PDF")
    assert profile.readiness(candidate, output) == []

    candidate["email"] = "different@example.org"
    assert "Resume PDF does not contain the profile email" in profile.readiness(candidate, output)


def test_resume_variant_paths_cannot_escape_cv_directory():
    assert profile.cv_path_for_variant("default", {"cv_variants": {"default": "resume.pdf"}}).name == "resume.pdf"
    with pytest.raises(ValueError):
        profile.cv_path_for_variant("bad", {"cv_variants": {"bad": "../private.pdf"}})
