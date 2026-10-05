"""Idempotent preparation queue with atomic claims and bounded crash recovery."""
from __future__ import annotations

import json
import time
from . import boards
from .. import config
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
          "waiting_captcha", "unsupported", "failed", "submitted", "skipped", "discarded", "history_hold"}


def _discarded(conn, job_hash):
    from .application_discard import discarded
    if discarded(config.ROOT, job_hash):
        conn.execute("UPDATE applications SET state='discarded',lease_until=NULL,available_at=0 "
                     "WHERE job_hash=? AND state NOT IN ('submitted','submission_uncertain')", (job_hash,))
        return True
    return False


def is_greenhouse(url: str) -> bool:
    return greenhouse_identity(url) is not None




def initialize(conn):
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(applications)")}
    for name, declaration in [("available_at", "INTEGER NOT NULL DEFAULT 0"), ("error_kind", "TEXT")]:
        if name not in columns:
            conn.execute(f"ALTER TABLE applications ADD COLUMN {name} {declaration}")
    conn.commit()


def _history(conn, job_hash, job, *, now=None):
    from .historical import match
    prior = match(conn, job)
    if prior:
        job = {**job, "historical_application": prior}
        conn.execute("UPDATE applications SET state=?,job_json=?,lease_until=NULL,available_at=0,"
                     "error_kind='historical_application',updated_at=? WHERE job_hash=? "
                     "AND state NOT IN ('submitted','submission_uncertain','discarded')",
                     ("skipped" if prior["disposition"] == "exclude" else "history_hold", json.dumps(job),
                      int(time.time()) if now is None else now, job_hash))
    return prior


def filter_history(conn, *, limit=1000):
    """Retire old drafts/backlog without changing receipts, packets or live tabs."""
    now, changed = int(time.time()), 0
    rows = conn.execute("SELECT job_hash,job_json FROM applications WHERE state NOT IN "
                        "('submitted','submission_uncertain','discarded','skipped','history_hold') "
                        "AND (state<>'running' OR lease_until IS NULL OR lease_until<=?) "
                        "ORDER BY updated_at LIMIT ?", (now, limit)).fetchall()
    for row in rows:
        changed += bool(_history(conn, row["job_hash"], json.loads(row["job_json"]), now=now))
    conn.commit()
    return changed


def enqueue(conn, jobs) -> int:
    initialize(conn)
    count = 0
    for job in jobs:
        row = job if isinstance(job, dict) else {
            "dedupe_hash": job.dedupe_hash, "source": job.source, "company": job.company,
            "title": job.title, "url": job.url, "role_classes": ",".join(job.role_classes),
            "raw": getattr(job, "raw", {})}
        url = row.get("url", "")
        identity = boards.job_identity(url)
        routed_board = boards.route_board(url, row.get("board_type"))
        if identity is None or not boards.preparation_supported(routed_board):
            continue
        from .tracking import confirmed_application, _restore_confirmation
        confirmation = confirmed_application(conn, url)
        if confirmation:
            _restore_confirmation(conn, confirmation)
            continue
        application_hash = boards.application_hash(url)
        if _discarded(conn, application_hash):
            continue
        row = {**row, "source_job_hash": row.get("source_job_hash", row["dedupe_hash"]),
               "dedupe_hash": application_hash, "board_type": routed_board, "job_identity": list(identity),
               "adapter_skill": boards.adapter(routed_board)["skill"]}
        from ..eligibility import preliminary, POLICY_ID
        findings = preliminary(row)
        if findings:
            row["eligibility"] = {"policy": POLICY_ID, "findings": findings, "state": "skipped"}
        from .historical import match
        prior = match(conn, row)
        if prior:
            row["historical_application"] = prior
        state = "skipped" if findings or prior and prior["disposition"] == "exclude" else "history_hold" if prior else "queued"
        count += conn.execute("INSERT OR IGNORE INTO applications(job_hash,job_json,state,updated_at) VALUES(?,?,?,?)",
                              (row["dedupe_hash"], json.dumps(row), state, int(time.time()))).rowcount
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
        # Bound archival scans; a later cycle can drain the next batch.
        for _ in range(100):
            row = conn.execute("SELECT * FROM applications WHERE ((state IN ('queued','retry') AND available_at <= ?) OR "
                               "(state='running' AND (lease_until IS NULL OR lease_until <= ?))) AND attempts < ? "
                               "ORDER BY updated_at,job_hash LIMIT 1", (now, now, max_attempts)).fetchone()
            if row is None:
                break
            from .tracking import confirmed_application, _restore_confirmation
            job = json.loads(row["job_json"])
            if _discarded(conn, row["job_hash"]):
                row = None
                continue
            confirmation = confirmed_application(conn, job.get("url"))
            if confirmation and confirmation["job"]["dedupe_hash"] == row["job_hash"]:
                _restore_confirmation(conn, confirmation)
                continue
            if _history(conn, row["job_hash"], job, now=now):
                continue
            from ..eligibility import preliminary, POLICY_ID
            findings = preliminary(job)
            if findings:
                job["eligibility"] = {"policy": POLICY_ID, "state": "skipped", "findings": findings}
                conn.execute("UPDATE applications SET state='skipped',job_json=?,lease_until=NULL,available_at=0,"
                             "updated_at=? WHERE job_hash=?", (json.dumps(job), now, row["job_hash"]))
                continue
            break
        else:
            conn.commit()
            return None
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
    if _discarded(conn, job_hash):
        conn.commit()
        return
    # A preparation worker can finish from an older snapshot after an
    # interactive submission was confirmed. Preserve that terminal record.
    conn.execute("UPDATE applications SET state=?,lease_until=NULL,available_at=0,error_kind=NULL,"
                 "updated_at=?,packet=?,notified_at=NULL "
                 "WHERE job_hash=? AND state<>'discarded' AND (state NOT IN ('submitted','waiting_review','submission_uncertain','skipped','history_hold') OR state=? "
                 "OR ?='submitted' OR (state='waiting_review' AND ?='skipped'))",
                 (state, int(time.time()), str(packet) if packet else None, job_hash, state, state, state))
    conn.commit()


def resume(conn, job_hash):
    if _discarded(conn, job_hash):
        conn.commit()
        return
    conn.execute("UPDATE applications SET state='queued',lease_until=NULL,attempts=0,available_at=0,error_kind=NULL,updated_at=? "
                 "WHERE job_hash=? AND state NOT IN ('running','waiting_review','submission_uncertain','submitted','skipped','discarded','history_hold')", (int(time.time()), job_hash))
    conn.commit()


def retry(conn, job_hash, *, error_kind, packet=None, retry_seconds=300, max_attempts=3):
    """Retry only classified technical failures; protected handoffs never qualify."""
    now = int(time.time())
    if _discarded(conn, job_hash):
        conn.commit()
        return False
    changed = conn.execute(
        "UPDATE applications SET state='retry',available_at=?,error_kind=?,lease_until=NULL,"
        "updated_at=?,packet=COALESCE(?,packet),notified_at=NULL WHERE job_hash=? "
        "AND state IN ('running','failed') AND attempts < ?",
        (now+retry_seconds, error_kind, now, str(packet) if packet else None, job_hash, max_attempts),
    ).rowcount
    conn.commit()
    return bool(changed)


def defer_capacity(conn, item, *, packet=None, retry_seconds=60):
    """Release only this untouched claim without spending a browser-failure try."""
    if type(retry_seconds) is not int or not 30 <= retry_seconds <= 600:
        raise ValueError("Browser capacity delay must be between 30 and 600 seconds")
    now = int(time.time())
    if _discarded(conn, item["job_hash"]):
        conn.commit()
        return False
    changed = conn.execute(
        "UPDATE applications SET state='retry',available_at=?,error_kind='browser_capacity',"
        "lease_until=NULL,attempts=attempts-1,updated_at=?,packet=COALESCE(?,packet) "
        "WHERE job_hash=? AND state='running' AND attempts=? AND attempts>0",
        (now+retry_seconds, now, str(packet) if packet else None, item["job_hash"], item["attempts"]),
    ).rowcount
    conn.commit()
    return bool(changed)
