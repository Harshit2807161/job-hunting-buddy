import asyncio
import fcntl
import hashlib
import json
import time

import pytest

from jhb import config, store
from jhb.applications import approvals, boards, booklet, overnight, queue


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setenv("JHB_PORTAL_SUBMISSIONS_ENABLED", "1")
    monkeypatch.setenv("JHB_REQUIRE_PORTAL_APPROVAL", "1")
    conn = store.connect(tmp_path / "synthetic.sqlite3")
    url = "https://job-boards.greenhouse.io/example/jobs/123"
    key = boards.application_hash(url)
    job = {"url": url, "dedupe_hash": key, "company": "Synthetic", "title": "Software Engineer"}
    queue.enqueue(conn, [job]); queue.claim(conn)
    packet_path = tmp_path / "private" / "applications" / key / "packet.json"
    packet = {"job": job, "review_inventory": {"fields": [{"ref": "optional", "status": "blank"}]}}
    booklet.write_private(packet_path, packet)
    queue.finish(conn, key, "waiting_review", packet_path.with_name("review.html"))
    binding = {"packet_path": str(packet_path), "selected_role": "sde"}
    monkeypatch.setattr(approvals, "_draft", lambda *args: (packet, binding, "synthetic-revision"))
    monkeypatch.setattr(approvals, "validate_binding", lambda *args: True)
    yield conn, key, tmp_path / "private" / "book.json"
    conn.close()


def approve(context):
    conn, key, book = context
    return approvals.approve(conn, key, "synthetic-revision", ["optional"], book)


def attempt(context, *, clicked=False, malformed=False):
    conn, key, book = context
    overnight.initialize(conn)
    row = conn.execute("SELECT * FROM application_approvals ORDER BY rowid DESC LIMIT 1").fetchone()
    _, _, digest = overnight._read_private(row["authorization_path"])
    path = config.ROOT / "private" / "authorized-submissions" / key / "attempt.json"
    data = {"job_hash": key, "authorization_id": digest, "runtime_click_started": clicked,
            "authorization_path": row["authorization_path"]}
    if malformed: data.pop("runtime_click_started")
    booklet.write_private(path, data)
    conn.execute("INSERT INTO authorized_submission_attempts(job_hash,authorization_id,application_url,state,started_at,updated_at,attempt_path) "
                 "VALUES(?,?,?,'in_progress',?,?,?)", (key, digest, "https://job-boards.greenhouse.io/example/jobs/123", int(time.time()), int(time.time()), str(path)))
    conn.commit()
    return path


def test_expired_approval_allows_fresh_human_review_and_preserves_old_authority_bytes(context):
    conn, key, book = context
    first = approve(context)
    row = conn.execute("SELECT * FROM application_approvals").fetchone()
    path = __import__("pathlib").Path(row["authorization_path"])
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    conn.execute("UPDATE application_approvals SET expires_at=0"); conn.commit()
    review = approvals.review(conn, key, book)
    assert review["can_approve"] is True and review["approval"]["state"] == "expired"
    assert review["selected_role"] == "sde"
    second = approve(context)
    assert first["approval_id"] != second["approval_id"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("marker,state", [(False, "needs_review"), (True, "uncertain"), (None, "uncertain")])
def test_interrupted_dispatch_requires_persisted_no_click_for_new_review(context, monkeypatch, marker, state):
    conn, key, book = context
    approve(context)
    path = attempt(context, clicked=marker is True, malformed=marker is None)
    async def fail(*args, **kwargs):
        raise RuntimeError("Synthetic private diagnostics must not reach portal")
    monkeypatch.setattr(overnight, "drain", fail)
    result = asyncio.run(approvals.drain(conn, book))
    row = conn.execute("SELECT state,result_json FROM application_approvals").fetchone()
    assert row["state"] == state
    assert "private diagnostics" not in row["result_json"]
    assert result["uncertain"] == int(state == "uncertain")
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == ("waiting_review" if marker is False else "submission_uncertain")
    assert json.loads(path.read_text()).get("runtime_click_started") is marker
    # Permission is never restored automatically after any interruption.
    assert asyncio.run(approvals.drain(conn, book))["attempted"] == 0


def test_concurrent_pre_click_revocation_is_not_overwritten_by_handoff(context, monkeypatch):
    conn, key, book = context
    approve(context)
    async def revoked(*args, **kwargs):
        approvals.revoke(conn, key)
        return {"attempted": 0, "submitted": 0, "uncertain": 0, "handoffs": 1}
    monkeypatch.setattr(overnight, "drain", revoked)
    asyncio.run(approvals.drain(conn, book))
    assert conn.execute("SELECT state FROM application_approvals").fetchone()[0] == "revoked"


def test_revoked_no_click_attempt_remains_revoked_when_dispatch_is_interrupted(context, monkeypatch):
    conn, key, book = context
    approve(context); attempt(context, clicked=False)
    async def revoked_then_failed(*args, **kwargs):
        approvals.revoke(conn, key)
        raise RuntimeError("Synthetic pre-click cancellation")
    monkeypatch.setattr(overnight, "drain", revoked_then_failed)
    result = asyncio.run(approvals.drain(conn, book))
    assert result["uncertain"] == 0 and result["handoffs"] == 1
    assert conn.execute("SELECT state FROM application_approvals").fetchone()[0] == "revoked"
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "waiting_review"


def test_cancelled_post_click_dispatch_records_uncertainty_before_propagating(context, monkeypatch):
    conn, key, book = context
    approve(context); attempt(context, clicked=True)
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(overnight, "drain", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(approvals.drain(conn, book))
    assert conn.execute("SELECT state FROM application_approvals").fetchone()[0] == "uncertain"
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "submission_uncertain"


def test_post_click_attempt_refuses_authority_mutation(context):
    conn, key, book = context
    approve(context); attempt(context, clicked=True)
    row = conn.execute("SELECT * FROM application_approvals").fetchone()
    from pathlib import Path
    path = Path(row["authorization_path"])
    before = path.read_bytes()
    with pytest.raises(ValueError, match="terminal attempt"):
        approvals.revoke(conn, key)
    assert path.read_bytes() == before
    assert conn.execute("SELECT state FROM application_approvals").fetchone()[0] == "approved"


def test_active_browser_lane_refuses_revocation_without_blocking_or_rewriting_authority(context):
    conn, key, book = context
    approve(context)
    row = conn.execute("SELECT * FROM application_approvals").fetchone()
    from pathlib import Path
    before = Path(row["authorization_path"]).read_bytes()
    lane = config.ROOT / "private" / "browser-lane.lock"
    lane.touch()
    with lane.open("r+") as active:
        fcntl.flock(active, fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(ValueError, match="browser operation is active"):
            approvals.revoke(conn, key)
        assert time.monotonic()-started < .5
    assert Path(row["authorization_path"]).read_bytes() == before


def test_concurrent_revocation_during_binding_validation_remains_revoked(context, monkeypatch):
    conn, key, book = context
    approve(context)
    def revoked_before_binding(*args, **kwargs):
        approvals.revoke(conn, key)
        raise ValueError("Synthetic approval was revoked")
    monkeypatch.setattr(approvals, "validate_binding", revoked_before_binding)
    assert asyncio.run(approvals.drain(conn, book))["attempted"] == 0
    assert conn.execute("SELECT state FROM application_approvals").fetchone()[0] == "revoked"
