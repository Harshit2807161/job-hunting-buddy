"""Recover scoped saved answers after a crash before the SQLite queue update.

The booklet's explicit response is the durable outbox. Generic recovery only
requeues an unchanged waiting_input packet with exact current answer proof.
Reviewed drafts additionally need an explicit server-authored edit intent and
verified no-click evidence. Recovery grants no submission authority.
"""
from __future__ import annotations

import json
import hashlib
import re
import time
import stat
from pathlib import Path

from .. import config
from . import boards, booklet, questions


MAX_PACKET_BYTES = 2 * 1024 * 1024


def _country(value):
    return booklet.normalize(str(value)) if value is not None else None


def _response(book, record, job_hash, *, allow_declined=False):
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
    approved_value = (item.get("status") == "verified" and item.get("value") is not None
                      or allow_declined and item.get("status") == "declined" and item.get("value") is None)
    return (approved_value
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
    raw_missing = packet.get("missing", [])
    if not isinstance(raw_missing, list) or any(not isinstance(item, dict) for item in raw_missing):
        return False
    missing = [item for item in raw_missing if item.get("required", True)]
    if not missing:
        return False  # technical/source/role handoffs without an exact ledger gap are not user answers
    try:
        scope = questions._scope(job)
    except (ValueError, TypeError):
        return False
    ledger = [record for record in book.get("question_handoffs", {}).values()
              if isinstance(record, dict) and record.get("scope") == scope and job_hash in record.get("contexts", {})]
    from .question_routing import CANDIDATE, route
    if any(record.get("status") == "pending" and record["contexts"][job_hash].get("required")
           and not record["contexts"][job_hash].get("resolved")
           and route(book, record, record["contexts"][job_hash]) == CANDIDATE for record in ledger):
        return False
    explicit_responses = 0
    for item in missing:
        if not isinstance(item.get("question"), str) or questions._SECRET.search(item["question"]):
            return False
        matches = [record for record in ledger
                   if record.get("normalized_question") == booklet.normalize(item["question"])
                   and _country(record.get("country_context")) == _country(item.get("country_context"))
                   and record["contexts"][job_hash].get("ref") == item.get("ref")
                   and record["contexts"][job_hash].get("required") is True]
        if len(matches) != 1:
            return False
        record = matches[0]
        if _response(book, record, job_hash):
            explicit_responses += 1
        elif (record.get("status") != "pending"
              or route(book, record, record["contexts"][job_hash]) == CANDIDATE):
            return False
    # Merely routing a technical/document task never creates a replay trigger.
    return explicit_responses > 0


def _active_authority(conn, job_hash):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "application_approvals" in tables and conn.execute(
            "SELECT 1 FROM application_approvals WHERE job_hash=? AND state IN ('approved','submitting')", (job_hash,)).fetchone():
        return True
    if "authorized_submission_attempts" in tables and conn.execute(
            "SELECT 1 FROM authorized_submission_attempts WHERE job_hash=? AND state IN ('in_progress','uncertain','submitted')", (job_hash,)).fetchone():
        return True
    return False


def _no_terminal_click(conn, job_hash, job):
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE name='authorized_submission_attempts'").fetchone()
    if not table:
        return True
    row = conn.execute("SELECT * FROM authorized_submission_attempts WHERE job_hash=?", (job_hash,)).fetchone()
    if row is None:
        return True
    if row["state"] not in {"waiting_review", "waiting_input", "waiting_login", "waiting_captcha"}:
        return False
    try:
        from .authorized_submission import private_file
        proof = json.loads(private_file(row["attempt_path"]).read_bytes())
        return (proof.get("runtime_click_started") is False and proof.get("job_hash") == job_hash
                and proof.get("authorization_id") == row["authorization_id"]
                and boards.job_identity(proof.get("application_url")) == boards.job_identity(job.get("url")))
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        return False


def _explicit_review_edit(book, job, packet, packet_path, packet_bytes, book_path):
    """A server-authored edit intent proves which reviewed draft the user edited.

    Old facts legitimately differ after the answer; the original binding's
    digest proves the old review revision instead of silently recomputing it.
    Generic answered records can never reset a waiting_review application.
    """
    job_hash = job.get("dedupe_hash")
    if (packet.get("state") != "waiting_review" or packet.get("submitted") is not False
            or packet.get("missing") or packet.get("verification")
            or packet.get("job", {}).get("dedupe_hash") != job_hash
            or boards.application_hash(job.get("url")) != job_hash
            or boards.job_identity(packet.get("job", {}).get("url")) != boards.job_identity(job.get("url"))
            or booklet.job_excluded(book, job)):
        return False
    inventory = packet.get("review_inventory", {})
    if inventory.get("complete") is not True or not isinstance(inventory.get("fields"), list):
        return False
    fields = inventory["fields"]
    if not fields or any(not isinstance(f, dict) or not f.get("ref") or not f.get("question") for f in fields):
        return False
    if len({f["ref"] for f in fields}) != len(fields):
        return False
    try:
        scope = questions._scope(job)
        from .approvals import _digest
        from .capture import MAX_BYTES, valid as valid_capture
        image = packet_path.with_name("browser.png")
        from .authorized_submission import private_file
        image_stat = image.lstat()
        if (image.is_symlink() or any(parent.is_symlink() for parent in image.parents)
                or not image.resolve().is_relative_to((config.ROOT / "private").resolve())
                or not stat.S_ISREG(image_stat.st_mode) or image_stat.st_mode & 0o077
                or image_stat.st_size > MAX_BYTES):
            return False
        image_bytes = image.read_bytes()
        if packet.get("capture", {}).get("verified") is not True or not valid_capture(packet, image_bytes):
            return False
    except (OSError, ValueError, TypeError):
        return False
    for record in book.get("question_handoffs", {}).values():
        if not isinstance(record, dict) or record.get("scope") != scope or not _response(book, record, job_hash, allow_declined=True):
            continue
        context = record.get("contexts", {}).get(job_hash, {})
        intent = record.get("candidate_edit_intents", {}).get(job_hash, {})
        if (not isinstance(intent, dict) or intent.get("provider") != "local_dashboard_explicit_edit"
                or intent.get("job_hash") != job_hash or intent.get("question_id") != record.get("id")
                or intent.get("answer_revision") != record.get("answer_revision")
                or intent.get("approval_revoked") is not True or context.get("resolved")):
            continue
        matching = [f for f in fields if f["ref"] == context.get("ref")
                    and booklet.normalize(f["question"]) == record.get("normalized_question")]
        if len(matching) != 1:
            continue
        response = book.get("custom_answers", {}).get(record.get("custom_answer_key"), {})
        if response.get("status") == "declined" and (context.get("required") is not False
                                                    or matching[0].get("required") is not False):
            continue
        binding = intent.get("review_binding", {})
        if (not isinstance(binding, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(intent.get("review_revision", "")))
                or _digest(binding) != intent["review_revision"]
                or binding.get("packet_sha256") != hashlib.sha256(packet_bytes).hexdigest()
                or intent.get("packet_sha256") != binding["packet_sha256"]
                or binding.get("screenshot_sha256") != hashlib.sha256(image_bytes).hexdigest()
                or binding.get("selected_role") not in {"sde", "ml"}):
            continue
        try:
            if (private_file(intent.get("packet_path", "")) != packet_path
                    or private_file(binding.get("packet_path", "")) != packet_path
                    or private_file(binding.get("book_path", "")) != Path(book_path).resolve()):
                continue
        except (OSError, ValueError, TypeError):
            continue
        return True
    return False


def recover(conn, book_path=booklet.DEFAULT_PATH, *, limit=100):
    """Idempotent, bounded restart reconciliation, serialized with answer writes.

    Generic recovery only touches waiting_input. A waiting_review draft needs
    a server-authored exact explicit-edit intent and no terminal-click proof.
    Running, submitted, uncertain and active-authority records stay protected;
    no approval is renewed or retry reset repeated once the job is queued.
    """
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Answer recovery limit must be between 1 and 200")
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='applications'").fetchone():
        return 0
    from .authorized_submission import private_file
    recovered = 0
    with questions._locked(book_path):
        book = booklet.load(book_path)
        rows = conn.execute("SELECT job_hash,job_json,packet,state FROM applications WHERE state IN ('waiting_input','waiting_review') "
                            "AND packet IS NOT NULL ORDER BY updated_at,job_hash LIMIT ?", (limit,)).fetchall()
        for row in rows:
            if _active_authority(conn, row["job_hash"]):
                continue
            try:
                path = private_file(Path(row["packet"]).with_name("packet.json"))
                if not path.is_file() or path.stat().st_size > MAX_PACKET_BYTES:
                    continue
                packet_bytes = path.read_bytes()
                packet, job = json.loads(packet_bytes), json.loads(row["job_json"])
                if job.get("dedupe_hash") != row["job_hash"]:
                    continue
                safe = (_no_terminal_click(conn, row["job_hash"], job) and
                        (eligible(book, job, packet) if row["state"] == "waiting_input" else
                         _explicit_review_edit(book, job, packet, path, packet_bytes, book_path)))
                if not safe:
                    continue
            except (OSError, ValueError, TypeError, KeyError):
                continue
            recovered += conn.execute(
                "UPDATE applications SET state='queued',lease_until=NULL,attempts=0,available_at=0,error_kind=NULL,"
                "updated_at=?,notified_at=NULL WHERE job_hash=? AND state=?",
                (int(time.time()), row["job_hash"], row["state"])).rowcount
        conn.commit()
    return recovered
