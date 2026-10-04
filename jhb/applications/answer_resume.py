"""Recover scoped saved answers after a crash before the SQLite queue update.

The booklet's explicit response is the durable outbox. Recovery only requeues
an unchanged waiting_input packet whose entire required handoff has exact,
current answered ledger proof. It grants no submission authority.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import boards, booklet, questions


MAX_PACKET_BYTES = 2 * 1024 * 1024


def _country(value):
    return booklet.normalize(str(value)) if value is not None else None


def _response(book, record, job_hash):
    revision = record.get("answer_revision")
    if not isinstance(revision, str) or not revision or record.get("status") != "answered":
        return False
    if record.get("kind") == "role":
        item = book.get("job_role_answers", {}).get(job_hash, {})
        if item.get("value") not in {"sde", "ml"}:
            return False
    else:
        item = book.get("custom_answers", {}).get(record.get("custom_answer_key"), {})
        if not item:
            item = book.get("answers", {}).get(record.get("answer_key"), {})
    source = item.get("source", {})
    return (item.get("status") == "verified" and item.get("value") is not None
            and isinstance(source, dict) and source.get("provider") == "explicit user question response"
            and source.get("question_id") == record.get("id")
            and source.get("answered_at") == revision and source.get("scope") == record.get("scope")
            and job_hash in source.get("contexts", []))


def eligible(book, job, packet):
    """All required questions must match current explicit answer revisions."""
    job_hash = job.get("dedupe_hash")
    if (not isinstance(job_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", job_hash)
            or packet.get("state") != "waiting_input" or packet.get("submitted") is not False
            or packet.get("job", {}).get("dedupe_hash") != job_hash
            or boards.job_identity(packet.get("job", {}).get("url")) != boards.job_identity(job.get("url"))
            or boards.application_hash(job.get("url")) != job_hash):
        return False
    if booklet.job_excluded(book, job):
        return False
    missing = [item for item in packet.get("missing", []) if isinstance(item, dict) and item.get("required", True)]
    if not missing:
        return False  # technical/source/role handoffs without an exact ledger gap are not user answers
    try:
        scope = questions._scope(job)
    except (ValueError, TypeError):
        return False
    ledger = [record for record in book.get("question_handoffs", {}).values()
              if isinstance(record, dict) and record.get("scope") == scope and job_hash in record.get("contexts", {})]
    if any(record.get("status") == "pending" and record["contexts"][job_hash].get("required")
           and not record["contexts"][job_hash].get("resolved") for record in ledger):
        return False
    for item in missing:
        if not isinstance(item.get("question"), str) or questions._SECRET.search(item["question"]):
            return False
        matches = [record for record in ledger
                   if record.get("normalized_question") == booklet.normalize(item["question"])
                   and _country(record.get("country_context")) == _country(item.get("country_context"))
                   and record["contexts"][job_hash].get("ref") == item.get("ref")
                   and record["contexts"][job_hash].get("required") is True]
        if len(matches) != 1 or not _response(book, matches[0], job_hash):
            return False
    return True


def _active_authority(conn, job_hash):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "application_approvals" in tables and conn.execute(
            "SELECT 1 FROM application_approvals WHERE job_hash=? AND state IN ('approved','submitting')", (job_hash,)).fetchone():
        return True
    if "authorized_submission_attempts" in tables and conn.execute(
            "SELECT 1 FROM authorized_submission_attempts WHERE job_hash=? AND state IN ('in_progress','uncertain','submitted')", (job_hash,)).fetchone():
        return True
    return False


def recover(conn, book_path=booklet.DEFAULT_PATH, *, limit=100):
    """Idempotent, bounded restart reconciliation, serialized with answer writes.

    Only waiting_input can become queued. Reviewed, running, submitted,
    uncertain and active-authority records stay protected; no old approval is
    renewed. A later pass cannot reset retries again once the job is queued.
    """
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Answer recovery limit must be between 1 and 200")
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='applications'").fetchone():
        return 0
    from .authorized_submission import private_file
    recovered = 0
    with questions._locked(book_path):
        book = booklet.load(book_path)
        rows = conn.execute("SELECT job_hash,job_json,packet FROM applications WHERE state='waiting_input' "
                            "AND packet IS NOT NULL ORDER BY updated_at,job_hash LIMIT ?", (limit,)).fetchall()
        for row in rows:
            if _active_authority(conn, row["job_hash"]):
                continue
            try:
                path = private_file(Path(row["packet"]).with_name("packet.json"))
                if not path.is_file() or path.stat().st_size > MAX_PACKET_BYTES:
                    continue
                packet, job = json.loads(path.read_bytes()), json.loads(row["job_json"])
                if job.get("dedupe_hash") != row["job_hash"] or not eligible(book, job, packet):
                    continue
            except (OSError, ValueError, TypeError, KeyError):
                continue
            recovered += conn.execute(
                "UPDATE applications SET state='queued',lease_until=NULL,attempts=0,available_at=0,error_kind=NULL,"
                "updated_at=?,notified_at=NULL WHERE job_hash=? AND state='waiting_input'",
                (int(time.time()), row["job_hash"])).rowcount
        conn.commit()
    return recovered
