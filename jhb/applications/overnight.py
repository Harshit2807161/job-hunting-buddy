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
from . import boards, booklet, queue, tracking

SCOPE = "new Phase 1 Greenhouse jobs discovered during this authorization window"
MULTI_SCOPE = "approved existing and new application drafts on enabled boards during this authorization window"
MULTI_JOB_POLICY = "existing_and_new_verified_drafts"
PORTAL_SCOPE = "one exact application explicitly approved in the local review portal"
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


def valid_authority(auth, *, now=None):
    """Validate one finite user instruction; recognition never grants a submit adapter."""
    now = time.time() if now is None else now
    start, expiry = _timestamp(auth["authorized_at"]), _timestamp(auth["expires_at"])
    content = auth.get("content", "").casefold()
    if auth.get("scope") == SCOPE:
        explicit = (re.search(r"\b(?:keep|continue) submitting\b", content)
                    and "phase 1" in content and "night" in content
                    and not re.search(r"\b(?:do not|don't|never|stop|cancel|disable)\b", content))
        scoped = auth.get("board") == "greenhouse"
    elif auth.get("scope") == MULTI_SCOPE:
        explicit = (re.search(r"\b(?:submit|submitting|apply|applying)\b", content)
                    and re.search(r"\b(?:all (?:job )?boards|everything)\b", content)
                    and not re.search(r"\b(?:do not|don't|never|stop|cancel|disable)\s+(?:apply|applying|submit|submitting|applications?|automation)\b", content))
        enabled = auth.get("boards")
        scoped = (isinstance(enabled, list) and bool(enabled) and len(enabled) == len(set(enabled))
                  and all(isinstance(board, str) and board in boards.ADAPTERS for board in enabled)
                  and auth.get("candidate_job_policy") == MULTI_JOB_POLICY
                  and auth.get("require_independent_review") is True)
    elif auth.get("scope") == PORTAL_SCOPE:
        explicit = auth.get("action") == "approve" and auth.get("source") == "local_review_portal"
        scoped = (bool(re.fullmatch(r"[a-f0-9]{64}", auth.get("job_hash", "")))
                  and bool(re.fullmatch(r"[a-f0-9]{32}", auth.get("approval_id", "")))
                  and isinstance(auth.get("boards"), list) and len(auth["boards"]) == 1
                  and auth["boards"][0] in boards.ADAPTERS
                  and auth.get("require_independent_review") is True)
    else:
        return False
    return (auth.get("enabled") is True and auth.get("status") == "verified" and auth.get("role") == "user"
            and bool(explicit) and scoped and start <= now < expiry and 0 < expiry-start <= 86400
            and all(auth.get(key) is True for key in (
                "require_browser_double_check", "pause_unknown_answers", "require_receipt_before_sheet")))


def gate_enabled(auth):
    portal = auth.get("scope") == PORTAL_SCOPE
    if os.environ.get("JHB_REQUIRE_PORTAL_APPROVAL") == "1" and not portal:
        return False
    return os.environ.get("JHB_PORTAL_SUBMISSIONS_ENABLED" if portal else "JHB_OVERNIGHT_SUBMISSIONS_ENABLED") == "1"


def load_authorization(path=None, *, now=None):
    """Fail closed without the separate reviewed runtime gate and user evidence."""
    now = time.time() if now is None else now
    try:
        path, auth, digest = _read_private(path or config.ROOT / "private" / AUTH_NAME)
        if not gate_enabled(auth) or not valid_authority(auth, now=now):
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


def _safe_preclick_retry(previous, packet_sha, *, now, authorization_id=None):
    """Require affirmative persisted no-click proof, changed inputs or backoff."""
    if previous["state"] not in {"waiting_input", "waiting_login", "waiting_captcha", "waiting_review"} or previous["attempt_count"] >= 3:
        return False
    try:
        _, attempt, _ = _read_private(previous["attempt_path"])
        if (attempt.get("runtime_click_started") is not False or attempt.get("job_hash") != previous["job_hash"]
                or attempt.get("authorization_id") != previous["authorization_id"]):
            return False
        if authorization_id and authorization_id != previous["authorization_id"]:
            return True  # A renewed explicit window re-audits only proven no-click attempts.
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


def _candidate(conn, row, auth, book):
    job = json.loads(row["job_json"])
    if auth.get("scope") == PORTAL_SCOPE and auth.get("job_hash") != row["job_hash"]:
        return None
    if booklet.job_excluded(book, job):
        return None
    identity = boards.job_identity(job.get("url", ""))
    allowed = ["greenhouse"] if auth.get("scope") == SCOPE else auth.get("boards", [])
    if (not identity or identity[0] not in allowed or not boards.submission_supported(identity[0])
            or row["state"] != "waiting_review" or not row["packet"]
            or boards.application_hash(job["url"]) != row["job_hash"]):
        return None
    if auth.get("scope") == SCOPE:
        source = conn.execute("SELECT j.first_seen,s.state,s.board,s.application_url FROM jobs j "
                              "JOIN application_sources s ON s.source_job_hash=j.dedupe_hash "
                              "WHERE j.dedupe_hash=?", (job.get("source_job_hash"),)).fetchone()
        start, expiry = _timestamp(auth["authorized_at"]), _timestamp(auth["expires_at"])
        if (not source or not start <= source["first_seen"] < expiry or source["state"] != "resolved"
                or source["board"] != "greenhouse" or boards.job_identity(source["application_url"]) != identity):
            return None
    if conn.execute("SELECT name FROM sqlite_master WHERE name='confirmed_submissions'").fetchone():
        if any(boards.job_identity(item[0]) == identity for item in conn.execute("SELECT application_url FROM confirmed_submissions")):
            return None
    packet_path, packet, _ = _read_private(Path(row["packet"]).parent / "packet.json")
    if auth.get("scope") == PORTAL_SCOPE:
        from .approvals import validate_binding
        validate_binding(auth, packet_path)
    if (packet.get("state") != "waiting_review" or packet.get("submitted") is not False
            or packet.get("missing") or packet.get("verification")
            or packet.get("job", {}).get("dedupe_hash") != row["job_hash"]
            or boards.job_identity(packet.get("job", {}).get("url", "")) != identity):
        return None
    _, eligibility, _ = _read_private(packet_path.parent / "eligibility.json")
    description = verified_description({**job, "verified_job_description": eligibility.get("description", {})})
    if (eligibility.get("state") != "eligible" or eligibility.get("policy") != POLICY_ID
            or not description or restrictions(description["text"])):
        return None
    if os.environ.get("JHB_ROLE_FIT_REVIEW") == "1":
        from .role_fit import evidence_hash, POLICY as FIT_POLICY
        from .worker import role_for_job
        _, fit, _ = _read_private(packet_path.parent / "role-fit.json")
        choice = book.get("job_role_answers", {}).get(job["dedupe_hash"], {})
        role = choice.get("value") if choice.get("status") == "verified" else role_for_job(job)
        if (role not in {"sde", "ml"} or fit.get("state") != "eligible" or fit.get("source") != FIT_POLICY
                or fit.get("mode") != "independent_codex" or fit.get("selected_role") != role
                or fit.get("evidence_hash") != evidence_hash({**job, "verified_job_description": description}, book, role)):
            return None
    return job, packet_path, packet


def register_manual_draft(conn, packet_path):
    """Import an exact private reviewed draft; authority is checked at drain time."""
    path, packet, _ = _read_private(packet_path)
    job = packet.get("job", {})
    key = boards.application_hash(job.get("url"))
    if (path.name != "packet.json" or key is None or packet.get("state") != "waiting_review" or packet.get("submitted") is not False
            or packet.get("missing") or packet.get("verification") or job.get("dedupe_hash") != key):
        raise ValueError("Manual draft requires a complete exact-job review packet")
    queue.initialize(conn)
    # Existing submitted/uncertain/running rows retain their terminal ownership.
    with conn:
        count = conn.execute("INSERT OR IGNORE INTO applications(job_hash,job_json,state,updated_at,packet) "
                             "VALUES(?,?,'waiting_review',?,?)", (key, json.dumps(job), int(time.time()), str(path.with_name("review.html")))).rowcount
    return count


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


def _checked_receipt(row, proof):
    """Match modern receipts to immutable audits, documents and separate review."""
    if proof.get("check_count") != 2 or proof.get("authorization_id") != row["authorization_id"]:
        return False
    _, attempt, _ = _read_private(row["attempt_path"])
    if attempt.get("require_independent_review") is not True:
        return True  # Existing v1 receipts retain their reviewed contract.
    _, checks, _ = _read_private(Path(row["attempt_path"]).parent / "checks.json")
    _, token, token_digest = _read_private(Path(row["attempt_path"]).parent / "independent-review.json")
    from .authorized_submission import audit_hash
    from .application_review import snapshot_digest
    snapshots, documents = checks.get("checks"), checks.get("documents", {})
    if (attempt.get("runtime_click_started") is not True or not isinstance(snapshots, list) or len(snapshots) != 2
            or snapshots[0] != snapshots[1] or checks.get("check_count") != 2 or not isinstance(documents, dict)
            or "documents.resume" not in documents or proof.get("job_hash") != row["job_hash"]
            or boards.job_identity(proof.get("url")) != boards.job_identity(row["application_url"])
            or checks.get("authorization_id") != row["authorization_id"] or checks.get("job_hash") != row["job_hash"]):
        return False
    hashes = {key: item.get("sha256") for key, item in documents.items() if isinstance(item, dict)}
    if (len(hashes) != len(documents) or not all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) for value in hashes.values())
            or proof.get("document_sha256") != hashes or proof.get("resume_sha256") != hashes["documents.resume"]):
        return False
    review = token.get("review", {})
    return (token.get("verdict") == "approved" and token.get("source") == "independent_application_review"
            and proof.get("independent_review_sha256") == token_digest
            and token.get("job_hash") == row["job_hash"] and token.get("authorization_id") == row["authorization_id"]
            and token.get("packet_sha256") == attempt.get("packet_sha256")
            and token.get("audit_sha256") == audit_hash(snapshots[0], documents)
            and review.get("verdict") == "approved" and review.get("reviewer") == "codex-readonly"
            and review.get("source") == "independent_application_review" and review.get("issues") == []
            and review.get("snapshot_sha256") == snapshot_digest(snapshots[0])
            and review.get("job_hash") == row["job_hash"] and review.get("authorization_id") == row["authorization_id"])


def _reconcile_receipts(conn, recorder):
    reconciled = 0
    for row in conn.execute("SELECT * FROM authorized_submission_attempts WHERE state<>'submitted'").fetchall():
        receipt = Path(row["attempt_path"]).parent / "receipt.json"
        jobrow = conn.execute("SELECT job_json FROM applications WHERE job_hash=?", (row["job_hash"],)).fetchone()
        if not receipt.is_file() or not jobrow:
            continue
        try:
            _, proof, _ = _read_private(receipt)
            if not _checked_receipt(row, proof):
                continue
            result = recorder(conn, json.loads(jobrow["job_json"]), receipt)
            if result.get("state") == "submitted":
                _finish_attempt(conn, row, "submitted", result, receipt)
                reconciled += 1
        except Exception:
            continue  # Read-only receipt reconciliation; never click again.
    return reconciled


async def drain(conn, book_path, *, limit=3, submitter=None, recorder=None, now=None, authorization_path=None):
    """Submit only scoped ready drafts; attempts are durable before browser work."""
    initialize(conn)
    recorder = recorder or tracking.record_confirmed
    summary = {"attempted": 0, "submitted": 0, "uncertain": 0, "handoffs": 0,
               "reconciled": _reconcile_receipts(conn, recorder), "enabled": False}
    auth = load_authorization(authorization_path, now=now)
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
        latest = load_authorization(authorization_path, now=now)
        if latest is None or latest["authorization_id"] != auth["authorization_id"]:
            break
        try:
            current_book = booklet.load(book_path)
            candidate = _candidate(conn, row, auth, current_book)
            if candidate is None:
                continue
            job, packet_path, packet = candidate
            manifest = _manifest(job, packet, current_book)
            packet_sha = hashlib.sha256(packet_path.read_bytes()).hexdigest()
            previous = conn.execute("SELECT * FROM authorized_submission_attempts WHERE job_hash=?", (row["job_hash"],)).fetchone()
            if previous and not _safe_preclick_retry(previous, packet_sha, now=time.time() if now is None else now,
                                                    authorization_id=auth["authorization_id"]):
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
                   "authorization_scope": auth["scope"],
                   "require_independent_review": auth.get("require_independent_review") is True,
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
                persisted_row = conn.execute("SELECT * FROM authorized_submission_attempts WHERE job_hash=?", (row["job_hash"],)).fetchone()
                if not _checked_receipt(persisted_row, proof):
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
