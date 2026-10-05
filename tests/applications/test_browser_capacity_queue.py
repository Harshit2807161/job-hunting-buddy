import time

import pytest

from jhb import config, store
from jhb.applications import booklet, pipeline, queue, source_queue, worker
from jhb.applications.cli_browser import BrowserOperationError


class Capacity(BrowserOperationError):
    condition = "browser_capacity"
    mutation_started = False


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    db = store.connect(tmp_path / "jobs.sqlite3")
    path = tmp_path / "private" / "book.json"
    booklet.write_private(path, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}, "custom_answers": {}})
    yield db, path
    db.close()


def job(url="https://job-boards.greenhouse.io/synthetic/jobs/123"):
    return store.Job("synthetic", "capacity", "Example", "Software Engineer", url, role_classes=["swe"])


def test_full_browser_defers_without_candidate_question_or_retry_budget_loss(setup):
    db, path = setup
    queue.enqueue(db, [job()])
    async def runner(*args, **kwargs):
        return worker.failure_result(Capacity("Worker-owned browser tab capacity reached")), path.parent / "review.html"
    for _ in range(5):
        result = pipeline.run_cycle(db, path, runner=runner)
        row = db.execute("SELECT * FROM applications").fetchone()
        assert row["state"] == "retry" and row["attempts"] == 0
        assert row["available_at"] >= int(time.time())+55
        assert row["error_kind"] == "browser_capacity"
        assert result["applications_prepared"] == result["question_handoffs"] == 0
        assert result["browser_capacity_deferred"] == 1
        assert not booklet.load(path).get("question_handoffs")
        db.execute("UPDATE applications SET available_at=0"); db.commit()


def test_linkedin_capacity_preserves_source_budget_and_does_not_fall_back(setup):
    db, path = setup
    source_queue.enqueue(db, [job("https://www.linkedin.com/jobs/view/123/")])
    async def authenticated(*args, **kwargs):
        raise Capacity("Worker-owned browser tab capacity reached")
    async def isolated(*args, **kwargs):
        pytest.fail("A capacity wait must not become a second source attempt")
    result = pipeline.run_cycle(db, path, resolver=isolated, authenticated_resolver=authenticated)
    row = db.execute("SELECT * FROM application_sources").fetchone()
    assert row["state"] == "retry" and row["attempts"] == 0
    assert row["available_at"] >= int(time.time())+55
    assert result["browser_capacity_deferred"] == 1


def test_exact_reviewed_direct_ats_precedes_linkedin_retry_without_faking_timestamps(setup):
    db, _ = setup
    urls = ['https://www.linkedin.com/jobs/view/123/',
            'https://jobs.ashbyhq.com/synthetic/11111111-2222-3333-4444-555555555555/application',
            'https://job-boards.greenhouse.io/synthetic/jobs/9876',
            'https://job-boards.greenhouse.io.attacker.example/synthetic/jobs/1']
    for index, url in enumerate(urls):
        source_queue.enqueue(db, [{"dedupe_hash": str(index), "url": url, "company": "Synthetic", "title": "Engineer"}])
    db.execute("UPDATE application_sources SET state='retry',available_at=0,updated_at=100+CAST(source_job_hash AS INTEGER)")
    db.commit()
    original = dict(db.execute("SELECT source_job_hash,updated_at FROM application_sources"))
    assert source_queue.claim(db)["source_job_hash"] == '1'
    assert source_queue.claim(db)["source_job_hash"] == '2'
    assert source_queue.claim(db)["source_job_hash"] == '0'
    # The unclaimed lookalike remains in the ordinary wrapper tier and retains age.
    assert db.execute("SELECT updated_at FROM application_sources WHERE source_job_hash='3'").fetchone()[0] == original['3']


@pytest.mark.parametrize("module,table,key,terminal", [
    (queue, "applications", "job_hash", "submitted"),
    (source_queue, "application_sources", "source_job_hash", "resolved"),
])
def test_late_capacity_result_cannot_reset_a_new_claim_or_terminal(setup, module, table, key, terminal):
    db, _ = setup
    module.enqueue(db, [job()])
    item = module.claim(db)
    db.execute(f"UPDATE {table} SET attempts=attempts+1"); db.commit()
    assert not module.defer_capacity(db, item)
    db.execute(f"UPDATE {table} SET attempts=?,state=?", (item["attempts"], terminal)); db.commit()
    assert not module.defer_capacity(db, item)
    assert db.execute(f"SELECT state FROM {table}").fetchone()[0] == terminal


@pytest.mark.parametrize("change", [{"filled": [{"value": "already typed"}]}, {"mutation_started": True},
    {"runtime_click_started": True}, {"missing": [{"question": "New fact"}]}, {"submitted": True}])
def test_only_proven_untouched_capacity_failures_may_refund_attempt(change):
    result = worker.failure_result(Capacity("capacity"))
    assert pipeline._capacity_wait(result)
    assert not pipeline._capacity_wait({**result, **change})
