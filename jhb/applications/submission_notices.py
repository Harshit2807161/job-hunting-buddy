"""One durable outcome-review handoff for a possibly clicked submission.

This only reports an existing uncertain attempt; it never reconciles evidence,
changes application state, retries a submission or infers a candidate answer.
"""
from __future__ import annotations

import hashlib
import json
import re

from .. import config, notify
from . import booklet, notices, overnight, queue

CATEGORY = "submission_outcome_review"


def _pending(conn):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"applications", "authorized_submission_attempts"} <= tables:
        return {}
    confirmed = set()
    if "confirmed_submissions" in tables:
        confirmed = {queue.greenhouse_identity(row[0]) for row in
                     conn.execute("SELECT application_url FROM confirmed_submissions")}
    records = {}
    for row in conn.execute("SELECT t.job_hash,t.authorization_id,t.application_url,t.attempt_path,a.job_json "
                            "FROM authorized_submission_attempts t JOIN applications a ON a.job_hash=t.job_hash "
                            "WHERE t.state='uncertain' AND a.state='submission_uncertain'"):
        job_hash, authorization_id, url, attempt_path, job_json = row
        if not all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)
                   for value in (job_hash, authorization_id)):
            continue
        identity = queue.greenhouse_identity(url)
        if identity is None or identity in confirmed:
            continue
        try:
            path, attempt, _ = overnight._read_private(attempt_path)
            job = json.loads(job_json)
            if (not isinstance(job, dict) or attempt.get("runtime_click_started") is not True
                    or attempt.get("job_hash") != job_hash or attempt.get("authorization_id") != authorization_id
                    or queue.greenhouse_identity(attempt.get("application_url")) != identity
                    or queue.greenhouse_identity(job.get("url")) != identity):
                continue
        except (OSError, ValueError, TypeError):
            continue
        key = f"submission-uncertain:{job_hash}:{authorization_id}"
        # Select only posting metadata; never send packet fields, answers or
        # raw exception/page content. Message identity is scoped to this event.
        public = {"title": str(job.get("title") or "Application")[:200],
                  "company": str(job.get("company") or "Employer")[:200], "url": job["url"],
                  "dedupe_hash": hashlib.sha256(key.encode()).hexdigest()}
        records[key] = {"job_hash": job_hash, "authorization_id": authorization_id,
                        "state": "submission_uncertain", "confirmed_submitted": False,
                        "job": public, "attempt_path": str(path),
                        "checks_path": str(path.parent / "checks.json")}
    return records


def notify_uncertain(conn, *, send_email=False):
    """Persist local diagnostics; email an unresolved outcome at most once.

    Authorization expiry does not erase an existing outcome requiring review.
    Local-only checks leave the durable email delivery ledger untouched.
    """
    records = _pending(conn)
    for key, record in records.items():
        filename = "submission-uncertain-" + hashlib.sha256(key.encode()).hexdigest() + ".json"
        booklet.write_private(config.ROOT / "private" / "notifications" / filename, record)
    if not records or not send_email:
        return 0
    notices.initialize(conn)
    previous = {row[0] for row in conn.execute(
        "SELECT notice_key FROM application_notice_delivery WHERE category=? AND delivered_at IS NOT NULL", (CATEGORY,))}

    def send(keys):
        # Another controller may have reconciled a receipt since selection.
        active = _pending(conn)
        current = [active[key] for key in sorted(keys) if key in active]
        if not current:
            return True  # Resolved events need no further delivery attempt.
        details = ["Submission outcome requires review. No validated submission receipt is recorded.",
                   "Do not retry or resubmit these applications until their outcomes have been checked."]
        for record in current:
            details.extend(["", record["job"]["url"], "Private checks: " + record["checks_path"],
                            "Private attempt: " + record["attempt_path"]])
        return notify.send([record["job"] for record in current],
                           subject_prefix="[Submission outcome review] ", details="\n".join(details))

    delivered = notices.deliver(conn, records, CATEGORY, send)
    return len(delivered - previous)
