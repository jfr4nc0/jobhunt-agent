from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "applications.sqlite3"
STATE_DIR = ROOT / ".state"
PROFILE_PATH = STATE_DIR / "profile.json"
CV_DIR = ROOT / "cv"
SOURCE_CV_PATH = CV_DIR / "resume.pdf"
APPLICATION_CV_PATH = SOURCE_CV_PATH
UPDATED_CV_PATH = CV_DIR / "resume.generated.pdf"
SCREENSHOT_DIR = STATE_DIR / "screenshots"
BROWSER_PROFILE_DIR = STATE_DIR / "browser-profile"
