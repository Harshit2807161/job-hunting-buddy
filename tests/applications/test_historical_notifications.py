"""Discovery emails use the same cached seen set as the browser queues."""
from jhb import config, poll, store
from jhb.applications import booklet, historical, source_queue, tracking
from test_pipeline import setup, job
from test_tracking import Sheets, ASHBY


def sheet_history(conn):
    settings = config.ROOT / "private" / tracking.CONFIG_NAME
    booklet.write_private(settings, {"enabled": True, "spreadsheet_id": "synthetic-sheet", "sheet_name": "Sheet1",
        "headers": tracking.HEADERS, "timezone": "America/Los_Angeles", "date_style": "ordinal_day_short_month"})
    sheets = Sheets()
    sheets.rows[2] = ["Example", "Software Engineer", "", "15th sep", "", "", "", ASHBY]
    historical.import_sheet(conn, executor=sheets)
    return sheets


def test_cached_historical_role_is_recorded_but_never_emailed_as_new(setup, monkeypatch):
    conn, _ = setup
    store.upsert_jobs(conn, [store.Job("seed", "seed", "History", "Seed", "https://example.test/seed")], mark_notified=True)
    sheets = sheet_history(conn)
    duplicate = job("already-applied", ASHBY)
    fresh = store.Job("synthetic", "new", "Another Corp", "Software Engineer", "https://example.test/jobs/new")
    monkeypatch.setattr(poll.simplify, "poll", lambda conn: ([duplicate, fresh], "synthetic"))
    sent = []
    monkeypatch.setattr(poll.notify, "send", lambda rows, **kwargs: sent.append(rows) or True)
    result = poll.run_once(conn, use_jobspy=False)
    assert result["historical_seen"] == 1 and result["sources_queued"] == 2
    assert len(sent) == 1 and len(sent[0]) == 1 and sent[0][0]["company"] == "Another Corp"
    row = conn.execute("SELECT state,attempts FROM application_sources WHERE source_job_hash=?", (duplicate.dedupe_hash,)).fetchone()
    assert row["state"] == "filtered" and row["attempts"] == 0
    assert conn.execute("SELECT notified_at FROM jobs WHERE dedupe_hash=?", (duplicate.dedupe_hash,)).fetchone()[0] is not None
    assert sheets.appends == 0
    assert poll.run_once(conn, use_jobspy=False)["sources_queued"] == 0


def test_explicit_dry_run_does_not_mark_historical_notification_seen(setup, monkeypatch):
    conn, _ = setup
    store.upsert_jobs(conn, [store.Job("seed", "seed", "History", "Seed", "https://example.test/seed")], mark_notified=True)
    sheet_history(conn)
    duplicate = job("already-applied", ASHBY)
    monkeypatch.setattr(poll.simplify, "poll", lambda conn: ([duplicate], "synthetic"))
    result = poll.run_once(conn, use_jobspy=False, dry_run=True)
    assert result["historical_seen"] == 1 and result["emails"] == 0
    assert conn.execute("SELECT notified_at FROM jobs WHERE dedupe_hash=?", (duplicate.dedupe_hash,)).fetchone()[0] is None
