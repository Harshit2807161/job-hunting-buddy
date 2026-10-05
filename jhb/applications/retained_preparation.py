"""Keep generic preparation away from canonical reviewed or approved drafts."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .. import config
from . import boards


def retained_review(job, book, book_path=None):
    """Read canonical state, including packets outside the default artifact dir.

    A fresh explicit portal edit may requeue its bound draft. An approval, old
    saved answer, caller-selected artifact directory, or stale retry cannot.
    This function neither grants authority nor edits application evidence.
    """
    database = config.ROOT / "data" / "jobs.sqlite3"
    if not database.exists():
        return None
    with sqlite3.connect(database.resolve().as_uri()+"?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "applications" not in tables:
            return None
        row = conn.execute("SELECT * FROM applications WHERE job_hash=?", (job["dedupe_hash"],)).fetchone()
        if row is None:
            return None
        approval = conn.execute("SELECT state FROM application_approvals WHERE job_hash=? "
                                "ORDER BY approved_at DESC,rowid DESC LIMIT 1", (job["dedupe_hash"],)).fetchone() if "application_approvals" in tables else None
        protected = row["state"] == "waiting_review" or approval and approval["state"] in {"approved", "submitting", "needs_review"}
        try:
            from .authorized_submission import private_file
            path = private_file(Path(row["packet"]).with_name("packet.json"))
            raw = path.read_bytes()
            packet = json.loads(raw)
            if (packet.get("job", {}).get("dedupe_hash") != job["dedupe_hash"]
                    or boards.application_hash(job.get("url")) != job["dedupe_hash"]
                    or boards.job_identity(packet.get("job", {}).get("url")) != boards.job_identity(job.get("url"))):
                raise ValueError("Canonical review identity changed")
        except (OSError, ValueError, TypeError, KeyError):
            if not protected:
                return None
            return {"packet": None, "review_path": None, "reason": "Canonical reviewed draft requires explicit repair; preparation did not replace it"}
        reviewed = packet.get("state") == "waiting_review" and packet.get("submitted") is False
        if not protected and not reviewed:
            return None
        if reviewed and row["state"] in {"queued", "running"} and book_path:
            from .answer_resume import _active_authority, _no_terminal_click, _explicit_review_edit
            if (not _active_authority(conn, job["dedupe_hash"])
                    and _no_terminal_click(conn, job["dedupe_hash"], job)
                    and _explicit_review_edit(book, job, packet, path, raw, book_path)):
                return None
        return {"packet": packet if reviewed else None, "review_path": path.with_name("review.html"),
                "reason": "Existing reviewed draft preserved; use its portal approval/submission path"}
