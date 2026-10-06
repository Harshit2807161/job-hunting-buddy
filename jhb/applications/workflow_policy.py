"""Finite user-selected submission policy; no browser or per-job approval writes."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import time

from . import boards, booklet, overnight, questions

EMPTY_REVISION = hashlib.sha256(b"no workflow authorization").hexdigest()
DURATION_SECONDS = 8 * 60 * 60


def _read(root):
    path = Path(root) / "private" / overnight.AUTH_NAME
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("Workflow policy cannot use symlinks")
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return path, {}, EMPTY_REVISION
    if len(data) > 2_000_000:
        raise ValueError("Workflow policy exceeds the private artifact limit")
    digest = hashlib.sha256(data).hexdigest()
    try:
        auth = json.loads(data)
        return path, auth if isinstance(auth, dict) else {}, digest
    except (ValueError, TypeError):
        return path, {}, digest


def snapshot(root, *, now=None):
    """Expose policy metadata, never candidate data or the authorization document."""
    now = time.time() if now is None else now
    _, auth, revision = _read(root)
    valid = False
    try:
        valid = (auth.get("approval_mode") == "independent_reviewer"
                 and auth.get("scope") == overnight.MULTI_SCOPE
                 and overnight.valid_authority(auth, now=now))
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    active = valid and overnight.gate_enabled(auth)
    requested = auth.get("enabled") is True and auth.get("approval_mode") == "independent_reviewer"
    reasons = []
    if requested and not valid:
        reasons.append("Authorization expired or is invalid; individual approval is required.")
    elif valid and not active:
        reasons.append("The local submission runtime is disabled; individual approval remains required.")
    return {"mode": "autonomous" if active else "review", "requested_mode": "autonomous" if requested else "review",
            "revision": revision, "enabled_until": auth.get("expires_at") if valid else None,
            "available_submission_boards": sorted(b for b in boards.ADAPTERS if boards.submission_supported(b)),
            "authorized_boards": auth.get("boards", []) if active else [], "gate_reasons": reasons,
            "independent_review_required": True, "unknown_facts_require_input": True,
            "max_duration_hours": DURATION_SECONDS // 3600}


def set_mode(root, mode, *, revision, now=None):
    """Called only after the portal's same-origin CSRF-protected user action.

    The artifact is the sole policy source, so returning to review revokes the
    broad authority without a split-brain sidecar or changes to actual receipts.
    Running terminal workers must re-read this authority before any final click.
    """
    if mode not in {"review", "autonomous"}:
        raise ValueError("Unknown workflow mode")
    now = int(time.time() if now is None else now)
    path = Path(root) / "private" / overnight.AUTH_NAME
    with questions._locked(path):
        _, previous, current_revision = _read(root)
        if revision != current_revision:
            raise ValueError("Workflow mode changed; refresh before changing it")
        when = datetime.fromtimestamp(now, timezone.utc).isoformat()
        if mode == "autonomous":
            enabled_boards = sorted(b for b in boards.ADAPTERS if boards.submission_supported(b))
            if not enabled_boards:
                raise ValueError("No submission adapters are enabled")
            auth = {"enabled": True, "status": "verified", "role": "user", "source": "local_review_portal",
                    "action": "enable_autonomy", "approval_mode": "independent_reviewer",
                    "scope": overnight.MULTI_SCOPE, "candidate_job_policy": overnight.MULTI_JOB_POLICY,
                    "boards": enabled_boards, "authorized_at": when,
                    "expires_at": datetime.fromtimestamp(now + DURATION_SECONDS, timezone.utc).isoformat(),
                    "require_independent_review": True, "require_complete_inventory": True,
                    "require_browser_double_check": True, "pause_unknown_answers": True,
                    "require_receipt_before_sheet": True, "event_id": secrets.token_hex(16)}
            if not overnight.valid_authority(auth, now=now):
                raise ValueError("Autonomous submission policy is unavailable in this runtime")
        else:
            auth = {**previous, "enabled": False, "status": "revoked", "revoked_at": when,
                    "revoked_by": "local_review_portal", "event_id": secrets.token_hex(16)}
        if previous:
            booklet.write_private(path.parent / "workflow-policy-history" / f"{current_revision}.json", previous)
        booklet.write_private(path, auth)
        return snapshot(root, now=now)
