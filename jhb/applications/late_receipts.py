"""Bounded observation of an already-clicked application, never a second submit.

This record-only path deliberately does not load current submission authority:
expiry or a later pause cannot undo an employer's persisted positive receipt. A
user pause still stops new browser reads. The original
click marker, identical audits, document hashes and reviewer remain mandatory.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from .. import config
from . import boards, booklet, overnight

WINDOW_SECONDS = 15 * 60
COOLDOWN_SECONDS = 30
MAX_READS = 2
SOURCE = "Live Browser Use CLI success page (delayed read-only observation)"
RECEIVED = re.compile(
    r"your application (?:was successfully submitted|has been submitted successfully|has been received)"
    r"|we(?: have|'ve)? received your application", re.I)
REJECTED = re.compile(r"could(?:n['’]t| not) submit|unable to submit|failed to submit|"
                      r"application (?:was not|has not been) (?:submitted|received)|flagged as (?:possible )?spam", re.I)
VERIFICATION = re.compile(r"verification code|verify your email(?: address)?|verify (?:you are|that you are) human|"
                          r"complete (?:the |this )?verification", re.I)
SCHEMA = """CREATE TABLE IF NOT EXISTS late_receipt_observations (
 attempt_key TEXT PRIMARY KEY, job_hash TEXT NOT NULL, checked_at REAL NOT NULL,
 observations INTEGER NOT NULL, state TEXT NOT NULL
);"""


def browser_paused():
    return any(path.exists() or path.is_symlink() for path in (
        config.ROOT / "private" / "pipeline-pause.json",
        config.ROOT / "private" / "overnight-monitor" / "repair-pending.json"))


def _may_read(job_hash):
    if browser_paused():
        raise ValueError("Late receipt browser observation is paused")
    from .application_discard import check
    check(config.ROOT, job_hash)


def _evidence(path, *, now=None):
    """Read the original click and its evidence, without granting fresh authority."""
    now = time.time() if now is None else now
    path, attempt, digest = overnight._read_private(path)
    identity = boards.job_identity(attempt.get("application_url"))
    job_hash = attempt.get("job_hash")
    if (not identity or identity[0] not in {"greenhouse", "ashby", "lever", "workable"}
            or boards.application_hash(attempt["application_url"]) != job_hash
            or path != config.ROOT / "private" / "authorized-submissions" / job_hash / "attempt.json"
            or attempt.get("runtime_click_started") is not True
            or attempt.get("state") == "submitted"):
        raise ValueError("Late receipt requires the original exact clicked attempt")
    clicked_at = overnight._timestamp(attempt.get("click_started_at", ""))
    if not clicked_at <= now <= clicked_at + WINDOW_SECONDS:
        raise ValueError("Late receipt observation window ended")
    _, checks, checks_digest = overnight._read_private(path.parent / "checks.json")
    snapshots, documents = checks.get("checks"), checks.get("documents")
    target = checks.get("target_id")
    if (checks.get("check_count") != 2 or not isinstance(snapshots, list) or len(snapshots) != 2
            or snapshots[0] != snapshots[1] or not isinstance(snapshots[0], dict)
            or not snapshots[0].get("fields") or not snapshots[0].get("retained")
            or checks.get("authorization_id") != attempt.get("authorization_id")
            or checks.get("job_hash") != job_hash or not isinstance(target, str) or not target
            or not isinstance(documents, dict) or "documents.resume" not in documents):
        raise ValueError("Late receipt needs original target and two retained audits")
    hashes = {key: item.get("sha256") for key, item in documents.items() if isinstance(item, dict)}
    if len(hashes) != len(documents) or not all(isinstance(v, str) and re.fullmatch(r"[a-f0-9]{64}", v) for v in hashes.values()):
        raise ValueError("Late receipt document hashes are unavailable")
    review_digest = None
    if attempt.get("require_independent_review") is True:
        _, _, review_digest = overnight._read_private(path.parent / "independent-review.json")
    row = {**attempt, "attempt_path": str(path)}
    proof = {"check_count": 2, "authorization_id": attempt["authorization_id"], "job_hash": job_hash,
             "url": attempt["application_url"], "document_sha256": hashes,
             "resume_sha256": hashes["documents.resume"], "independent_review_sha256": review_digest}
    if not overnight._checked_receipt(row, proof):
        raise ValueError("Original application review evidence is inconsistent")
    return {"attempt": attempt, "attempt_path": str(path), "attempt_sha256": digest,
            "checks_sha256": checks_digest, "target_id": target, "proof": proof,
            "attempt_key": hashlib.sha256(json.dumps([job_hash, attempt["authorization_id"],
                attempt["click_started_at"], attempt.get("packet_sha256")]).encode()).hexdigest()}


def confirmation_url(original, observed):
    """Greenhouse's confirmation route is deliberately not a source job URL."""
    identity = boards.job_identity(original)
    if not identity or not isinstance(observed, str):
        return False
    source, current = urlsplit(original), urlsplit(observed)
    if (source.scheme != "https" or current.scheme != "https" or current.username or current.password
            or current.netloc != source.netloc or source.username or source.password):
        return False
    if identity[0] == "greenhouse":
        return current.path.rstrip("/") == source.path.rstrip("/") + "/confirmation"
    return identity[0] in {"ashby", "lever", "workable"} and boards.job_identity(observed) == identity


PAGE_STATE = """(()=>{const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
const controls=[...document.querySelectorAll('input:not([type=hidden]),textarea,select,[role=combobox]')]
 .filter(e=>visible(e)&&!e.matches('input[type=search]'));
const forms=[...document.querySelectorAll('form,.ashby-application-form-container,#application_form,#application-form')]
 .filter(e=>visible(e)&&e.querySelector('input:not([type=hidden]):not([type=search]),textarea,select,[role=combobox],button[type=submit]'));
const terminals=[...document.querySelectorAll('button,input[type=submit],[role=button]')].filter(visible)
 .filter(e=>e.matches('input[type=submit],button[type=submit]')||/^(submit(?: application)?|apply(?: now)?|send application|finish application)$/i.test((e.innerText||e.value||e.getAttribute('aria-label')||'').trim()));
const challenges=[...document.querySelectorAll('iframe')].filter(e=>visible(e)&&e.getBoundingClientRect().height>90&&/recaptcha|hcaptcha|challenge/i.test(e.src));
return {url:location.href,title:document.title,body:document.body?.innerText||'',
 active_controls:controls.length,application_forms:forms.length,terminal_controls:terminals.length,verification_challenges:challenges.length};})()"""


def positive(original, observation):
    return (isinstance(observation, dict) and confirmation_url(original, observation.get("url"))
            and isinstance(observation.get("body"), str) and len(observation["body"]) <= 200_000
            and RECEIVED.search(observation["body"]) is not None
            and REJECTED.search(observation["body"]) is None
            and VERIFICATION.search(observation["body"]) is None
            and all(type(observation.get(key)) is int and observation[key] == 0
                    for key in ("active_controls", "application_forms", "terminal_controls", "verification_challenges")))


def valid_persisted_observation(row, proof):
    """A restart must retain the actual screenshot and exact captured page proof."""
    try:
        attempt_path, attempt, _ = overnight._read_private(row["attempt_path"])
        directory = attempt_path.parent
        observed_path, observed, _ = overnight._read_private(proof.get("observation_path", ""))
        _, checks, checks_digest = overnight._read_private(directory / "checks.json")
        screenshot = Path(proof.get("screenshot_path", ""))
        if (observed_path != directory / "late-confirmation.json" or screenshot != directory / "late-confirmation.png"
                or screenshot.is_symlink() or not screenshot.is_file() or screenshot.stat().st_mode & 0o077
                or not 8 < screenshot.stat().st_size <= 20_000_000):
            return False
        png = screenshot.read_bytes()
        clicked_at = overnight._timestamp(attempt.get("click_started_at", ""))
        received_at = overnight._timestamp(proof.get("confirmed_at", ""))
        return (png.startswith(b"\x89PNG\r\n\x1a\n")
            and hashlib.sha256(png).hexdigest() == proof.get("screenshot_sha256") == observed.get("screenshot_sha256")
            and observed.get("checks_sha256") == checks_digest
            and observed.get("target_id") == proof.get("target_id") == checks.get("target_id")
            and observed.get("observed_at") == proof.get("confirmed_at")
            and observed.get("url") == proof.get("observed_url") and observed.get("body") == proof.get("body")
            and clicked_at <= received_at <= clicked_at+WINDOW_SECONDS
            and positive(row["application_url"], observed))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def observe(request, helpers):
    """Fixed official-CLI read: attach exact extant target, read, capture, reread."""
    evidence = _evidence(request["attempt_path"])
    _may_read(evidence["attempt"]["job_hash"])
    if any(request.get(key) != evidence[key] for key in ("attempt_sha256", "checks_sha256", "target_id")):
        raise ValueError("Late receipt click evidence changed")
    path = Path(evidence["attempt_path"])
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(fd, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "attempt_active"}
        # Terminal runtime owns the same lock through its click/initial receipt.
        if _evidence(path)["attempt_sha256"] != evidence["attempt_sha256"]:
            raise ValueError("Late receipt attempt changed during observation")
        target = evidence["target_id"]
        _may_read(evidence["attempt"]["job_hash"])
        tabs = [tab for tab in helpers["list_tabs"]() if tab.get("targetId") == target]
        if len(tabs) != 1:
            return {"state": "target_absent"}
        original = evidence["attempt"]["application_url"]
        if not confirmation_url(original, tabs[0].get("url")):
            return {"state": "pending" if boards.job_identity(tabs[0].get("url")) == boards.job_identity(original) else "target_changed"}
        _may_read(evidence["attempt"]["job_hash"])
        helpers["switch_tab"](target)  # Attach only; never navigate or activate.
        def read():
            _may_read(evidence["attempt"]["job_hash"])
            current = helpers["current_tab"]()
            if current.get("targetId") != target or not confirmation_url(original, current.get("url")):
                raise ValueError("Late receipt target changed")
            return helpers["js"](PAGE_STATE)
        before = read()
        if not positive(original, before):
            return {"state": "pending"}
        _may_read(evidence["attempt"]["job_hash"])
        screenshot = helpers["cdp"]("Page.captureScreenshot", format="png", _response_timeout=15)["data"]
        after = read()
        if after != before or not positive(original, after):
            return {"state": "observation_changed"}
        # Receipt timestamp is when success was observed, not the attempted click.
        return {"state": "observed", "target_id": target, "observation": after,
                "observed_at": datetime.now(timezone.utc).isoformat(), "screenshot_base64": screenshot}
    finally:
        os.close(fd)


async def reconcile(conn, *, observer=None, recorder=None, now=None):
    """At most two due reads/cycle; durable cooldown also covers transport failure."""
    summary = {"observed": 0, "receipts": 0, "reconciled": 0}
    if os.environ.get("CI", "").lower() in {"1", "true", "yes"}:
        return summary
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='authorized_submission_attempts'").fetchone():
        return summary
    moment = time.time() if now is None else now
    conn.execute(SCHEMA)
    # This lock serializes service/pipeline observations, not browser authority.
    from .service import _worker_lock
    with _worker_lock("late-receipts.lock") as owned:
        if not owned:
            return summary
        from .tracking import record_confirmed
        recorder = recorder or record_confirmed
        summary["reconciled"] = overnight._reconcile_receipts(conn, recorder)
        if browser_paused():
            return summary  # Persisted receipts record; new browser work waits.
        rows = conn.execute("SELECT * FROM authorized_submission_attempts WHERE state<>'submitted' "
                            "AND started_at>=? ORDER BY started_at DESC LIMIT 100", (moment-WINDOW_SECONDS-600,)).fetchall()
        for row in rows:
            if summary["observed"] >= MAX_READS:
                break
            observation_key = None
            try:
                read_started = time.time() if now is None else now
                evidence = _evidence(row["attempt_path"], now=read_started)
                attempt = evidence["attempt"]
                _may_read(attempt["job_hash"])
                if (any(row[key] != attempt.get(key) for key in ("job_hash", "authorization_id", "application_url"))
                        or row["packet_sha256"] != attempt.get("packet_sha256")):
                    continue
                previous = conn.execute("SELECT checked_at FROM late_receipt_observations WHERE attempt_key=?",
                                        (evidence["attempt_key"],)).fetchone()
                if previous and read_started-previous["checked_at"] < COOLDOWN_SECONDS:
                    continue
                with conn:
                    conn.execute("INSERT INTO late_receipt_observations VALUES(?,?,?,1,'observing') "
                                 "ON CONFLICT(attempt_key) DO UPDATE SET checked_at=excluded.checked_at,"
                                 "observations=observations+1,state='observing'",
                                 (evidence["attempt_key"], row["job_hash"], read_started))
                summary["observed"] += 1
                observation_key = evidence["attempt_key"]
                payload = {key: evidence[key] for key in ("attempt_path", "attempt_sha256", "checks_sha256", "target_id")}
                if observer is None:
                    from .authorized_submission import AuthorizedSubmissionCLI
                    client = AuthorizedSubmissionCLI(timeout=25)
                    client.expected_url = attempt["application_url"]
                    result = await client.invoke("observe_receipt", **payload)
                else:
                    result = await observer(**payload)
                state = result.get("state")
                if state not in {"observed", "pending", "target_absent", "target_changed", "attempt_active", "observation_changed"}:
                    state = "observation_failed"
                with conn:
                    conn.execute("UPDATE late_receipt_observations SET state=? WHERE attempt_key=?", (state, evidence["attempt_key"]))
                if state != "observed":
                    continue
                current = _evidence(row["attempt_path"], now=read_started)
                observed_at = overnight._timestamp(result.get("observed_at", ""))
                observation = result.get("observation")
                if (current != evidence or result.get("target_id") != evidence["target_id"]
                        or not read_started-1 <= observed_at <= read_started+30
                        or observed_at > overnight._timestamp(attempt["click_started_at"])+WINDOW_SECONDS
                        or not positive(attempt["application_url"], observation)):
                    continue
                png = base64.b64decode(result.get("screenshot_base64", ""), validate=True)
                if not png.startswith(b"\x89PNG\r\n\x1a\n") or not 8 < len(png) <= 20_000_000:
                    continue
                directory = Path(row["attempt_path"]).parent
                screenshot = directory / "late-confirmation.png"
                fd = os.open(screenshot, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "wb") as handle:
                    os.fchmod(handle.fileno(), 0o600); handle.write(png)
                booklet.write_private(directory / "late-confirmation.json", {**observation, "target_id": evidence["target_id"],
                    "observed_at": result["observed_at"], "attempt_sha256": evidence["attempt_sha256"],
                    "checks_sha256": evidence["checks_sha256"], "screenshot_sha256": hashlib.sha256(png).hexdigest()})
                proof = {**evidence["proof"], "state": "submitted", "confirmed": True,
                    "application_url": attempt["application_url"], "observed_url": observation["url"],
                    "target_id": evidence["target_id"], "confirmed_at": result["observed_at"],
                    "confirmation": RECEIVED.search(observation["body"])[0], "body": observation["body"],
                    "source": SOURCE,
                    "board": boards.job_identity(attempt["application_url"])[0], "double_check_count": 2,
                    "checks_path": str(directory / "checks.json"), "screenshot_path": str(screenshot),
                    "screenshot_sha256": hashlib.sha256(png).hexdigest(), "observation_path": str(directory / "late-confirmation.json")}
                if not overnight._checked_receipt(row, proof):
                    continue
                receipt = directory / "receipt.json"
                if receipt.exists():
                    continue  # Never replace another controller's receipt.
                booklet.write_private(receipt, proof)
                summary["receipts"] += 1
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                if observation_key is not None:
                    with conn:
                        conn.execute("UPDATE late_receipt_observations SET state='observation_failed' WHERE attempt_key=?",
                                     (observation_key,))
                continue  # Missing evidence or a read error leaves the original uncertainty.
        summary["reconciled"] += overnight._reconcile_receipts(conn, recorder)
    return summary
