"""Candidate-requested, exact-application cancellation with preserved evidence.

The marker is the durable cancellation authority. Browser operations stop at
their next boundary; the daemon and other applications are never terminated.
Only a target captured for this exact job can be closed, under the browser lane.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from .. import config
from . import boards, booklet

HASH = re.compile(r"[a-f0-9]{64}")


class ApplicationDiscarded(RuntimeError):
    pass


class ApplicationActionBusy(ValueError):
    pass


def _path(root, folder, job_hash):
    if not isinstance(job_hash, str) or not HASH.fullmatch(job_hash):
        raise ValueError("Invalid application identity")
    path = Path(root) / "private" / folder / (job_hash + ".json")
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Application cancellation evidence must remain private")
    return path


def _read(path):
    if not path.exists():
        return None
    if path.stat().st_size > 2_000_000:
        raise ValueError("Application cancellation evidence is invalid")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Application cancellation evidence is invalid")
    return value


def discarded(root, job_hash):
    return _path(root, "application-discards", job_hash).exists()


def check(root, job_hash):
    if job_hash and discarded(root, job_hash):
        raise ApplicationDiscarded("Candidate discarded this application")


@contextmanager
def action_lock(root, job_hash, *, blocking=True):
    """Linearize the irreversible native press against a candidate discard."""
    path = _path(root, "application-action-locks", job_hash).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(fd, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise ApplicationActionBusy("A terminal application action is in progress; wait for its outcome") from None
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _protect_terminal(conn, root, job_hash, row):
    if row["state"] in {"submitted", "submission_uncertain"}:
        raise ValueError("Submitted or uncertain applications cannot be discarded")
    if _table(conn, "confirmed_submissions") and any(boards.application_hash(record[0]) == job_hash
            for record in conn.execute("SELECT application_url FROM confirmed_submissions")):
        raise ValueError("A confirmed application cannot be discarded")
    if _table(conn, "authorized_submission_attempts"):
        attempt_row = conn.execute("SELECT * FROM authorized_submission_attempts WHERE job_hash=?", (job_hash,)).fetchone()
        if attempt_row:
            path = _path(root, "authorized-submissions", job_hash).parent / job_hash / "attempt.json"
            if (Path(attempt_row["attempt_path"]) != path or path.is_symlink()
                    or any(parent.is_symlink() for parent in path.parents)):
                raise ValueError("Terminal attempt evidence must be reviewed before discarding")
            attempt = _read(path)
            if (not attempt or attempt.get("job_hash") != job_hash
                    or attempt.get("authorization_id") != attempt_row["authorization_id"]
                    or attempt.get("runtime_click_started") is not False
                    or attempt_row["state"] in {"submitted", "uncertain"}):
                raise ValueError("The terminal attempt may have started; discarding cannot undo it")


def _capture_target(root, row, job):
    """Prefer this active worker's captured target, then its preserved packet."""
    state = _read(_path(root, "application-workers", row["job_hash"])) or {}
    candidates = [(state.get("target_id"), state.get("url"))] if state.get("state") == "active" else []
    packet_path = Path(row["packet"]) if row["packet"] else None
    if packet_path:
        if not packet_path.is_absolute():
            packet_path = Path(root) / packet_path
        packet_path = packet_path.with_name("packet.json")
        if (not packet_path.is_symlink() and not any(p.is_symlink() for p in packet_path.parents)
                and packet_path.resolve().is_relative_to((Path(root) / "private").resolve()) and packet_path.is_file()):
            packet = _read(packet_path) or {}
            if packet.get("job", {}).get("dedupe_hash") == row["job_hash"]:
                candidates.append((packet.get("capture", {}).get("target_id"), packet.get("job", {}).get("url")))
    if state.get("state") != "active":
        candidates.append((state.get("target_id"), state.get("url")))
    for target, url in candidates:
        if (isinstance(target, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target)
                and boards.job_identity(url) == boards.job_identity(job["url"])):
            return target
    return None


def request(conn, job_hash, *, root=None, book_path=None):
    """Persist a portal click. This function itself never touches the browser."""
    root = Path(root or config.ROOT)
    path = _path(root, "application-discards", job_hash)
    with action_lock(root, job_hash, blocking=False):
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM applications WHERE job_hash=?", (job_hash,)).fetchone()
            if row is None:
                raise ValueError("Unknown application")
            job = json.loads(row["job_json"])
            if boards.application_hash(job.get("url")) != job_hash:
                raise ValueError("Application identity changed")
            _protect_terminal(conn, root, job_hash, row)
            record = _read(path)
            if record is None:
                worker = _read(_path(root, "application-workers", job_hash)) or {}
                target = _capture_target(root, row, job)
                record = {"schema_version": 1, "job_hash": job_hash, "url": job["url"], "state": "discarded",
                    "source": "candidate_portal_discard", "requested_at": time.time(), "previous_state": row["state"],
                    "packet_path": row["packet"], "target_id": target,
                    "tab_close": {"state": "pending" if target or worker.get("state") == "active" else "no_captured_tab"},
                    "worker_stop": {"state": "stopping" if worker.get("state") == "active" else "not_running"}}
                if row["packet"]:
                    directory = Path(row["packet"]).parent
                    if (directory.is_absolute() and directory.resolve().is_relative_to((root / "private").resolve())
                            and not any(p.is_symlink() for p in [directory, *directory.parents])):
                        archive = path.parent / job_hash / "evidence-at-discard"
                        if archive.is_symlink() or any(p.is_symlink() for p in archive.parents):
                            raise ValueError("Discard evidence archive must remain private")
                        archive.mkdir(parents=True, exist_ok=True, mode=0o700)
                        archived = {}
                        for name in ("packet.json", "browser.png", "events.json", "review.html"):
                            original = directory / name
                            if original.is_file() and not original.is_symlink() and original.stat().st_size <= 16_000_000:
                                content = original.read_bytes()
                                destination = archive / name
                                if destination.exists():
                                    if destination.is_symlink() or destination.read_bytes() != content:
                                        raise ValueError("An earlier evidence archive needs reconciliation")
                                else:
                                    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                                    with os.fdopen(fd, "wb") as stream:
                                        stream.write(content)
                                archived[name] = hashlib.sha256(content).hexdigest()
                        record["preserved_evidence"] = archived
                booklet.write_private(path, record)
            # Do not rewrite packets, screenshots, answers, attempts or receipts.
            conn.execute("UPDATE applications SET state='discarded',lease_until=NULL,available_at=0,error_kind=NULL,"
                         "updated_at=?,notified_at=NULL WHERE job_hash=?", (int(time.time()), job_hash))
            if _table(conn, "application_approvals"):
                conn.execute("UPDATE application_approvals SET state='revoked' WHERE job_hash=? AND state IN ('approved','submitting')", (job_hash,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if book_path is not None:
            # Caller serializes booklet writes with questions._locked.
            book = booklet.load(book_path)
            book.setdefault("job_exclusions", {})[job_hash] = {"status": "verified", "source": "Candidate clicked Discard in the local portal",
                "url": job["url"], "recorded_at": record["requested_at"], "discard_path": str(path)}
            from .questions import _now
            for question in book.get("question_handoffs", {}).values():
                context = question.get("contexts", {}).get(job_hash)
                if context and not context.get("resolved"):
                    context.update(resolved=True, resolved_at=_now(), resolution="Candidate discarded this exact application")
                    question.setdefault("history", []).append({"event": "application_discarded", "job_hash": job_hash, "at": _now()})
                    if question.get("status") == "pending" and all(item.get("resolved") for item in question["contexts"].values()):
                        question["status"] = "resolved"
            booklet.write_private(Path(book_path), book)
    return public(record)


def public(record):
    return {key: record[key] for key in ("job_hash", "state", "tab_close", "worker_stop")}


def status(root, job_hash):
    record = _read(_path(root, "application-discards", job_hash))
    return public(record) if record else None


def worker_started(root, job):
    job_hash = job["dedupe_hash"]
    with action_lock(root, job_hash):
        check(root, job_hash)
        token = uuid.uuid4().hex
        booklet.write_private(_path(root, "application-workers", job_hash), {"job_hash": job_hash, "url": job["url"],
            "state": "active", "worker_token": token, "pid": os.getpid(), "started_at": time.time()})
        return token


def remember_target(root, job_hash, target, url):
    if not job_hash or not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target):
        return
    if boards.application_hash(url) != job_hash:
        return
    with action_lock(root, job_hash):
        path = _path(root, "application-workers", job_hash)
        worker = _read(path)
        if worker and worker.get("state") == "active":
            booklet.write_private(path, {**worker, "target_id": target, "url": url})
            discard_path = _path(root, "application-discards", job_hash)
            record = _read(discard_path)
            # The in-flight open may finish after the click. Record only that
            # worker's returned exact target; never search or adopt arbitrary tabs.
            if record and not record.get("target_id"):
                booklet.write_private(discard_path, {**record, "target_id": target, "tab_close": {"state": "pending"}})


def worker_stopped(root, job_hash, token):
    with action_lock(root, job_hash):
        path = _path(root, "application-workers", job_hash)
        worker = _read(path)
        if not worker or worker.get("worker_token") != token:
            return
        if worker:
            booklet.write_private(path, {**worker, "state": "stopped", "stopped_at": time.time()})
        discard_path = _path(root, "application-discards", job_hash)
        record = _read(discard_path)
        if record:
            if not record.get("target_id") and record["tab_close"]["state"] == "pending":
                record["tab_close"] = {"state": "no_captured_tab"}
            booklet.write_private(discard_path, {**record, "worker_stop": {"state": "stopped"}})


def close_dispatch(request, helpers, *, root, owner):
    """Invoked only by the official CLI after it acquired the browser lane."""
    job_hash = request.get("job_hash")
    with action_lock(root, job_hash, blocking=False):
        path = _path(root, "application-discards", job_hash)
        record = _read(path)
        if not record or record.get("source") != "candidate_portal_discard" or record.get("job_hash") != job_hash:
            raise ValueError("No candidate discard authorizes this close")
        target, url = record.get("target_id"), record.get("url")
        if (not target or target != request.get("target_id") or boards.application_hash(url) != job_hash
                or boards.job_identity(request.get("expected_url")) != boards.job_identity(url)):
            raise ValueError("Discard does not bind this captured tab")
        if record["tab_close"]["state"] not in {"pending", "deferred"}:
            return public(record)
        tabs = {tab["targetId"]: tab for tab in helpers["list_tabs"]()}
        if target not in tabs:
            outcome = {"state": "already_closed"}
        elif boards.job_identity(tabs[target].get("url")) != boards.job_identity(url):
            outcome = {"state": "preserved", "reason": "Captured tab now belongs to a different page"}
        else:
            # Write ahead: a transport failure cannot blindly repeat this close.
            record["tab_close"] = {"state": "close_unconfirmed"}
            booklet.write_private(path, record)
            if target in owner.tabs:
                closed = owner.close(target, "candidate_discarded_exact_application", expected_url=tabs[target]["url"])
            else:
                # Explicit candidate authority permits the captured draft even
                # if it predated ownership bookkeeping. Other tabs remain intact.
                helpers["close_tab"](target)
                closed = target not in {tab["targetId"] for tab in helpers["list_tabs"]()}
            outcome = {"state": "closed" if closed else "close_unconfirmed"}
        record["tab_close"] = outcome
        booklet.write_private(path, record)
        return public(record)


def finalize(root, job_hash, *, timeout=8, client=None):
    """Bounded best effort; disconnected/busy browsers retain a durable retry."""
    path = _path(root, "application-discards", job_hash)
    record = _read(path)
    if not record:
        raise ValueError("No discarded application")
    if record["tab_close"]["state"] not in {"pending", "deferred"}:
        return public(record)
    if not record.get("target_id"):
        return public(record)
    from . import cli_browser
    if client is None and Path(root).resolve() != cli_browser.ROOT.resolve():
        # An injected dashboard/test root never gets the real browser runtime.
        return {**public(record), "tab_close": {"state": "deferred", "reason": "Browser runtime belongs to a different workspace"}}
    client = client or cli_browser.BrowserUseCLI(timeout=timeout)
    client.target_id, client.expected_url = record["target_id"], record["url"]
    try:
        return client.call("discard_application_tab", job_hash=job_hash)
    except (OSError, ValueError, RuntimeError, TimeoutError):
        with action_lock(root, job_hash):
            current = _read(path)
            if current["tab_close"]["state"] in {"pending", "deferred"}:
                current["tab_close"] = {"state": "deferred", "reason": "Browser busy or disconnected; close will retry safely"}
                booklet.write_private(path, current)
            return public(current)


def reconcile_pending(root=None, *, limit=2):
    root = Path(root or config.ROOT)
    directory = root / "private" / "application-discards"
    if not directory.exists():
        return
    pending = []
    for path in directory.glob("*.json"):
        if HASH.fullmatch(path.stem):
            record = _read(_path(root, "application-discards", path.stem))
            if record.get("tab_close", {}).get("state") in {"pending", "deferred"} and record.get("target_id"):
                pending.append(path.stem)
    for job_hash in sorted(pending)[:limit]:
        finalize(root, job_hash, timeout=3)
