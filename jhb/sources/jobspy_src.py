"""JobSpy poller -- secondary source.

Measured characteristics (live run, 2026-09-14):
  indeed        400 rows  ~2s/query    works
  linkedin      400 rows  ~22s/query   works, slow
  google          0 rows               returns nothing
  zip_recruiter   0 rows  HTTP 403     blocked

41% of raw rows survive the new-grad title filter; only 14% carry a direct
employer ATS link. Hence: secondary, rate-limited, blocklist-filtered.
"""

from __future__ import annotations

import logging
import re
import time

from .. import config
from ..matching import classify_title, is_us_location
from ..store import Job

log = logging.getLogger(__name__)



def _norm_co(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|company|co|group|technologies"
               r"|technology|labs|systems|usa|the)\b", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def _blocked(company: str) -> bool:
    n = _norm_co(company)
    if not n or n == "nan":
        return True
    return any(b.replace(" ", "") in n for b in config.COMPANY_BLOCKLIST if b)


def poll(conn) -> tuple[list[Job], str]:
    """Scrape Indeed + LinkedIn. Rate-limited to once per JOBSPY_MIN_INTERVAL_SEC."""
    from ..store import get_meta, set_meta

    last = int(get_meta(conn, "jobspy_last_run", "0") or 0)
    now = int(time.time())
    if now - last < config.JOBSPY_MIN_INTERVAL_SEC:
        mins = (config.JOBSPY_MIN_INTERVAL_SEC - (now - last)) // 60
        return [], f"skipped-rate-limit({mins}m)"

    try:
        from jobspy import scrape_jobs
    except ImportError:
        return [], "skipped-not-installed"

    rows, ok, fail = [], 0, 0
    for site in config.JOBSPY_SITES:
        for term in config.JOBSPY_TERMS:
            try:
                df = scrape_jobs(
                    site_name=[site], search_term=term, location="United States",
                    results_wanted=config.JOBSPY_RESULTS_PER_TERM,
                    hours_old=config.JOBSPY_HOURS_OLD,
                    country_indeed="usa", verbose=0,
                )
                ok += 1
                for _, r in df.iterrows():
                    rows.append({
                        "site": site,
                        "id": str(r.get("id") or ""),
                        "title": str(r.get("title") or "").strip(),
                        "company": str(r.get("company") or "").strip(),
                        "location": str(r.get("location") or "").strip(),
                        "url": str(r.get("job_url_direct") or r.get("job_url") or ""),
                        "date_posted": str(r.get("date_posted") or ""),
                    })
            except Exception as e:
                fail += 1
                log.warning("jobspy %s/%s failed: %s", site, term, e)

    set_meta(conn, "jobspy_last_run", str(now))

    out, seen = [], set()
    for r in rows:
        if _blocked(r["company"]):
            continue
        m = classify_title(r["title"])
        if not m.any:
            continue
        # Indeed returns "Austin, TX, US"; LinkedIn returns "Austin, TX" or
        # "New York, United States". is_us_location() handles all three.
        # None = unrecognised/blank -> keep, since the query was US-scoped.
        loc = r["location"]
        if loc and is_us_location(loc) is False:
            continue
        key = (_norm_co(r["company"]), r["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        ts = None
        if r["date_posted"]:
            try:
                ts = int(time.mktime(time.strptime(r["date_posted"][:10], "%Y-%m-%d")))
            except Exception:
                pass
        out.append(Job(
            source=f"jobspy:{r['site']}", source_id=r["id"], company=r["company"],
            title=r["title"], url=r["url"], locations=[loc] if loc else [],
            role_classes=m.classes, date_posted=ts,
            raw={"site": r["site"]},
        ))
    return out, f"queries ok={ok} fail={fail} raw={len(rows)} kept={len(out)}"
