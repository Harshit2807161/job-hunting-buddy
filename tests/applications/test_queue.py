import json
import sqlite3

import pytest

from jhb.applications import queue


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    queue.initialize(db)
    yield db
    db.close()


def job(url, source_id="source-a"):
    return {"dedupe_hash": source_id, "url": url, "source": "synthetic", "company": "Example", "title": "Engineer"}


def test_canonical_identity_deduplicates_tracking_and_old_host(conn):
    assert queue.enqueue(conn, [job("https://boards.greenhouse.io/example/jobs/1234?gh_src=aaa"),
                                job("https://job-boards.greenhouse.io/example/jobs/1234", "source-b")]) == 1
    row = conn.execute("SELECT * FROM applications").fetchone()
    assert json.loads(row["job_json"])["source_job_hash"] == "source-a"
    assert row["job_hash"] != "source-a"
    assert queue.enqueue(conn, [job("https://job-boards.eu.greenhouse.io/example/jobs/1234")]) == 1


@pytest.mark.parametrize("url", [
    "http://job-boards.greenhouse.io/example/jobs/1234",
    "https://job-boards.greenhouse.io.attacker.example/example/jobs/1234",
    "https://job-boards.greenhouse.io@example.net/example/jobs/1234",
    "https://user:password@job-boards.greenhouse.io/example/jobs/1234",
    "https://job-boards.greenhouse.io:444/example/jobs/1234",
    "https://job-boards.greenhouse.io/example/jobs/../login",
    "https://job-boards.greenhouse.io/example/jobs/1234/extra",
])
def test_rejects_out_of_scope_urls(url):
    assert not queue.is_greenhouse(url)


def test_claim_recovery_is_bounded_and_review_cannot_auto_resume(conn):
    queue.enqueue(conn, [job("https://job-boards.greenhouse.io/example/jobs/1234")])
    item = queue.claim(conn)
    assert item
    assert queue.claim(conn) is None
    conn.execute("UPDATE applications SET lease_until=0, attempts=3")
    conn.commit()
    assert queue.claim(conn) is None
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "failed"
    queue.resume(conn, item["job_hash"])
    assert queue.claim(conn)
    queue.finish(conn, item["job_hash"], "waiting_review")
    queue.resume(conn, item["job_hash"])
    assert queue.claim(conn) is None


def test_user_confirmed_submission_cannot_be_requeued_or_resumed(conn, monkeypatch, tmp_path):
    from jhb import config
    from jhb.applications.worker import notify_pending
    candidate_job = job("https://job-boards.greenhouse.io/example/jobs/5678")
    queue.enqueue(conn, [candidate_job])
    item = queue.claim(conn)
    queue.finish(conn, item["job_hash"], "submitted", "synthetic-receipt.html")
    queue.resume(conn, item["job_hash"])
    assert queue.enqueue(conn, [candidate_job]) == 0
    assert queue.claim(conn) is None
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "submitted"
    monkeypatch.setattr(config, "ROOT", tmp_path)
    notify_pending(conn)
    assert not (tmp_path / "private" / "notifications").exists()


def test_verified_embed_and_hosted_job_share_application_identity(conn):
    assert queue.enqueue(conn, [job("https://boards.greenhouse.io/embed/job_app?for=example&token=1234"),
                                job("https://job-boards.greenhouse.io/example/jobs/1234", "other-source")]) == 1
    assert queue.greenhouse_identity("https://boards.eu.greenhouse.io/embed/job_app?for=example&token=1234") == ("eu", "example", "1234")
    assert not queue.is_greenhouse("https://boards.greenhouse.io/embed/job_app?token=1234")
    assert not queue.is_greenhouse("https://boards.greenhouse.io/embed/job_app?for=example&for=other&token=1234")
    assert not queue.is_greenhouse("https://boards.greenhouse.io/embed/job_app?for=example&token=1234&token=5678")


@pytest.mark.parametrize("stale_state", ["waiting_review", "waiting_input", "waiting_login", "failed", "skipped"])
def test_stale_preparation_finish_preserves_confirmed_submission(conn, stale_state):
    queue.enqueue(conn, [job("https://job-boards.greenhouse.io/example/jobs/5678")])
    claimed = queue.claim(conn)
    queue.finish(conn, claimed["job_hash"], "submitted", "synthetic-confirmation.json")
    before = dict(conn.execute("SELECT * FROM applications").fetchone())

    queue.finish(conn, claimed["job_hash"], stale_state, "stale-preparation.json")

    assert dict(conn.execute("SELECT * FROM applications").fetchone()) == before
    assert queue.claim(conn) is None
