"""Private, bounded pipeline heartbeat; queue facts are read-only aggregates."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import sqlite3
import time
import uuid

from .. import config
from .booklet import write_private

INTERVAL = 15
STAGES = {"initializing", "source_resolution", "preparation", "submission", "notifications", "tracking", "complete"}
PROCESSED = {"sources_checked", "applications_queued", "sources_replayed", "applications_prepared",
             "question_handoffs", "auto_requeued", "technical_recovered", "technical_retries", "browser_capacity_deferred",
             "sources_history_blocked", "applications_history_blocked"}
REASONS = {"repair_quarantine", "manager_active", "draft_capacity", "candidate_answers_required",
           "submission_authority_inactive", "no_ready_jobs", "cycle_failed", "cycle_interrupted", "automation_paused",
           "local_browser_disconnected", "local_browser_unavailable", "preparation_disabled", "portal_required", "browser_capacity",
           "application_history_unavailable"}


def _timestamp(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).isoformat()


def aggregates(conn):
    """No queue initialization, candidate records, or application URL exposure."""
    result = {}
    if conn is None:
        return result
    for key, table in [("applications", "applications"), ("sources", "application_sources"),
                       ("sheet_delivery", "submission_sheet_delivery")]:
        try:
            rows = conn.execute(f"SELECT state,COUNT(*) FROM {table} GROUP BY state").fetchall()
            allowed = {"queued", "running", "retry", "waiting_review", "submission_uncertain", "waiting_input",
                       "waiting_login", "waiting_captcha", "unsupported", "failed", "submitted", "skipped",
                       "resolved", "unknown", "pending", "synced", "uncertain", "syncing", "history_hold"}
            result[key] = {state: count for state, count in rows if state in allowed}
        except sqlite3.OperationalError:
            result[key] = {}
    return result


class Heartbeat:
    def __init__(self, conn):
        self.conn = conn
        self.path = config.ROOT / "private" / "pipeline-status.json"
        self.value = {"schema_version": 1, "cycle_id": uuid.uuid4().hex, "status": "running",
                      "stage": "initializing", "started_at": _timestamp(), "completed_at": None,
                      "heartbeat_interval_seconds": INTERVAL, "stale_after_seconds": INTERVAL*3,
                      "processed": {}, "reason_codes": []}

    def update(self, *, stage=None, summary=None, status=None, reasons=None):
        if stage is not None:
            if stage not in STAGES:
                raise ValueError("Unknown pipeline stage")
            self.value["stage"] = stage
        if status is not None:
            if status not in {"running", "completed", "blocked", "failed", "paused"}:
                raise ValueError("Unknown pipeline status")
            self.value["status"] = status
            if status != "running":
                self.value["completed_at"] = _timestamp()
        if summary is not None:
            self.value["processed"] = {key: value for key, value in summary.items()
                                        if key in PROCESSED and type(value) is int and value >= 0}
            for key, prefix, fields in [("authorized_submissions", "submission", {"attempted", "submitted", "uncertain", "handoffs"}),
                                         ("submission_tracking", "sheet", {"synced", "uncertain", "failed"})]:
                self.value["processed"].update({f"{prefix}_{name}": number for name, number in summary.get(key, {}).items()
                                                if name in fields and type(number) is int and number >= 0})
        if reasons is not None:
            self.value["reason_codes"] = sorted(set(reasons) & REASONS)
        self.value["updated_at"] = _timestamp()
        self.value["queue"] = aggregates(self.conn)
        # Observability cannot change an application outcome. A missing/unwritable
        # status file becomes a stale heartbeat, rather than a queue mutation.
        try:
            write_private(self.path, self.value)
        except (OSError, ValueError):
            pass

    async def pulse(self):
        while True:
            await asyncio.sleep(INTERVAL)
            self.update()


async def monitor_cycle(conn, book_path, operation, **kwargs):
    heartbeat = Heartbeat(conn)
    heartbeat.update()
    pulse = asyncio.create_task(heartbeat.pulse())
    try:
        result = await operation(conn, book_path, heartbeat=heartbeat, **kwargs)
        reasons = []
        if result.get("history_unavailable"):
            reasons.append("application_history_unavailable")
        if result.get("capacity_blocked"):
            reasons.append("draft_capacity")
        if result.get("browser_capacity_deferred"):
            reasons.append("browser_capacity")
        if heartbeat.value["queue"].get("applications", {}).get("waiting_input", 0):
            reasons.append("candidate_answers_required")
        if result.get("authorized_submissions", {}).get("enabled") is False:
            reasons.append("submission_authority_inactive")
        if (not result.get("browser_capacity_deferred") and not result.get("sources_checked")
                and not result.get("applications_prepared") and not result.get("authorized_submissions", {}).get("attempted")):
            reasons.append("no_ready_jobs")
        heartbeat.update(stage="complete", summary=result, status="blocked" if result.get("history_unavailable") else "completed", reasons=reasons)
        return result
    except BaseException as exc:
        heartbeat.update(status="failed", reasons=["cycle_interrupted" if isinstance(exc, asyncio.CancelledError) else "cycle_failed"])
        raise
    finally:
        pulse.cancel()
        try:
            await pulse
        except asyncio.CancelledError:
            pass
