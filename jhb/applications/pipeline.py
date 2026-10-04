"""Bounded Phase 1 → isolated source checks → Greenhouse preparation manager.

Only the manager touches SQLite. Independent source checks and application
planners overlap; Browser Use CLI operations keep their existing atomic lock.
All real applications stop at review or explicit question/verification handoff.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import time
from pathlib import Path

from .. import config, notify
from . import booklet, queue, source_queue


def _limit(name, fallback, maximum):
    value = int(os.environ.get(name, str(fallback)))
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def limits_from_env():
    return {
        "source_limit": _limit("JHB_SOURCE_BATCH_SIZE", 3, 20),
        "application_limit": _limit("JHB_APPLICATION_BATCH_SIZE", 3, 10),
        "concurrency": _limit("JHB_PIPELINE_CONCURRENCY", 2, 4),
        "source_timeout": _limit("JHB_SOURCE_TIMEOUT_SECONDS", 90, 300),
        "application_timeout": _limit("JHB_APPLICATION_TIMEOUT_SECONDS", 600, 1800),
        "max_active_drafts": _limit("JHB_MAX_ACTIVE_DRAFTS", 10, 50),
    }


def _source_artifact(item, outcome):
    # Hash even a legacy/nonstandard source key before using it as a filename.
    key = hashlib.sha256(item["source_job_hash"].encode()).hexdigest()
    path = config.ROOT / "private" / "source-checks" / (key + ".json")
    booklet.write_private(path, {"source_job_hash": item["source_job_hash"], "job": item["job"], **outcome})
    return path


def notify_source_handoffs(conn, *, send_email=False):
    """Persist every source verification/error handoff and retry failed delivery."""
    source_queue.initialize(conn)
    rows = conn.execute("SELECT * FROM application_sources WHERE state IN "
                        "('waiting_login','waiting_captcha','unknown','failed') AND notified_at IS NULL").fetchall()
    for row in rows:
        job = json.loads(row["job_json"])
        notification = {"source_job_hash": row["source_job_hash"], "state": row["state"],
                        "board": row["board"], "evidence_path": row["evidence_path"], "submitted": False}
        filename = "source-" + hashlib.sha256(row["source_job_hash"].encode()).hexdigest() + ".json"
        booklet.write_private(config.ROOT / "private" / "notifications" / filename, notification)
        if send_email:
            details = (f"Application source status: {row['state']}\nDetected board: {row['board']}\n"
                       f"Local evidence: {row['evidence_path']}\nSource ID: {row['source_job_hash']}\n"
                       "The source checker needs review or verification. No application was submitted.\n"
                       "Resolve the issue, then use jhb-apply source-resume <Source ID> to retry.")
            if not notify.send([job], subject_prefix=f"[source {row['state']}] ", details=details):
                continue
        if not send_email:
            continue
        conn.execute("UPDATE application_sources SET notified_at=? WHERE source_job_hash=?",
                     (int(time.time()), row["source_job_hash"]))
        conn.commit()


def _requeue_answered_handoff(conn, job, result, book_path):
    """Recover an explicit answer arriving while its worker held an older book.

    Every current required missing control must have an answered ledger context.
    An incompatible answer reopened by collect remains pending and stops here;
    authentication, review and submitted states never qualify for this retry.
    """
    missing = [item for item in result.get("missing", []) if item.get("required", True)]
    if result.get("state") != "waiting_input" or not missing:
        return False
    job_hash = job["dedupe_hash"]
    ledger = booklet.load(book_path).get("question_handoffs", {})
    records = [record for record in ledger.values() if job_hash in record.get("contexts", {})]
    if any(record["status"] == "pending" and record["contexts"][job_hash].get("required")
           and not record["contexts"][job_hash].get("resolved") for record in records):
        return False
    for item in missing:
        text = booklet.normalize(item["question"])
        country = booklet.normalize(str(item["country_context"])) if item.get("country_context") is not None else None
        matching = [record for record in records if record["normalized_question"] == text
                    and record.get("country_context") == country
                    and record["contexts"][job_hash].get("ref") == item.get("ref")]
        if not matching or not all(record["status"] == "answered" for record in matching):
            return False
    changed = conn.execute("UPDATE applications SET state='queued',lease_until=NULL,attempts=0,updated_at=?,notified_at=NULL "
                           "WHERE job_hash=? AND state='waiting_input'", (int(time.time()), job_hash)).rowcount
    conn.commit()
    return bool(changed)


async def _resolve_one(item, resolver, semaphore, timeout):
    async with semaphore:
        try:
            outcome = await asyncio.wait_for(resolver(item["job"], timeout=timeout), timeout=timeout + 10)
            if not isinstance(outcome, dict) or outcome.get("state") not in {"greenhouse", "not_greenhouse", "blocked", "ambiguous", "error"}:
                raise ValueError("Invalid source classifier outcome")
            return outcome
        except Exception as exc:
            # Exception messages can contain page content, cookies or form values.
            return {"state": "error", "board_type": "unknown", "reason": f"Source check failed: {type(exc).__name__}", "evidence": []}


async def _prepare_one(item, runner, book, semaphore, timeout, planner_name):
    async with semaphore:
        try:
            result, packet = await asyncio.wait_for(runner(item["job"], book, planner_name=planner_name), timeout=timeout)
            if not isinstance(result, dict) or result.get("state") not in queue.STATES - {"queued", "running", "submitted"}:
                raise ValueError("Invalid preparation outcome")
            return result, packet
        except Exception as exc:
            from .worker import write_packet
            result = {"state": "failed", "reason": f"Preparation failed: {type(exc).__name__}", "events": [], "filled": []}
            directory = config.ROOT / "private" / "applications" / item["job_hash"]
            packet = await write_packet(None, directory, item["job"], result)
            return result, packet


async def cycle(conn, book_path, *, resolver=None, runner=None, source_limit=3, application_limit=3,
                concurrency=2, source_timeout=90, application_timeout=600, planner_name="codex", send_email=False,
                max_active_drafts=10):
    """Run one already-authorized batch. Caller owns the single manager lock."""
    if not (1 <= concurrency <= 4 and 1 <= source_limit <= 20 and 1 <= application_limit <= 10):
        raise ValueError("Pipeline batch/concurrency limits out of range")
    if not (1 <= source_timeout <= 300 and 1 <= application_timeout <= 1800):
        raise ValueError("Pipeline time limits out of range")
    if not 1 <= max_active_drafts <= 50:
        raise ValueError("Active draft limit out of range")
    if resolver is None:
        from .greenhouse_source import resolve_job
        resolver = resolve_job
    if runner is None:
        from .worker import run_job
        from .job_context import fetch
        async def runner(job, book, **kwargs):
            context = await asyncio.to_thread(fetch, job["url"])
            if context:
                booklet.write_private(config.ROOT / "private" / "applications" / job["dedupe_hash"] / "public-job-context.json", context)
                job = {**job, "work_country": context.get("country_context"),
                       "advertised_salary_ranges": context.get("advertised_salary_ranges", [])}
            return await run_job(job, book, **kwargs)
    from .worker import notify_pending

    queue.initialize(conn)
    source_queue.initialize(conn)
    notify_pending(conn, send_email=send_email)
    notify_source_handoffs(conn, send_email=send_email)
    summary = {"sources_checked": 0, "boards": {}, "applications_queued": 0,
               "applications_prepared": 0, "states": {}, "question_handoffs": 0, "auto_requeued": 0}
    sources = []
    source_lease = math.ceil(source_limit / concurrency) * (source_timeout + 10) + 120
    for _ in range(source_limit):
        item = source_queue.claim(conn, lease_seconds=source_lease)
        if item is None:
            break
        sources.append(item)
    source_semaphore = asyncio.Semaphore(concurrency)
    outcomes = await asyncio.gather(*[_resolve_one(item, resolver, source_semaphore, source_timeout)
                                      for item in sources])
    for item, outcome in zip(sources, outcomes):
        state = outcome.get("state", "error")
        board = outcome.get("board_type") or outcome.get("ats") or ("greenhouse" if state == "greenhouse" else "unknown")
        path = _source_artifact(item, outcome)
        application_url = outcome.get("application_url")
        if state == "greenhouse" and queue.is_greenhouse(application_url):
            resolved = {**item["job"], "url": application_url,
                        "source_url": item["job"]["url"], "source_evidence": str(path)}
            # Canonical queue identity makes crash/re-resolution and multiple wrappers idempotent.
            summary["applications_queued"] += queue.enqueue(conn, [resolved])
            terminal = "resolved"
        elif state == "not_greenhouse":
            terminal = "resolved"
        elif state == "blocked":
            terminal = outcome.get("handoff", "waiting_login")
            if terminal not in {"waiting_login", "waiting_captcha"}:
                terminal = "unknown"
        elif state == "ambiguous":
            terminal = "unknown"
        else:
            terminal = "retry" if item["attempts"] < 3 else "failed"
        source_queue.finish(conn, item["source_job_hash"], terminal, board=board,
                            application_url=application_url, evidence_path=path,
                            retry_seconds=60 * 2 ** (item["attempts"] - 1))
        summary["sources_checked"] += 1
        summary["boards"][board] = summary["boards"].get(board, 0) + 1

    items = []
    lease = math.ceil(application_limit / concurrency) * application_timeout + 120
    active = conn.execute("SELECT COUNT(*) FROM applications WHERE state IN "
                          "('waiting_review','waiting_input','waiting_login','waiting_captcha') OR "
                          "(state='running' AND lease_until >= ?)", (int(time.time()),)).fetchone()[0]
    for _ in range(min(application_limit, max(0, max_active_drafts - active))):
        item = queue.claim(conn, lease_seconds=lease)
        if item is None:
            break
        items.append(item)
    if items:
        try:
            book = booklet.load(book_path)
        except Exception:
            # Missing profile is a candidate input handoff, not a repeated browser failure.
            from .worker import write_packet
            for item in items:
                result = {"state": "waiting_input", "reason": "Private answer booklet must be configured before preparation",
                          "missing": [{"question": "Configure the private answer booklet"}], "events": [], "filled": []}
                packet = await write_packet(None, config.ROOT / "private" / "applications" / item["job_hash"], item["job"], result)
                queue.finish(conn, item["job_hash"], result["state"], packet)
            summary["states"]["waiting_input"] = len(items)
            summary["question_handoffs"] = len(items)
        else:
            semaphore = asyncio.Semaphore(concurrency)
            results = await asyncio.gather(*[_prepare_one(item, runner, book, semaphore, application_timeout, planner_name)
                                            for item in items])
            for item, (result, packet) in zip(items, results):
                from .questions import collect, reconcile
                collect(item["job"], result, book_path, observed_book=book)
                reconcile(item["job"], result, book_path)
                queue.finish(conn, item["job_hash"], result["state"], packet)
                if _requeue_answered_handoff(conn, item["job"], result, book_path):
                    summary["auto_requeued"] += 1
                summary["applications_prepared"] += 1
                state = result["state"]
                summary["states"][state] = summary["states"].get(state, 0) + 1
                if state == "waiting_input":
                    summary["question_handoffs"] += 1
    # Refresh from the authoritative ledger, including earlier batches and
    # answered questions. Never leave a stale outbox after successful resumption.
    try:
        from .questions import pending
        pending_questions = pending(book_path)
    except (FileNotFoundError, ValueError):
        pending_questions = None
    if pending_questions is not None:
        applications = {}
        for question in pending_questions:
            for job_hash, context in question["contexts"].items():
                record = applications.setdefault(job_hash, {"job_hash": job_hash, "company": context["company"], "questions": []})
                record["questions"].append({"question_id": question["id"], "question": question["question"],
                                            "required": context["required"]})
        booklet.write_private(config.ROOT / "private" / "notifications" / "pending-questions.json",
                              {"questions": pending_questions, "applications": list(applications.values())})
        summary["pending_question_ids"] = [question["id"] for question in pending_questions]
        from .questions import notify_new
        summary["questions_notified"] = notify_new(conn, book_path, send_email=send_email)
    notify_pending(conn, send_email=send_email)
    notify_source_handoffs(conn, send_email=send_email)
    # Delivery is the final stage for separately confirmed receipts. Preparation
    # outcomes and submitted markers cannot create tracker entries here.
    from .tracking import sync_pending
    summary["submission_tracking"] = sync_pending(conn)
    return summary


def run_cycle(conn, book_path, **kwargs):
    """Share the worker manager lock so cron/manual workers cannot double-claim."""
    path = config.ROOT / "private" / "application-worker.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.touch(mode=0o600, exist_ok=True)
    with path.open("r+") as manager:
        try:
            fcntl.flock(manager, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"skipped": "Another pipeline/worker manager is active"}
        return asyncio.run(cycle(conn, book_path, **kwargs))
