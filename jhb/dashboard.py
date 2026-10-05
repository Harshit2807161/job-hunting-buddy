"""Loopback-only dashboard over the existing durable application pipeline.

GETs read SQLite and a small explicit artifact allowlist. Explicit candidate
actions save scoped answers or approve/revoke one immutable review draft.
Explicit draft focus reuses an existing guarded browser tab. An authenticated
candidate approval can dispatch that exact application for immediate submission.
GET requests never start browser work or external writes.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from typing import Literal

from . import config
from .applications import boards, booklet, questions

ZONE = ZoneInfo("America/Los_Angeles")
HASH = re.compile(r"[a-f0-9]{64}")
PNG = b"\x89PNG\r\n\x1a\n"


def _json(text, fallback=None):
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else ({} if fallback is None else fallback)
    except (ValueError, TypeError):
        return {} if fallback is None else fallback


def _location(job):
    value = job.get("location")
    if isinstance(value, str) and value.strip():
        return _text(value, 500)
    locations = job.get("locations", [])
    return _text(", ".join(item for item in locations if isinstance(item, str)), 500) if isinstance(locations, list) else ""


def _related_submissions(conn, job):
    """Informational same-company/title warning, never proof of duplicate jobs."""
    identity = boards.job_identity(job.get("url"))
    company, title = booklet.normalize(str(job.get("company") or "")), booklet.normalize(str(job.get("title") or ""))
    if not identity or not company or not title or not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='confirmed_submissions'").fetchone():
        return []
    matches = []
    for row in conn.execute("SELECT application_url,job_json,confirmed_at FROM confirmed_submissions ORDER BY confirmed_at DESC"):
        previous, prior_identity = _json(row["job_json"]), boards.job_identity(row["application_url"])
        if (not prior_identity or prior_identity == identity
                or booklet.normalize(str(previous.get("company") or "")) != company
                or booklet.normalize(str(previous.get("title") or "")) != title):
            continue
        canonical = boards.canonical_url(row["application_url"])
        if previous.get("url") and boards.job_identity(previous["url"]) != prior_identity:
            continue
        matches.append({"job_hash": boards.application_hash(canonical), "url": canonical,
                        "company": _text(previous.get("company")), "title": _text(previous.get("title")),
                        "location": _location(previous), "confirmed_at": row["confirmed_at"],
                        "confirmed_date": _day(row["confirmed_at"]), "relation": "same_title_prior_submission"})
        if len(matches) == 10:
            break
    return matches


def _stamp(value):
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _day(value):
    stamp = _stamp(value)
    return stamp.astimezone(ZONE).date().isoformat() if stamp else None


def _text(value, length=300):
    return value[:length] if isinstance(value, str) else ""


def _application_presentation(conn, job_hash, state):
    """Expose durable approval progress without changing queue authority."""
    stage, approval_state = state, None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='application_approvals'").fetchone():
        row = conn.execute("SELECT state,expires_at FROM application_approvals WHERE job_hash=? "
                           "ORDER BY approved_at DESC,rowid DESC LIMIT 1", (job_hash,)).fetchone()
        if row:
            approval_state = row["state"]
            if approval_state == "approved" and row["expires_at"] <= datetime.now(timezone.utc).timestamp():
                approval_state = "expired"
    if state == "waiting_review":
        stage = {"approved": "approval_queued", "submitting": "submitting",
                 "needs_review": "needs_review", "failed": "needs_review", "expired": "needs_review",
                 "uncertain": "submission_uncertain"}.get(approval_state, state)
    return {"display_state": stage, "approval_state": approval_state}


def _approval_outcome(conn, job_hash):
    """Explain a consumed approval without granting another submission attempt."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='application_approvals'").fetchone():
        return None
    approval = conn.execute("SELECT state,approved_at,expires_at,result_json FROM application_approvals "
                            "WHERE job_hash=? ORDER BY approved_at DESC,rowid DESC LIMIT 1", (job_hash,)).fetchone()
    if not approval or approval["state"] not in {"needs_review", "invalidated", "expired", "uncertain", "failed"}:
        return None
    result = _json(approval["result_json"])
    attempt = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='authorized_submission_attempts'").fetchone():
        attempt = conn.execute("SELECT result_json FROM authorized_submission_attempts "
                               "WHERE job_hash=? AND started_at>=?", (job_hash, approval["approved_at"])).fetchone()
    attempted = _json(attempt["result_json"]) if attempt else {}
    reason = _text(result.get("reason") or attempted.get("reason"), 1000)
    # These older diagnostics incorrectly implied that candidate edits were an
    # error. Current-form approval captures those edits without restoring them.
    reason = {
        "Approved binding or retained field validity changed":
            "The previous submission check could not validate the current form. Your browser edits were preserved.",
        "An approved answer or document did not remain intact":
            "The previous submission check could not verify an answer or attachment. Your browser edits were preserved.",
    }.get(reason, reason)
    if not reason:
        reason = ("The approved draft did not pass submission preflight; no submission was recorded."
                  if result.get("attempted") == 0 else "The previous approval needs review before submission can continue.")
    return {"state": approval["state"], "reason": reason,
            "approved_at": approval["approved_at"], "expires_at": approval["expires_at"],
            "expired": approval["expires_at"] <= datetime.now(timezone.utc).timestamp(),
            "click_started": attempted.get("click_started") if isinstance(attempted.get("click_started"), bool) else None}


class DashboardStore:
    def __init__(self, root, db_path, book_path):
        self.root, self.db_path, self.book_path = Path(root), Path(db_path), Path(book_path)
        self.answer_lock = threading.Lock()

    @contextmanager
    def connection(self, *, write=False):
        if not self.db_path.is_file() or self.db_path.is_symlink():
            raise FileNotFoundError("Pipeline database unavailable")
        conn = sqlite3.connect(self.db_path.as_uri()+f"?mode={'rw' if write else 'ro'}", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def private_bytes(self, path, *, limit=1024*1024, read_count=None):
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):
            raise ValueError("Private artifacts cannot be symlinks")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to((self.root / "private").resolve()) or not resolved.is_file():
            raise ValueError("Artifact outside private allowlist")
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            if os.fstat(stream.fileno()).st_size > limit:
                raise ValueError("Artifact exceeds allowed size")
            return stream.read(limit+1 if read_count is None else read_count)

    def book(self):
        # Do not serialize the booklet: only pending question metadata escapes.
        self.private_bytes(self.book_path)
        return booklet.load(self.book_path)

    def packet(self, row, job, *, with_digest=False):
        candidates = []
        if row["packet"]:
            candidates.append(Path(row["packet"]).parent / "packet.json")
        for folder in ("applications", "multi-board/applications"):
            candidates.append(self.root / "private" / folder / row["job_hash"] / "packet.json")
        for path in dict.fromkeys(candidates):
            try:
                content = self.private_bytes(path)
                packet = _json(content)
                pjob = packet.get("job", {})
                if (pjob.get("dedupe_hash") == row["job_hash"]
                        and boards.application_hash(pjob.get("url")) == row["job_hash"]
                        and boards.job_identity(pjob.get("url")) == boards.job_identity(job.get("url"))):
                    return (path, packet, hashlib.sha256(content).hexdigest()) if with_digest else (path, packet)
            except (ValueError, OSError, TypeError):
                continue
        return (None, {}, None) if with_digest else (None, {})

    def tab_ledger(self):
        """Read existing reconciliation evidence; GET never probes Chrome."""
        try:
            value = _json(self.private_bytes(self.root / "private" / "browser-tab-ledger.json"))
            return value.get("tabs", {}) if value.get("schema_version") == 1 and isinstance(value.get("tabs"), dict) else {}
        except (ValueError, OSError, TypeError):
            return {}

    @staticmethod
    def draft_target_state(packet, tabs):
        capture = packet.get("capture", {})
        job = packet.get("job", {})
        if not isinstance(capture, dict) or capture.get("verified") is not True or capture.get("method") != "browser_use_cli":
            return "unknown"
        target = capture.get("target_id")
        row = tabs.get(target, {}) if isinstance(target, str) else {}
        identity = boards.job_identity(job.get("url"))
        if (not isinstance(row, dict) or not identity or row.get("job_identity") != list(identity)
                or boards.job_identity(row.get("requested_url")) != identity
                or row.get("creation_proof") not in {"official_new_tab_returned_new_target", "native_linkedin_apply_opener"}):
            return "unknown"
        state = row.get("state")
        if state in {"closed", "departed"}:
            observed, captured = _stamp(row.get(state+"_at")), _stamp(capture.get("captured_at"))
            if observed and captured and observed >= captured:
                return "unavailable"
        return "retained" if state == "active" else "unknown"

    def screenshot(self, job_hash):
        if not HASH.fullmatch(job_hash):
            raise ValueError("Invalid job identity")
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM applications WHERE job_hash=?", (job_hash,)).fetchone()
        if not row:
            raise FileNotFoundError("No application screenshot")
        path, packet = self.packet(row, _json(row["job_json"]))
        if path is None:
            raise FileNotFoundError("No validated review packet")
        content = self.private_bytes(path.with_name("browser.png"), limit=15*1024*1024)
        from .applications.capture import valid as valid_capture
        if not valid_capture(packet, content):
            raise ValueError("Review artifact is not a matching current capture")
        return content

    def pending(self, book=None, queue_states=None):
        book = self.book() if book is None else book
        manual_states = {}
        for record in book.get("manual_application_records", {}).values():
            if isinstance(record, dict) and (key := boards.application_hash(record.get("url"))):
                manual_states[key] = record.get("state")
        output = []
        for record in book.get("question_handoffs", {}).values():
            if record.get("status") != "pending" or questions._SECRET.search(record.get("question", "")):
                continue
            contexts = []
            from .applications.question_routing import candidate_contexts
            routed_contexts = candidate_contexts(book, record)
            for key, context in record.get("contexts", {}).items():
                if key not in routed_contexts or context.get("resolved") or (queue_states is not None and queue_states.get(key) in
                                               {"submitted", "submission_uncertain", "skipped", "discarded"}) or manual_states.get(key) in {
                                                   "submitted", "submission_uncertain", "skipped", "declined"} or booklet.job_excluded(book, {"dedupe_hash": key}):
                    continue
                contexts.append({"job_hash": key, "company": _text(context.get("company")),
                    "title": _text(context.get("title")), "url": boards.canonical_url(context.get("url")),
                    "required": context.get("required") is True, "type": _text(context.get("type"), 50),
                    "reason": _text(context.get("reason"), 500),
                    "description": _text(context.get("description"), 4096),
                    "description_truncated": context.get("description_truncated") is True or
                        isinstance(context.get("description"), str) and len(context["description"]) > 4096,
                    "public_metadata_description": _text(context.get("public_question_metadata", {}).get("description"), 4096)
                        if isinstance(context.get("public_question_metadata"), dict) and context["public_question_metadata"].get("source") == "official_public_question_metadata" else "",
                    "choices": [_text(option) for option in context.get("choices", []) if isinstance(option, str)][:100]})
            if not contexts:
                continue
            output.append({"id": record["id"], "question": _text(record["question"], 3000),
                "kind": record.get("kind", "field"), "updated_at": record.get("updated_at"),
                "country_context": record.get("country_context"), "contexts": contexts,
                "required": any(c["required"] for c in contexts)})
        return sorted(output, key=lambda q: (q["updated_at"] or "", q["id"]))

    def paused(self):
        path = self.root / "private" / "pipeline-pause.json"
        return path.exists() or path.is_symlink()

    def submission_hold_reason(self):
        if self.paused():
            return "Automation is paused. Resume it before approving a submission. Your browser edits are preserved."
        repair = self.root / "private" / "overnight-monitor" / "repair-pending.json"
        if repair.exists() or repair.is_symlink():
            return "Submission is temporarily held while a pipeline repair is validated. Your browser edits are preserved."
        return None

    def details(self, job_hash):
        if not HASH.fullmatch(job_hash):
            raise ValueError("Invalid application identity")
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM applications WHERE job_hash=?", (job_hash,)).fetchone()
        if not row:
            raise FileNotFoundError("Application unavailable")
        job = _json(row["job_json"])
        if boards.application_hash(job.get("url")) != job_hash:
            raise ValueError("Application identity mismatch")
        with self.connection() as conn:
            related = _related_submissions(conn, job)
            presentation = _application_presentation(conn, job_hash, row["state"])
            approval_outcome = _approval_outcome(conn, job_hash)
        path, packet, displayed_packet_sha = self.packet(row, job, with_digest=True)
        draft_target = self.draft_target_state(packet, self.tab_ledger())
        manifest = packet.get("review_inventory", {})
        inventory = manifest.get("fields", packet.get("review_questions", [])) if isinstance(manifest, dict) else []
        complete_inventory = isinstance(inventory, list) and bool(inventory) and manifest.get("complete") is True
        filled = packet.get("filled", [])
        if not complete_inventory:
            inventory = [{"ref": r.get("ref"), "question": r.get("question"), "status": "answered",
                          "answer_key": r.get("key"), "required": None} for r in filled]
            inventory += [{**r, "status": "blank"} for name in ("missing", "optional_questions", "unknown_questions")
                          for r in packet.get(name, [])]
        output = []
        for field in inventory:
            if not isinstance(field, dict) or questions._SECRET.search(str(field.get("question", ""))):
                continue
            record = next((r for r in filled if r.get("ref") == field.get("ref") and
                           (not field.get("answer_key") or r.get("key") == field.get("answer_key"))), None)
            value = record.get("value") if record else None
            if record and str(record.get("key", "")).startswith("documents."):
                value = Path(str(value)).name
            if isinstance(value, dict):
                value = value.get("choice")  # Approved autocomplete display only.
            if isinstance(value, str):
                value = _text(value, 10000)
            elif isinstance(value, list):
                value = [_text(v) for v in value if isinstance(v, str)][:100]
            elif value is not None and not isinstance(value, (bool, int, float)):
                value = None
            output.append({"ref": _text(field.get("ref")), "question": _text(field.get("question"), 3000),
                "type": _text(field.get("type"), 50), "required": field.get("required"),
                "description": _text(field.get("description"), 4096),
                "description_truncated": field.get("description_truncated") is True or
                    isinstance(field.get("description"), str) and len(field["description"]) > 4096,
                "category": _text(field.get("category"), 100), "status": _text(field.get("status"), 100),
                "candidate_wording_required": field.get("candidate_wording_required") is True,
                "step": field.get("step") if isinstance(field.get("step"), (str, int)) else None,
                "answer": value, "answer_key": _text(field.get("answer_key")), "has_source": bool(record and record.get("source")),
                "proposed": bool(field.get("proposed") or record and (record.get("proposed") or
                    isinstance(record.get("source"), dict) and record["source"].get("kind") == "grounded_narrative"))})
        documents = [{"kind": record["key"].split(".")[-1], "filename": Path(str(record.get("value"))).name}
                     for record in filled if str(record.get("key", "")).startswith("documents.")]
        issues = []
        review_verdict, review_at = None, None
        try:
            token = _json(self.private_bytes(self.root / "private" / "authorized-submissions" / job_hash / "independent-review.json"))
            latest = token.get("review", {})
        except (ValueError, OSError, TypeError):
            latest = {}
        review_directory = self.root / "private" / "application-reviews" / job_hash
        if not review_directory.is_symlink() and not any(p.is_symlink() for p in review_directory.parents):
            for result_path in review_directory.glob("*-result.json"):
                if not re.fullmatch(r"[a-f0-9]{32}-result\.json", result_path.name):
                    continue
                try:
                    result = _json(self.private_bytes(result_path))
                    if (result.get("job_hash") == job_hash and result.get("source") == "independent_application_review"
                            and result.get("reviewer") == "codex-readonly" and _stamp(result.get("reviewed_at"))
                            and (_stamp(result["reviewed_at"]) > (_stamp(latest.get("reviewed_at")) or datetime.min.replace(tzinfo=timezone.utc)))):
                        latest = result
                except (ValueError, OSError, TypeError):
                    continue
        if latest.get("job_hash") == job_hash and latest.get("source") == "independent_application_review":
            issues = [_text(issue if isinstance(issue, str) else
                      ("["+_text(issue.get("field_ref"), 100)+"] " if issue.get("field_ref") else "")+
                      _text(issue.get("reason") or issue.get("message"), 1000), 1100)
                      for issue in latest.get("issues", []) if isinstance(issue, (dict, str))]
            review_verdict, review_at = _text(latest.get("verdict"), 50), latest.get("reviewed_at")
        if not issues:
            try:
                with self.connection() as conn:
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='authorized_submission_attempts'").fetchone():
                        attempt = conn.execute("SELECT state,result_json FROM authorized_submission_attempts WHERE job_hash=?", (job_hash,)).fetchone()
                        result = _json(attempt["result_json"]) if attempt else {}
                        if attempt and attempt["state"] not in {"submitted", "in_progress"} and result.get("reason"):
                            issues = ["Last final-check handoff: "+_text(result["reason"], 1000)]
            except sqlite3.Error:
                pass
        incident = None
        try:
            data = _json(self.private_bytes(self.root / "private" / "application-incidents" / (job_hash+".json")))
            if data.get("job_hash") == job_hash:
                incident = {"state": _text(data.get("state")), "summary": _text(data.get("summary"), 3000),
                    "blank_questions": [{"ref": _text(q.get("ref")), "question": _text(q.get("question"), 3000)}
                                        for q in data.get("blank_questions", []) if isinstance(q, dict)]}
        except (ValueError, OSError, TypeError):
            pass
        approval = {"can_approve": False, "reason": "Full form inventory must be captured before approval", "blank_questions": []}
        try:
            from .applications import approvals
            with self.connection(write=True) as conn:
                approval = approvals.review(conn, job_hash, book_path=self.book_path)
        except (ImportError, ValueError, OSError, sqlite3.Error):
            pass
        if approval.get("can_approve") and approval.get("packet_sha256") != displayed_packet_sha:
            # The fields above and the approval revision must describe the
            # same bytes, even if a worker atomically replaces the packet while
            # this request is rendering. Never attach a newer authority token
            # to older displayed answers.
            approval = {**approval, "can_approve": False,
                        "reason": "Draft changed during review; close and reopen it", "revision": None}
        # Older drafts and confirmed submissions can be inspected but never
        # approved by inferring completeness from the absence of required gaps.
        if not complete_inventory or row["state"] != "waiting_review":
            approval = {**approval, "can_approve": False, "reason": "Already submitted" if row["state"] == "submitted"
                        else "Full form inventory must be captured before approval"}
        try:
            pending_questions = [q for q in self.pending(queue_states={job_hash: row["state"]})
                                 if any(c["job_hash"] == job_hash for c in q["contexts"])]
        except (ValueError, OSError, KeyError, TypeError):
            pending_questions = []
            approval = {**approval, "can_approve": False, "reason": "Answer booklet unavailable; refresh before approval"}
        if any(any(c["job_hash"] == job_hash and c["required"] for c in q["contexts"]) for q in pending_questions):
            approval = {**approval, "can_approve": False, "reason": "Answer the remaining required questions before approval"}
        from .applications.question_routing import route, CANDIDATE
        agent_tasks = []
        if row["state"] not in {"submitted", "skipped", "submission_uncertain", "discarded"}:
            packet_tasks = packet.get("agent_tasks", [])
            native_fields = manifest.get("fields", []) if isinstance(manifest, dict) else []
            if not isinstance(native_fields, list):
                native_fields = []
            for task in packet_tasks if isinstance(packet_tasks, list) else []:
                if (not isinstance(task, dict)
                        or questions._SECRET.search(" ".join(str(task.get(key) or "") for key in ("question", "ref")))
                        or task.get("task_kind") not in
                        {"document_generation", "narrative_generation", "known_answer_fill"}
                        or not any(field.get("ref") == task.get("ref") and field.get("question") == task.get("question")
                                   for field in native_fields if isinstance(field, dict))):
                    continue
                agent_tasks.append({"question": _text(task.get("question")), "ref": _text(task.get("ref")),
                                    "task_kind": task["task_kind"], "required": task.get("required") is True})
        try:
            current_book = self.book()
        except (ValueError, OSError):
            current_book = {}
        for record in current_book.get("question_handoffs", {}).values():
            context = record.get("contexts", {}).get(job_hash)
            if record.get("status") in {"pending", "answered"} and context and not context.get("resolved"):
                if questions._SECRET.search(" ".join(str(value or "") for value in (record.get("question"), context.get("ref")))):
                    continue
                if record.get("status") == "answered" and context.get("routing") not in {"known_answer_fill", "document_generation"}:
                    continue
                kind = route(current_book, record, context)
                if (kind != CANDIDATE and row["state"] not in {"submitted", "skipped", "submission_uncertain", "discarded"}
                        and not any(task["ref"] == context.get("ref") for task in agent_tasks)):
                    agent_tasks.append({"question": _text(record.get("question")), "ref": _text(context.get("ref")),
                                        "task_kind": kind, "required": context.get("required") is True})
        if agent_tasks:
            approval = {**approval, "can_approve": False, "reason": "Agent work must be verified before approval"}
        fit = packet.get("role_fit", {})
        fit_notes = [_text(note if isinstance(note, str) else note.get("reason") or note.get("message"), 1500)
                     for note in fit.get("review_notes", []) if isinstance(note, (dict, str))] if isinstance(fit, dict) else []
        screenshot = {"available": False, "revision": None, "captured_at": None}
        if path is not None:
            try:
                from .applications.capture import valid as valid_capture
                image = self.private_bytes(path.with_name("browser.png"), limit=15*1024*1024)
                if valid_capture(packet, image):
                    screenshot = {"available": True, "revision": hashlib.sha256(image).hexdigest(),
                                  "captured_at": packet.get("capture", {}).get("captured_at") or packet.get("created_at")}
            except (ValueError, OSError, TypeError, AttributeError):
                pass
        from .applications.application_discard import status as discard_status
        if row["state"] == "waiting_review" and draft_target == "unavailable":
            if presentation["display_state"] in {"waiting_review", "needs_review"}:
                presentation["display_state"] = "needs_review"
            approval = {**approval, "can_approve": False,
                        "reason": "The saved browser tab is closed. The retained review is available, but the draft needs recovery before submission."}
        if hold := self.submission_hold_reason():
            approval = {**approval, "can_approve": False, "reason": hold}
        return {"job_hash": job_hash, "state": row["state"], **presentation, "location": _location(job),
            "discard": discard_status(self.root, job_hash),
            "related_submissions": related, "fields": output, "role_fit_notes": fit_notes[:20],
            "screenshot": screenshot, "packet_revision": displayed_packet_sha,
            "draft_target_state": draft_target,
            "draft_focus_available": draft_target != "unavailable" and screenshot["available"] and self._focusable(row, packet),
            "questions": pending_questions, "agent_tasks": agent_tasks,
            "inventory_complete": complete_inventory, "documents": documents,
            "resume_role": packet.get("selected_role") or packet.get("resume_role"),
            "reviewer_issues": issues, "reviewer_verdict": review_verdict, "reviewer_reviewed_at": review_at,
            "approval_outcome": approval_outcome,
            "incident": incident, "approval": approval, "automation_paused": self.paused(),
            "submission_supported": boards.submission_supported(boards.route_board(job.get("url"), job.get("board_type")))}

    @staticmethod
    def _focusable(row, packet):
        item = packet.get("capture", {})
        return (row["state"] in {"waiting_review", "waiting_input", "failed", "retry", "waiting_login", "waiting_captcha"}
                and isinstance(item, dict) and item.get("verified") is True
                and item.get("method") == "browser_use_cli" and isinstance(item.get("target_id"), str)
                and bool(item["target_id"]) and len(item["target_id"]) <= 200)

    def focus_snapshot(self, job_hash, revision):
        if not HASH.fullmatch(job_hash) or not HASH.fullmatch(revision):
            raise ValueError("Refresh the saved draft before opening it")
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM applications WHERE job_hash=?", (job_hash,)).fetchone()
        if row is None:
            raise ValueError("Saved draft is unavailable")
        job = _json(row["job_json"])
        if boards.application_hash(job.get("url")) != job_hash:
            raise ValueError("Saved draft job identity changed")
        path, packet, current_revision = self.packet(row, job, with_digest=True)
        if current_revision != revision or path is None or not self._focusable(row, packet):
            raise ValueError("Saved draft changed or is no longer available for review")
        if self.draft_target_state(packet, self.tab_ledger()) == "unavailable":
            raise ValueError("Saved draft tab is closed; the retained review needs draft recovery")
        from .applications.capture import valid
        if not valid(packet, self.private_bytes(path.with_name("browser.png"), limit=15*1024*1024)):
            raise ValueError("Saved draft capture is unavailable; no new form was opened")
        return job, packet["capture"]["target_id"]

    def heartbeat(self, *, submission=False):
        try:
            data = _json(self.private_bytes(self.root / "private" /
                         ("pipeline-submit-status.json" if submission else "pipeline-status.json"), limit=65536))
        except (ValueError, OSError):
            return None
        safe = {key: _text(data.get(key), 100) for key in
                ("cycle_id", "stage", "status", "started_at", "updated_at", "completed_at")}
        for key in ("queues", "processed"):
            values = data.get(key)
            if isinstance(values, dict):
                safe[key] = {name: count for name, count in values.items() if re.fullmatch(r"[a-z_]+", name)
                             and type(count) is int and 0 <= count <= 1000000}
        safe["queue"] = {kind: {state: count for state, count in values.items()
            if re.fullmatch(r"[a-z_]+", state) and type(count) is int and 0 <= count <= 1000000}
            for kind, values in data.get("queue", {}).items()
            if kind in {"applications", "sources", "sheet_delivery"} and isinstance(values, dict)}
        allowed_reasons = {"repair_quarantine", "draft_capacity", "browser_capacity", "candidate_answers_required", "automation_paused",
                           "submission_authority_inactive", "no_ready_jobs", "cycle_failed", "cycle_interrupted",
                           "portal_disabled", "portal_required", "local_browser_unavailable", "local_browser_disconnected", "no_approvals",
                           "external_approval_active", "operation_failed", "operation_interrupted", "approval_crash_recovered",
                           "service_window_ended", "service_window_changed"}
        safe["reason_codes"] = [reason for reason in data.get("reason_codes", []) if reason in allowed_reasons]
        for name in ("approval_states",):
            values = data.get(name, {})
            if isinstance(values, dict):
                safe[name] = {key: count for key, count in values.items() if re.fullmatch(r"[a-z_]+", key)
                              and type(count) is int and 0 <= count <= 1000000}
        safe["active_jobs"] = [key for key in data.get("active_jobs", []) if isinstance(key, str) and HASH.fullmatch(key)][:10]
        updated = _stamp(data.get("updated_at"))
        safe["stale"] = not updated or (datetime.now(timezone.utc)-updated).total_seconds() > (20 if submission else 45)
        # No free-text error messages, paths, environment, or logs are exposed.
        return safe

    def openings(self, *, limit=25, before_time=None, before_id=None):
        """Bounded Phase 1 ledger page, distinct from actual application progress."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Opening page size must be between 1 and 100")
        if ((before_time is None) != (before_id is None) or
                before_time is not None and (type(before_time) is not int or before_time < 0
                    or not isinstance(before_id, str) or not HASH.fullmatch(before_id))):
            raise ValueError("Invalid opening page cursor")
        with self.connection() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "jobs" not in tables:
                return {"items": [], "total": 0, "next_cursor": None}
            where, params = "", []
            if before_time is not None:
                where = " WHERE first_seen < ? OR (first_seen = ? AND dedupe_hash > ?)"
                params = [before_time, before_time, before_id]
            rows = conn.execute("SELECT dedupe_hash,company,title,url,locations,source,first_seen FROM jobs" +
                where + " ORDER BY first_seen DESC,dedupe_hash LIMIT ?", [*params, limit+1]).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            items = []
            tabs = self.tab_ledger()
            for row in rows[:limit]:
                source = conn.execute("SELECT state,board,application_url,updated_at,job_json FROM application_sources WHERE source_job_hash=?",
                    (row["dedupe_hash"],)).fetchone() if "application_sources" in tables else None
                destination = source["application_url"] if source and source["state"] == "resolved" else None
                # An observed destination can be shown, but only an exact job
                # identity can be joined to a real Phase 2 application.
                app_hash = boards.application_hash(destination or row["url"])
                application = conn.execute("SELECT * FROM applications WHERE job_hash=?", (app_hash,)).fetchone() if app_hash and "applications" in tables else None
                application_stage = _application_presentation(conn, app_hash, application["state"])["display_state"] if application else None
                if application and application["state"] == "waiting_review" and application_stage in {"waiting_review", "needs_review"}:
                    _, draft = self.packet(application, _json(application["job_json"]))
                    if self.draft_target_state(draft, tabs) == "unavailable":
                        application_stage = "needs_review"
                try:
                    locations = json.loads(row["locations"] or "[]")
                except (ValueError, TypeError):
                    locations = []
                eligibility = _json(source["job_json"]).get("eligibility", {}) if source else {}
                if not eligibility and application and application["state"] == "skipped":
                    job = _json(application["job_json"])
                    eligibility = job.get("eligibility", {})
                    if not eligibility:
                        _, packet = self.packet(application, job)
                        eligibility = packet.get("eligibility", {})
                findings = eligibility.get("findings", []) if isinstance(eligibility, dict) else []
                items.append({"id": row["dedupe_hash"], "company": _text(row["company"]), "title": _text(row["title"]),
                    "location": _text(", ".join(v for v in locations if isinstance(v, str)), 500) if isinstance(locations, list) else "",
                    "source": _text(row["source"]), "url": row["url"] if boards._parts(row["url"]) else None,
                    "first_seen": row["first_seen"], "date": _day(row["first_seen"]),
                    "classification_state": source["state"] if source else "discovered",
                    "filter_reasons": [{"category": _text(f.get("category"), 100), "evidence": _text(f.get("evidence"), 1000)}
                                       for f in findings[:10] if isinstance(f, dict)] if isinstance(findings, list) else [],
                    "board": _text(source["board"]) if source else "unknown",
                    "application_url": boards.canonical_url(destination) if destination else None,
                    "application_id": app_hash if application else None,
                    "application_state": application["state"] if application else None,
                    "application_display_state": application_stage})
            last = rows[limit-1] if len(rows) > limit else None
            return {"items": items, "total": total, "next_cursor":
                    {"before_time": last["first_seen"], "before_id": last["dedupe_hash"]} if last else None}

    def overview(self, selected=None):
        now = datetime.now(timezone.utc)
        if selected is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", selected):
            raise ValueError("Invalid calendar date")
        selected = date.fromisoformat(selected) if selected else now.astimezone(ZONE).date()
        daily = {(selected-timedelta(days=13-i)).isoformat(): {"date": (selected-timedelta(days=13-i)).isoformat(),
                 "confirmed": 0, "prepared": 0} for i in range(14)}
        state_counts, source_counts, queue_states = Counter(), Counter(), {}
        confirmed, applications, events = {}, [], []
        tabs = self.tab_ledger()
        synced = set()
        storage_available = True
        try:
            with self.connection() as conn:
                conn.execute("BEGIN")
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "confirmed_submissions" in tables:
                    for row in conn.execute("SELECT application_url,confirmed_at,job_json,submission_key FROM confirmed_submissions"):
                        key, when = boards.application_hash(row["application_url"]), _day(row["confirmed_at"])
                        if key is None or key in confirmed:
                            continue
                        confirmed[key] = {"confirmed_at": row["confirmed_at"], "date": when, "submission_key": row["submission_key"]}
                        if when in daily:
                            daily[when]["confirmed"] += 1
                if "submission_sheet_delivery" in tables:
                    synced = {r[0] for r in conn.execute("SELECT DISTINCT submission_key FROM submission_sheet_delivery WHERE state='synced'")}
                if "application_sources" in tables:
                    source_counts.update({row[0]: row[1] for row in conn.execute("SELECT state,COUNT(*) FROM application_sources GROUP BY state")})
                if "applications" in tables:
                    rows = conn.execute("SELECT * FROM applications ORDER BY updated_at DESC,job_hash").fetchall()
                    for row in rows:
                        state_counts[row["state"]] += 1
                        queue_states[row["job_hash"]] = row["state"]
                        job = _json(row["job_json"])
                        if not HASH.fullmatch(row["job_hash"]) or boards.application_hash(job.get("url")) != row["job_hash"]:
                            continue
                        packet_path, packet = self.packet(row, job)
                        prepared_day = _day(packet.get("created_at"))
                        proof = confirmed.get(row["job_hash"])
                        incident = False
                        try:
                            report = _json(self.private_bytes(self.root / "private" / "application-incidents" / (row["job_hash"]+".json")))
                            incident = report.get("job_hash") == row["job_hash"]
                        except (ValueError, OSError):
                            pass
                        shot = False
                        if packet_path:
                            try:
                                from .applications.capture import valid as valid_capture
                                content = self.private_bytes(packet_path.with_name("browser.png"), limit=15*1024*1024,
                                                             read_count=None if "capture" in packet else 8)
                                shot = valid_capture(packet, content)
                            except (ValueError, OSError):
                                pass
                        inventory = packet.get("review_inventory", {})
                        inventory = inventory if isinstance(inventory, dict) else {}
                        fields = inventory.get("fields", [])
                        filled_refs = {record.get("ref") for record in packet.get("filled", [])}
                        inventory_verified = bool(inventory.get("complete") is True and fields
                            and not packet.get("missing") and not packet.get("verification") and shot
                            and all(isinstance(field, dict) and field.get("ref") and field.get("question")
                                and field.get("status") in {"answered", "blank", "declined"}
                                and (not field.get("required") or field.get("status") == "answered")
                                and (field.get("status") != "answered" or field.get("ref") in filled_refs) for field in fields)
                            and len({field.get("ref") for field in fields}) == len(fields))
                        capture = packet.get("capture", {})
                        if (packet.get("state") == "waiting_review" and inventory_verified
                                and isinstance(capture, dict) and capture.get("verified") is True and prepared_day in daily):
                            daily[prepared_day]["prepared"] += 1
                        draft_target = self.draft_target_state(packet, tabs)
                        inventory_ready = row["state"] == "waiting_review" and inventory_verified
                        presentation = _application_presentation(conn, row["job_hash"], row["state"])
                        if (row["state"] == "waiting_review" and draft_target == "unavailable"
                                and presentation["display_state"] in {"waiting_review", "needs_review"}):
                            presentation["display_state"] = "needs_review"
                        location = job.get("location") or (", ".join(v for v in job.get("locations", []) if isinstance(v, str))
                                    if isinstance(job.get("locations"), list) else "")
                        application = {"id": row["job_hash"], "company": _text(job.get("company")),
                            "title": _text(job.get("title")), "location": _text(location),
                            "url": boards.canonical_url(job.get("url")), "board": boards.board_type(job.get("url")),
                            "state": row["state"], **presentation,
                            "updated_at": row["updated_at"], "date": _day(row["updated_at"]),
                            "attempts": row["attempts"], "filled_count": len(packet.get("filled", [])),
                            "missing_count": len(packet.get("missing", [])), "has_screenshot": shot,
                            "inventory_ready": inventory_ready,
                            "draft_target_state": draft_target,
                            "has_incident": incident,
                            "screenshot_at": packet.get("created_at") if shot else None,
                            "confirmed_at": proof["confirmed_at"] if proof else None,
                            "confirmed_date": proof["date"] if proof else None,
                            "sheet_synced": bool(proof and proof["submission_key"] in synced)}
                        applications.append(application)
                        events.append({"job_hash": application["id"], "company": application["company"],
                            "title": application["title"], "state": application["display_state"], "at": application["updated_at"]})
                if "manual_applications" in tables:
                    columns = {r[1] for r in conn.execute("PRAGMA table_info(manual_applications)")}
                    if {"job_hash", "state"} <= columns:
                        for key, state in conn.execute("SELECT job_hash,state FROM manual_applications"):
                            if state in {"submitted", "submission_uncertain", "skipped", "declined"}:
                                queue_states[key] = state
        except (sqlite3.Error, FileNotFoundError):
            storage_available = False
        try:
            current_book = self.book()
            pending = self.pending(book=current_book, queue_states=queue_states)
            from .applications.question_routing import agent_contexts
            agent_blockers = Counter(key for record in current_book.get("question_handoffs", {}).values()
                                    if record.get("status") in {"pending", "answered"}
                                    for key in agent_contexts(current_book, record)
                                    if queue_states.get(key) not in {"submitted", "submission_uncertain", "skipped"})
            booklet_available = True
        except (ValueError, OSError, KeyError, TypeError):
            pending, booklet_available, agent_blockers = [], False, Counter()
        blockers = Counter(context["job_hash"] for q in pending for context in q["contexts"] if context["required"])
        for application in applications:
            application["inventory_verified"] = application["inventory_ready"]
            application["pending_required_questions"] = blockers[application["id"]]
            application["pending_agent_tasks"] = agent_blockers[application["id"]]
            if (application["pending_required_questions"] or application["pending_agent_tasks"] or not booklet_available
                    or application["draft_target_state"] == "unavailable"):
                application["inventory_ready"] = False
        return {"generated_at": now.isoformat(), "timezone": str(ZONE), "selected_date": selected.isoformat(),
            "storage_available": storage_available, "booklet_available": booklet_available, "automation_paused": self.paused(),
            "summary": {"confirmed_today": daily[selected.isoformat()]["confirmed"],
                "confirmed_total": len(confirmed), "prepared_today": daily[selected.isoformat()]["prepared"],
                "ready": sum(app["inventory_ready"] and app["display_state"] == "waiting_review" for app in applications),
                "legacy_review": sum(app["state"] == "waiting_review" and not app["inventory_verified"] for app in applications),
                "running": state_counts["running"],
                "queued": state_counts["queued"]+state_counts["retry"], "questions": len(pending),
                "uncertain": state_counts["submission_uncertain"],
                "sheet_synced": sum(proof["submission_key"] in synced for proof in confirmed.values())},
            "daily": list(daily.values()), "states": dict(state_counts), "source_states": dict(source_counts),
            "applications": applications[:5000], "applications_truncated": len(applications)>5000,
            "questions": pending, "activity": events[:15], "pipeline": self.heartbeat(),
            "submission_pipeline": self.heartbeat(submission=True)}


class WorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["review", "autonomous"]
    revision: str


class AnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str | bool | int | float | list[str] | None = None
    decline: bool = False
    revision: str


class FocusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    acknowledged_blank_refs: list[str] = []


def _prepare_review_edits(conn, question):
    """Revoke prior authority before an explicit answer changes a saved draft."""
    from .applications import approvals
    from .applications.authorized_submission import private_file
    reviewed = []
    attempts_exist = conn.execute("SELECT 1 FROM sqlite_master WHERE name='authorized_submission_attempts'").fetchone()
    approvals_exist = conn.execute("SELECT 1 FROM sqlite_master WHERE name='application_approvals'").fetchone()
    for job_hash, context in question.get("contexts", {}).items():
        if not HASH.fullmatch(job_hash) or context.get("resolved"):
            continue
        row = conn.execute("SELECT state FROM applications WHERE job_hash=?", (job_hash,)).fetchone()
        if not row or row["state"] != "waiting_review":
            continue
        attempt = conn.execute("SELECT * FROM authorized_submission_attempts WHERE job_hash=?", (job_hash,)).fetchone() if attempts_exist else None
        if attempt:
            try:
                evidence = _json(private_file(attempt["attempt_path"]).read_bytes())
                safe = (attempt["state"] in {"waiting_review", "waiting_input", "waiting_login", "waiting_captcha"}
                        and evidence.get("runtime_click_started") is False
                        and evidence.get("job_hash") == job_hash
                        and evidence.get("authorization_id") == attempt["authorization_id"])
            except (OSError, ValueError, TypeError):
                safe = False
            if not safe:
                raise HTTPException(409, "This application's final attempt needs review before its answers can change")
        pending = conn.execute("SELECT 1 FROM application_approvals WHERE job_hash=? AND state IN ('approved','submitting')", (job_hash,)).fetchone() if approvals_exist else None
        if pending:
            try:
                approvals.revoke(conn, job_hash)
            except (OSError, ValueError):
                raise HTTPException(409, "Approval cannot safely be revoked yet; wait for the active final check") from None
        reviewed.append(job_hash)
    return reviewed


def _prepare_review_edit_intents(conn, question, book_path, answer_revision):
    """Bind durable refill intent to an explicit edit of this reviewed snapshot.

    Generic answered records cannot requeue reviewed applications. This private
    proof survives a book/SQL boundary failure without authorizing submission.
    Incomplete legacy drafts get no automatic reviewed-edit recovery proof.
    """
    from .applications import approvals
    snapshots = {}
    for job_hash, context in question.get("contexts", {}).items():
        if context.get("resolved") or not HASH.fullmatch(job_hash):
            continue
        try:
            _, binding, revision = approvals._draft(conn, job_hash, book_path)
            snapshots[job_hash] = (binding, revision)
        except (ValueError, OSError, KeyError, TypeError):
            continue
    reviewed = _prepare_review_edits(conn, question)
    for job_hash in reviewed:
        if job_hash not in snapshots:
            continue
        binding, revision = snapshots[job_hash]
        question.setdefault("candidate_edit_intents", {})[job_hash] = {
            "provider": "local_dashboard_explicit_edit", "question_id": question["id"],
            "answer_revision": answer_revision, "job_hash": job_hash,
            "packet_path": binding["packet_path"], "packet_sha256": binding["packet_sha256"],
            "review_revision": revision, "review_binding": binding, "approval_revoked": True}
    return reviewed


def create_app(*, root=None, db_path=None, book_path=None, static_dir=None):
    root = Path(root or config.ROOT).resolve()
    store = DashboardStore(root, Path(db_path or config.DB_PATH).absolute(),
                           Path(book_path or root / "private" / "answer-booklet.json").absolute())
    app = FastAPI(title="Job Hunting Buddy local dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.csrf = store, secrets.token_urlsafe(32)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        try:
            host = urlsplit("http://"+request.headers.get("host", "")).hostname
        except ValueError:
            host = None
        client = request.client.host if request.client else None
        if host not in {"127.0.0.1", "localhost", "::1"} or client not in {"127.0.0.1", "::1"}:
            return JSONResponse({"detail": "Dashboard requires a local connection"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-site requests are refused"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            expected = f"{request.url.scheme}://{request.headers['host']}"
            if (request.headers.get("origin") != expected or
                    not secrets.compare_digest(request.headers.get("x-jhb-csrf", ""), app.state.csrf)):
                return JSONResponse({"detail": "Same-origin confirmation is required"}, status_code=403)
            if not request.headers.get("content-type", "").split(";")[0] == "application/json":
                return JSONResponse({"detail": "JSON is required"}, status_code=415)
            body = await request.body()
            if len(body) > 32768:
                return JSONResponse({"detail": "Answer is too large"}, status_code=413)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"})
        return response

    @app.get("/api/v1/session")
    def session():
        return {"csrf_token": app.state.csrf}

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "local_only": True, "storage_available": store.db_path.is_file()}

    @app.get("/api/v1/overview")
    def overview(day: str | None = None):
        try:
            return store.overview(day)
        except ValueError:
            raise HTTPException(400, "Date must use YYYY-MM-DD") from None

    @app.get("/api/v1/workflow-policy")
    def workflow_policy():
        from .applications import workflow_policy as policy
        try:
            return policy.snapshot(root)
        except (ValueError, OSError):
            raise HTTPException(503, "Workflow policy is unavailable; automatic submission is disabled") from None

    @app.post("/api/v1/workflow-policy")
    def update_workflow_policy(payload: WorkflowInput):
        from .applications import workflow_policy as policy
        try:
            return policy.set_mode(root, payload.mode, revision=payload.revision)
        except (ValueError, OSError):
            raise HTTPException(409, "Workflow policy changed or is unavailable; refresh before changing it") from None

    @app.get("/api/v1/openings")
    def openings(limit: int = 25, before_time: int | None = None, before_id: str | None = None):
        try:
            return store.openings(limit=limit, before_time=before_time, before_id=before_id)
        except ValueError:
            raise HTTPException(400, "Invalid opening page size or cursor") from None
        except (OSError, sqlite3.Error):
            raise HTTPException(503, "Phase 1 storage is unavailable") from None

    @app.get("/api/v1/applications/{job_hash}/screenshot")
    def screenshot(job_hash: str, revision: str | None = None):
        try:
            content = store.screenshot(job_hash)
            if revision is not None and (not HASH.fullmatch(revision) or hashlib.sha256(content).hexdigest() != revision):
                raise ValueError("Screenshot changed after this review snapshot")
            return Response(content, media_type="image/png", headers={"Cache-Control": "no-store"})
        except (ValueError, OSError, sqlite3.Error):
            raise HTTPException(404, "Validated review screenshot is unavailable") from None

    @app.get("/api/v1/applications/{job_hash}")
    def application_review(job_hash: str):
        try:
            return store.details(job_hash)
        except (ValueError, OSError, sqlite3.Error):
            raise HTTPException(404, "Application review is unavailable") from None

    @app.post("/api/v1/applications/{job_hash}/focus")
    def focus_application(job_hash: str, payload: FocusInput):
        try:
            job, target = store.focus_snapshot(job_hash, payload.revision)
            from .applications.cli_browser import BrowserUseCLI
            client = BrowserUseCLI(timeout=15)
            client.target_id, client.expected_url = target, job["url"]
            def revalidate():
                fresh_job, fresh_target = store.focus_snapshot(job_hash, payload.revision)
                if fresh_target != target or boards.job_identity(fresh_job["url"]) != boards.job_identity(job["url"]):
                    raise ValueError("Saved draft changed before focus")
            result = client.call("review_focus", url=job["url"], _before_run=revalidate)
            if result.get("focused") is not True or result.get("guarded") is not True:
                raise ValueError("Saved draft could not be focused")
            return {"state": "focused", "job_hash": job_hash, "guarded": True}
        except (ValueError, OSError, sqlite3.Error, RuntimeError, TimeoutError):
            raise HTTPException(409, "Saved draft is unavailable, changed, or disconnected. No new form was opened; refresh this review.") from None

    @app.post("/api/v1/applications/{job_hash}/approve")
    def approve_application(job_hash: str, payload: ApprovalInput):
        if not HASH.fullmatch(job_hash):
            raise HTTPException(404, "Unknown application")
        try:
            current_form = os.environ.get("JHB_APPROVE_CURRENT_LIVE_FORM") == "1"
            if hold := store.submission_hold_reason():
                raise HTTPException(409, hold)
            if current_form and os.environ.get("JHB_PORTAL_SUBMISSIONS_ENABLED") != "1":
                raise HTTPException(409, "Submission dispatch is temporarily held for maintenance; your browser edits are preserved")
            with store.answer_lock, questions._locked(store.book_path):
                details = store.details(job_hash)
                if (not details["inventory_complete"] or details["state"] != "waiting_review"
                        or not details["approval"].get("can_approve")):
                    raise HTTPException(409, "The complete current application must be reviewed first")
                from .applications import approvals
                with store.connection(write=True) as conn:
                    if current_form:
                        from .applications.live_review import capture_current
                        packet, binding, revision = approvals._draft(conn, job_hash, store.book_path)
                        if revision != payload.revision:
                            raise ValueError("Saved job identity changed before approval")
                        # Capture reads the exact existing tab, including every
                        # candidate edit; it never fills or replaces uploads.
                        asyncio.run(capture_current(binding["packet_path"], acknowledged_blank_refs=payload.acknowledged_blank_refs))
                        live, _, revision = approvals._draft(conn, job_hash, store.book_path)
                        blanks = [f["ref"] for f in live["review_inventory"]["fields"] if f["status"] != "answered"]
                        result = approvals.approve(conn, job_hash, revision,
                            acknowledged_blank_refs=blanks, book_path=store.book_path, current_form=True)
                    else:
                        result = approvals.approve(conn, job_hash, payload.revision,
                            acknowledged_blank_refs=payload.acknowledged_blank_refs, book_path=store.book_path)
            if current_form:
                # Start this exact user-approved job in this request instead of
                # waiting for the scheduled worker's next polling interval.
                from .applications.service import _approved_lock
                with _approved_lock() as owned:
                    if owned:
                        with store.connection(write=True) as conn:
                            outcome = asyncio.run(approvals.drain(conn, store.book_path, limit=1, job_hash=job_hash))
                            row = conn.execute("SELECT state FROM application_approvals WHERE approval_id=?", (result["approval_id"],)).fetchone()
                        return {**result, "state": row[0] if row else "needs_review", "outcome": outcome,
                                "manual_edits_preserved": True}
                return {**result, "state": "approved", "reason": "Queued behind an active browser submission", "manual_edits_preserved": True}
            return {key: result[key] for key in ("approval_id", "state", "job_hash") if key in result}
        except ImportError:
            raise HTTPException(503, "Application approval is not configured") from None
        except (ValueError, OSError, sqlite3.Error, RuntimeError, TimeoutError) as exc:
            message = "Draft changed or blank questions were not acknowledged; refresh the review"
            if current_form:
                text = str(exc)
                if "attachment" in text or "uploaded" in text or "upload" in text or text.endswith((": Resume", ": Resume/CV", ": Cover Letter")):
                    message = "The current attachment could not be verified. It was left unchanged; review the saved attachment in Chrome."
                elif "optional blank" in text:
                    message = "A current optional field is blank. Refresh the review and acknowledge each field you want to leave blank."
                elif "spreadsheet application" in text or "duplicate submission" in text:
                    message = "This role matches an existing application in your history. Reconcile that match before reapplying."
                elif "tab" in text.lower() or "Current page differs" in text:
                    message = "The saved tab is not showing the expected application form. Open the saved draft before submitting."
                else:
                    message = "The current browser form could not be verified. Your answers were left unchanged; refresh this review."
            raise HTTPException(409, message) from None

    @app.post("/api/v1/applications/{job_hash}/discard")
    def discard_application(job_hash: str, background_tasks: BackgroundTasks):
        if not HASH.fullmatch(job_hash):
            raise HTTPException(404, "Unknown application")
        try:
            from .applications import application_discard
            with store.answer_lock, questions._locked(store.book_path):
                with store.connection(write=True) as conn:
                    application_discard.request(conn, job_hash, root=store.root, book_path=store.book_path)
            result = application_discard.finalize(store.root, job_hash, timeout=3)
            if result["tab_close"]["state"] in {"pending", "deferred"}:
                background_tasks.add_task(application_discard.finalize, store.root, job_hash, timeout=30)
            return result
        except (ValueError, OSError, sqlite3.Error):
            raise HTTPException(409, "Application cannot be discarded while its submission outcome is uncertain or a final click is in progress. Refresh its status.") from None

    @app.post("/api/v1/applications/{job_hash}/revoke")
    def revoke_application(job_hash: str):
        if not HASH.fullmatch(job_hash):
            raise HTTPException(404, "Unknown application")
        try:
            from .applications import approvals
            with store.connection(write=True) as conn:
                approvals.revoke(conn, job_hash)
            return {"state": "revoked", "job_hash": job_hash}
        except (ImportError, ValueError, OSError, sqlite3.Error):
            raise HTTPException(409, "Application approval cannot be revoked") from None

    @app.post("/api/v1/questions/{question_id}/answer")
    def answer_question(question_id: str, payload: AnswerInput):
        if not re.fullmatch(r"q_[a-f0-9]{24}", question_id):
            raise HTTPException(404, "Unknown question")
        with store.answer_lock:
            saved_jobs = []
            try:
                record = store.book().get("question_handoffs", {}).get(question_id)
                if not record or questions._SECRET.search(record.get("question", "")):
                    raise HTTPException(404, "Unknown candidate question")
                if record.get("status") != "pending" or record.get("updated_at") != payload.revision:
                    raise HTTPException(409, "Question changed; refresh before answering")
                with store.connection(write=True) as conn:
                    queue_states = dict(conn.execute("SELECT job_hash,state FROM applications"))
                    visible = next((q for q in store.pending(queue_states=queue_states) if q["id"] == question_id), {})
                    context_job_hashes = [c["job_hash"] for c in visible.get("contexts", [])]
                    review_edits = []
                    affected = questions.answer(question_id, payload.value, store.book_path,
                                                conn, decline=payload.decline, expected_revision=payload.revision,
                                                before_save=lambda current, stamp: review_edits.extend(
                                                    _prepare_review_edit_intents(conn, current, store.book_path, stamp)),
                                                after_save=saved_jobs.extend, context_job_hashes=context_job_hashes)
                    # This transition is an explicit candidate edit, not a
                    # generic resume. Paused workers leave queued edits alone
                    # until the user resumes automation; the edit is durable.
                    for job_hash in review_edits:
                        conn.execute("UPDATE applications SET state='queued',lease_until=NULL,attempts=0,available_at=0,"
                                     "error_kind=NULL,updated_at=?,notified_at=NULL WHERE job_hash=? AND state='waiting_review'",
                                     (int(datetime.now(timezone.utc).timestamp()), job_hash))
                    conn.commit()
                    queued = [key for key in affected if (row := conn.execute("SELECT state FROM applications WHERE job_hash=?", (key,)).fetchone())
                              and row[0] == "queued"]
                    pending = store.book().get("question_handoffs", {}).values()
                    states = []
                    for key in affected:
                        row = conn.execute("SELECT state FROM applications WHERE job_hash=?", (key,)).fetchone()
                        from .applications.question_routing import candidate_contexts
                        current_book = store.book()
                        remaining = sum(1 for q in pending if q.get("status") == "pending"
                            and (context := candidate_contexts(current_book, q).get(key))
                            and context.get("required") and not context.get("resolved"))
                        states.append({"job_hash": key, "state": row[0] if row else "untracked",
                                       "remaining_required_questions": remaining})
                return {"status": "answered", "affected_jobs": affected, "resumed_jobs": queued,
                        "applications": states, "automation_paused": store.paused(),
                        "saved_at": int(datetime.now(timezone.utc).timestamp())}
            except questions.QuestionChanged:
                raise HTTPException(409, "Question changed; refresh before answering") from None
            except (ValueError, TypeError):
                raise HTTPException(422, "Answer does not match this question; optional questions alone may be declined") from None
            except (OSError, sqlite3.Error):
                if saved_jobs:
                    # Candidate evidence is durable even if the separate SQL
                    # transition failed. Do not invite a duplicate answer or
                    # claim filling started; reconciliation confirms the queue.
                    return JSONResponse(status_code=202, content={"status": "answered", "affected_jobs": saved_jobs,
                        "resumed_jobs": [], "applications": [], "resume_pending": True,
                        "automation_paused": store.paused(),
                        "saved_at": int(datetime.now(timezone.utc).timestamp())})
                raise HTTPException(503, "Answer storage is unavailable; refresh and retry") from None

    build = Path(static_dir or root / "frontend" / "out")
    if build.is_dir():
        app.mount("/", StaticFiles(directory=build, html=True), name="dashboard")
    else:
        @app.get("/")
        def unbuilt():
            return JSONResponse({"detail": "Build frontend with npm ci && npm run build in frontend/"}, status_code=503)
    return app


def main():
    parser = argparse.ArgumentParser(description="Serve the private dashboard on localhost")
    parser.add_argument("--port", type=int, default=8030)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Choose a port between 1024 and 65535")
    # Match scheduled workers: the CLI loads the ignored local configuration;
    # create_app stays injectable and never reads a developer's .env in tests.
    config.load_dotenv()
    config.refresh_from_env()
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
