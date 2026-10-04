"""Expiring, explicit submission authorization for newly discovered Greenhouse jobs.

The browser owns fresh audits and the terminal click. This manager owns scope,
write-ahead attempt records, receipt reconciliation and the tracking final step.
An interrupted or uncertain attempt is never replayed automatically.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .. import config
from ..eligibility import POLICY_ID, restrictions, verified_description
from . import booklet, queue, tracking

SCOPE = "new Phase 1 Greenhouse jobs discovered during this authorization window"
AUTH_NAME = "overnight-submission-authorization.json"
SCHEMA = """
CREATE TABLE IF NOT EXISTS authorized_submission_attempts (
 job_hash TEXT PRIMARY KEY, authorization_id TEXT NOT NULL,
 application_url TEXT NOT NULL, state TEXT NOT NULL,
 started_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 attempt_path TEXT NOT NULL, receipt_path TEXT, result_json TEXT,
 attempt_count INTEGER NOT NULL DEFAULT 1, available_at INTEGER NOT NULL DEFAULT 0,
 packet_sha256 TEXT
);
"""


def _timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Authorization timestamps require a timezone")
    return parsed.timestamp()


def _read_private(path):
    path = Path(path)
    if not path.is_absolute():
        path = config.ROOT / path
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Submission evidence must not be a symlink")
    path = path.resolve(strict=True)
    if not path.is_relative_to((config.ROOT / "private").resolve()) or path.stat().st_size > 2_000_000:
        raise ValueError("Submission evidence must remain private")
    data = path.read_bytes()
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Submission evidence must be an object")
    return path, value, hashlib.sha256(data).hexdigest()


def load_authorization(path=None, *, now=None):
    """Fail closed without the separate reviewed runtime gate and user evidence."""
    if os.environ.get("JHB_OVERNIGHT_SUBMISSIONS_ENABLED") != "1":
        return None
    now = time.time() if now is None else now
    try:
        path, auth, digest = _read_private(path or config.ROOT / "private" / AUTH_NAME)
        start, expiry = _timestamp(auth["authorized_at"]), _timestamp(auth["expires_at"])
        content = auth.get("content", "").casefold()
        explicit = (re.search(r"\b(?:keep|continue) submitting\b", content)
                    and "phase 1" in content and "night" in content
                    and not re.search(r"\b(?:do not|don't|never|stop|cancel|disable)\b", content))
        if (auth.get("enabled") is not True or auth.get("status") != "verified"
                or auth.get("role") != "user" or not explicit or auth.get("scope") != SCOPE
                or auth.get("board") != "greenhouse" or not start <= now < expiry
                or not 0 < expiry-start <= 86400
                or any(auth.get(key) is not True for key in (
                    "require_browser_double_check", "pause_unknown_answers", "require_receipt_before_sheet"))):
            return None
        return {**auth, "authorization_path": str(path), "authorization_id": digest}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def initialize(conn):
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(authorized_submission_attempts)")}
    for name, declaration in [("attempt_count", "INTEGER NOT NULL DEFAULT 1"),
                              ("available_at", "INTEGER NOT NULL DEFAULT 0"), ("packet_sha256", "TEXT")]:
        if name not in columns:
            conn.execute(f"ALTER TABLE authorized_submission_attempts ADD COLUMN {name} {declaration}")
    conn.commit()


def _safe_preclick_retry(previous, packet_sha, *, now):
    """Require affirmative persisted no-click proof, changed inputs or backoff."""
    if previous["state"] not in {"waiting_input", "waiting_login", "waiting_captcha", "waiting_review"} or previous["attempt_count"] >= 3:
        return False
    try:
        _, attempt, _ = _read_private(previous["attempt_path"])
        if attempt.get("runtime_click_started") is not False:
            return False
        old_sha = previous["packet_sha256"] or attempt.get("packet_sha256")
        if old_sha and packet_sha != old_sha:
            return True
        result = json.loads(previous["result_json"] or "{}")
        from .pipeline import TRANSIENT_KINDS
        return (previous["state"] == "waiting_review" and result.get("retryable") is True
                and result.get("error_kind") in TRANSIENT_KINDS | {"BrowserOperationError"}
                and previous["available_at"] <= now)
    except (OSError, ValueError, TypeError):
        return False


def _manifest(job, packet, book):
    from .worker import role_for_job
    choice = book.get("job_role_answers", {}).get(job["dedupe_hash"], {})
    role = choice.get("value") if choice.get("status") == "verified" else role_for_job(job)
    if role not in {"sde", "ml"}:
        raise ValueError("Submission needs a verified resume variant")
    if any(q.get("status") == "pending" and q.get("contexts", {}).get(job["dedupe_hash"], {}).get("required")
           and not q["contexts"][job["dedupe_hash"]].get("resolved")
           for q in book.get("question_handoffs", {}).values()):
        raise ValueError("Submission has an unresolved required question")
    answers = booklet.for_role(book, role)
    override = book.get("job_document_answers", {}).get(job["dedupe_hash"], {})
    if override.get("role") == role and "documents.cover_letter" in override:
        answers["documents.cover_letter"] = override["documents.cover_letter"]
    uploaded = {field.get("key") for field in packet.get("filled", [])}
    documents = {key: answers[key] for key in ("documents.resume", "documents.cover_letter")
                 if key in uploaded and answers.get(key, {}).get("status") == "verified"}
    if "documents.resume" not in documents:
        raise ValueError("Submission needs an approved resume")
    manifest = {"selected_role": role, "documents": documents, "filled": packet.get("filled", [])}
    national = answers.get("identity.phone_national", {})
    if national.get("status") == "verified":
        manifest["approved_phone_national"] = national
    return manifest


def _candidate(conn, row, auth):
    job = json.loads(row["job_json"])
    identity = queue.greenhouse_identity(job.get("url", ""))
    if not identity or row["state"] != "waiting_review" or not row["packet"]:
        return None
    source = conn.execute("SELECT j.first_seen,s.state,s.board,s.application_url FROM jobs j "
                          "JOIN application_sources s ON s.source_job_hash=j.dedupe_hash "
                          "WHERE j.dedupe_hash=?", (job.get("source_job_hash"),)).fetchone()
    start, expiry = _timestamp(auth["authorized_at"]), _timestamp(auth["expires_at"])
    if (not source or not start <= source["first_seen"] < expiry or source["state"] != "resolved"
            or source["board"] != "greenhouse" or queue.greenhouse_identity(source["application_url"]) != identity):
        return None
    packet_path, packet, _ = _read_private(Path(row["packet"]).parent / "packet.json")
    if (packet.get("state") != "waiting_review" or packet.get("submitted") is not False
            or packet.get("missing") or packet.get("verification")
            or packet.get("job", {}).get("dedupe_hash") != row["job_hash"]
            or queue.greenhouse_identity(packet.get("job", {}).get("url", "")) != identity):
        return None
    _, eligibility, _ = _read_private(packet_path.parent / "eligibility.json")
    description = verified_description({**job, "verified_job_description": eligibility.get("description", {})})
    if (eligibility.get("state") != "eligible" or eligibility.get("policy") != POLICY_ID
            or not description or restrictions(description["text"])):
        return None
    return job, packet_path, packet


def _finish_attempt(conn, row, state, result=None, receipt=None):
    path, attempt, _ = _read_private(row["attempt_path"])
    booklet.write_private(path, {**attempt, "state": state, "updated_at": datetime.now(timezone.utc).isoformat(),
                                 "result_state": (result or {}).get("state"), "receipt_path": str(receipt) if receipt else None})
    count = conn.execute("SELECT attempt_count FROM authorized_submission_attempts WHERE job_hash=?", (row["job_hash"],)).fetchone()[0]
    available = int(time.time()) + 300 * 2 ** (count-1) if state == "waiting_review" and (result or {}).get("retryable") is True else 0
    conn.execute("UPDATE authorized_submission_attempts SET state=?,updated_at=?,receipt_path=?,result_json=?,available_at=? "
                 "WHERE job_hash=?", (state, int(time.time()), str(receipt) if receipt else None,
                                      json.dumps(result or {}), available, row["job_hash"]))
    conn.commit()


def _reconcile_receipts(conn, recorder):
    reconciled = 0
    for row in conn.execute("SELECT * FROM authorized_submission_attempts WHERE state<>'submitted'").fetchall():
        receipt = Path(row["attempt_path"]).parent / "receipt.json"
        jobrow = conn.execute("SELECT job_json FROM applications WHERE job_hash=?", (row["job_hash"],)).fetchone()
        if not receipt.is_file() or not jobrow:
            continue
        try:
            _, proof, _ = _read_private(receipt)
            if proof.get("check_count") != 2 or proof.get("authorization_id") != row["authorization_id"]:
                continue
            result = recorder(conn, json.loads(jobrow["job_json"]), receipt)
            if result.get("state") == "submitted":
                _finish_attempt(conn, row, "submitted", result, receipt)
                reconciled += 1
        except Exception:
            continue  # Read-only receipt reconciliation; never click again.
    return reconciled


async def drain(conn, book_path, *, limit=3, submitter=None, recorder=None, now=None):
    """Submit only scoped ready drafts; attempts are durable before browser work."""
    initialize(conn)
    recorder = recorder or tracking.record_confirmed
    summary = {"attempted": 0, "submitted": 0, "uncertain": 0, "handoffs": 0,
               "reconciled": _reconcile_receipts(conn, recorder), "enabled": False}
    auth = load_authorization(now=now)
    if auth is None:
        return summary
    summary["enabled"] = True
    if not 0 <= limit <= 10:
        raise ValueError("Submission batch limit out of range")
    if submitter is None:
        from .authorized_submission import submit_reviewed
        submitter = submit_reviewed
    book = booklet.load(book_path)
    rows = conn.execute("SELECT a.* FROM applications a WHERE a.state='waiting_review' "
                        "ORDER BY a.updated_at,a.job_hash").fetchall()
    for row in rows:
        if summary["attempted"] >= limit:
            break
        # Re-read authorization for revocation, mutation and expiry between jobs.
        latest = load_authorization(now=now)
        if latest is None or latest["authorization_id"] != auth["authorization_id"]:
            break
        try:
            candidate = _candidate(conn, row, auth)
            if candidate is None:
                continue
            job, packet_path, packet = candidate
            manifest = _manifest(job, packet, book)
            packet_sha = hashlib.sha256(packet_path.read_bytes()).hexdigest()
            previous = conn.execute("SELECT * FROM authorized_submission_attempts WHERE job_hash=?", (row["job_hash"],)).fetchone()
            if previous and not _safe_preclick_retry(previous, packet_sha, now=time.time() if now is None else now):
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        attempt_path = config.ROOT / "private" / "authorized-submissions" / row["job_hash"] / "attempt.json"
        started = int(time.time())
        if previous:
            # Preserve the preceding no-click audit before replacing its active
            # file. This never archives or resets an uncertain/clicked attempt.
            _, old_attempt, _ = _read_private(previous["attempt_path"])
            booklet.write_private(attempt_path.parent / f"attempt-{previous['attempt_count']}.json", old_attempt)
            changed = conn.execute("UPDATE authorized_submission_attempts SET authorization_id=?,application_url=?,"
                                   "state='in_progress',started_at=?,updated_at=?,attempt_count=attempt_count+1,available_at=0,"
                                   "packet_sha256=?,receipt_path=NULL,result_json=NULL WHERE job_hash=? AND state=? "
                                   "AND attempt_count=? AND EXISTS(SELECT 1 FROM applications WHERE job_hash=? AND state='waiting_review')",
                                   (auth["authorization_id"], job["url"], started, started, packet_sha, row["job_hash"],
                                    previous["state"], previous["attempt_count"], row["job_hash"])).rowcount
        else:
            changed = conn.execute("INSERT OR IGNORE INTO authorized_submission_attempts "
                                   "(job_hash,authorization_id,application_url,state,started_at,updated_at,attempt_path,packet_sha256) "
                                   "SELECT ?,?,?,'in_progress',?,?,?,? WHERE EXISTS "
                                   "(SELECT 1 FROM applications WHERE job_hash=? AND state='waiting_review')",
                                   (row["job_hash"], auth["authorization_id"], job["url"], started, started, str(attempt_path),
                                    packet_sha, row["job_hash"])).rowcount
        conn.commit()
        if not changed:
            continue
        summary["attempted"] += 1
        attempt = {"job_hash": row["job_hash"], "application_url": job["url"],
                   "authorization_id": auth["authorization_id"], "authorization_path": auth["authorization_path"],
                   "started_at": datetime.now(timezone.utc).isoformat(), "state": "in_progress",
                   "runtime_click_started": False, "attempt_count": previous["attempt_count"]+1 if previous else 1,
                   "packet_path": str(packet_path), "packet_sha256": packet_sha}
        try:
            booklet.write_private(attempt_path, attempt)
            result = await asyncio.wait_for(submitter(job, packet_path, manifest, authorization=auth,
                                                     attempt=attempt_path), timeout=300)
            receipt = result.get("receipt_path") if isinstance(result, dict) else None
            if result.get("state") == "submitted" and receipt:
                if result.get("check_count") != 2:
                    raise ValueError("Submission requires two live retained-answer checks")
                _, proof, _ = _read_private(receipt)
                if proof.get("check_count") != 2 or proof.get("authorization_id") != auth["authorization_id"]:
                    raise ValueError("Submission receipt lacks matching authorization and double-check evidence")
                # Receipt validation, durable confirmation and sheet update are one
                # existing final-step API. A click or state marker is insufficient.
                confirmed = recorder(conn, job, receipt)
                if confirmed.get("state") != "submitted":
                    raise ValueError("Submission receipt was not confirmed")
                _finish_attempt(conn, {"attempt_path": str(attempt_path), "job_hash": row["job_hash"]}, "submitted", confirmed, receipt)
                summary["submitted"] += 1
            else:
                state = result.get("state", "uncertain")
                _, persisted, _ = _read_private(attempt_path)
                if persisted.get("runtime_click_started"):
                    state = "uncertain"
                if state not in {"waiting_login", "waiting_captcha", "waiting_input", "waiting_review"}:
                    state = "uncertain"
                _finish_attempt(conn, {"attempt_path": str(attempt_path), "job_hash": row["job_hash"]}, state, result)
                if state in {"waiting_input", "waiting_login", "waiting_captcha"}:
                    # This is a fresh authorized audit, not an older preparation
                    # worker overwriting a completed review snapshot.
                    conn.execute("UPDATE applications SET state=?,updated_at=? WHERE job_hash=? AND state='waiting_review'",
                                 (state, int(time.time()), row["job_hash"]))
                    conn.commit()
                    if state == "waiting_input" and result.get("missing"):
                        from .questions import collect
                        collect(job, result, book_path, observed_book=book)
                elif state == "uncertain":
                    conn.execute("UPDATE applications SET state='submission_uncertain',updated_at=? "
                                 "WHERE job_hash=? AND state='waiting_review'", (int(time.time()), row["job_hash"]))
                    conn.commit()
                summary["uncertain" if state == "uncertain" else "handoffs"] += 1
        except Exception as exc:
            # Preserve uncertain durable attempts, including a possible terminal
            # click followed by transport failure. No subsequent automatic replay.
            conn.execute("UPDATE authorized_submission_attempts SET state='uncertain',updated_at=?,result_json=? WHERE job_hash=?",
                         (int(time.time()), json.dumps({"state": "uncertain", "error_kind": type(exc).__name__}), row["job_hash"]))
            conn.commit()
            conn.execute("UPDATE applications SET state='submission_uncertain',updated_at=? "
                         "WHERE job_hash=? AND state='waiting_review'", (int(time.time()), row["job_hash"]))
            conn.commit()
            summary["uncertain"] += 1
    return summary
