"""Finite preparation supervision consent, without submission authority."""
from __future__ import annotations

from pathlib import Path
import re
import time

from .. import config
from . import hourly_reports, overnight


WINDOW_NAME = "preparation-monitor-window.json"
SCOPE = "preparation monitoring and technical repair only"


def load(path=None, *, now=None):
    """A reporting window allows observation; repair needs separate consent.

    The mere existence of a dedicated window selects it, including invalid or
    revoked records. Never fall back to another grant after its revocation.
    Neither returned record is accepted by the submission authorization loader.
    """
    now = time.time() if now is None else now
    selected = Path(path or config.ROOT / "private" / WINDOW_NAME)
    if not selected.is_absolute():
        selected = config.ROOT / selected
    if path is None and not selected.exists() and not selected.is_symlink():
        selected = config.ROOT / "private" / hourly_reports.REPORT_WINDOW
    try:
        selected, window, digest = overnight._read_private(selected)
        if selected.stat().st_size > 65536:
            return None
        if window.get("scope") == hourly_reports.REPORT_SCOPE:
            report = hourly_reports.load_report_window(selected, now=now)
            if report is None or report["authorization_id"] != digest:
                return None
            return {**report, "authorization_path": str(selected),
                    "monitoring_kind": "report_observation", "repair_authority": False}
        start = overnight._timestamp(window["authorized_at"])
        expiry = overnight._timestamp(window["expires_at"])
        content = window.get("content", "")
        if (window.get("scope") != SCOPE or window.get("role") != "user"
                or window.get("status") != "verified" or window.get("enabled") is not True
                or window.get("submission_authority") is not False
                or window.get("repair_authority") is not True
                or not isinstance(window.get("source"), str) or not window["source"].strip()
                or not isinstance(content, str)
                or not re.search(r"\b(?:monitor(?:ing)?|oversee(?:ing)?|watch(?:ing)?|check(?:ing)?\s+logs)\b", content, re.I)
                or not re.search(r"\b(?:repair(?:s|ing)?|fix(?:ing)?|improv(?:e|ing))\b", content, re.I)
                or re.search(r"\b(?:do not|don't|never|stop|cancel|disable)\s+(?:monitor(?:ing)?|repair(?:s|ing)?|fix(?:ing)?|improv(?:e|ing))\b", content, re.I)
                or not start <= now < expiry or not 0 < expiry-start <= 86400):
            return None
        return {**window, "authorization_path": str(selected), "authorization_id": digest,
                "monitoring_kind": "preparation_repair"}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def configured():
    """A malformed/revoked window must not reactivate legacy submission consent."""
    return any(path.exists() or path.is_symlink() for path in (
        config.ROOT / "private" / WINDOW_NAME,
        config.ROOT / "private" / hourly_reports.REPORT_WINDOW,
    ))
