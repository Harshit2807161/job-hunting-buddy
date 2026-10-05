"""Loopback-only dashboard over the existing durable application pipeline.

GETs read SQLite and a small explicit artifact allowlist. Explicit candidate
actions save scoped answers or approve/revoke one immutable review draft.
This server never launches a browser, submits an application, or sends mail.
"""
from __future__ import annotations

import argparse
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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

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
            for key, context in record.get("contexts", {}).items():
                if context.get("resolved") or (queue_states is not None and queue_states.get(key) in
                                               {"submitted", "submission_uncertain", "skipped"}) or manual_states.get(key) in {
                                                   "submitted", "submission_uncertain", "skipped", "declined"} or booklet.job_excluded(book, {"dedupe_hash": key}):
                    continue
                contexts.append({"job_hash": key, "company": _text(context.get("company")),
                    "title": _text(context.get("title")), "url": boards.canonical_url(context.get("url")),
                    "required": context.get("required") is True, "type": _text(context.get("type"), 50),
                    "reason": _text(context.get("reason"), 500),
                    "description": _text(context.get("description"), 4096),
                    "description_truncated": context.get("description_truncated") is True or
                        isinstance(context.get("description"), str) and len(context["description"]) > 4096,
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
        path, packet, displayed_packet_sha = self.packet(row, job, with_digest=True)
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
        return {"job_hash": job_hash, "state": row["state"], "fields": output, "role_fit_notes": fit_notes[:20],
            "screenshot": screenshot,
            "questions": pending_questions,
            "inventory_complete": complete_inventory, "documents": documents,
            "resume_role": packet.get("selected_role") or packet.get("resume_role"),
            "reviewer_issues": issues, "reviewer_verdict": review_verdict, "reviewer_reviewed_at": review_at,
            "incident": incident, "approval": approval, "automation_paused": self.paused()}

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

    def overview(self, selected=None):
        now = datetime.now(timezone.utc)
        if selected is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", selected):
            raise ValueError("Invalid calendar date")
        selected = date.fromisoformat(selected) if selected else now.astimezone(ZONE).date()
        daily = {(selected-timedelta(days=13-i)).isoformat(): {"date": (selected-timedelta(days=13-i)).isoformat(),
                 "confirmed": 0, "prepared": 0} for i in range(14)}
        state_counts, source_counts, queue_states = Counter(), Counter(), {}
        confirmed, applications, events = {}, [], []
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
                        if packet.get("state") == "waiting_review" and prepared_day in daily:
                            daily[prepared_day]["prepared"] += 1
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
                        inventory_ready = bool(row["state"] == "waiting_review" and inventory.get("complete") is True and fields
                            and not packet.get("missing") and not packet.get("verification") and shot
                            and all(isinstance(field, dict) and field.get("ref") and field.get("question")
                                and field.get("status") in {"answered", "blank", "declined"}
                                and (not field.get("required") or field.get("status") == "answered")
                                and (field.get("status") != "answered" or field.get("ref") in filled_refs) for field in fields)
                            and len({field.get("ref") for field in fields}) == len(fields))
                        location = job.get("location") or (", ".join(v for v in job.get("locations", []) if isinstance(v, str))
                                    if isinstance(job.get("locations"), list) else "")
                        application = {"id": row["job_hash"], "company": _text(job.get("company")),
                            "title": _text(job.get("title")), "location": _text(location),
                            "url": boards.canonical_url(job.get("url")), "board": boards.board_type(job.get("url")),
                            "state": row["state"], "updated_at": row["updated_at"], "date": _day(row["updated_at"]),
                            "attempts": row["attempts"], "filled_count": len(packet.get("filled", [])),
                            "missing_count": len(packet.get("missing", [])), "has_screenshot": shot,
                            "inventory_ready": inventory_ready,
                            "has_incident": incident,
                            "screenshot_at": packet.get("created_at") if shot else None,
                            "confirmed_at": proof["confirmed_at"] if proof else None,
                            "confirmed_date": proof["date"] if proof else None,
                            "sheet_synced": bool(proof and proof["submission_key"] in synced)}
                        applications.append(application)
                        events.append({"job_hash": application["id"], "company": application["company"],
                            "title": application["title"], "state": application["state"], "at": application["updated_at"]})
                if "manual_applications" in tables:
                    columns = {r[1] for r in conn.execute("PRAGMA table_info(manual_applications)")}
                    if {"job_hash", "state"} <= columns:
                        for key, state in conn.execute("SELECT job_hash,state FROM manual_applications"):
                            if state in {"submitted", "submission_uncertain", "skipped", "declined"}:
                                queue_states[key] = state
        except (sqlite3.Error, FileNotFoundError):
            storage_available = False
        try:
            pending = self.pending(queue_states=queue_states)
            booklet_available = True
        except (ValueError, OSError, KeyError, TypeError):
            pending, booklet_available = [], False
        blockers = Counter(context["job_hash"] for q in pending for context in q["contexts"] if context["required"])
        for application in applications:
            application["inventory_verified"] = application["inventory_ready"]
            application["pending_required_questions"] = blockers[application["id"]]
            if application["pending_required_questions"] or not booklet_available:
                application["inventory_ready"] = False
        return {"generated_at": now.isoformat(), "timezone": str(ZONE), "selected_date": selected.isoformat(),
            "storage_available": storage_available, "booklet_available": booklet_available, "automation_paused": self.paused(),
            "summary": {"confirmed_today": daily[selected.isoformat()]["confirmed"],
                "confirmed_total": len(confirmed), "prepared_today": daily[selected.isoformat()]["prepared"],
                "ready": sum(app["inventory_ready"] for app in applications),
                "legacy_review": sum(app["state"] == "waiting_review" and not app["inventory_verified"] for app in applications),
                "running": state_counts["running"],
                "queued": state_counts["queued"]+state_counts["retry"], "questions": len(pending),
                "uncertain": state_counts["submission_uncertain"],
                "sheet_synced": sum(proof["submission_key"] in synced for proof in confirmed.values())},
            "daily": list(daily.values()), "states": dict(state_counts), "source_states": dict(source_counts),
            "applications": applications[:5000], "applications_truncated": len(applications)>5000,
            "questions": pending, "activity": events[:15], "pipeline": self.heartbeat(),
            "submission_pipeline": self.heartbeat(submission=True)}


class AnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str | bool | int | float | list[str] | None = None
    decline: bool = False
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

    @app.post("/api/v1/applications/{job_hash}/approve")
    def approve_application(job_hash: str, payload: ApprovalInput):
        if not HASH.fullmatch(job_hash):
            raise HTTPException(404, "Unknown application")
        try:
            with store.answer_lock, questions._locked(store.book_path):
                details = store.details(job_hash)
                if (not details["inventory_complete"] or details["state"] != "waiting_review"
                        or not details["approval"].get("can_approve")):
                    raise HTTPException(409, "The complete current application must be reviewed first")
                from .applications import approvals
                with store.connection(write=True) as conn:
                    result = approvals.approve(conn, job_hash, payload.revision,
                        acknowledged_blank_refs=payload.acknowledged_blank_refs, book_path=store.book_path)
            return {key: result[key] for key in ("approval_id", "state", "job_hash") if key in result}
        except ImportError:
            raise HTTPException(503, "Application approval is not configured") from None
        except (ValueError, OSError, sqlite3.Error):
            raise HTTPException(409, "Draft changed or blank questions were not acknowledged; refresh the review") from None

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
                        remaining = sum(1 for q in pending if q.get("status") == "pending"
                            and (context := q.get("contexts", {}).get(key))
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
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
