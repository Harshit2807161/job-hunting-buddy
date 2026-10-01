"""Configuration: filters, credentials, paths.

Secrets come from a gitignored .env and are never logged. This file holds the
*filter* definition -- the thing RESEARCH.md calls out as "Phase 1 is largely
this filter."
"""

from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "jobs.sqlite3"

# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
SIMPLIFY_JSON_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions"
    "/dev/.github/scripts/listings.json"
)

# JobSpy runs far less often than Simplify: it is slow, rate-limited, and only
# 14% of its rows carry a direct employer ATS link.
JOBSPY_SITES = ["indeed", "linkedin"]
JOBSPY_TERMS = [
    "new grad software engineer",
    "entry level software engineer",
    "software engineer I",
    "associate software engineer",
    "machine learning engineer new grad",
    "AI engineer entry level",
    "data scientist new grad",
    "applied scientist",
]
JOBSPY_RESULTS_PER_TERM = 50
JOBSPY_HOURS_OLD = 72
JOBSPY_MIN_INTERVAL_SEC = 1 * 3600  # at most every 1h

# Staffing agencies, body shops and aggregators reposting other people's roles.
# Measured from a live JobSpy run -- these dominated the "not in Simplify" tail.
COMPANY_BLOCKLIST = {
    "emonics", "rkinfotech", "beaconfire", "jobrightai", "jobright",
    "diverselynx", "diverse lynx", "sunplussoftwaresolutions", "intellectt",
    "tekwissen", "mindlance", "collabera", "insightglobal", "teksystems",
    "randstad", "roberthalf", "apexsystems", "motionrecruitment", "cybercoders",
    "jobot", "dice", "ziprecruiter", "lensa", "talentify", "getitrecruit",
    "getit", "energyjobline", "clearancejobs", "hireio",
    "crossover", "turing", "andela", "toptal", "upwork",
}

# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------
REQUIRE_US = True
# `sponsorship` is 3030/3040 "Other" in the live feed, so this excludes ~8 rows.
# Kept because the cost is zero and a citizenship-required role is a hard no.
EXCLUDE_SPONSORSHIP = {"U.S. Citizenship is Required", "Does Not Offer Sponsorship"}

# --------------------------------------------------------------------------
# Notification
# --------------------------------------------------------------------------
EMAIL_TO = os.environ.get("JHB_EMAIL_TO", "dhankharharshit@gmail.com")
SMTP_HOST = os.environ.get("JHB_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("JHB_SMTP_PORT", "465"))
SMTP_USER = os.environ.get("JHB_SMTP_USER", "")
SMTP_PASS = os.environ.get("JHB_SMTP_PASS", "")

# Safety rail: if a cycle would notify more than this, treat it as a bad diff
# (feed reshuffle, schema change) and send one summary instead of N emails.
BURST_THRESHOLD = 25


def load_dotenv(path: pathlib.Path | None = None) -> None:
    """Minimal .env loader -- avoids a python-dotenv dependency."""
    p = path or (ROOT / ".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def refresh_from_env() -> None:
    """Re-read module-level settings after load_dotenv()."""
    global EMAIL_TO, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
    EMAIL_TO = os.environ.get("JHB_EMAIL_TO", EMAIL_TO)
    SMTP_HOST = os.environ.get("JHB_SMTP_HOST", SMTP_HOST)
    SMTP_PORT = int(os.environ.get("JHB_SMTP_PORT", str(SMTP_PORT)))
    SMTP_USER = os.environ.get("JHB_SMTP_USER", SMTP_USER)
    SMTP_PASS = os.environ.get("JHB_SMTP_PASS", SMTP_PASS)
