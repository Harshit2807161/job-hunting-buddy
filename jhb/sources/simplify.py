"""SimplifyJobs/New-Grad-Positions poller.

Layered polling per RESEARCH.md:
  1. Atom feed on /commits/dev.atom -- cheap check for a data commit.
  2. Conditional GET with If-None-Match on the 14 MB listings.json.
  3. Set-diff by `id` (stable UUID, never reused).

Step 2 is what makes a 15-minute cadence free: an unchanged feed returns 304
with no body, so we do not re-download 14 MB 96 times a day.
"""

from __future__ import annotations

import json

import requests

from .. import config
from ..eligibility import preliminary
from ..matching import classify_title, listing_in_us
from ..store import Job

UA = "job-hunting-buddy/0.1 (personal job search tool)"


def fetch_listings(etag: str | None) -> tuple[list[dict] | None, str | None, str]:
    """Return (listings, new_etag, status). listings is None when unchanged."""
    headers = {"User-Agent": UA}
    if etag:
        headers["If-None-Match"] = etag
    r = requests.get(config.SIMPLIFY_JSON_URL, headers=headers, timeout=60)
    if r.status_code == 304:
        return None, etag, "304-unchanged"
    r.raise_for_status()
    return r.json(), r.headers.get("ETag"), f"200-{len(r.content)//1024}kb"


def to_jobs(listings: list[dict]) -> list[Job]:
    """Apply Stage 03 filters and convert survivors into ledger rows."""
    out: list[Job] = []
    for r in listings:
        if not (r.get("active") and r.get("is_visible")):
            continue
        if r.get("sponsorship") in config.EXCLUDE_SPONSORSHIP:
            continue
        if config.REQUIRE_US and not listing_in_us(r.get("locations")):
            continue
        if preliminary({"title": r.get("title", ""), "raw": r}):
            continue
        m = classify_title(r.get("title", ""))
        if not m.any:
            continue
        company = (r.get("company_name") or "").strip()
        if _blocked(company):
            continue
        out.append(Job(
            source="simplify",
            source_id=r.get("id", ""),
            company=company,
            title=(r.get("title") or "").strip(),
            url=r.get("url") or "",
            locations=r.get("locations") or [],
            role_classes=m.classes,
            date_posted=r.get("date_posted"),
            raw={k: r.get(k) for k in ("category", "degrees", "sponsorship", "source")},
        ))
    return out


def _blocked(company: str) -> bool:
    norm = "".join(ch for ch in company.lower() if ch.isalnum())
    return any(b.replace(" ", "") in norm for b in config.COMPANY_BLOCKLIST if b)


def poll(conn) -> tuple[list[Job], str]:
    """Fetch + filter. Returns (matching_jobs, status)."""
    from ..store import get_meta, set_meta
    etag = get_meta(conn, "simplify_etag")
    listings, new_etag, status = fetch_listings(etag)
    if listings is None:
        return [], status
    if new_etag:
        set_meta(conn, "simplify_etag", new_etag)
    return to_jobs(listings), status
