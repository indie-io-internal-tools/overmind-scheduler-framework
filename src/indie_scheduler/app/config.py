"""Framework configuration.

The scheduler is consumed as a pip package, so paths are split:
  - Package-internal (templates, static): relative to this file.
  - Team-controlled (jobs, .env, data): relative to SCHEDULER_WORKING_DIR,
    which defaults to the current working directory when the entry point
    is launched. Set explicitly in PM2's ecosystem config.

Environment variables (all optional except where noted):
  SCHEDULER_WORKING_DIR     where jobs/ and .env live (default: CWD)
  SCHEDULER_PORT            HTTP port (default: 3014)
  SCHEDULER_BASE_PATH       URL prefix when reverse-proxied (e.g. /scheduler)
  SCHEDULER_DB_PATH         override DB location
  SCHEDULER_TIMEZONE        IANA TZ for cron expressions (default: America/Los_Angeles)
  SCHEDULER_RETENTION_DAYS  run-row retention (default: 90)
  SCHEDULER_WEBHOOK_SECRET  shared-secret for jobs declaring auth="shared_secret"
  SLACK_BOT_TOKEN           failure notifier
  SLACK_FAILURE_CHANNEL     failure notifier channel id
"""

import io
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


# Package-internal paths (templates + static served from inside the install).
_PKG_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = _PKG_DIR / "templates"
STATIC_DIR = _PKG_DIR / "static"

# Team-controlled root. Defaults to CWD so a team running `indie-scheduler-serve`
# from their box repo automatically finds jobs/, .env, and data/.
WORKING_DIR = Path(os.environ.get("SCHEDULER_WORKING_DIR", os.getcwd())).resolve()
load_dotenv(WORKING_DIR / ".env")

JOBS_DIR = WORKING_DIR / "jobs"
DATA_DIR = WORKING_DIR / "data"
DB_PATH = Path(os.environ.get("SCHEDULER_DB_PATH", str(DATA_DIR / "runs.db")))

PORT = int(os.environ.get("SCHEDULER_PORT", "3014"))
BASE_PATH = os.environ.get("SCHEDULER_BASE_PATH", "").rstrip("/")


def _derive_team() -> tuple[str, str]:
    """Derive (team_slug, team_home_url) from BASE_PATH.

    BASE_PATH "/marketing/scheduler" -> ("marketing", "/marketing/").
    BASE_PATH "/operations/scheduler" -> ("operations", "/operations/").
    BASE_PATH "" -> ("", "/").

    Override either via SCHEDULER_TEAM_NAME / SCHEDULER_TEAM_HOME.
    """
    parts = [p for p in BASE_PATH.split("/") if p]
    slug = parts[0] if parts else ""
    home = f"/{slug}/" if slug else "/"
    return slug, home


_default_slug, _default_home = _derive_team()
TEAM_NAME = os.environ.get("SCHEDULER_TEAM_NAME", _default_slug or "Operations")
TEAM_HOME_URL = os.environ.get("SCHEDULER_TEAM_HOME", _default_home)
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
SLACK_FAILURE_CHANNEL = os.environ.get("SLACK_FAILURE_CHANNEL", "").strip()

# All cron expressions in JOB dicts are interpreted in this timezone.
# AWS hosts default to UTC; jobs are typically authored in local time.
TIMEZONE = ZoneInfo(os.environ.get("SCHEDULER_TIMEZONE", "America/Los_Angeles"))

# Run rows older than this are purged nightly.
RETENTION_DAYS = int(os.environ.get("SCHEDULER_RETENTION_DAYS", "90"))

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
