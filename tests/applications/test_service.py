import asyncio
from datetime import datetime, timezone
import fcntl
import json

import pytest

from jhb import config, store
from jhb.applications import approvals, booklet, service


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("CI", raising=False)
    for key in ["JHB_SOURCE_BATCH_SIZE", "JHB_APPLICATION_BATCH_SIZE", "JHB_PIPELINE_CONCURRENCY", "JHB_MAX_ACTIVE_DRAFTS"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("JHB_APPLICATIONS_ENABLED", "1")
    monkeypatch.setenv("JHB_REQUIRE_PORTAL_APPROVAL", "1")
    monkeypatch.setenv("JHB_PORTAL_SUBMISSIONS_ENABLED", "1")
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:12345")
    from jhb.applications import browser_connection
    monkeypatch.setattr(browser_connection, "available", lambda *a, **kw: True)
    database = tmp_path / "synthetic.sqlite3"
    book = tmp_path / "private" / "book.json"
    return lambda: store.connect(database), book


@pytest.mark.parametrize("mode", ["prepare", "approved"])
def test_ci_blocks_service_before_database_or_browser_or_private_writes(context, monkeypatch, mode):
    monkeypatch.setenv("CI", "true")
    def forbidden(*args, **kwargs): pytest.fail("CI worker touched private/runtime state")
    result = service.once(mode, connector=forbidden, prepare=forbidden, approved=forbidden)
    assert result == {"state": "disabled", "reason_code": "ci_disabled"}
    assert not (config.ROOT / "private").exists()


@pytest.mark.parametrize("mode", ["prepare", "approved"])
@pytest.mark.parametrize("path,reason,state", [
    ("pipeline-pause.json", "automation_paused", "paused"),
    ("overnight-monitor/repair-pending.json", "repair_quarantine", "blocked"),
])
def test_pause_and_repair_block_both_service_lanes_without_claims(context, mode, path, reason, state):
    booklet.write_private(config.ROOT / "private" / path, {"source": "Synthetic explicit stop"})
    def forbidden(*args, **kwargs): pytest.fail("Blocked worker opened database/browser")
    result = service.once(mode, connector=forbidden, prepare=forbidden, approved=forbidden)
    assert result == {"state": state, "reason_code": reason}
    if mode == "approved":
        status = json.loads((config.ROOT / "private" / "pipeline-submit-status.json").read_text())
        assert status["status"] == state and status["reason_codes"] == [reason]


@pytest.mark.parametrize("endpoint", ["https://remote.example:443", "http://secret@127.0.0.1:12345", "", "http://localhost:invalid"])
def test_service_never_routes_candidate_work_to_remote_or_invalid_browser(context, monkeypatch, endpoint):
    monkeypatch.setenv("BU_CDP_URL", endpoint)
    monkeypatch.delenv("BU_CDP_WS", raising=False)
    assert service.once("prepare", connector=lambda: pytest.fail("Remote browser gate failed"))["reason_code"] == "local_browser_unavailable"


def test_prepare_once_uses_existing_bounded_pipeline_with_env_overrides_and_sanitized_output(context, monkeypatch):
    connector, book = context
    calls = []
    def prepare(conn, path, **kwargs):
        calls.append(kwargs)
        assert path == book
        return {"applications_prepared": 2, "question_handoffs": 1, "private_candidate": "Synthetic private fact"}
    result = service.once("prepare", connector=connector, book_path=book, prepare=prepare)
    assert calls[0]["source_limit"] == 8 and calls[0]["application_limit"] == 3
    assert calls[0]["concurrency"] == 2 and calls[0]["max_active_drafts"] == 20
    assert calls[0]["planner_name"] == "codex"
    assert result == {"state": "completed", "applications_prepared": 2, "question_handoffs": 1}
    monkeypatch.setenv("JHB_SOURCE_BATCH_SIZE", "4")
    service.once("prepare", connector=connector, book_path=book, prepare=prepare)
    assert calls[1]["source_limit"] == 4


def test_approved_once_with_no_approval_does_not_invoke_any_submission(context):
    connector, book = context
    async def forbidden(*args, **kwargs): pytest.fail("No approval must never dispatch")
    result = service.once("approved", connector=connector, book_path=book, approved=forbidden)
    assert result == {"state": "idle", "reason_code": "no_approvals"}
    status = json.loads((config.ROOT / "private" / "pipeline-submit-status.json").read_text())
    assert status["active_jobs"] == [] and status["stage"] == "idle"


def seed(connector, state="approved"):
    conn = connector()
    approvals.initialize(conn)
    conn.execute("INSERT INTO application_approvals VALUES(?, ?, ?, 1, 9999999999, 'fixture-revision', 'private/fixture.json', NULL)",
                 ("a"*32, "b"*64, state)); conn.commit(); conn.close()


def test_approved_service_pulses_status_during_async_work_and_uses_only_portal_drain(context, monkeypatch):
    connector, book = context
    seed(connector)
    monkeypatch.setattr(service, "PULSE_SECONDS", .01)
    calls = []
    async def drain(conn, book_path, **kwargs):
        calls.append(kwargs)
        conn.execute("UPDATE application_approvals SET state='submitting'"); conn.commit()
        initial = json.loads((config.ROOT / "private" / "pipeline-submit-status.json").read_text())["updated_at"]
        await asyncio.sleep(.04)
        status = json.loads((config.ROOT / "private" / "pipeline-submit-status.json").read_text())
        assert status["active_jobs"] == ["b"*64] and status["status"] == "running"
        assert status["updated_at"] > initial
        return {"enabled": True, "attempted": 1, "submitted": 0, "uncertain": 0, "handoffs": 1, "private_fact": "Hidden"}
    result = service.once("approved", connector=connector, book_path=book, approved=drain)
    assert calls == [{"limit": 3}]
    assert "private_fact" not in result and result["handoffs"] == 1
    final = json.loads((config.ROOT / "private" / "pipeline-submit-status.json").read_text())
    assert final["processed"]["attempted"] == 1 and final["status"] == "completed"
    assert (config.ROOT / "private" / "pipeline-submit-status.json").stat().st_mode & 0o777 == 0o600


def test_external_pipeline_approval_dispatch_is_reported_active_without_double_claim(context):
    connector, book = context
    seed(connector, "submitting")
    with service._worker_lock("application-worker.lock") as pipeline_active:
        assert pipeline_active
        result = service.once("approved", connector=connector, book_path=book)
    assert result["state"] == "running" and result["reason_code"] == "external_approval_active"
    status = json.loads((config.ROOT / "private" / "pipeline-submit-status.json").read_text())
    assert status["active_jobs"] == ["b"*64]


def test_duplicate_service_does_not_overwrite_owned_active_status(context):
    connector, book = context
    with service._approved_lock() as first:
        assert first
        service.SubmissionStatus().update(stage="submission")
        path = config.ROOT / "private" / "pipeline-submit-status.json"
        before = path.read_bytes()
        assert service.once("approved", connector=connector, book_path=book)["reason_code"] == "approved_worker_active"
        assert path.read_bytes() == before


def test_watch_ends_at_current_window_expiry_and_does_not_renew_or_spawn_processes(context):
    connector, book = context
    clock = [10000]
    window = {"authorization_id": "fixed", "expires_at": datetime.fromtimestamp(10012, timezone.utc).isoformat()}
    calls, emitted = [], []
    def loader(*, now): return window if now < 10012 else None
    def operate(mode, **kwargs): calls.append((mode, clock[0])); return {"state": "idle"}
    def sleep(seconds): clock[0] += seconds
    result = service.watch("approved", operation=operate, window_loader=loader, clock=lambda: clock[0], sleep=sleep, emit=emitted.append)
    assert calls == [("approved", 10000), ("approved", 10005), ("approved", 10010)]
    assert clock[0] == 10012 and result["state"] == "expired"
    assert len(emitted) == 2
    assert json.loads((config.ROOT / "private" / "pipeline-submit-status.json").read_text())["status"] == "expired"


def test_watch_does_not_silently_extend_when_consent_window_changes(context):
    clock = [10000]
    calls = []
    def loader(*, now):
        return {"authorization_id": "first" if now == 10000 else "renewed", "expires_at": datetime.fromtimestamp(20000, timezone.utc).isoformat()}
    result = service.watch("prepare", operation=lambda *a, **kw: calls.append(1) or {"state": "idle"},
                           window_loader=loader, clock=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0]+seconds), emit=lambda result: None)
    assert result["reason_code"] == "service_window_changed" and calls == [1]


def test_main_ci_does_not_load_private_dotenv_or_start_watch(context, monkeypatch, capsys):
    monkeypatch.setenv("CI", "1")
    monkeypatch.setattr(config, "load_dotenv", lambda: pytest.fail("CI loaded local credentials"))
    assert service.main(["--approved-watch"]) == 0
    assert json.loads(capsys.readouterr().out)["reason_code"] == "ci_disabled"


def test_prepare_service_cannot_fall_back_to_legacy_blanket_mode(context, monkeypatch):
    monkeypatch.setenv("JHB_REQUIRE_PORTAL_APPROVAL", "0")
    assert service.once("prepare", connector=lambda: pytest.fail("Legacy submit policy reached service"))["reason_code"] == "portal_required"


@pytest.mark.parametrize("authority,allowed", [(None, False), ({"approval_mode": "legacy"}, False),
    ({"approval_mode": "independent_reviewer"}, True)])
def test_preparation_in_delegated_mode_needs_current_valid_authority(context, monkeypatch, authority, allowed):
    from jhb.applications import overnight
    connector, book = context
    monkeypatch.setenv("JHB_REQUIRE_PORTAL_APPROVAL", "0")
    monkeypatch.setattr(overnight, "load_authorization", lambda: authority)
    called = []
    def prepare(*args, **kwargs):
        called.append(True)
        return {"applications_prepared": 1}
    result = service.once("prepare", connector=connector, book_path=book, prepare=prepare)
    assert bool(called) is allowed
    if not allowed:
        assert result["reason_code"] == "portal_required"


def test_async_portal_failure_records_safe_status_without_echoing_exception_data(context):
    connector, book = context
    seed(connector)
    async def failed(*args, **kwargs):
        raise ValueError("Synthetic private application answer")
    with pytest.raises(ValueError):
        service.once("approved", connector=connector, book_path=book, approved=failed)
    status = (config.ROOT / "private" / "pipeline-submit-status.json").read_text()
    assert json.loads(status)["status"] == "failed"
    assert "private application answer" not in status


def test_portal_service_does_not_wait_for_long_preparation_manager_lock(context):
    connector, book = context
    seed(connector)
    calls = []
    async def approved(conn, book_path, **kwargs):
        calls.append(1)
        return {"enabled": True, "attempted": 1, "submitted": 0, "uncertain": 0, "handoffs": 1}
    lane = config.ROOT / "private" / "application-worker.lock"
    lane.parent.mkdir(parents=True, exist_ok=True)
    lane.touch()
    with lane.open("r+") as preparing:
        fcntl.flock(preparing, fcntl.LOCK_EX)
        result = service.once("approved", connector=connector, book_path=book, approved=approved)
    assert calls == [1] and result["attempted"] == 1
