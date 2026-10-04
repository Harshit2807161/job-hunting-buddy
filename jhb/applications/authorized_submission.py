"""Finite, explicit submission authorization; normal preparation stays guarded."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from .booklet import write_private
from .cli_browser import BrowserOperationError, BrowserUseCLI
from .queue import greenhouse_identity

SCOPE = "new Phase 1 Greenhouse jobs discovered during this authorization window"


def private_file(value):
    path = Path(value)
    root = config.ROOT / "private"
    if (not path.is_absolute() or path.is_symlink() or any(p.is_symlink() for p in path.parents)
            or not path.resolve().is_relative_to(root.resolve()) or not path.is_file()
            or path.stat().st_mode & 0o077 or path.stat().st_size > 2_000_000):
        raise ValueError("Submission evidence must be a private local file")
    return path


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Submission timestamps require timezone information")
    return result.astimezone(timezone.utc)


def load_gate(authorization_path, attempt_path, *, now=None, allow_clicked=False):
    """Re-read immutable user authority and the durable, job-specific attempt."""
    authorization_path, attempt_path = private_file(authorization_path), private_file(attempt_path)
    raw = authorization_path.read_bytes()
    authority = json.loads(raw)
    attempt = json.loads(attempt_path.read_text())
    moment = now or datetime.now(timezone.utc)
    start = timestamp(authority.get("authorized_at", authority.get("started_at", "")))
    expiry = timestamp(authority.get("expires_at", authority.get("expiry", "")))
    content = authority.get("content", "").casefold()
    explicit = ("submitting" in content and "phase 1" in content and "night" in content
                and "do not submit" not in content and "don't submit" not in content)
    if (os.environ.get("JHB_OVERNIGHT_SUBMISSIONS_ENABLED") != "1"
            or authority.get("role") != "user" or authority.get("enabled") is not True or not explicit
            or authority.get("status") != "verified" or authority.get("scope") != SCOPE
            or authority.get("board") != "greenhouse"
            or not all(authority.get(key) is True for key in (
                "require_browser_double_check", "pause_unknown_answers", "require_receipt_before_sheet"))
            or not start <= moment < expiry or not 0 < (expiry-start).total_seconds() <= 86400):
        raise ValueError("No active finite user authorization for this submission")
    digest = hashlib.sha256(raw).hexdigest()
    identity = greenhouse_identity(attempt.get("application_url"))
    if (attempt.get("authorization_id") != digest
            or attempt.get("authorization_path") != str(authorization_path)
            or attempt.get("state") != "in_progress"
            or not re.fullmatch(r"[a-f0-9]{64}", attempt.get("job_hash", ""))
            or identity is None
            or hashlib.sha256("|".join(identity or ()).encode()).hexdigest() != attempt.get("job_hash")
            or (attempt.get("runtime_click_started") and not allow_clicked)
            or not start <= timestamp(attempt.get("started_at", "")) < expiry):
        raise ValueError("Submission attempt is mismatched, consumed, or not durable")
    expected = config.ROOT / "private" / "authorized-submissions" / attempt["job_hash"] / "attempt.json"
    if attempt_path != expected:
        raise ValueError("Submission attempt has an unexpected private location")
    packet_path = private_file(attempt.get("packet_path", ""))
    packet_bytes = packet_path.read_bytes()
    packet = json.loads(packet_bytes)
    if (hashlib.sha256(packet_bytes).hexdigest() != attempt.get("packet_sha256")
            or packet.get("state") != "waiting_review" or packet.get("submitted") is True
            or packet.get("missing") or packet.get("blocked_requests", 0)
            or packet.get("job", {}).get("dedupe_hash") != attempt["job_hash"]
            or greenhouse_identity(packet.get("job", {}).get("url")) != greenhouse_identity(attempt["application_url"])):
        raise ValueError("Submission packet is changed, incomplete, or for another job")
    return authority, attempt, packet


def document_manifest(answers, packet):
    if answers.get("selected_role") not in {"sde", "ml"} or answers.get("filled") != packet.get("filled"):
        raise ValueError("Submission manifest must preserve the selected role and approved packet")
    documents = answers.get("documents", {})
    if "documents.resume" not in documents:
        raise ValueError("The selected role's verified resume is required")
    manifest = {}
    for key, record in documents.items():
        if key not in {"documents.resume", "documents.cover_letter"} or record.get("status") != "verified" or not record.get("source"):
            raise ValueError("Submission documents require verified source records")
        path = Path(record["value"])
        if (not path.is_absolute() or not path.is_file() or path.suffix.lower() != ".pdf"
                or not path.read_bytes().startswith(b"%PDF-")):
            raise ValueError("Approved application document is not an existing PDF")
        filled = [r for r in packet.get("filled", []) if r.get("key") == key]
        if not filled or any(Path(r.get("value", "")) != path for r in filled):
            raise ValueError("Application packet uses a different approved document")
        manifest[key] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                         "filename": path.name}
    filled_keys = {r.get("key") for r in packet.get("filled", []) if str(r.get("key", "")).startswith("documents.")}
    if filled_keys != set(manifest):
        raise ValueError("Every attached document must have an approved manifest record")
    return manifest


class AuthorizedSubmissionCLI(BrowserUseCLI):
    _dispatch_module = "jhb.applications.submission_runtime"


async def submit_reviewed(job, packet_path, answers, *, authorization, attempt, cli=None):
    """Submit once, after durable ownership and two fresh retained-value checks.

    ``answers`` contains selected_role, documents (verified booklet records), and
    filled (the packet's exact approved records). Caller owns queue/tracking;
    receipt.json is persisted before a confirmed success is returned.
    """
    client = cli or AuthorizedSubmissionCLI(timeout=120)
    attempt_path = Path(attempt)
    authorization_path = authorization["authorization_path"]
    context = {"authorization_path": authorization_path, "attempt_path": str(attempt_path)}
    national = answers.get("approved_phone_national", {})
    if national.get("status") == "verified" and national.get("source") and isinstance(national.get("value"), str):
        context["approved_phone_national"] = national
    try:
        _, persisted, packet = load_gate(authorization_path, attempt_path)
        if (str(private_file(packet_path)) != persisted["packet_path"]
                or job.get("dedupe_hash") != persisted["job_hash"]
                or greenhouse_identity(job.get("url")) != greenhouse_identity(persisted["application_url"])):
            raise ValueError("Requested job differs from the durable attempt")
        documents = document_manifest(answers, packet)
        located = await client.invoke("locate", **context)
        if located.get("state"):
            return located
        client.target_id, client.expected_url = located["target_id"], persisted["application_url"]
        # A freshly instantiated controller cannot trust an attached basename.
        # Replace only approved documents; mutation-sensitive receipts bind bytes.
        for key, document in documents.items():
            snapshot = await client.invoke("check", **context, documents=documents, require_receipts=False)
            if snapshot.get("state"):
                return snapshot
            matches = [f for f in snapshot["fields"] if f.get("answer_key") == key]
            if len(matches) != 1:
                raise ValueError("Approved document control is unavailable or ambiguous")
            result = await client.invoke("document", **context, field=matches[0], value=document["path"])
            if not result.get("verified") or not result.get("upload_receipt"):
                raise ValueError("Approved document upload was not verified")
            document["receipt"] = result["upload_receipt"]
        return await client.invoke("submit", **context, documents=documents)
    except Exception as exc:
        # Transport failure after a durable click is uncertain, never replayable.
        try:
            safe_attempt = private_file(attempt_path)
            latest = json.loads(safe_attempt.read_text())
        except (OSError, ValueError, TypeError):
            safe_attempt, latest = None, {}
        result = {"state": "uncertain" if latest.get("runtime_click_started") else "waiting_review",
                  "reason": "Authorized submission needs technical review", "error_kind": type(exc).__name__,
                  "click_started": bool(latest.get("runtime_click_started")),
                  "retryable": not latest.get("runtime_click_started") and (
                      isinstance(exc, (TimeoutError, ConnectionError, FileNotFoundError))
                      or isinstance(exc, BrowserOperationError) and exc.retryable)}
        if safe_attempt:
            write_private(attempt_path.parent / "runtime-error.json", result)
        return result
