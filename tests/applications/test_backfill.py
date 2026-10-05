"""Synthetic discovery, history and queue tests; no browser or external tools."""
import json
import sqlite3

import pytest

from jhb import config, store
from jhb.applications import backfill, boards, booklet, cli, historical, queue, source_queue
from test_tracking import setup, ASHBY
from test_historical import row as history_row


@pytest.fixture
def seeded(setup):
    conn, job, _, sheets, _ = setup
    conn.executescript(store.SCHEMA)
    historical.import_sheet(conn, executor=sheets)
    book = {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}, "job_exclusions": {}}
    return conn, job, sheets, book


def seed(conn, number=1, **changes):
    job = {"dedupe_hash": f"source-{number}", "source": "synthetic", "company": f"Example {number}",
           "title": "Software Engineer - New Grad", "url": ASHBY.replace("555555555555", f"{number:012d}"),
           "locations": '["San Diego, CA"]', "role_classes": "swe", "date_posted": 1000+number,
           "first_seen": 1000, "notified_at": 1000, "raw": "{}", **changes}
    columns = list(job)
    conn.execute(f"INSERT INTO jobs ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", list(job.values()))
    conn.commit()
    return job


def test_preview_is_read_only_bounded_and_ranks_early_career_before_recency(seeded):
    conn, _, _, book = seeded
    seed(conn, 1, title="Software Engineer", date_posted=9999)
    junior = seed(conn, 2, title="Junior Full Stack Engineer", date_posted=200)
    seed(conn, 3, title="Software Engineer - New Grad", date_posted=100)
    before = list(conn.iterdump())
    result, selected = backfill.select(conn, book, limit=1)
    assert result["available"] == 3 and result["selected"] == 1
    assert selected[0]["dedupe_hash"] == junior["dedupe_hash"]
    assert result["candidates"][0]["eligibility_verified"] is False
    assert selected[0]["backfill"]["requires"] == ["fresh_official_description", "full_description_eligibility", "role_fit"]
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("changes", [
    {"url": "https://jobs.ashbyhq.com/example"},
    {"url": "https://jobs.lever.co/example/11111111-2222-3333-4444-555555555555"},
    {"title": "Robotics Software Engineer - New Grad"},
    {"title": "Junior Embedded Software Engineer"},
    {"title": "Software Engineer - New Grad - Public Sector"},
    {"title": "Software Engineer - New Grad - Starship"},
    {"title": "Associate Software Engineer - UF Only"},
    {"title": "Senior Software Engineer"},
    {"title": "Software Engineer Intern"},
    {"title": "Account Manager"},
    {"title": "Junior Software Engineer - TS/SCI required"},
    {"raw": json.dumps({"sponsorship": "Does Not Offer Sponsorship"})},
    {"raw": json.dumps({"description": "US citizenship is required for this role."})},
    {"notified_at": None},
])
def test_backfill_excludes_unsupported_unsuitable_ineligible_and_unnotified_rows(seeded, changes):
    conn, _, _, book = seeded
    seed(conn, **changes)
    assert backfill.select(conn, book)[0]["selected"] == 0


def test_exact_duplicate_urls_and_existing_routed_applications_are_not_requeued(seeded):
    conn, _, _, book = seeded
    first = seed(conn, 1)
    seed(conn, 2, url=first["url"].split("/application")[0]+"?embed=true")
    assert backfill.select(conn, book)[0]["selected"] == 1
    queue.enqueue(conn, [first])
    assert backfill.select(conn, book)[0]["selected"] == 0
    # Even damaged packet metadata cannot discard the durable exact-job guard.
    conn.execute("UPDATE applications SET job_json='{}'")
    assert backfill.select(conn, book)[0]["selected"] == 0


def test_resolved_wrapper_destination_suppresses_duplicate_seed_identity(seeded):
    conn, _, _, book = seeded
    first = seed(conn)
    source_queue.enqueue(conn, [{**first, "dedupe_hash": "wrapper", "url": "https://example.test/jobs/1"}])
    source_queue.finish(conn, "wrapper", "resolved", board="ashby", application_url=first["url"])
    assert backfill.select(conn, book)[0]["selected"] == 0


def test_backfill_omits_possible_reposted_role_without_marking_another_id_applied(seeded):
    conn, _, _, book = seeded
    first = seed(conn)
    second = seed(conn, 2, company=first["company"])
    queue.enqueue(conn, [first])
    result, _ = backfill.select(conn, book)
    assert result["selected"] == 0 and result["excluded"]["same_role_already_tracked"] == 1
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
    assert historical.match(conn, second) is None


def test_verified_exact_exclusions_and_legacy_sheet_holds_are_respected(seeded):
    conn, _, sheets, book = seeded
    first, second = seed(conn), seed(conn, 2)
    book["job_exclusions"][boards.application_hash(first["url"])] = {"status": "verified", "source": "Synthetic user exclusion"}
    sheets.rows[2] = history_row({**second, "locations": ["San Diego, CA"]}, url="")
    historical.import_sheet(conn, executor=sheets, refresh_seconds=0)
    result, _ = backfill.select(conn, book)
    assert result["selected"] == 0
    assert result["excluded"]["candidate_excluded"] == result["excluded"]["application_history"] == 1


def test_enqueue_only_adds_source_checks_and_is_idempotent(seeded):
    conn, _, _, book = seeded
    seed(conn)
    original_notified = conn.execute("SELECT notified_at FROM jobs").fetchone()[0]
    result = backfill.enqueue(conn, book)
    assert result["enqueued"] == 1
    source = conn.execute("SELECT state,job_json FROM application_sources").fetchone()
    assert source["state"] == "queued"
    assert json.loads(source["job_json"])["backfill"]["eligibility_verified"] is False
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name='applications'").fetchone()
    assert conn.execute("SELECT notified_at FROM jobs").fetchone()[0] == original_notified
    assert backfill.enqueue(conn, book)["enqueued"] == 0


def test_enqueue_rechecks_history_added_after_preview(seeded):
    conn, _, sheets, book = seeded
    job = seed(conn)
    assert backfill.select(conn, book)[0]["selected"] == 1
    sheets.rows[2] = history_row({**job, "locations": ["San Diego, CA"]})
    historical.import_sheet(conn, executor=sheets, refresh_seconds=0)
    assert backfill.enqueue(conn, book)["enqueued"] == 0


def test_history_must_be_available_without_fetching_or_schema_initialization(seeded, monkeypatch):
    conn, _, _, book = seeded
    seed(conn)
    monkeypatch.setattr(historical, "cached_ready", lambda _: False)
    before = list(conn.iterdump())
    assert backfill.enqueue(conn, book)["state"] == "history_required"
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("limit", [0, 31, True, -1])
def test_limit_is_explicitly_bounded(seeded, limit):
    conn, _, _, book = seeded
    with pytest.raises(ValueError, match="between 1 and 30"):
        backfill.select(conn, book, limit=limit)


def test_cli_default_preview_opens_database_read_only(seeded, monkeypatch, tmp_path, capsys):
    conn, _, _, book = seeded
    seed(conn)
    database = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(database) as destination:
        conn.backup(destination)
    path = tmp_path / "private" / "book.json"
    booklet.write_private(path, book)
    monkeypatch.setattr(config, "DB_PATH", database)
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setattr(store, "connect", lambda *a: pytest.fail("Preview must not initialize or migrate database"))
    original = database.read_bytes()
    assert cli.main(["--booklet", str(path), "backfill-supported", "--limit", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["selected"] == 1
    assert database.read_bytes() == original
