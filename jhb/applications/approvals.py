"""One immutable draft, one explicit portal approval, one guarded submission."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from .. import config
from . import boards, booklet, overnight

SCHEMA = """
CREATE TABLE IF NOT EXISTS application_approvals (
 approval_id TEXT PRIMARY KEY, job_hash TEXT NOT NULL, state TEXT NOT NULL,
 approved_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revision TEXT NOT NULL,
 authorization_path TEXT NOT NULL, result_json TEXT
);
CREATE INDEX IF NOT EXISTS approval_jobs ON application_approvals(job_hash,approved_at);
"""


def initialize(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _expire_pending(conn, job_hash=None):
    # Timestamp expiry revokes permission without rewriting immutable evidence
    # that could still be needed to validate an already captured receipt.
    query = "UPDATE application_approvals SET state='expired' WHERE state='approved' AND expires_at<=?"
    values = [int(time.time())]
    if job_hash is not None:
        query += " AND job_hash=?"
        values.append(job_hash)
    conn.execute(query, values)
    conn.commit()


def _attempt_status(conn, approval):
    """Only exact persisted no-click evidence permits a new human review."""
    if not conn.execute("SELECT name FROM sqlite_master WHERE name='authorized_submission_attempts'").fetchone():
        return "absent", None
    row = conn.execute("SELECT * FROM authorized_submission_attempts WHERE job_hash=?", (approval["job_hash"],)).fetchone()
    if row is None:
        return "absent", None
    try:
        _, auth, digest = overnight._read_private(approval["authorization_path"])
        _, attempt, _ = overnight._read_private(row["attempt_path"])
        if (auth.get("approval_id") != approval["approval_id"]
                or attempt.get("job_hash") != approval["job_hash"] or attempt.get("authorization_id") != row["authorization_id"]
                or (row["authorization_id"] != digest and attempt.get("authorization_path") != approval["authorization_path"])):
            return "unknown", row
        if attempt.get("runtime_click_started") is True:
            return "clicked", row
        if attempt.get("runtime_click_started") is False:
            return "no_click", row
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return "unknown", row


def _orphan_evidence(conn, approval):
    """Bind historical evidence without granting fresh permission or clicking."""
    has_attempts = conn.execute("SELECT name FROM sqlite_master WHERE name='authorized_submission_attempts'").fetchone()
    attempt_row = conn.execute("SELECT * FROM authorized_submission_attempts WHERE job_hash=?",
                               (approval["job_hash"],)).fetchone() if has_attempts else None
    try:
        _, auth, digest = overnight._read_private(approval["authorization_path"])
        job_row = conn.execute("SELECT job_json FROM applications WHERE job_hash=?", (approval["job_hash"],)).fetchone()
        job = json.loads(job_row["job_json"]) if job_row else {}
        identity = boards.job_identity(job.get("url"))
        if (auth.get("scope") != overnight.PORTAL_SCOPE or auth.get("role") != "user"
                or auth.get("status") != "verified" or auth.get("action") != "approve"
                or auth.get("approval_id") != approval["approval_id"] or auth.get("job_hash") != approval["job_hash"]
                or boards.application_hash(job.get("url")) != approval["job_hash"] or identity is None):
            return "unknown", attempt_row, None, None
        if attempt_row is None:
            return "absent", None, None, job
        _, attempt, _ = overnight._read_private(attempt_row["attempt_path"])
        if (attempt_row["authorization_id"] != digest or attempt.get("authorization_id") != digest
                or attempt.get("job_hash") != approval["job_hash"]
                or attempt.get("authorization_path") != approval["authorization_path"]
                or boards.job_identity(attempt_row["application_url"]) != identity
                or boards.job_identity(attempt.get("application_url")) != identity
                or attempt.get("packet_sha256") != auth.get("binding", {}).get("packet_sha256")
                or not isinstance(attempt.get("packet_sha256"), str)):
            return "unknown", attempt_row, None, None
        marker = attempt.get("runtime_click_started")
        return ("clicked" if marker is True else "no_click" if marker is False else "unknown"), attempt_row, attempt, job
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown", attempt_row, None, None


def recover_stalled(conn, *, recorder=None):
    """Recover orphan approvals only under both service and pipeline worker locks.

    The service establishes exclusive ownership before calling this function.
    Receipt reconciliation is read-only browser work; no state grants a replay.
    Immutable authority/attempt files are retained even after expiry or recovery.
    """
    from . import tracking
    initialize(conn)
    recorder = recorder or tracking.record_confirmed
    summary = {"recovered": 0, "submitted": 0, "needs_review": 0, "expired": 0, "uncertain": 0}
    for approval in conn.execute("SELECT * FROM application_approvals WHERE state='submitting'").fetchall():
        marker, row, attempt, job = _orphan_evidence(conn, approval)
        state = None
        receipt = Path(row["attempt_path"]).parent / "receipt.json" if row is not None else None
        if marker == "clicked" and attempt.get("require_independent_review") is True and receipt.is_file():
            try:
                _, proof, _ = overnight._read_private(receipt)
                _, auth, digest = overnight._read_private(approval["authorization_path"])
                documents = auth.get("binding", {}).get("document_sha256", {})
                if (digest == row["authorization_id"] and auth.get("require_independent_review") is True
                        and isinstance(documents, dict) and documents.get("documents.resume")
                        and proof.get("document_sha256") == documents
                        and overnight._checked_receipt(row, proof)):
                    result = recorder(conn, job, receipt)
                    if result.get("state") == "submitted":
                        state = "submitted"
            except Exception:
                pass  # Missing/invalid receipts or a failed recorder cannot authorize replay.
        if state is None:
            safe = marker in {"absent", "no_click"}
            state = ("expired" if approval["expires_at"] <= int(time.time()) else "needs_review") if safe else "uncertain"
        result = {"state": state, "source": "orphan_approval_recovery", "click_evidence": marker}
        with conn:
            changed = conn.execute("UPDATE application_approvals SET state=?,result_json=? "
                                   "WHERE approval_id=? AND state='submitting'", (state, json.dumps(result), approval["approval_id"])).rowcount
            if not changed:
                continue  # A concurrent explicit revoke is never overwritten.
            if row is not None:
                if state == "submitted":
                    conn.execute("UPDATE authorized_submission_attempts SET state='submitted',receipt_path=?,updated_at=?,result_json=? "
                                 "WHERE job_hash=? AND authorization_id=?",
                                 (str(receipt), int(time.time()), json.dumps(result), approval["job_hash"], row["authorization_id"]))
                else:
                    conn.execute("UPDATE authorized_submission_attempts SET state=?,updated_at=?,result_json=? "
                                 "WHERE job_hash=? AND authorization_id=? AND state<>'submitted'",
                                 ("waiting_review" if state != "uncertain" else "uncertain", int(time.time()), json.dumps(result),
                                  approval["job_hash"], row["authorization_id"]))
            if state == "uncertain":
                conn.execute("UPDATE applications SET state='submission_uncertain',updated_at=? "
                             "WHERE job_hash=? AND state NOT IN ('submitted','submission_uncertain')",
                             (int(time.time()), approval["job_hash"]))
        summary["recovered"] += 1
        summary[state] += 1
    return summary


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _facts(book, job, role):
    from .questions import _scope
    scope = _scope(job)
    return _digest({"answers": booklet.for_role(book, role), "education": book.get("education_records", []),
                    "preferences": book.get("workflow_preferences", {}),
                    "custom": {k: v for k, v in book.get("custom_answers", {}).items()
                               if v.get("scope") == scope and (not v.get("job_hash") or v["job_hash"] == job["dedupe_hash"])
                               and (not v.get("job_hashes") or job["dedupe_hash"] in v["job_hashes"])},
                    "role_override": book.get("job_role_answers", {}).get(job["dedupe_hash"]),
                    "document_override": book.get("job_document_answers", {}).get(job["dedupe_hash"]),
                    "exclusion": book.get("job_exclusions", {}).get(job["dedupe_hash"])})


def _snapshot(packet_path, book_path):
    from .authorized_submission import document_manifest, private_file
    from .worker import role_for_job
    path = private_file(str(packet_path))
    packet_bytes = path.read_bytes()
    packet = json.loads(packet_bytes)
    job = packet["job"]
    if (boards.application_hash(job.get("url")) != job.get("dedupe_hash") or packet.get("state") != "waiting_review"
            or packet.get("submitted") is not False or packet.get("missing") or packet.get("verification")):
        raise ValueError("Application is not a complete review draft")
    inventory = packet.get("review_inventory", {})
    if inventory.get("complete") is not True or not inventory.get("fields"):
        raise ValueError("A complete fresh inventory of all application questions is required")
    fields = inventory["fields"]
    if any(not isinstance(f, dict) or not f.get("ref") or not f.get("question") or
           f.get("status") not in {"answered", "blank", "declined"} for f in fields):
        raise ValueError("Application inventory is incomplete")
    refs = [f["ref"] for f in fields]
    if len(refs) != len(set(refs)):
        raise ValueError("Review field identities are ambiguous")
    if any(f.get("required") and f["status"] != "answered" for f in fields):
        raise ValueError("Required application answers are missing")
    filled = {r["ref"] for r in packet.get("filled", [])}
    if any(f["status"] == "answered" and f["ref"] not in filled for f in fields):
        raise ValueError("Inventory claims an answer absent from retained evidence")
    for pending in packet.get("optional_questions", []):
        if pending.get("ref") not in refs:
            raise ValueError("A blank optional question is absent from the review inventory")
    book_file = private_file(str(Path(book_path).resolve()))
    book = booklet.load(book_file)
    if booklet.job_excluded(book, job):
        raise ValueError("Candidate declined this role")
    selected = book.get("job_role_answers", {}).get(job["dedupe_hash"], {})
    role = selected.get("value") if selected.get("status") == "verified" else role_for_job(job)
    manifest = overnight._manifest(job, packet, book)
    documents = document_manifest(manifest, packet)
    screenshot = path.with_name("browser.png")
    if not screenshot.is_file() or screenshot.is_symlink() or screenshot.stat().st_size > 15*1024*1024:
        raise ValueError("A retained final review screenshot is required")
    screenshot_bytes = screenshot.read_bytes()
    from .capture import valid as valid_capture
    if not valid_capture(packet, screenshot_bytes):
        raise ValueError("The current review screenshot lacks matching fresh capture evidence")
    binding = {"packet_path": str(path), "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
               "book_path": str(book_file), "facts_sha256": _facts(book, job, role), "selected_role": role,
               "document_sha256": {k: d["sha256"] for k, d in documents.items()},
               "screenshot_sha256": hashlib.sha256(screenshot_bytes).hexdigest()}
    return packet, binding, _digest(binding)


def _draft(conn, job_hash, book_path):
    if not re.fullmatch(r"[a-f0-9]{64}", job_hash):
        raise ValueError("Invalid application identity")
    row = conn.execute("SELECT state,packet FROM applications WHERE job_hash=?", (job_hash,)).fetchone()
    if row is None or row["state"] != "waiting_review" or not row["packet"]:
        raise ValueError("Application is not awaiting review")
    path = Path(row["packet"]).parent / "packet.json"
    packet, binding, revision = _snapshot(path, book_path)
    if packet["job"]["dedupe_hash"] != job_hash:
        raise ValueError("Review packet belongs to a different application")
    if not boards.submission_supported(boards.job_identity(packet["job"]["url"])[0]):
        raise ValueError("This board's final submission adapter still needs validation")
    return packet, binding, revision


def review(conn, job_hash, book_path=booklet.DEFAULT_PATH):
    initialize(conn)
    _expire_pending(conn, job_hash)
    latest = conn.execute("SELECT approval_id,state,approved_at,expires_at,revision FROM application_approvals "
                          "WHERE job_hash=? ORDER BY approved_at DESC,rowid DESC LIMIT 1", (job_hash,)).fetchone()
    result = {"job_hash": job_hash, "revision": None, "can_approve": False, "reason": "", "blank_questions": [],
              "approval": dict(latest) if latest else None}
    try:
        packet, binding, revision = _draft(conn, job_hash, book_path)
        result.update(revision=revision, can_approve=True, selected_role=binding["selected_role"],
                      packet_sha256=binding.get("packet_sha256"),
                      blank_questions=[f for f in packet["review_inventory"]["fields"] if f["status"] != "answered"])
        if latest and latest["state"] in {"approved", "submitting"}:
            result.update(can_approve=False, reason="This draft already has a pending approval")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result["reason"] = str(exc) if isinstance(exc, ValueError) else "Review evidence is unavailable"
    return result


def approve(conn, job_hash, expected_revision, acknowledged_blank_refs=None, book_path=booklet.DEFAULT_PATH, *, current_form=False):
    """Called only by the portal's authenticated same-origin explicit click route."""
    initialize(conn)
    _expire_pending(conn, job_hash)
    packet, binding, revision = _draft(conn, job_hash, book_path)
    if expected_revision != revision:
        raise ValueError("The draft changed; reload and review it again")
    blanks = {f["ref"] for f in packet["review_inventory"]["fields"] if f["status"] != "answered"}
    acknowledged = acknowledged_blank_refs or []
    if not isinstance(acknowledged, list) or any(not isinstance(r, str) for r in acknowledged) or set(acknowledged) != blanks:
        raise ValueError("Explicitly acknowledge each optional blank answer before approval")
    pending = conn.execute("SELECT 1 FROM application_approvals WHERE job_hash=? AND state IN ('approved','submitting')", (job_hash,)).fetchone()
    if pending:
        raise ValueError("Application already has a pending approval")
    stamp = datetime.now(timezone.utc); expiry = stamp + timedelta(hours=2); aid = uuid.uuid4().hex
    path = config.ROOT / "private" / "application-approvals" / job_hash / (aid + ".json")
    auth = {"scope": overnight.PORTAL_SCOPE, "role": "user", "status": "verified", "enabled": True,
            "source": "local_review_portal", "action": "approve", "approval_id": aid,
            "job_hash": job_hash, "boards": [boards.job_identity(packet["job"]["url"])[0]],
            "authorized_at": stamp.isoformat(), "expires_at": expiry.isoformat(),
            "require_browser_double_check": True, "pause_unknown_answers": True,
            "require_receipt_before_sheet": True, "require_independent_review": True,
            "revision": revision, "binding": binding, "acknowledged_blank_refs": sorted(blanks)}
    if current_form:
        from .live_review import MODE
        if packet.get("review_mode") != MODE or not packet.get("live_review", {}).get("snapshot_sha256"):
            raise ValueError("Current-form approval requires fresh live evidence")
        auth.update(approval_mode=MODE,
                    current_form_snapshot_sha256=packet["live_review"]["snapshot_sha256"],
                    preserve_candidate_live_edits=True)
    booklet.write_private(path, auth)
    with conn:
        # A concurrent click cannot approve the same draft twice.
        inserted = conn.execute("INSERT INTO application_approvals SELECT ?,?,'approved',?,?,?,?,NULL "
                                "WHERE NOT EXISTS(SELECT 1 FROM application_approvals WHERE job_hash=? AND state IN ('approved','submitting'))",
                                (aid, job_hash, int(stamp.timestamp()), int(expiry.timestamp()), revision, str(path), job_hash)).rowcount
    if not inserted:
        booklet.write_private(path, {**auth, "enabled": False})
        raise ValueError("Application already has a pending approval")
    return {"approval_id": aid, "state": "approved", "job_hash": job_hash}


def validate_binding(auth, packet_path):
    """Recheck the user's exact evidence before every final browser operation."""
    binding = auth.get("binding", {})
    packet, current, revision = _snapshot(packet_path, binding.get("book_path", ""))
    if (auth.get("job_hash") != packet["job"]["dedupe_hash"] or binding != current or auth.get("revision") != revision
            or set(auth.get("acknowledged_blank_refs", [])) !=
            {f["ref"] for f in packet["review_inventory"]["fields"] if f["status"] != "answered"}):
        raise ValueError("Portal-approved answers or documents changed; approval is invalid")
    return True


def revoke(conn, job_hash):
    initialize(conn)
    lane = config.ROOT / "private" / "browser-lane.lock"
    if lane.is_symlink() or any(parent.is_symlink() for parent in lane.parents):
        raise ValueError("Browser lane must remain in its private directory")
    lane.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lane, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A browser operation is active; wait for its outcome before revoking") from None
        rows = conn.execute("SELECT * FROM application_approvals WHERE job_hash=? AND state IN ('approved','submitting')", (job_hash,)).fetchall()
        for row in rows:
            state, attempt = _attempt_status(conn, row)
            if state == "clicked" or (state == "unknown" and attempt is not None):
                raise ValueError("The terminal attempt may have started; preserve its authorization and review the outcome")
        for row in rows:
            path, auth, _ = overnight._read_private(row["authorization_path"])
            booklet.write_private(path, {**auth, "enabled": False})
            conn.execute("UPDATE application_approvals SET state='revoked' WHERE approval_id=? AND state IN ('approved','submitting')", (row["approval_id"],))
        conn.commit()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return {"job_hash": job_hash, "state": "revoked"}


def _preflight_handoff(conn, job_hash, auth, book_path):
    """Explain a zero-attempt handoff without changing any submission guard."""
    row = conn.execute("SELECT * FROM applications WHERE job_hash=?", (job_hash,)).fetchone()
    code = "submission_preflight_blocked"
    try:
        if row is None or row["state"] != "waiting_review":
            code = "application_not_waiting_review"
        else:
            rejected = []
            overnight._candidate(conn, row, auth, booklet.load(book_path), rejections=rejected)
            code = rejected[0] if rejected else code
    except (OSError, ValueError, KeyError, TypeError):
        code = "submission_evidence_invalid"
    messages = {
        "application_not_waiting_review": "The application's saved state changed after approval; it is no longer ready for submission.",
        "application_history_blocked": "Existing application history blocks a possible duplicate submission.",
        "candidate_excluded_job": "Your saved exclusion prevents this application from being submitted.",
        "eligibility_not_verified": "Current job eligibility evidence is unavailable, stale, or contains an excluded requirement.",
        "role_fit_not_eligible": "The role-fit assessment rejected this role; the approval was not submitted.",
        "role_fit_not_verified": "The role-fit assessment does not satisfy the current submission requirements.",
        "review_packet_not_ready": "The saved review packet is incomplete or no longer matches this application.",
        "submission_evidence_invalid": "Submission evidence needs review before the application can be sent.",
    }
    return {"job_hash": job_hash, "reason_code": code,
            "reason": messages.get(code, "Submission checks blocked this application before any submit attempt.")}


async def drain(conn, book_path, *, limit=3, submitter=None, recorder=None, job_hash=None):
    initialize(conn)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 10:
        raise ValueError("Portal submission batch limit out of range")
    _expire_pending(conn)
    summary = {"attempted": 0, "submitted": 0, "uncertain": 0, "handoffs": 0, "enabled": False}
    if os.environ.get("JHB_PORTAL_SUBMISSIONS_ENABLED") != "1":
        return summary
    summary["enabled"] = True
    if job_hash is not None and not re.fullmatch(r"[a-f0-9]{64}", job_hash):
        raise ValueError("Invalid exact approval job")
    query = "SELECT * FROM application_approvals WHERE state='approved'"
    params = []
    if job_hash:
        query += " AND job_hash=?"; params.append(job_hash)
    query += " ORDER BY approved_at LIMIT ?"; params.append(limit)
    for row in conn.execute(query, params).fetchall():
        try:
            auth = overnight.load_authorization(row["authorization_path"])
            if not auth:
                raise ValueError("Portal approval expired or was revoked")
            validate_binding(auth, auth["binding"]["packet_path"])
        except (OSError, ValueError, KeyError, TypeError):
            conn.execute("UPDATE application_approvals SET state='invalidated' WHERE approval_id=? AND state='approved'", (row["approval_id"],)); conn.commit()
            continue
        changed = conn.execute("UPDATE application_approvals SET state='submitting' WHERE approval_id=? AND state='approved'", (row["approval_id"],)).rowcount
        conn.commit()
        if not changed:
            continue
        try:
            result = await overnight.drain(conn, book_path, limit=1, submitter=submitter, recorder=recorder,
                                           authorization_path=row["authorization_path"])
        except BaseException as exc:
            attempt_state, attempt = _attempt_status(conn, row)
            safe = attempt_state in {"absent", "no_click"}
            state = "needs_review" if safe else "uncertain"
            result = {"attempted": 0 if attempt is None else 1, "submitted": 0,
                      "uncertain": int(not safe), "handoffs": int(safe), "error_kind": type(exc).__name__}
            if attempt is not None and attempt_state in {"no_click", "clicked", "unknown"}:
                conn.execute("UPDATE authorized_submission_attempts SET state=?,updated_at=?,result_json=? WHERE job_hash=? AND authorization_id=? AND state<>'submitted'",
                             ("waiting_review" if safe else "uncertain", int(time.time()), json.dumps(result),
                              row["job_hash"], attempt["authorization_id"]))
            if not safe:
                conn.execute("UPDATE applications SET state='submission_uncertain',updated_at=? WHERE job_hash=? AND state='waiting_review'",
                             (int(time.time()), row["job_hash"]))
            conn.execute("UPDATE application_approvals SET state=?,result_json=? WHERE approval_id=? AND state='submitting'",
                         (state, json.dumps(result), row["approval_id"]))
            conn.commit()
            if not isinstance(exc, Exception):
                raise
        else:
            if not result.get("attempted") and not result.get("submitted") and not result.get("uncertain"):
                handoff = _preflight_handoff(conn, row["job_hash"], auth, book_path)
                result = {**result, **handoff, "handoffs": max(1, result.get("handoffs", 0))}
                summary.setdefault("blocked", []).append(handoff)
            state = "submitted" if result.get("submitted") else "uncertain" if result.get("uncertain") else "needs_review"
            conn.execute("UPDATE application_approvals SET state=?,result_json=? WHERE approval_id=? AND (state='submitting' OR ?='submitted')",
                         (state, json.dumps(result), row["approval_id"], state)); conn.commit()
        for key in ("attempted", "submitted", "uncertain", "handoffs"):
            summary[key] += result.get(key, 0)
    return summary
