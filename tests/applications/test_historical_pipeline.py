"""No browser may open before historical duplicate checks; synthetic only."""
import asyncio
import json
from pathlib import Path
import sqlite3
import time

import pytest

from jhb import config
from jhb.applications import booklet, boards, historical, pipeline, queue, source_queue, tracking, worker
from test_tracking import setup, ASHBY
from test_historical import row


def candidate(job):
    return {**job, "dedupe_hash": "synthetic-source", "source": "synthetic", "role_classes": "swe"}


@pytest.mark.parametrize("exact", [True, False])
def test_discovery_and_preparation_enqueue_persist_history_before_claim(setup, exact):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job, url=job["url"] if exact else "")
    historical.import_sheet(conn, executor=sheets)
    source_queue.enqueue(conn, [candidate(job)])
    queue.enqueue(conn, [candidate(job)])
    assert source_queue.claim(conn) is None
    assert queue.claim(conn) is None
    source = conn.execute("SELECT * FROM application_sources").fetchone()
    application = conn.execute("SELECT * FROM applications").fetchone()
    assert source["state"] == ("filtered" if exact else "history_hold")
    assert application["state"] == ("skipped" if exact else "history_hold")
    assert source["attempts"] == application["attempts"] == 0
    assert json.loads(application["job_json"])["historical_application"]["submission_confirmed"] is False
    source_queue.resume(conn, source["source_job_hash"])
    queue.resume(conn, application["job_hash"])
    queue.finish(conn, application["job_hash"], "waiting_review")
    assert source_queue.claim(conn) is None and queue.claim(conn) is None
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == application["state"]


def test_claim_rechecks_history_imported_after_discovery_even_beyond_backlog_scan(setup, monkeypatch):
    conn, job, _, sheets, _ = setup
    source_queue.enqueue(conn, [candidate(job)])
    queue.enqueue(conn, [candidate(job)])
    sheets.rows[2] = row(job)
    historical.import_sheet(conn, executor=sheets)
    monkeypatch.setattr(source_queue, "filter_history", lambda *a, **kw: 0)
    assert source_queue.claim(conn) is None and queue.claim(conn) is None
    assert conn.execute("SELECT state FROM application_sources").fetchone()[0] == "filtered"
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "skipped"


def test_retiring_historical_drafts_preserves_manual_packet_and_real_terminal_states(setup):
    conn, job, _, sheets, _ = setup
    queue.enqueue(conn, [candidate(job)])
    key = boards.application_hash(job["url"])
    packet = config.ROOT / "private" / "manual-review.json"
    booklet.write_private(packet, {"state": "waiting_review", "filled": [{"value": "candidate edit"}]})
    original = packet.read_bytes()
    conn.execute("UPDATE applications SET state='waiting_review',packet=?", (str(packet),));conn.commit()
    sheets.rows[2] = row(job, url="")
    historical.import_sheet(conn, executor=sheets)
    assert queue.filter_history(conn) == 1
    assert packet.read_bytes() == original
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "history_hold"
    for state in ("submitted", "submission_uncertain", "discarded"):
        conn.execute("UPDATE applications SET state=?", (state,));conn.commit()
        assert queue.filter_history(conn) == 0
        assert conn.execute("SELECT state FROM applications").fetchone()[0] == state


def test_imported_history_blocks_source_browser_and_runner_in_one_pipeline_cycle(setup, monkeypatch):
    conn, job, _, sheets, _ = setup
    source_queue.enqueue(conn, [candidate(job)])
    queue.enqueue(conn, [candidate(job)])
    sheets.rows[2] = row(job)
    monkeypatch.setattr(tracking, "ComposioSheets", lambda *a: sheets)
    book = config.ROOT / "private" / "book.json"
    booklet.write_private(book, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}, "custom_answers": {}})
    async def forbidden(*a, **kw):
        pytest.fail("Historical duplicate reached a browser")
    result = pipeline.run_cycle(conn, book, resolver=forbidden, runner=forbidden)
    assert result["history_import"]["state"] == "imported"
    assert result["sources_history_blocked"] == result["applications_history_blocked"] == 1
    assert result["sources_checked"] == result["applications_prepared"] == 0
    assert sheets.appends == 0


def test_missing_first_history_read_blocks_browser_but_preserves_discovered_jobs(setup, monkeypatch):
    conn, job, _, _, _ = setup
    source_queue.enqueue(conn, [candidate(job)])
    def fail(*a):
        raise RuntimeError("Synthetic connector unavailable")
    monkeypatch.setattr(tracking, "ComposioSheets", lambda *a: fail)
    async def forbidden(*a, **kw):
        pytest.fail("History unavailable; browser must not open")
    result = pipeline.run_cycle(conn, config.ROOT / "private" / "absent-book.json", resolver=forbidden, runner=forbidden)
    assert result["history_unavailable"] is True
    assert result["sources_checked"] == result["applications_prepared"] == 0
    assert conn.execute("SELECT state FROM application_sources").fetchone()[0] == "queued"
    health = json.loads((config.ROOT / "private" / "pipeline-status.json").read_text())
    assert health["status"] == "blocked" and "application_history_unavailable" in health["reason_codes"]


def test_redirect_to_existing_ats_history_is_stopped_before_filler(setup, monkeypatch):
    conn, job, _, sheets, _ = setup
    # Different legacy labels mean only the exact redirect establishes identity.
    source_queue.enqueue(conn, [{**candidate(job), "company": "Unresolved", "title": "Unknown role", "url": "https://example.test/jobs/wrapper"}])
    sheets.rows[2] = row(job)
    monkeypatch.setattr(tracking, "ComposioSheets", lambda *a: sheets)
    book = config.ROOT / "private" / "book.json"
    booklet.write_private(book, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}, "custom_answers": {}})
    async def resolve(*a, **kw):
        return {"state": "not_greenhouse", "board_type": "ashby", "application_url": job["url"]}
    async def forbidden(*a, **kw):
        pytest.fail("Resolved existing application reached filler")
    result = pipeline.run_cycle(conn, book, resolver=resolve, runner=forbidden)
    assert result["sources_checked"] == 1 and result["applications_prepared"] == 0
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "skipped"


def test_cached_guard_protects_direct_worker_before_eligibility_or_browser_and_preserves_packet(setup, monkeypatch):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job)
    historical.import_sheet(conn, executor=sheets)
    key = boards.application_hash(job["url"])
    directory = config.ROOT / "private" / "applications" / key
    directory.mkdir(parents=True)
    review = directory / "review.html";review.write_text("existing candidate-edited packet")
    monkeypatch.setattr(historical, "cached_match", lambda current: historical.match(conn, current))
    from jhb import eligibility
    monkeypatch.setattr(eligibility, "assess_job", lambda *a: pytest.fail("Duplicate reached eligibility/browser preparation"))
    result, packet = asyncio.run(worker.run_job({**candidate(job), "dedupe_hash": key}, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}}))
    assert result["state"] == "skipped" and packet == review
    assert review.read_text() == "existing candidate-edited packet"


def test_cache_readiness_is_bound_to_current_sheet_configuration(setup):
    conn, job, _, sheets, settings = setup
    sheets.rows[2] = row(job)
    historical.import_sheet(conn, executor=sheets)
    assert historical.cached_ready(conn)
    saved = json.loads(settings.read_text());saved["spreadsheet_id"] = "another-sheet"
    booklet.write_private(settings, saved)
    assert not historical.cached_ready(conn)


def test_cached_match_uses_explicit_root_read_only_database_and_never_creates_missing_db(setup, monkeypatch):
    conn, job, _, sheets, _ = setup
    assert historical.cached_match(job, root=config.ROOT) is None
    assert not (config.ROOT / "data").exists()
    sheets.rows[2] = row(job)
    historical.import_sheet(conn, executor=sheets)
    path = config.ROOT / "data" / "jobs.sqlite3"
    path.parent.mkdir()
    with sqlite3.connect(path) as copy:
        conn.backup(copy)
    original = path.read_bytes()
    connect = sqlite3.connect
    calls = []
    def readonly(database, *args, **kwargs):
        calls.append(database)
        assert database.endswith("?mode=ro") and kwargs.get("uri") is True
        return connect(database, *args, **kwargs)
    monkeypatch.setattr(historical.sqlite3, "connect", readonly)
    monkeypatch.setattr(config, "DB_PATH", Path("/synthetic/forbidden-real-db.sqlite3"))
    assert historical.cached_match(job, root=config.ROOT)["disposition"] == "exclude"
    assert historical.cached_match(job)["disposition"] == "exclude"
    assert path.read_bytes() == original and len(calls) == 2
    selected_root = config.ROOT
    monkeypatch.setattr(config, "ROOT", selected_root / "unrelated-default-root")
    assert historical.cached_match(job, root=selected_root)["disposition"] == "exclude"
    assert path.read_bytes() == original


def test_file_database_refresh_does_not_block_heartbeat_during_sheet_reads(setup):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job)
    path = config.ROOT / "jobs.sqlite3"
    with sqlite3.connect(path) as saved:
        conn.backup(saved)
    def slow_read(slug, payload):
        time.sleep(.05)
        return sheets(slug, payload)
    pulses = []
    async def run():
        with sqlite3.connect(path) as real:
            task = asyncio.create_task(historical.refresh_sheet(real, executor=slow_read))
            await asyncio.sleep(.01)
            pulses.append(not task.done())
            return await task
    assert asyncio.run(run())["state"] == "imported"
    assert pulses == [True]


def test_confirmed_receipt_blocks_source_before_final_sheet_append(setup):
    conn, job, receipt, _, settings = setup
    settings.unlink()  # Receipt is durable while the configured sink is unavailable.
    tracking.record_confirmed(conn, job, receipt)
    source_queue.enqueue(conn, [candidate(job)])
    assert source_queue.claim(conn) is None
    match = historical.match(conn, job)
    assert match["disposition"] == "exclude" and match["existing_receipt_verified"] is True
    assert match["match_kind"] == "existing_submission_record"
    assert conn.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 1
    assert conn.execute("SELECT state FROM application_sources").fetchone()[0] == "filtered"
