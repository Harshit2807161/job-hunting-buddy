"""Bounded Phase 1 → isolated source checks → reviewed ATS adapters.

Only the manager touches SQLite. Independent source checks and application
planners overlap; Browser Use CLI operations keep their existing atomic lock.
Preparation stops at review or handoff. A separate finite user authorization can
enable audited submission of new Phase 1 jobs through the receipt-only tracker.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

from .. import config, notify
from . import booklet, boards, queue, source_queue

TRANSIENT_KINDS = {"TimeoutError", "TimeoutExpired", "ConnectionError", "ConnectionResetError",
                   "ConnectionAbortedError", "BrokenPipeError", "FileNotFoundError",
                   "browser_transport", "browser_mechanics", "browser_capture", "planner_transport", "job_description_transport", "document_generation", "narrative_generation"}


def _exception_recovery(exc):
    from .cli_browser import BrowserOperationError
    if (isinstance(exc, BrowserOperationError) and getattr(exc, "condition", None) == "browser_capacity"
            and getattr(exc, "mutation_started", None) is False):
        return {"retryable": True, "error_kind": "browser_capacity", "mutation_started": False}
    kind = type(exc).__name__
    if kind in TRANSIENT_KINDS:
        return {"retryable": True, "error_kind": kind}
    # Transport wrappers deliberately expose fixed messages, not site content.
    if isinstance(exc, RuntimeError):
        if str(exc) in {"Browser Use CLI failed; run browser-use --doctor",
                        "Browser Use CLI returned no structured result"}:
            return {"retryable": True, "error_kind": "browser_transport"}
        if str(exc) in {"Codex planning failed; check local sign-in, usage limits, and network access",
                        "Codex produced no structured plan"}:
            return {"retryable": True, "error_kind": "planner_transport"}
    return {"retryable": False, "error_kind": kind}


def _recoverable(result):
    return (result.get("state") == "failed" and result.get("retryable") is True
            and result.get("error_kind") in TRANSIENT_KINDS and not result.get("missing")
            and not result.get("verification") and not result.get("submitted")
            and ("runtime_click_started" not in result or result["runtime_click_started"] is False))


def _capacity_wait(result):
    return (result.get("state") in {"failed", "error"} and result.get("error_kind") == "browser_capacity"
            and result.get("mutation_started") is False and not result.get("filled")
            and not result.get("missing") and not result.get("verification")
            and not result.get("submitted") and not result.get("runtime_click_started"))


def recover_technical_failures(conn):
    """Migrate old sanitized technical packets once, without resetting attempts."""
    from .authorized_submission import private_file
    changed = 0
    for row in conn.execute("SELECT job_hash,job_json,packet FROM applications WHERE state='failed' AND attempts < 3").fetchall():
        if not row["packet"]:
            continue
        # Legacy preparation recovery has no authority to revisit a reviewed
        # draft or any past terminal attempt, including an orphan private marker.
        protected = any(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
                        and conn.execute(f"SELECT 1 FROM {table} WHERE job_hash=?", (row["job_hash"],)).fetchone()
                        for table in ("authorized_submission_attempts", "application_approvals"))
        marker = config.ROOT / 'private' / 'authorized-submissions' / row['job_hash'] / 'attempt.json'
        if protected or marker.exists() or marker.is_symlink():
            continue
        try:
            path = private_file(str(Path(row["packet"]).parent / "packet.json"))
            if not path.resolve().is_relative_to((config.ROOT / "private" / "applications").resolve()):
                continue
            result = json.loads(path.read_text())
            original = json.loads(row['job_json'])
            if (not isinstance(result, dict) or result.get('job', {}).get('dedupe_hash') != row['job_hash']
                    or boards.application_hash(original.get('url')) != row['job_hash']
                    or boards.job_identity(result['job'].get('url')) != boards.job_identity(original.get('url'))):
                continue
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
        # Prior workers stored only the class name. Generic ValueError or
        # RuntimeError could be a deliberate safety rejection and stays stopped.
        legacy = re.fullmatch(r"Preparation failed: (\w+)", str(result.get("reason", "")))
        if legacy and legacy[1] in TRANSIENT_KINDS and "retryable" not in result:
            result = {**result, "retryable": True, "error_kind": legacy[1]}
        if _recoverable(result):
            changed += queue.retry(conn, row["job_hash"], error_kind=result["error_kind"], retry_seconds=0)
    return changed


def recover_description_handoffs(conn, *, limit=20):
    """Reconsider pre-refresh, zero-field technical handoffs once; retain budgets."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("Description handoff recovery limit out of range")
    from .authorized_submission import private_file
    from .tracking import confirmed_application
    conn.execute("CREATE TABLE IF NOT EXISTS job_description_refresh_rechecks "
                 "(job_hash TEXT PRIMARY KEY,packet_sha256 TEXT NOT NULL,rechecked_at INTEGER NOT NULL)")
    rows = conn.execute("SELECT a.* FROM applications a LEFT JOIN job_description_refresh_rechecks r "
                        "ON r.job_hash=a.job_hash WHERE a.state='waiting_input' AND a.attempts<3 "
                        "AND r.job_hash IS NULL ORDER BY a.updated_at,a.job_hash LIMIT ?", (limit,)).fetchall()
    changed = 0
    for row in rows:
        if not row["packet"]:
            continue
        protected = False
        for table in ("authorized_submission_attempts", "application_approvals"):
            if (conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
                    and conn.execute(f"SELECT 1 FROM {table} WHERE job_hash=?", (row["job_hash"],)).fetchone()):
                protected = True
                break
        if protected:
            continue
        try:
            path = private_file(str(Path(row["packet"]).parent / "packet.json"))
            raw = path.read_bytes()
            packet, original = json.loads(raw), json.loads(row["job_json"])
            verification = packet.get("eligibility", {}).get("verification", {})
            if (packet.get("state") != "waiting_input" or packet.get("filled") or packet.get("missing")
                    or packet.get("submitted") is True or packet.get("runtime_click_started") is True
                    or packet.get("optional_questions")
                    or packet.get("review_inventory", {}).get("fields") or packet.get("verification")
                    or verification.get("kind") != "job_description"
                    or packet.get("job", {}).get("dedupe_hash") != row["job_hash"]
                    or boards.application_hash(original.get("url")) != row["job_hash"]
                    or boards.job_identity(packet["job"].get("url")) != boards.job_identity(original.get("url"))):
                continue
            if confirmed_application(conn, original.get("url")):
                continue
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
        with conn:
            updated = conn.execute("UPDATE applications SET state='queued',lease_until=NULL,available_at=0,error_kind=NULL,"
                                   "notified_at=NULL,updated_at=? WHERE job_hash=? AND state='waiting_input' AND attempts<3",
                                   (int(time.time()), row["job_hash"])).rowcount
            if updated:
                conn.execute("INSERT OR IGNORE INTO job_description_refresh_rechecks VALUES(?,?,?)",
                             (row["job_hash"], hashlib.sha256(raw).hexdigest(), int(time.time())))
                changed += 1
    return changed


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
    """Keep technical source outcomes local; digest sustained auth challenges."""
    source_queue.initialize(conn)
    rows = conn.execute("SELECT * FROM application_sources WHERE state IN "
                        "('waiting_login','waiting_captcha','unknown','failed') AND notified_at IS NULL").fetchall()
    for row in rows:
        notification = {"source_job_hash": row["source_job_hash"], "state": row["state"],
                        "board": row["board"], "evidence_path": row["evidence_path"], "submitted": False}
        filename = "source-" + hashlib.sha256(row["source_job_hash"].encode()).hexdigest() + ".json"
        booklet.write_private(config.ROOT / "private" / "notifications" / filename, notification)
    if not send_email:
        return
    now = int(time.time())
    aged = [row for row in rows if row["state"] in {"waiting_login", "waiting_captcha"}
            and row["updated_at"] <= now - 3600]
    by_key = {f"source-auth:{row['source_job_hash']}:{row['state']}": row for row in aged}
    if not by_key:
        return
    from . import notices
    def send(keys):
        jobs = [json.loads(by_key[key]["job_json"]) for key in sorted(keys)]
        return notify.send(jobs, subject_prefix="[Source access review] ", details=(
            "These source checks have remained blocked by site authentication or a verification "
            "challenge for at least one hour. The isolated source checker does not reuse your "
            "personal browser session. No application was submitted. Details remain in "
            "private/source-checks; use source-status to inspect and source-resume after resolving access."))
    delivered = notices.deliver(conn, by_key, "source_auth_digest", send, now=now)
    for key in delivered:
        conn.execute("UPDATE application_sources SET notified_at=? WHERE source_job_hash=? AND state=?",
                     (now, by_key[key]["source_job_hash"], by_key[key]["state"]))
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


async def _resolve_one(item, resolver, semaphore, timeout, authenticated_resolver=None):
    from ..eligibility import preliminary, POLICY_ID
    findings = preliminary(item["job"])
    if findings:
        return {"state": "filtered", "board_type": boards.board_type(item["job"].get("url")),
                "reason": "Mandatory eligibility requirement excluded before source browser access",
                "policy": POLICY_ID, "findings": findings, "evidence": []}
    async with semaphore:
        try:
            states = {"greenhouse", "not_greenhouse", "blocked", "ambiguous", "error"}
            if authenticated_resolver is not None and boards.board_type(item["job"].get("url")) == "linkedin":
                # The user explicitly enabled their signed-in source session.
                # Local resolution itself isolates any external ATS destination.
                try:
                    local = await asyncio.wait_for(authenticated_resolver(
                        item["job"], isolated_outcome=None, timeout=timeout), timeout=timeout + 10)
                    if isinstance(local, dict) and local.get("state") in states - {"ambiguous", "error"}:
                        return local
                except Exception as exc:
                    recovery = _exception_recovery(exc)
                    if recovery.get("error_kind") == "browser_capacity":
                        return {"state": "error", "board_type": "linkedin", "reason": "Waiting for browser tab capacity",
                                "evidence": [], **recovery}
                    pass
            outcome = await asyncio.wait_for(resolver(item["job"], timeout=timeout), timeout=timeout + 10)
            if not isinstance(outcome, dict) or outcome.get("state") not in states:
                raise ValueError("Invalid source classifier outcome")
            return outcome
        except Exception as exc:
            # Exception messages can contain page content, cookies or form values.
            return {"state": "error", "board_type": "unknown", "reason": f"Source check failed: {type(exc).__name__}", "evidence": []}


def _screen_source(item, outcome):
    """Screen the exact observed ATS description before dispatching its filler."""
    from ..eligibility import preliminary, POLICY_ID
    from .job_context import valid_description
    url = outcome.get("application_url")
    identity = boards.job_identity(url)
    observed_board = outcome.get("board_type") or outcome.get("ats")
    if (outcome.get("closed") or outcome.get("state") not in {"greenhouse", "not_greenhouse"}
            or not identity or observed_board and observed_board not in {identity[0], "unknown"}):
        return outcome
    job = {**item["job"], "url": url}
    description = outcome.get("verified_job_description")
    if valid_description(description, url):
        job["verified_job_description"] = description
    findings = preliminary(job)
    if findings:
        eligibility = {"state": "skipped", "policy": POLICY_ID, "findings": findings,
                       "reason": "Official job requirements conflict with candidate employment policy"}
        return {**outcome, "state": "filtered", "eligibility": eligibility, **{k: eligibility[k] for k in ("policy", "findings", "reason")}}
    return outcome


def _route_source(conn, item, outcome, path):
    url = outcome.get("application_url")
    identity = boards.job_identity(url)
    observed_board = outcome.get("board_type") or outcome.get("ats")
    board = boards.route_board(url, observed_board)
    if (outcome.get("closed") or outcome.get("state") not in {"greenhouse", "not_greenhouse"}
            or not identity or not boards.preparation_supported(board)
            or observed_board and observed_board not in {board, "unknown"}):
        return 0
    resolved = {**item["job"], "url": url, "board_type": board,
                "source_url": item["job"]["url"], "source_evidence": str(path)}
    from .job_context import valid_description
    description = outcome.get("verified_job_description")
    if valid_description(description, url):
        resolved["verified_job_description"] = description
    count = queue.enqueue(conn, [resolved])
    # One source can wrap the same canonical job as another source. The route
    # marker records both without changing any existing application state.
    conn.execute("INSERT OR IGNORE INTO source_application_routes(source_job_hash,application_hash,routed_at) VALUES(?,?,?)",
                 (item["source_job_hash"], boards.application_hash(url), int(time.time())))
    conn.commit()
    return count


def replay_resolved_sources(conn, *, limit=100):
    """Newly reviewed adapters may consume old classifications without resetting jobs."""
    conn.execute("CREATE TABLE IF NOT EXISTS source_application_routes (source_job_hash TEXT PRIMARY KEY,"
                 "application_hash TEXT NOT NULL,routed_at INTEGER NOT NULL)")
    supported = [name for name in boards.ADAPTERS if boards.preparation_supported(name)]
    if not supported:
        return 0
    placeholders = ",".join("?" for _ in supported)
    rows = conn.execute("SELECT s.* FROM application_sources s LEFT JOIN source_application_routes r "
                        "ON r.source_job_hash=s.source_job_hash WHERE s.state='resolved' AND s.application_url IS NOT NULL "
                        f"AND s.board IN ({placeholders}) AND r.source_job_hash IS NULL ORDER BY s.updated_at,s.source_job_hash LIMIT ?",
                        (*supported, limit)).fetchall()
    inserted = 0
    for row in rows:
        try:
            path = Path(row["evidence_path"])
            if not path.resolve().is_relative_to((config.ROOT / "private" / "source-checks").resolve()) or path.stat().st_size > 2_000_000:
                continue
            outcome = json.loads(path.read_text())
            if outcome.get("application_url") != row["application_url"] or outcome.get("board_type", outcome.get("ats")) != row["board"]:
                continue
            item = {**dict(row), "job": json.loads(row["job_json"])}
            outcome = _screen_source(item, outcome)
            if outcome.get("state") == "filtered":
                path = _source_artifact(item, outcome)
                source_queue.finish(conn, item["source_job_hash"], "filtered", board=row["board"],
                                    application_url=row["application_url"], evidence_path=path,
                                    eligibility=outcome["eligibility"])
                continue
            inserted += _route_source(conn, item, outcome, path)
        except (OSError, TypeError, ValueError):
            continue
    return inserted


def recover_authenticated_linkedin_sources(conn, *, limit=3):
    """An explicitly enabled local session may reconsider old isolated handoffs once."""
    if os.environ.get("JHB_LINKEDIN_LOCAL_RESOLUTION") != "1":
        return 0
    conn.execute("CREATE TABLE IF NOT EXISTS source_authenticated_rechecks (source_job_hash TEXT PRIMARY KEY,"
                 "rechecked_at INTEGER NOT NULL)")
    rows = conn.execute("SELECT s.* FROM application_sources s LEFT JOIN source_authenticated_rechecks r "
                        "ON r.source_job_hash=s.source_job_hash WHERE s.state IN "
                        "('waiting_login','waiting_captcha','unknown','resolved') AND r.source_job_hash IS NULL "
                        "AND (s.board IN ('linkedin','unknown') OR s.board IS NULL) "
                        "AND (json_extract(s.job_json,'$.url') LIKE 'https://www.linkedin.com/jobs/view/%' "
                        "OR json_extract(s.job_json,'$.url') LIKE 'https://linkedin.com/jobs/view/%') "
                        "ORDER BY s.updated_at,s.source_job_hash LIMIT 100").fetchall()
    changed = 0
    for row in rows:
        job = json.loads(row["job_json"])
        if (boards.board_type(job.get("url")) != "linkedin"
                or row["application_url"] and boards.board_type(row["application_url"]) not in {"linkedin", "unknown"}):
            continue
        conn.execute("INSERT INTO source_authenticated_rechecks VALUES(?,?)", (row["source_job_hash"], int(time.time())))
        conn.execute("UPDATE application_sources SET state='queued',lease_until=NULL,attempts=0,available_at=0,"
                     "notified_at=NULL,updated_at=? WHERE source_job_hash=?", (int(time.time()), row["source_job_hash"]))
        changed += 1
        if changed >= limit:
            break
    conn.commit()
    return changed


async def _prepare_one(item, runner, book, semaphore, timeout, planner_name):
    import uuid
    from .worker import _FEEDBACK_ATTEMPT, _record_attempt_feedback
    from . import application_discard
    async with semaphore:
        attempt_token = uuid.uuid4().hex
        context_token = _FEEDBACK_ATTEMPT.set(attempt_token)
        try:
            application_discard.check(config.ROOT, item["job_hash"])
            result, packet = await asyncio.wait_for(runner(item["job"], book, planner_name=planner_name), timeout=timeout)
            if not isinstance(result, dict) or result.get("state") not in queue.STATES - {"queued", "running", "retry", "submitted"}:
                raise ValueError("Invalid preparation outcome")
            verification = result.get("eligibility", {}).get("verification", {})
            if (result.get("state") == "waiting_input" and not result.get("missing")
                    and verification.get("kind") == "job_description"
                    and verification.get("error_type") in {"TimeoutError", "TimeoutExpired", "URLError", "OSError",
                                                            "ConnectionError", "ConnectionResetError"}):
                # The profile cannot answer a network failure. Retry only the
                # official-description transport, with the normal finite budget.
                from .worker import write_packet
                result = {**result, "state": "failed", "retryable": True, "error_kind": "job_description_transport",
                          "reason": "Official job-description transport failed before browser preparation"}
                packet = await write_packet(None, config.ROOT / "private" / "applications" / item["job_hash"], item["job"], result)
        except application_discard.ApplicationDiscarded:
            record = application_discard._read(application_discard._path(config.ROOT, "application-discards", item["job_hash"])) or {}
            result = {"state": "discarded", "reason": "Candidate discarded this application", "missing": [], "filled": [], "events": []}
            packet = record.get("packet_path")
        except Exception as exc:
            from .worker import write_packet
            result = {"state": "failed", "reason": f"Preparation failed: {type(exc).__name__}",
                      "events": [], "filled": [], **_exception_recovery(exc)}
            directory = config.ROOT / "private" / "applications" / item["job_hash"]
            packet = await write_packet(None, directory, item["job"], result)
        finally:
            _FEEDBACK_ATTEMPT.reset(context_token)
        # Persistence/capture can change readiness; record only its final
        # outcome. A telemetry failure is isolated from application retries.
        _record_attempt_feedback(item["job"], result, attempt_token, packet)
        return result, packet


async def cycle(conn, book_path, *, resolver=None, runner=None, source_limit=3, application_limit=3,
                concurrency=2, source_timeout=90, application_timeout=600, planner_name="codex", send_email=False,
                max_active_drafts=10, submission_runner=None, authenticated_resolver=None, heartbeat=None):
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
    if authenticated_resolver is None and os.environ.get("JHB_LINKEDIN_LOCAL_RESOLUTION") == "1":
        from .linkedin import resolve_source
        authenticated_resolver = resolve_source
    if runner is None:
        from .worker import run_job, write_packet
        from .source_refresh import refresh
        async def runner(job, book, **kwargs):
            from .retained_preparation import retained_review
            if retained_review(job, book, book_path):
                return await run_job(job, book, book_path=book_path, **kwargs)
            refreshed = await refresh(job, resolver=resolver, timeout=source_timeout)
            directory = config.ROOT / "private" / "applications" / job["dedupe_hash"]
            booklet.write_private(directory / "source-refresh.json", refreshed)
            if refreshed["state"] != "verified":
                return refreshed, await write_packet(None, directory, job, refreshed)
            context = refreshed["context"]
            booklet.write_private(directory / "public-job-context.json", context)
            # The refreshed source owns jurisdiction evidence. An ambiguous or
            # absent current country must not inherit an older US binding.
            job = {**job, "work_country": context.get("country_context"),
                   "advertised_salary_ranges": context.get("advertised_salary_ranges", []),
                   "verified_job_description": context["verified_job_description"]}
            return await run_job(job, book, book_path=book_path, **kwargs)
    from .worker import notify_pending

    queue.initialize(conn)
    source_queue.initialize(conn)
    from .application_discard import reconcile_pending
    await asyncio.to_thread(reconcile_pending)
    from . import historical
    history_import = await historical.refresh_sheet(conn)
    if history_import["state"] in {"pending", "busy"} and not historical.cached_ready(conn):
        # Never open browsers on the first run without loading the configured
        # application history. A previous complete cache survives connector outages.
        return {"sources_checked": 0, "applications_prepared": 0,
                "history_import": history_import, "history_unavailable": True,
                "reason": "Application history must be read before browser preparation"}
    from .tracking import restore_confirmed_applications
    historical_confirmations = restore_confirmed_applications(conn)
    sources_history_blocked = source_queue.filter_history(conn)
    applications_history_blocked = queue.filter_history(conn)
    local_rechecks = recover_authenticated_linkedin_sources(conn, limit=source_limit)
    replayed = replay_resolved_sources(conn)
    recovered = recover_technical_failures(conn)
    description_rechecks = recover_description_handoffs(conn)
    from .answer_resume import recover as recover_saved_answers
    try:
        answered_rechecks = recover_saved_answers(conn, book_path)
    except (OSError, ValueError):
        answered_rechecks = 0  # Missing profile keeps its existing explicit handoff.
    notify_source_handoffs(conn, send_email=send_email)
    summary = {"sources_checked": 0, "boards": {}, "applications_queued": replayed, "sources_replayed": replayed,
               "authenticated_sources_requeued": local_rechecks,
               "applications_prepared": 0, "states": {}, "question_handoffs": 0, "auto_requeued": answered_rechecks,
               "technical_recovered": recovered, "technical_retries": 0}
    summary["historical_confirmations_reconciled"] = historical_confirmations
    summary["history_import"] = history_import
    summary["sources_history_blocked"] = sources_history_blocked
    summary["applications_history_blocked"] = applications_history_blocked
    summary["description_handoffs_requeued"] = description_rechecks
    sources = []
    if heartbeat:
        heartbeat.update(stage="source_resolution", summary=summary)
    source_lease = math.ceil(source_limit / concurrency) * (source_timeout + 10) * (2 if authenticated_resolver else 1) + 120
    for _ in range(source_limit):
        item = source_queue.claim(conn, lease_seconds=source_lease)
        if item is None:
            break
        sources.append(item)
    source_semaphore = asyncio.Semaphore(concurrency)
    outcomes = await asyncio.gather(*[_resolve_one(item, resolver, source_semaphore, source_timeout, authenticated_resolver)
                                      for item in sources])
    for item, outcome in zip(sources, outcomes):
        outcome = _screen_source(item, outcome)
        state = outcome.get("state", "error")
        board = outcome.get("board_type") or outcome.get("ats") or ("greenhouse" if state == "greenhouse" else "unknown")
        path = _source_artifact(item, outcome)
        if _capacity_wait(outcome) and source_queue.defer_capacity(conn, item, evidence_path=path):
            summary["browser_capacity_deferred"] = summary.get("browser_capacity_deferred", 0) + 1
            continue
        application_url = outcome.get("application_url")
        if state == "filtered":
            terminal = "filtered"
        elif state in {"greenhouse", "not_greenhouse"}:
            summary["applications_queued"] += _route_source(conn, item, outcome, path)
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
                            eligibility=outcome.get("eligibility") or ({"state": "skipped", "policy": outcome.get("policy"),
                                "findings": outcome.get("findings", []), "reason": outcome.get("reason")} if terminal == "filtered" else None),
                            retry_seconds=60 * 2 ** (item["attempts"] - 1))
        summary["sources_checked"] += 1
        summary["boards"][board] = summary["boards"].get(board, 0) + 1

    items = []
    if heartbeat:
        heartbeat.update(stage="preparation", summary=summary)
    lease = math.ceil(application_limit / concurrency) * application_timeout + 120
    active = conn.execute("SELECT COUNT(*) FROM applications WHERE state IN "
                          "('waiting_review','waiting_input','waiting_login','waiting_captcha','submission_uncertain') OR "
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
                if _capacity_wait(result) and queue.defer_capacity(conn, item, packet=packet):
                    summary["browser_capacity_deferred"] = summary.get("browser_capacity_deferred", 0) + 1
                    summary["states"]["retry"] = summary["states"].get("retry", 0) + 1
                    continue
                from .questions import collect, reconcile
                collect(item["job"], result, book_path, observed_book=book)
                reconcile(item["job"], result, book_path)
                queue.finish(conn, item["job_hash"], result["state"], packet)
                if _recoverable(result) and queue.retry(conn, item["job_hash"], error_kind=result["error_kind"],
                                                       packet=packet, retry_seconds=300 * 2 ** (item["attempts"]-1)):
                    summary["technical_retries"] += 1
                if _requeue_answered_handoff(conn, item["job"], result, book_path):
                    summary["auto_requeued"] += 1
                if not result.get("preparation_preserved"):
                    summary["applications_prepared"] += 1
                state = conn.execute("SELECT state FROM applications WHERE job_hash=?", (item["job_hash"],)).fetchone()[0]
                summary["states"][state] = summary["states"].get(state, 0) + 1
                if state == "waiting_input":
                    summary["question_handoffs"] += 1
    # The current portal policy consumes only one explicit approval per exact
    # draft. Legacy finite-authority dispatch remains gated for compatibility.
    from .overnight import load_authorization, INDEPENDENT_MODE
    delegated = load_authorization()
    if (os.environ.get("JHB_REQUIRE_PORTAL_APPROVAL") == "1"
            and not (delegated and delegated.get("approval_mode") == INDEPENDENT_MODE)):
        from .approvals import drain as drain_authorized
    else:
        from .overnight import drain as drain_authorized
    if heartbeat:
        heartbeat.update(stage="submission", summary=summary)
    summary["authorized_submissions"] = await drain_authorized(
        conn, book_path, limit=application_limit, submitter=submission_runner)
    from .submission_notices import notify_uncertain
    if heartbeat:
        heartbeat.update(stage="notifications", summary=summary)
    summary["submission_outcome_notices"] = notify_uncertain(conn, send_email=send_email)
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
    if heartbeat:
        heartbeat.update(stage="tracking", summary=summary)
    summary["submission_tracking"] = sync_pending(conn)
    from .hourly_reports import report
    summary["hourly_report"] = report(conn)
    summary["backlog"] = {
        "sources": conn.execute("SELECT COUNT(*) FROM application_sources WHERE state IN ('queued','retry')").fetchone()[0],
        "applications": conn.execute("SELECT COUNT(*) FROM applications WHERE state IN ('queued','retry')").fetchone()[0],
        "active_drafts": conn.execute("SELECT COUNT(*) FROM applications WHERE state IN "
                                      "('waiting_review','waiting_input','waiting_login','waiting_captcha','submission_uncertain') OR "
                                      "(state='running' AND lease_until > ?)", (int(time.time()),)).fetchone()[0],
        "draft_limit": max_active_drafts,
    }
    summary["capacity_blocked"] = (summary["backlog"]["applications"] > 0
                                   and summary["backlog"]["active_drafts"] >= max_active_drafts)
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
        pause = config.ROOT / "private" / "pipeline-pause.json"
        if pause.exists() or pause.is_symlink():
            from .pipeline_status import Heartbeat
            from .hourly_reports import report
            Heartbeat(conn).update(status="paused", reasons=["automation_paused"])
            return {"skipped": "Application automation is explicitly paused", "hourly_report": report(conn)}
        quarantine = config.ROOT / "private" / "overnight-monitor" / "repair-pending.json"
        if quarantine.exists() or quarantine.is_symlink():
            from .pipeline_status import Heartbeat
            from .hourly_reports import report
            Heartbeat(conn).update(status="blocked", reasons=["repair_quarantine"])
            return {"skipped": "Overnight repair requires validated recovery", "hourly_report": report(conn)}
        from .pipeline_status import monitor_cycle
        return asyncio.run(monitor_cycle(conn, book_path, cycle, **kwargs))
