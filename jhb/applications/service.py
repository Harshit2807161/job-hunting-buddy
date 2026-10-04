"""Small local preparation and per-draft portal workers; no blanket authority."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from urllib.parse import urlsplit

from .. import config, store
from . import booklet

PULSE_SECONDS = 5


def _ci():
    return os.environ.get("CI", "").lower() in {"1", "true", "yes"}


def _gate(mode):
    if _ci():
        return "ci_disabled"
    for path, reason in [(config.ROOT / "private" / "pipeline-pause.json", "automation_paused"),
                         (config.ROOT / "private" / "overnight-monitor" / "repair-pending.json", "repair_quarantine")]:
        if path.exists() or path.is_symlink():
            return reason
    if mode == "prepare":
        if os.environ.get("JHB_APPLICATIONS_ENABLED") != "1":
            return "preparation_disabled"
        if os.environ.get("JHB_REQUIRE_PORTAL_APPROVAL") != "1":
            return "portal_required"
    elif os.environ.get("JHB_REQUIRE_PORTAL_APPROVAL") != "1" or os.environ.get("JHB_PORTAL_SUBMISSIONS_ENABLED") != "1":
        return "portal_disabled"
    endpoint = os.environ.get("BU_CDP_WS") or os.environ.get("BU_CDP_URL", "")
    try:
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in {"http", "https", "ws", "wss"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parsed.username is not None or parsed.password is not None or not parsed.port):
            return "local_browser_unavailable"
    except ValueError:
        return "local_browser_unavailable"
    from .browser_connection import available
    if not available(endpoint):
        return "local_browser_disconnected"
    return None


def record_gate(mode, reason):
    """Publish a gate outcome without opening SQLite or overwriting an owner."""
    state = "paused" if reason == "automation_paused" else "blocked" if reason in {
        "repair_quarantine", "local_browser_disconnected"} else "disabled"
    if reason != "ci_disabled":
        with _worker_lock("approved-worker.lock" if mode == "approved" else "application-worker.lock") as owned:
            if owned:
                if mode == "approved":
                    SubmissionStatus().update(status=state, reason=reason)
                else:
                    from .pipeline_status import Heartbeat
                    # Disabled is a service outcome; the dashboard's pipeline
                    # status vocabulary uses blocked for a stopped dispatch.
                    Heartbeat(None).update(status=state if state != "disabled" else "blocked", reasons=[reason])
    return {"state": state, "reason_code": reason}


@contextmanager
def _worker_lock(name):
    path = config.ROOT / "private" / name
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Approved worker lock must remain private")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(fd, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def _approved_lock():
    return _worker_lock("approved-worker.lock")


def _stamp():
    return datetime.now(timezone.utc).isoformat()


class SubmissionStatus:
    def __init__(self, conn=None):
        self.conn = conn
        self.path = config.ROOT / "private" / "pipeline-submit-status.json"
        self.value = {"schema_version": 1, "cycle_id": uuid.uuid4().hex, "mode": "approved", "status": "running",
                      "stage": "initializing", "started_at": _stamp(), "completed_at": None,
                      "heartbeat_interval_seconds": PULSE_SECONDS, "stale_after_seconds": 20,
                      "processed": {}, "approval_states": {}, "active_jobs": [], "reason_codes": []}

    def update(self, *, status=None, stage=None, reason=None, result=None):
        if status:
            self.value["status"] = status
            if status not in {"running"}:
                self.value["completed_at"] = _stamp()
        if stage:
            self.value["stage"] = stage
        self.value["reason_codes"] = [reason] if reason else []
        if result:
            self.value["processed"] = {key: result[key] for key in ("attempted", "submitted", "uncertain", "handoffs", "recovered")
                                         if type(result.get(key)) is int and result[key] >= 0}
        if self.conn is not None:
            try:
                states = {"approved", "submitting", "expired", "invalidated", "needs_review", "revoked", "submitted", "uncertain"}
                self.value["approval_states"] = {state: count for state, count in self.conn.execute(
                    "SELECT state,COUNT(*) FROM application_approvals GROUP BY state") if state in states}
                self.value["active_jobs"] = [row[0] for row in self.conn.execute(
                    "SELECT DISTINCT job_hash FROM application_approvals WHERE state='submitting' ORDER BY job_hash LIMIT 10")
                    if isinstance(row[0], str) and len(row[0]) == 64 and all(c in "0123456789abcdef" for c in row[0])]
            except sqlite3.OperationalError:
                self.value["approval_states"], self.value["active_jobs"] = {}, []
        self.value["updated_at"] = _stamp()
        try:
            booklet.write_private(self.path, self.value)
        except (OSError, ValueError):
            pass  # Missing telemetry must not create or alter application state.

    async def pulse(self):
        while True:
            await asyncio.sleep(PULSE_SECONDS)
            self.update(stage="submission")


def preparation_limits():
    from .pipeline import limits_from_env
    limits = limits_from_env()
    limits["source_limit"] = int(os.environ.get("JHB_SOURCE_BATCH_SIZE", "8"))
    limits["application_limit"] = int(os.environ.get("JHB_APPLICATION_BATCH_SIZE", "3"))
    limits["concurrency"] = int(os.environ.get("JHB_PIPELINE_CONCURRENCY", "2"))
    limits["max_active_drafts"] = int(os.environ.get("JHB_MAX_ACTIVE_DRAFTS", "20"))
    return limits


async def _approved_once(conn, book_path, drain, limit):
    status = SubmissionStatus(conn)
    status.update(stage="submission")
    pulse = asyncio.create_task(status.pulse())
    try:
        result = await drain(conn, book_path, limit=limit)
        status.update(status="completed", stage="complete", result=result)
        return {"state": "completed", **{key: value for key, value in result.items()
                 if key in {"attempted", "submitted", "uncertain", "handoffs", "enabled"} and type(value) in {int, bool}}}
    except BaseException as exc:
        status.update(status="failed", reason="operation_interrupted" if isinstance(exc, asyncio.CancelledError) else "operation_failed")
        raise
    finally:
        pulse.cancel()
        try:
            await pulse
        except asyncio.CancelledError:
            pass


def once(mode, *, book_path=booklet.DEFAULT_PATH, connector=None, prepare=None, approved=None):
    """One injected-testable local batch; no service can approve a draft."""
    if mode not in {"prepare", "approved"}:
        raise ValueError("Unknown worker service mode")
    reason = _gate(mode)
    if reason:
        return record_gate(mode, reason)
    if mode == "prepare":
        if prepare is None:
            from .pipeline import run_cycle
            prepare = run_cycle
        conn = (connector or store.connect)()
        try:
            result = prepare(conn, book_path, planner_name="codex", send_email=os.environ.get("JHB_APPLICATION_EMAIL") == "1",
                             **preparation_limits())
            return {"state": "skipped" if result.get("skipped") else "completed",
                    **{key: value for key, value in result.items() if key in {
                        "sources_checked", "applications_queued", "applications_prepared", "question_handoffs"}
                        and type(value) is int and value >= 0}}
        finally:
            conn.close()
    with _approved_lock() as owned:
        if not owned:
            return {"state": "skipped", "reason_code": "approved_worker_active"}
        # Pause/repair may have arrived between the first gate and lock claim.
        reason = _gate(mode)
        if reason:
            state = "paused" if reason == "automation_paused" else "blocked" if reason in {"repair_quarantine", "local_browser_disconnected"} else "disabled"
            SubmissionStatus().update(status=state, reason=reason)
            return {"state": state, "reason_code": reason}
        conn = (connector or store.connect)()
        try:
            status = SubmissionStatus(conn)
            from . import approvals
            recovery = {}
            # A submitting claim can belong to the independent pipeline. Only
            # exclusive ownership of both locks proves that it is an orphan.
            # Do not hold the preparation lock during ordinary approved work.
            with _worker_lock("application-worker.lock") as pipeline_idle:
                if pipeline_idle:
                    recovery = approvals.recover_stalled(conn)
            status.update()
            pending = status.value["approval_states"].get("approved", 0)
            if not pending:
                external = bool(status.value["active_jobs"])
                state = "running" if external else "completed"
                reason = "external_approval_active" if external else "no_approvals"
                recovered = recovery.get("recovered", 0)
                if recovered and not external:
                    reason = "approval_crash_recovered"
                status.update(status=state, stage="submission" if external else "idle", reason=reason, result=recovery)
                result = {"state": "running" if external else "idle", "reason_code": reason}
                if recovered:
                    result.update(recovered=recovered, submitted=recovery.get("submitted", 0), uncertain=recovery.get("uncertain", 0))
                return result
            if approved is None:
                approved = approvals.drain
            limit = int(os.environ.get("JHB_APPROVED_BATCH_SIZE", "3"))
            if not 1 <= limit <= 10:
                raise ValueError("Approved worker batch limit out of range")
            result = asyncio.run(_approved_once(conn, book_path, approved, limit))
            if recovery.get("recovered"):
                result["recovered"] = recovery["recovered"]
            return result
        except Exception:
            status.update(status="failed", reason="operation_failed")
            raise
        finally:
            conn.close()


def watch(mode, *, interval=None, book_path=booklet.DEFAULT_PATH, operation=None, window_loader=None,
          sleep=None, clock=None, emit=None):
    """The finite report window bounds service uptime, not submit permission."""
    interval = (5 if mode == "approved" else 30) if interval is None else interval
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not 1 <= interval <= 60:
        raise ValueError("Worker watch interval must be between one and sixty seconds")
    if _ci():
        return {"state": "disabled", "reason_code": "ci_disabled"}
    from .hourly_reports import load_report_window
    from .overnight import _timestamp
    window_loader = window_loader or load_report_window
    sleep, clock = sleep or time.sleep, clock or time.time
    operation, emit = operation or once, emit or (lambda value: print(json.dumps(value), flush=True))
    last, window_id = None, None
    while True:
        window = window_loader(now=clock())
        changed = window is not None and window_id is not None and window.get("authorization_id") != window_id
        if window is None or changed:
            reason = "service_window_changed" if changed else "service_window_ended"
            result = {"state": "expired", "reason_code": reason}
            if mode == "approved":
                with _approved_lock() as owned:
                    if owned:
                        SubmissionStatus().update(status="expired", reason=reason)
            emit(result)
            return result
        window_id = window["authorization_id"]
        try:
            result = operation(mode, book_path=book_path)
        except Exception as exc:
            result = {"state": "failed", "error_kind": type(exc).__name__}
        if result != last:
            emit(result)
            last = result
        remaining = _timestamp(window["expires_at"]) - clock()
        if remaining > 0:
            sleep(min(interval, remaining))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    for mode in ("prepare", "approved"):
        actions.add_argument("--"+mode+"-once", action="store_const", const=(mode, False), dest="action")
        actions.add_argument("--"+mode+"-watch", action="store_const", const=(mode, True), dest="action")
    parser.add_argument("--booklet", type=Path, default=booklet.DEFAULT_PATH)
    parser.add_argument("--interval", type=float)
    args = parser.parse_args(argv)
    if _ci():
        print(json.dumps({"state": "disabled", "reason_code": "ci_disabled"}))
        return 0
    config.load_dotenv(); config.refresh_from_env()
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    mode, watching = args.action
    if watching:
        watch(mode, interval=args.interval, book_path=args.booklet)
    else:
        try:
            result = once(mode, book_path=args.booklet)
        except Exception as exc:
            result = {"state": "failed", "error_kind": type(exc).__name__}
        print(json.dumps(result), flush=True)
        return int(result["state"] == "failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
