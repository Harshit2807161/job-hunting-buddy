import asyncio
import json

import pytest

from jhb import config, store
from jhb.applications import booklet, pipeline, pipeline_status, queue, source_queue


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    conn = store.connect(tmp_path / "synthetic.sqlite3")
    path = tmp_path / "private" / "book.json"
    booklet.write_private(path, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}, "custom_answers": {}})
    yield conn, path, tmp_path / "private" / "pipeline-status.json"
    conn.close()


def test_real_pipeline_parks_fact_question_and_prepares_other_job_with_private_aggregate_status(context):
    conn, book, status = context
    for index in [1, 2]:
        queue.enqueue(conn, [{"dedupe_hash": str(index)*64, "company": "Synthetic Secret Company",
                              "title": "Software Engineer", "url": f"https://job-boards.greenhouse.io/example/jobs/{index}"}])
    stages = []
    async def runner(job, answers, **kwargs):
        stages.append(json.loads(status.read_text())["stage"])
        if job["url"].endswith("/1"):
            return {"state": "waiting_input", "missing": [{"question": "A private new factual question", "required": True, "ref": "new"}],
                    "events": [], "filled": []}, book.parent / "review.html"
        return {"state": "waiting_review", "missing": [], "events": [], "filled": []}, book.parent / "review.html"
    result = pipeline.run_cycle(conn, book, runner=runner)
    assert result["applications_prepared"] == 2
    assert result["states"] == {"waiting_input": 1, "waiting_review": 1}
    assert stages == ["preparation", "preparation"]
    document = json.loads(status.read_text())
    assert document["status"] == "completed" and document["stage"] == "complete"
    assert document["completed_at"] and document["queue"]["applications"] == {"waiting_input": 1, "waiting_review": 1}
    assert document["processed"]["applications_prepared"] == 2
    assert "candidate_answers_required" in document["reason_codes"]
    assert "private new factual" not in status.read_text() and "Secret Company" not in status.read_text()
    assert status.stat().st_mode & 0o777 == 0o600


def test_quarantine_is_truthfully_blocked_and_does_not_touch_queue(context):
    conn, book, status = context
    queue.initialize(conn)
    source_queue.initialize(conn)
    quarantine = config.ROOT / "private" / "overnight-monitor" / "repair-pending.json"
    booklet.write_private(quarantine, {"state": "synthetic interrupted repair"})
    async def forbidden(*args, **kwargs):
        pytest.fail("Quarantined manager must not run source/preparation/submission")
    result = pipeline.run_cycle(conn, book, resolver=forbidden, runner=forbidden)
    assert "skipped" in result
    assert json.loads(status.read_text())["reason_codes"] == ["repair_quarantine"]
    assert json.loads(status.read_text())["status"] == "blocked"
    assert quarantine.exists()
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


def test_periodic_heartbeat_updates_while_work_waits_and_records_sanitized_failure(context, monkeypatch):
    conn, book, status = context
    monkeypatch.setattr(pipeline_status, "INTERVAL", .01)
    async def operation(conn, book, *, heartbeat):
        heartbeat.update(stage="source_resolution")
        before = json.loads(status.read_text())["updated_at"]
        await asyncio.sleep(.04)
        after = json.loads(status.read_text())
        assert after["status"] == "running" and after["updated_at"] > before
        raise ValueError("Synthetic private secret must not appear in dashboard")
    with pytest.raises(ValueError):
        asyncio.run(pipeline_status.monitor_cycle(conn, book, operation))
    result = json.loads(status.read_text())
    assert result["status"] == "failed" and result["reason_codes"] == ["cycle_failed"]
    assert "private secret" not in status.read_text()


def test_manager_contention_preserves_active_heartbeat(context):
    import fcntl
    conn, book, status = context
    heartbeat = pipeline_status.Heartbeat(conn)
    heartbeat.update(stage="preparation")
    previous = status.read_bytes()
    lane = config.ROOT / "private" / "application-worker.lock"
    lane.touch()
    with lane.open("r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert "skipped" in pipeline.run_cycle(conn, book)
    assert status.read_bytes() == previous


def test_explicit_pause_is_durable_and_takes_precedence_over_repair_quarantine(context):
    conn, book, status = context
    pause = config.ROOT / "private" / "pipeline-pause.json"
    booklet.write_private(pause, {"status": "paused", "source": "Synthetic explicit user stop instruction"})
    quarantine = config.ROOT / "private" / "overnight-monitor" / "repair-pending.json"
    booklet.write_private(quarantine, {"state": "development"})
    async def forbidden(*args, **kwargs):
        pytest.fail("Paused manager must not resolve, prepare, or submit")
    for _ in range(2):
        result = pipeline.run_cycle(conn, book, resolver=forbidden, runner=forbidden)
        assert result["skipped"] == "Application automation is explicitly paused"
        current = json.loads(status.read_text())
        assert current["status"] == "paused" and current["reason_codes"] == ["automation_paused"]
    assert pause.exists() and quarantine.exists()
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='applications'").fetchone() is None


def test_portal_policy_never_calls_legacy_blanket_dispatch(context, monkeypatch):
    from jhb.applications import approvals, overnight
    conn, book, status = context
    monkeypatch.setenv("JHB_REQUIRE_PORTAL_APPROVAL", "1")
    calls = []
    async def portal(conn, book, **kwargs):
        calls.append(kwargs)
        return {"enabled": False, "attempted": 0, "submitted": 0, "uncertain": 0, "handoffs": 0}
    async def forbidden(*args, **kwargs): pytest.fail("Portal policy cannot fall back to blanket authorization")
    monkeypatch.setattr(approvals, "drain", portal)
    monkeypatch.setattr(overnight, "drain", forbidden)
    pipeline.run_cycle(conn, book)
    assert len(calls) == 1 and calls[0]["limit"] == 3


def test_hourly_reporting_still_runs_when_application_pipeline_is_paused(context, monkeypatch):
    from jhb.applications import hourly_reports
    conn, book, status = context
    booklet.write_private(config.ROOT / "private" / "pipeline-pause.json", {"status": "paused"})
    calls = []
    def report(read_conn):
        assert read_conn is conn
        calls.append("report")
        return {"state": "not_due"}
    monkeypatch.setattr(hourly_reports, "report", report)
    result = pipeline.run_cycle(conn, book)
    assert result["hourly_report"]["state"] == "not_due" and calls == ["report"]
    assert json.loads(status.read_text())["status"] == "paused"
