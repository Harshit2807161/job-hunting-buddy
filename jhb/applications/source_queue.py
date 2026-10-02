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
STATES = {"queued", "running", "retry", "resolved", "waiting_login", "waiting_captcha", "unknown", "failed"}


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
            "role_classes": ",".join(job.role_classes),
        }
        inserted += conn.execute(
            "INSERT OR IGNORE INTO application_sources(source_job_hash,job_json,updated_at) VALUES(?,?,?)",
            (row["dedupe_hash"], json.dumps(row), int(time.time())),
        ).rowcount
    conn.commit()
    return inserted


def claim(conn, *, lease_seconds=300, max_attempts=3):
    initialize(conn)
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE application_sources SET state='failed',lease_until=NULL,updated_at=? "
                     "WHERE state='running' AND lease_until < ? AND attempts >= ?", (now, now, max_attempts))
        row = conn.execute(
            "SELECT * FROM application_sources WHERE attempts < ? AND "
            "((state IN ('queued','retry') AND available_at <= ?) OR "
            "(state='running' AND lease_until < ?)) ORDER BY updated_at,source_job_hash LIMIT 1",
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
           retry_seconds=60):
    if state not in STATES - {"queued", "running"}:
        raise ValueError("Invalid source classification state")
    now = int(time.time())
    conn.execute("UPDATE application_sources SET state=?,board=?,application_url=?,evidence_path=?,lease_until=NULL,"
                 "available_at=?,updated_at=?,notified_at=NULL WHERE source_job_hash=?",
                 (state, board, application_url, str(evidence_path) if evidence_path else None,
                  now + retry_seconds if state == "retry" else 0, now, source_job_hash))
    conn.commit()


def resume(conn, source_job_hash):
    """Explicitly retry a verification/unknown/error handoff, preserving identity."""
    conn.execute("UPDATE application_sources SET state='queued',lease_until=NULL,attempts=0,available_at=0,notified_at=NULL,updated_at=? "
                 "WHERE source_job_hash=? AND state NOT IN ('running','resolved')", (int(time.time()), source_job_hash))
    conn.commit()
