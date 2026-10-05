"""Durable source classification, independent of application preparation opt-in.

This module intentionally uses only the standard library: Phase 1 runs in its
own environment and must record indirect URLs before email marks them seen.
"""
from __future__ import annotations

import json
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS application_sources (
 source_job_hash TEXT PRIMARY KEY,
 job_json TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'queued',
 board TEXT,
 application_url TEXT,
 evidence_path TEXT,
 lease_until INTEGER,
 available_at INTEGER NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0,
 notified_at INTEGER,
 updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_application_sources_ready
 ON application_sources(state, available_at, updated_at);
"""
STATES = {"queued", "running", "retry", "resolved", "waiting_login", "waiting_captcha", "unknown", "failed", "filtered"}


def _eligibility(row):
    from ..eligibility import preliminary, POLICY_ID
    findings = preliminary(row)
    if findings:
        return {"policy": POLICY_ID, "state": "skipped", "findings": findings,
                "reason": "Mandatory employment eligibility conflicts with candidate policy"}
    return None


def filter_ineligible(conn, *, limit=1000):
    """Retire old source backlog before any browser; preserve its audit history."""
    initialize(conn)
    now, changed = int(time.time()), 0
    rows = conn.execute("SELECT source_job_hash,job_json FROM application_sources WHERE state<>'filtered' "
                        "AND (state<>'running' OR lease_until IS NULL OR lease_until<=?) "
                        "ORDER BY updated_at LIMIT ?", (now, limit)).fetchall()
    for item in rows:
        row = json.loads(item["job_json"])
        if result := _eligibility(row):
            row["eligibility"] = result
            changed += conn.execute("UPDATE application_sources SET state='filtered',job_json=?,lease_until=NULL,"
                                    "available_at=0,updated_at=? WHERE source_job_hash=?",
                                    (json.dumps(row), now, item["source_job_hash"])).rowcount
    conn.commit()
    return changed


def initialize(conn):
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(application_sources)")}
    if "notified_at" not in columns:
        conn.execute("ALTER TABLE application_sources ADD COLUMN notified_at INTEGER")
        conn.commit()


def enqueue(conn, jobs) -> int:
    initialize(conn)
    inserted = 0
    for job in jobs:
        row = dict(job) if isinstance(job, dict) or hasattr(job, "keys") else {
            "dedupe_hash": job.dedupe_hash, "source": job.source,
            "company": job.company, "title": job.title, "url": job.url,
            "role_classes": ",".join(job.role_classes), "raw": getattr(job, "raw", {}),
        }
        result = _eligibility(row)
        if result:
            row["eligibility"] = result
        inserted += conn.execute(
            "INSERT OR IGNORE INTO application_sources(source_job_hash,job_json,state,updated_at) VALUES(?,?,?,?)",
            (row["dedupe_hash"], json.dumps(row), "filtered" if result else "queued", int(time.time())),
        ).rowcount
    conn.commit()
    return inserted


def claim(conn, *, lease_seconds=300, max_attempts=3):
    initialize(conn)
    filter_ineligible(conn)
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE application_sources SET state='failed',lease_until=NULL,updated_at=? "
                     "WHERE state='running' AND (lease_until IS NULL OR lease_until <= ?) AND attempts >= ?", (now, now, max_attempts))
        row = conn.execute(
            "SELECT * FROM application_sources WHERE attempts < ? AND "
            "((state IN ('queued','retry') AND available_at <= ?) OR "
            "(state='running' AND (lease_until IS NULL OR lease_until <= ?))) ORDER BY updated_at,source_job_hash LIMIT 1",
            (max_attempts, now, now),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute("UPDATE application_sources SET state='running',lease_until=?,attempts=attempts+1,updated_at=? "
                     "WHERE source_job_hash=?", (now + lease_seconds, now, row["source_job_hash"]))
        conn.commit()
        return {**dict(row), "attempts": row["attempts"] + 1, "job": json.loads(row["job_json"])}
    except BaseException:
        conn.rollback()
        raise


def finish(conn, source_job_hash, state, *, board=None, application_url=None, evidence_path=None,
           retry_seconds=60, eligibility=None):
    if state not in STATES - {"queued", "running"}:
        raise ValueError("Invalid source classification state")
    now = int(time.time())
    if eligibility is not None and state == "filtered":
        row = conn.execute("SELECT job_json FROM application_sources WHERE source_job_hash=?", (source_job_hash,)).fetchone()
        if row:
            job = {**json.loads(row["job_json"]), "eligibility": eligibility}
            conn.execute("UPDATE application_sources SET job_json=? WHERE source_job_hash=?", (json.dumps(job), source_job_hash))
    conn.execute("UPDATE application_sources SET state=?,board=?,application_url=?,evidence_path=?,lease_until=NULL,"
                 "available_at=?,updated_at=?,notified_at=NULL WHERE source_job_hash=?",
                 (state, board, application_url, str(evidence_path) if evidence_path else None,
                  now + retry_seconds if state == "retry" else 0, now, source_job_hash))
    conn.commit()


def resume(conn, source_job_hash):
    """Explicitly retry a verification/unknown/error handoff, preserving identity."""
    conn.execute("UPDATE application_sources SET state='queued',lease_until=NULL,attempts=0,available_at=0,notified_at=NULL,updated_at=? "
                 "WHERE source_job_hash=? AND state NOT IN ('running','resolved','filtered')", (int(time.time()), source_job_hash))
    conn.commit()


def defer_capacity(conn, item, *, evidence_path=None, retry_seconds=60):
    """A full local browser is not a failed public-source inspection."""
    if type(retry_seconds) is not int or not 30 <= retry_seconds <= 600:
        raise ValueError("Browser capacity delay must be between 30 and 600 seconds")
    now = int(time.time())
    changed = conn.execute(
        "UPDATE application_sources SET state='retry',available_at=?,lease_until=NULL,"
        "attempts=attempts-1,updated_at=?,evidence_path=COALESCE(?,evidence_path) "
        "WHERE source_job_hash=? AND state='running' AND attempts=? AND attempts>0",
        (now+retry_seconds, now, str(evidence_path) if evidence_path else None,
         item["source_job_hash"], item["attempts"]),
    ).rowcount
    conn.commit()
    return bool(changed)
