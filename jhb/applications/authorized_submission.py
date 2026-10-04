"""Finite, explicit submission authorization; normal preparation stays guarded."""
from __future__ import annotations

import hashlib
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from .booklet import write_private
from .cli_browser import BrowserOperationError, BrowserUseCLI
from . import boards, overnight

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
    # A pause can arrive while the separate reviewer or pointer settling is
    # running. Recheck it at every operation, including immediately before the
    # write-ahead click marker. Receipt reconciliation does not use this gate:
    # an already observed positive receipt remains valid after a later pause.
    for guard in (config.ROOT / "private" / "pipeline-pause.json",
                  config.ROOT / "private" / "overnight-monitor" / "repair-pending.json"):
        if guard.exists() or guard.is_symlink():
            raise ValueError("Application automation is paused or quarantined")
    authorization_path, attempt_path = private_file(authorization_path), private_file(attempt_path)
    raw = authorization_path.read_bytes()
    authority = json.loads(raw)
    attempt = json.loads(attempt_path.read_text())
    moment = now or datetime.now(timezone.utc)
    start = timestamp(authority.get("authorized_at", authority.get("started_at", "")))
    expiry = timestamp(authority.get("expires_at", authority.get("expiry", "")))
    if (not overnight.gate_enabled(authority)
            or not overnight.valid_authority(authority, now=moment.timestamp())):
        raise ValueError("No active finite user authorization for this submission")
    digest = hashlib.sha256(raw).hexdigest()
    identity = boards.job_identity(attempt.get("application_url"))
    allowed = ["greenhouse"] if authority.get("scope") == SCOPE else authority.get("boards", [])
    if (attempt.get("authorization_id") != digest
            or attempt.get("authorization_path") != str(authorization_path)
            or attempt.get("state") != "in_progress"
            or not re.fullmatch(r"[a-f0-9]{64}", attempt.get("job_hash", ""))
            or identity is None
            or identity[0] not in allowed or not boards.submission_supported(identity[0])
            or boards.application_hash(attempt.get("application_url")) != attempt.get("job_hash")
            or (authority.get("require_independent_review") is True and attempt.get("require_independent_review") is not True)
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
            or packet.get("state") != "waiting_review" or packet.get("submitted") is not False
            or packet.get("missing") or packet.get("verification") or packet.get("blocked_requests", 0)
            or packet.get("job", {}).get("dedupe_hash") != attempt["job_hash"]
            or boards.job_identity(packet.get("job", {}).get("url")) != identity):
        raise ValueError("Submission packet is changed, incomplete, or for another job")
    if authority.get("scope") == overnight.PORTAL_SCOPE:
        from .approvals import validate_binding
        validate_binding(authority, packet_path)
        if authority.get("job_hash") != attempt["job_hash"]:
            raise ValueError("Portal approval belongs to a different application")
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


def audit_hash(snapshot, documents):
    return hashlib.sha256(json.dumps({"snapshot": snapshot, "documents": documents}, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


async def submit_reviewed(job, packet_path, answers, *, authorization, attempt, cli=None, reviewer=None):
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
        authority, persisted, packet = load_gate(authorization_path, attempt_path)
        if (str(private_file(packet_path)) != persisted["packet_path"]
                or job.get("dedupe_hash") != persisted["job_hash"]
                or boards.job_identity(job.get("url")) != boards.job_identity(persisted["application_url"])):
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
        if authority.get("require_independent_review") is True:
            snapshot = await client.invoke("check", **context, documents=documents)
            if snapshot.get("state"):
                return snapshot
            if reviewer is None:
                from .application_review import review_application
                reviewer = review_application
            independent = await asyncio.to_thread(reviewer, job, {**answers, "documents": documents,
                                                                 "approved_documents": answers["documents"],
                                                                 "application_inventory": packet.get("review_inventory"),
                                                                 "user_blank_acknowledgments": authority.get("acknowledged_blank_refs", [])}, snapshot, authorization)
            from .application_review import snapshot_digest
            snapshot_sha = snapshot_digest(snapshot)
            if (not isinstance(independent, dict) or independent.get("verdict") != "approved"
                    or independent.get("reviewer") != "codex-readonly"
                    or independent.get("source") != "independent_application_review"
                    or independent.get("issues") != []
                    or independent.get("snapshot_sha256") != snapshot_sha
                    or independent.get("authorization_id") != persisted["authorization_id"]
                    or independent.get("job_hash") != persisted["job_hash"]):
                return {"state": "waiting_review", "reason": "Independent application review did not approve the retained draft", "click_started": False}
            reviewed_book = private_file(independent.get("approved_book_path", ""))
            if hashlib.sha256(reviewed_book.read_bytes()).hexdigest() != independent.get("approved_book_sha256"):
                return {"state": "waiting_review", "reason": "Candidate answers changed during independent review", "click_started": False}
            # The runtime verifies this exact snapshot again inside the browser
            # lane. A reviewer never receives authority to alter field values.
            token = {"verdict": "approved", "source": "independent_application_review",
                     "job_hash": persisted["job_hash"], "authorization_id": persisted["authorization_id"],
                     "packet_sha256": persisted["packet_sha256"], "audit_sha256": audit_hash(snapshot, documents),
                     "approved_book_path": str(reviewed_book), "approved_book_sha256": independent["approved_book_sha256"],
                     "review": independent, "reviewed_at": datetime.now(timezone.utc).isoformat()}
            write_private(attempt_path.parent / "independent-review.json", token)
        return await client.invoke("submit", **context, documents=documents)
    except Exception as exc:
        # Transport failure after a durable click is uncertain, never replayable.
        try:
            safe_attempt = private_file(attempt_path)
            latest = json.loads(safe_attempt.read_text())
        except (OSError, ValueError, TypeError):
            safe_attempt, latest = None, {}
        transport = isinstance(exc, RuntimeError) and str(exc) in {
            "Browser Use CLI failed; run browser-use --doctor",
            "Browser Use CLI returned no structured result",
        }
        result = {"state": "uncertain" if latest.get("runtime_click_started") else "waiting_review",
                  "reason": "Authorized submission needs technical review",
                  "error_kind": "browser_transport" if transport else type(exc).__name__,
                  "click_started": bool(latest.get("runtime_click_started")),
                  "retryable": not latest.get("runtime_click_started") and (
                      transport or isinstance(exc, (TimeoutError, ConnectionError, FileNotFoundError))
                      or isinstance(exc, BrowserOperationError) and exc.retryable)}
        if safe_attempt:
            write_private(attempt_path.parent / "runtime-error.json", result)
        return result
