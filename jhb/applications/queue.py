"""Idempotent preparation queue with atomic claims and bounded crash recovery."""
from __future__ import annotations

import json
import time
from . import boards
from .boards import greenhouse_identity  # Public compatibility API for existing Greenhouse clients.

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
 job_hash TEXT PRIMARY KEY,
 job_json TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'queued',
 lease_until INTEGER,
 attempts INTEGER NOT NULL DEFAULT 0,
 available_at INTEGER NOT NULL DEFAULT 0,
 error_kind TEXT,
 updated_at INTEGER NOT NULL,
 packet TEXT,
 notified_at INTEGER
);
"""
STATES = {"queued", "running", "retry", "waiting_review", "submission_uncertain", "waiting_input", "waiting_login",
          "waiting_captcha", "unsupported", "failed", "submitted", "skipped"}


def is_greenhouse(url: str) -> bool:
    return greenhouse_identity(url) is not None




def initialize(conn):
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(applications)")}
    for name, declaration in [("available_at", "INTEGER NOT NULL DEFAULT 0"), ("error_kind", "TEXT")]:
        if name not in columns:
            conn.execute(f"ALTER TABLE applications ADD COLUMN {name} {declaration}")
    conn.commit()


def enqueue(conn, jobs) -> int:
    initialize(conn)
    count = 0
    for job in jobs:
        row = job if isinstance(job, dict) else {
            "dedupe_hash": job.dedupe_hash, "source": job.source, "company": job.company,
            "title": job.title, "url": job.url, "role_classes": ",".join(job.role_classes)}
        url = row.get("url", "")
        identity = boards.job_identity(url)
        routed_board = boards.route_board(url, row.get("board_type"))
        if identity is None or not boards.preparation_supported(routed_board):
            continue
        application_hash = boards.application_hash(url)
        row = {**row, "source_job_hash": row.get("source_job_hash", row["dedupe_hash"]),
               "dedupe_hash": application_hash, "board_type": routed_board, "job_identity": list(identity),
               "adapter_skill": boards.adapter(routed_board)["skill"]}
        from ..eligibility import preliminary, POLICY_ID
        findings = preliminary(row)
        if findings:
            row["eligibility"] = {"policy": POLICY_ID, "findings": findings, "state": "skipped"}
        count += conn.execute("INSERT OR IGNORE INTO applications(job_hash,job_json,state,updated_at) VALUES(?,?,?,?)",
                              (row["dedupe_hash"], json.dumps(row), "skipped" if findings else "queued", int(time.time()))).rowcount
    conn.commit()
    return count


def claim(conn, lease_seconds=1200, *, max_attempts=3):
    initialize(conn)
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE applications SET state='failed',lease_until=NULL,updated_at=? "
                     "WHERE state='running' AND (lease_until IS NULL OR lease_until <= ?) AND attempts >= ?",
                     (now, now, max_attempts))
        row = conn.execute("SELECT * FROM applications WHERE ((state IN ('queued','retry') AND available_at <= ?) OR "
                           "(state='running' AND (lease_until IS NULL OR lease_until <= ?))) AND attempts < ? "
                           "ORDER BY updated_at,job_hash LIMIT 1", (now, now, max_attempts)).fetchone()
        if not row:
            conn.commit()
            return None
        conn.execute("UPDATE applications SET state='running',lease_until=?,attempts=attempts+1,updated_at=? WHERE job_hash=?",
                     (now + lease_seconds, now, row["job_hash"]))
        conn.commit()
        return {**dict(row), "attempts": row["attempts"]+1, "job": json.loads(row["job_json"])}
    except BaseException:
        conn.rollback()
        raise


def finish(conn, job_hash, state, packet=None):
    if state not in STATES - {"queued", "running", "retry"}:
        raise ValueError("Invalid terminal/handoff state")
    # A preparation worker can finish from an older snapshot after an
    # interactive submission was confirmed. Preserve that terminal record.
    conn.execute("UPDATE applications SET state=?,lease_until=NULL,available_at=0,error_kind=NULL,"
                 "updated_at=?,packet=?,notified_at=NULL "
                 "WHERE job_hash=? AND (state NOT IN ('submitted','waiting_review','submission_uncertain','skipped') OR state=? "
                 "OR ?='submitted' OR (state='waiting_review' AND ?='skipped'))",
                 (state, int(time.time()), str(packet) if packet else None, job_hash, state, state, state))
    conn.commit()


def resume(conn, job_hash):
    conn.execute("UPDATE applications SET state='queued',lease_until=NULL,attempts=0,available_at=0,error_kind=NULL,updated_at=? "
                 "WHERE job_hash=? AND state NOT IN ('running','waiting_review','submission_uncertain','submitted','skipped')", (int(time.time()), job_hash))
    conn.commit()


def retry(conn, job_hash, *, error_kind, packet=None, retry_seconds=300, max_attempts=3):
    """Retry only classified technical failures; protected handoffs never qualify."""
    now = int(time.time())
    changed = conn.execute(
        "UPDATE applications SET state='retry',available_at=?,error_kind=?,lease_until=NULL,"
        "updated_at=?,packet=COALESCE(?,packet),notified_at=NULL WHERE job_hash=? "
        "AND state IN ('running','failed') AND attempts < ?",
        (now+retry_seconds, error_kind, now, str(packet) if packet else None, job_hash, max_attempts),
    ).rowcount
    conn.commit()
    return bool(changed)
