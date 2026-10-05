"""Exact application cancellation, synthetic DB and official-CLI helper fakes."""
import asyncio
import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from jhb import config
from jhb.applications import application_discard as discard, boards, booklet, queue
from jhb.applications import cli_browser, worker


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(cli_browser, "ROOT", tmp_path)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9222")
    conn = sqlite3.connect(tmp_path / "jobs.sqlite3"); conn.row_factory = sqlite3.Row
    queue.initialize(conn)
    book = tmp_path / "private" / "book.json"
    booklet.write_private(book, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}})
    url = "https://job-boards.greenhouse.io/synthetic/jobs/1001"
    job = {"dedupe_hash": boards.application_hash(url), "url": url, "company": "Synthetic", "title": "Engineer"}
    other_url = "https://job-boards.greenhouse.io/synthetic/jobs/1002"
    other = {**job, "dedupe_hash": boards.application_hash(other_url), "url": other_url}
    queue.enqueue(conn, [job, other])
    directory = tmp_path / "private" / "applications" / job["dedupe_hash"]
    booklet.write_private(directory / "packet.json", {"job": job, "state": "waiting_review", "capture": {"target_id": "target-owned"}})
    (directory / "browser.png").write_bytes(b"synthetic screenshot")
    conn.execute("UPDATE applications SET state='waiting_review',packet=? WHERE job_hash=?", (str(directory / "review.html"), job["dedupe_hash"]))
    conn.commit()
    yield SimpleNamespace(root=tmp_path, conn=conn, book=book, job=job, other=other, directory=directory, key=job["dedupe_hash"])
    conn.close()


def test_discard_preserves_evidence_and_excludes_stale_queue_results(setup):
    s = setup
    packet_before = (s.directory / "packet.json").read_bytes()
    result = discard.request(s.conn, s.key, root=s.root, book_path=s.book)
    assert result["state"] == "discarded"
    assert result["tab_close"]["state"] == "pending"
    assert result["worker_stop"]["state"] == "not_running"
    assert booklet.job_excluded(booklet.load(s.book), s.job)
    assert (s.directory / "packet.json").read_bytes() == packet_before
    archived = s.root / "private" / "application-discards" / s.key / "evidence-at-discard"
    assert (archived / "packet.json").read_bytes() == packet_before
    assert (archived / "browser.png").read_bytes() == b"synthetic screenshot"
    for state in ("failed", "waiting_review", "skipped", "submitted"):
        queue.finish(s.conn, s.key, state, "stale.html")
    queue.resume(s.conn, s.key)
    assert not queue.retry(s.conn, s.key, error_kind="old_failure")
    assert queue.enqueue(s.conn, [s.job]) == 0
    assert s.conn.execute("SELECT state,packet FROM applications WHERE job_hash=?", (s.key,)).fetchone()[0] == "discarded"
    assert queue.claim(s.conn)["job_hash"] == s.other["dedupe_hash"]
    # Even a legacy direct state rewrite cannot evade the durable marker.
    s.conn.execute("UPDATE applications SET state='queued' WHERE job_hash=?", (s.key,)); s.conn.commit()
    assert queue.claim(s.conn) is None
    assert discard.request(s.conn, s.key, root=s.root)["state"] == "discarded"


@pytest.mark.parametrize("state", ["submitted", "submission_uncertain"])
def test_terminal_outcomes_cannot_be_discarded(setup, state):
    s = setup
    s.conn.execute("UPDATE applications SET state=? WHERE job_hash=?", (state, s.key)); s.conn.commit()
    with pytest.raises(ValueError, match="cannot be discarded"):
        discard.request(s.conn, s.key, root=s.root)
    assert not discard.discarded(s.root, s.key)


@pytest.mark.parametrize("clicked", [True, None, False])
def test_durable_terminal_attempt_protection(setup, clicked):
    s = setup
    s.conn.execute("CREATE TABLE authorized_submission_attempts(job_hash TEXT,authorization_id TEXT,attempt_path TEXT,state TEXT)")
    path = s.root / "private" / "authorized-submissions" / s.key / "attempt.json"
    booklet.write_private(path, {"job_hash": s.key, "authorization_id": "synthetic", "runtime_click_started": clicked})
    s.conn.execute("INSERT INTO authorized_submission_attempts VALUES(?,?,?,?)", (s.key, "synthetic", str(path), "in_progress")); s.conn.commit()
    if clicked is False:
        assert discard.request(s.conn, s.key, root=s.root)["state"] == "discarded"
    else:
        with pytest.raises(ValueError, match="cannot undo"):
            discard.request(s.conn, s.key, root=s.root)
        assert not discard.discarded(s.root, s.key)


def test_final_native_press_lock_prevents_false_discard(setup):
    s = setup
    with discard.action_lock(s.root, s.key):
        with pytest.raises(discard.ApplicationActionBusy):
            discard.request(s.conn, s.key, root=s.root)
    assert not discard.discarded(s.root, s.key)


def test_pending_approval_is_revoked_without_rewriting_authority(setup):
    s = setup
    from jhb.applications import approvals
    approvals.initialize(s.conn)
    s.conn.execute("INSERT INTO application_approvals VALUES(?,?,?,?,?,?,?,?)", ("approval", s.key, "approved", 1, 99, "revision", "immutable", None)); s.conn.commit()
    discard.request(s.conn, s.key, root=s.root)
    assert s.conn.execute("SELECT state FROM application_approvals").fetchone()[0] == "revoked"


def helpers(s, *, url=None):
    tabs = [{"targetId": "target-owned", "url": url or s.job["url"]}, {"targetId": "unrelated", "url": s.other["url"]}]
    closes = []
    def close(target):
        closes.append(target)
        tabs[:] = [tab for tab in tabs if tab["targetId"] != target]
    return {"list_tabs": lambda: list(tabs), "close_tab": close}, closes


def test_close_uses_only_captured_target_and_is_idempotent(setup):
    s = setup
    discard.request(s.conn, s.key, root=s.root)
    h, closes = helpers(s)
    request = {"job_hash": s.key, "target_id": "target-owned", "expected_url": s.job["url"]}
    result = discard.close_dispatch(request, h, root=s.root, owner=SimpleNamespace(tabs={}))
    assert result["tab_close"]["state"] == "closed"
    discard.close_dispatch(request, h, root=s.root, owner=SimpleNamespace(tabs={}))
    assert closes == ["target-owned"]
    assert [tab["targetId"] for tab in h["list_tabs"]()] == ["unrelated"]


def test_changed_page_and_caller_supplied_wrong_tab_are_preserved(setup):
    s = setup
    discard.request(s.conn, s.key, root=s.root)
    h, closes = helpers(s, url=s.other["url"])
    with pytest.raises(ValueError, match="captured tab"):
        discard.close_dispatch({"job_hash": s.key, "target_id": "unrelated", "expected_url": s.job["url"]}, h, root=s.root, owner=SimpleNamespace(tabs={}))
    result = discard.close_dispatch({"job_hash": s.key, "target_id": "target-owned", "expected_url": s.job["url"]}, h, root=s.root, owner=SimpleNamespace(tabs={}))
    assert result["tab_close"]["state"] == "preserved"
    assert not closes


def test_active_worker_stops_after_inflight_action_without_killing_daemon(setup, monkeypatch):
    s = setup
    token = discard.worker_started(s.root, s.job)
    client = cli_browser.BrowserUseCLI(timeout=1)
    client.job_hash, client.target_id, client.expected_url = s.key, "target-owned", s.job["url"]
    calls = []
    def run(script, env, deadline, cancelled):
        calls.append("native_action_completed")
        result = discard.request(s.conn, s.key, root=s.root)
        assert result["worker_stop"]["state"] == "stopping"
        assert not cancelled.is_set()  # No process/daemon interruption.
        return subprocess.CompletedProcess([], 0, cli_browser.MARKER + json.dumps({"verified": True}), "")
    monkeypatch.setattr(client, "_run", run)
    with pytest.raises(discard.ApplicationDiscarded):
        client.call("fill", field={}, value="Synthetic")
    with pytest.raises(discard.ApplicationDiscarded):
        client.call("observe")
    assert calls == ["native_action_completed"]
    discard.worker_stopped(s.root, s.key, token)
    assert discard.status(s.root, s.key)["worker_stop"]["state"] == "stopped"


def test_inflight_open_records_exact_returned_target_after_discard(setup):
    s = setup
    s.conn.execute("UPDATE applications SET packet=NULL WHERE job_hash=?", (s.key,)); s.conn.commit()
    token = discard.worker_started(s.root, s.job)
    assert discard.request(s.conn, s.key, root=s.root)["tab_close"]["state"] == "pending"
    discard.remember_target(s.root, s.key, "returned-native-target", s.job["url"])
    saved = json.loads(discard._path(s.root, "application-discards", s.key).read_text())
    assert saved["target_id"] == "returned-native-target"
    discard.worker_stopped(s.root, s.key, token)


def test_bounded_busy_close_is_durable_and_can_retry(setup):
    s = setup
    discard.request(s.conn, s.key, root=s.root)
    class Busy:
        def call(self, *args, **kwargs):
            raise TimeoutError("browser busy")
    assert discard.finalize(s.root, s.key, client=Busy())["tab_close"]["state"] == "deferred"
    h, closes = helpers(s)
    class Connected:
        def call(self, operation, **kwargs):
            assert operation == "discard_application_tab"
            return discard.close_dispatch({**kwargs, "target_id": self.target_id, "expected_url": self.expected_url}, h,
                                          root=s.root, owner=SimpleNamespace(tabs={}))
    assert discard.finalize(s.root, s.key, client=Connected())["tab_close"]["state"] == "closed"
    assert closes == ["target-owned"]


def test_worker_wrapper_preserves_packet_after_discard(setup, monkeypatch):
    s = setup
    async def run(job, book, **kwargs):
        discard.request(s.conn, s.key, root=s.root)
        discard.check(s.root, s.key)
    monkeypatch.setattr(worker, "_run_job", run)
    monkeypatch.setattr(discard, "finalize", lambda *a, **k: {})
    previous = (s.directory / "packet.json").read_bytes()
    result, packet = asyncio.run(worker.run_job(s.job, booklet.load(s.book)))
    assert result["state"] == "discarded"
    assert packet == str(s.directory / "review.html")
    assert (s.directory / "packet.json").read_bytes() == previous
    assert discard.status(s.root, s.key)["worker_stop"]["state"] == "stopped"
