# Resume files

Put your own selectable-text PDF at `cv/resume.pdf`. This directory is ignored for PDF files so your resume stays local.

Optional: create a simple PDF from `.state/profile.json` with `uv run jobhunt cv build`, then copy `cv/resume.generated.pdf` to `cv/resume.pdf`. Add other variants in the profile's `cv_variants` map and keep their PDFs in this directory.
