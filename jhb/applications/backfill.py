"""Bounded recovery of previously notified discovery rows into source checking.

Preview is read-only. Candidates are not qualified applications: the existing
source refresh, full-description eligibility and role-fit checks still run.
"""
from __future__ import annotations

from collections import Counter
import json
import re

from .. import store
from ..eligibility import preliminary
from . import boards, booklet, historical, source_queue

SUPPORTED = {"ashby", "greenhouse"}
EARLY = re.compile(r"\b(?:new[\s-]*grad(?:uate)?s?|early[\s-]*career|emerging[\s-]*talent|"
                   r"entry[\s-]*level|junior|associate|engineer\s+(?:I|1))\b", re.I)
SENIOR = re.compile(r"\b(?:senior|sr\.?|staff|principal|lead|manager|director|head|"
                    r"intern(?:ship)?|engineer\s+(?:III|IV|[3-9]))\b", re.I)
SPECIALIST = re.compile(r"\b(?:robotics?|embedded|firmware|flight|avionics|hardware|"
                        r"autonomy|autonomous\s+(?:driving|vehicles?)|computer\s+vision|"
                        r"remote\s+sensing|database\s+research|simulation|starship|starfall|"
                        r"starshield|public\s+sector|federal)\b", re.I)
FAMILY = re.compile(r"\b(?:software|full[\s-]*stack|front[\s-]*end|back[\s-]*end|"
                    r"product\s+engineer|data\s+(?:scientist|engineer)|"
                    r"(?:machine[\s-]*learning|ML|AI)\s+(?:engineer|scientist)|"
                    r"applied\s+scientist)\b", re.I)


def _school_only(title):
    # This delivery batch favors general openings over named-school campaigns;
    # omission is not a permanent eligibility decision or candidate handoff.
    match = re.search(r"[-–—]\s+(.{1,80}?)\s+only\s*$", title, re.I)
    return bool(match and match[1].casefold() not in {
        "remote", "hybrid", "onsite", "on-site", "us", "usa", "united states"})


def _identities(conn, table, columns):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
        return set()
    identities = set()
    for row in conn.execute(f"SELECT {','.join(columns)} FROM {table}"):
        for key, value in zip(columns, row):
            if key == "job_json":
                try:
                    value = json.loads(value).get("url")
                except (ValueError, TypeError, AttributeError):
                    continue
            if identity := boards.job_identity(value):
                identities.add(identity)
    return identities


def select(conn, book, *, limit=30):
    """Read and rank exact supported seed jobs without changing any ledger."""
    if type(limit) is not int or not 1 <= limit <= 30:
        raise ValueError("Backfill limit must be between 1 and 30")
    if not historical.cached_ready(conn):
        return {"state": "history_required", "candidates": [], "selected": 0,
                "reason": "Import the configured application history before backfill"}, []
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "jobs" not in tables:
        return {"state": "preview", "candidates": [], "selected": 0, "available": 0}, []
    existing = _identities(conn, "applications", ["job_json"])
    existing |= _identities(conn, "application_sources", ["job_json", "application_url"])
    protected_hashes = set()
    for table in ("applications", "authorized_submission_attempts"):
        if table in tables:
            protected_hashes.update(row[0] for row in conn.execute(f"SELECT job_hash FROM {table}"))
    existing_roles = set()
    for table in ("applications", "confirmed_submissions"):
        if table not in tables:
            continue
        for row in conn.execute(f"SELECT job_json FROM {table}"):
            try:
                job = json.loads(row[0])
                if job.get("company") and job.get("title"):
                    existing_roles.add(store.role_key(job["company"], job["title"]))
            except (ValueError, TypeError, AttributeError):
                continue
    queued_sources = ({row[0] for row in conn.execute("SELECT source_job_hash FROM application_sources")}
                      if "application_sources" in tables else set())
    reasons, candidates = Counter(), {}
    for row in conn.execute("SELECT * FROM jobs WHERE notified_at IS NOT NULL"):
        job = dict(row)
        identity = boards.job_identity(job.get("url"))
        if (identity is None or identity[0] not in SUPPORTED
                or not boards.preparation_supported(identity[0]) or not boards.submission_supported(identity[0])):
            reasons["unsupported_identity"] += 1
            continue
        if identity in existing or job["dedupe_hash"] in queued_sources:
            reasons["already_routed"] += 1
            continue
        application_hash = boards.application_hash(job["url"])
        if application_hash in protected_hashes:
            reasons["already_routed"] += 1
            continue
        if store.role_key(job["company"], job["title"]) in existing_roles:
            # Different requisition IDs remain distinct globally. This bounded
            # optional backfill prioritizes other roles over possible reposts.
            reasons["same_role_already_tracked"] += 1
            continue
        if booklet.job_excluded(book, {**job, "dedupe_hash": application_hash}):
            reasons["candidate_excluded"] += 1
            continue
        if preliminary(job):
            reasons["preliminary_requirement"] += 1
            continue
        if historical.match(conn, job):
            reasons["application_history"] += 1
            continue
        title = str(job.get("title") or "")
        if SENIOR.search(title) or SPECIALIST.search(title) or _school_only(title) or not FAMILY.search(title):
            reasons["outside_backfill_focus"] += 1
            continue
        rank = (0 if EARLY.search(title) else 1, -int(job.get("date_posted") or job["first_seen"]),
                job["company"].casefold(), title.casefold(), job["dedupe_hash"])
        job["backfill"] = {"source": "previously_notified_discovery", "eligibility_verified": False,
                           "requires": ["fresh_official_description", "full_description_eligibility", "role_fit"]}
        previous = candidates.get(identity)
        if previous is None or rank < previous[0]:
            candidates[identity] = (rank, job)
    ranked = sorted(candidates.values(), key=lambda item: item[0])
    selected = [job for _, job in ranked[:limit]]
    return {"state": "preview", "selected": len(selected), "available": len(ranked),
            "excluded": dict(reasons), "candidates": [
                {"source_job_hash": job["dedupe_hash"], "application_hash": boards.application_hash(job["url"]),
                 "company": job["company"], "title": job["title"], "url": job["url"],
                 "locations": job.get("locations"), "board": boards.board_type(job["url"]),
                 "priority": "early_career" if EARLY.search(job["title"]) else "general_software_ml",
                 "eligibility_verified": False} for job in selected]}, selected


def enqueue(conn, book, *, limit=30):
    """Explicit operator action, always reselecting against current local history."""
    preview, selected = select(conn, book, limit=limit)
    if preview["state"] != "preview":
        return {**preview, "enqueued": 0}
    count = source_queue.enqueue(conn, selected) if selected else 0
    return {**preview, "state": "enqueued", "enqueued": count,
            "reason": "Queued for source and eligibility checking; no application was filled or submitted"}


def replenish(conn, book, *, limit=3, max_pending=6):
    """Recover absent source rows during an authorized preparation cycle.

    Phase 1's seed/notified ledger must stay unchanged. A small outstanding
    allowance prevents the old discovery backlog from flooding source checking;
    existing source rows, including exhausted retries, are never reopened.
    """
    if type(limit) is not int or not 1 <= limit <= 3:
        raise ValueError("Scheduled backfill limit must be between 1 and 3")
    if type(max_pending) is not int or not 1 <= max_pending <= 6:
        raise ValueError("Scheduled backfill pending limit must be between 1 and 6")
    pending = 0
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='application_sources'").fetchone():
        for row in conn.execute("SELECT job_json FROM application_sources WHERE state IN ('queued','retry','running')"):
            try:
                marker = json.loads(row[0]).get("backfill", {})
                pending += isinstance(marker, dict) and marker.get("source") == "previously_notified_discovery"
            except (ValueError, TypeError, AttributeError):
                continue
    allowance = min(limit, max(0, max_pending - pending))
    if not allowance:
        return {"state": "pending_capacity", "enqueued": 0, "pending": pending}
    result = enqueue(conn, book, limit=allowance)
    # Keep cycle heartbeats small; the source ledger retains each original job
    # and unverified backfill marker for the normal resolver/worker checks.
    return {key: result[key] for key in ("state", "enqueued", "selected", "available") if key in result} | {
        "pending": pending + result["enqueued"]}
