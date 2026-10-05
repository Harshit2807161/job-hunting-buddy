import asyncio
import json
from pathlib import Path

import pytest

from jhb import config, poll, store
from jhb.applications import booklet, pipeline, queue, source_queue


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # Static repository skills/schemas remain at their real source locations;
    # only mutable artifacts/database paths are redirected for each fixture.
    from jhb.applications import worker  # noqa: F401
    monkeypatch.setattr(config, "ROOT", tmp_path)
    db = store.connect(tmp_path / "jobs.sqlite3")
    path = tmp_path / "private" / "book.json"
    booklet.write_private(path, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}, "custom_answers": {}})
    yield db, path
    db.close()


def job(sid, url):
    return store.Job("synthetic", sid, "Example", "Software Engineer", url, role_classes=["swe"])


def test_poll_records_indirect_and_collapsed_urls_before_notification(setup, monkeypatch):
    db, _ = setup
    store.upsert_jobs(db, [job("seed", "https://example.test/seed")], mark_notified=True)
    found = [job("wrapper", "https://linkedin.com/jobs/view/123"), job("employer", "https://example.test/careers/123")]
    monkeypatch.setattr(poll.simplify, "poll", lambda conn: (found, "synthetic"))
    monkeypatch.setattr(poll.jobspy_src, "poll", lambda conn: ([], "synthetic"))
    monkeypatch.delenv("JHB_APPLICATIONS_ENABLED", raising=False)
    def send(rows, **kwargs):
        assert db.execute("SELECT COUNT(*) FROM application_sources").fetchone()[0] == 2
        return True
    monkeypatch.setattr(poll.notify, "send", send)
    result = poll.run_once(db)
    assert result["sources_queued"] == 2
    assert db.execute("SELECT COUNT(*) FROM jobs WHERE notified_at IS NULL").fetchone()[0] == 0
    assert poll.run_once(db)["sources_queued"] == 0


@pytest.mark.parametrize("mode", ["seed", "dry_run"])
def test_seed_and_dry_run_do_not_schedule_automation(setup, monkeypatch, mode):
    db, _ = setup
    if mode == "dry_run":
        store.upsert_jobs(db, [job("seed", "https://example.test/seed")], mark_notified=True)
    monkeypatch.setattr(poll.simplify, "poll", lambda conn: ([job("new", "https://example.test/job")], "synthetic"))
    monkeypatch.setattr(poll.notify, "send", lambda *a, **kw: True)
    result = poll.run_once(db, use_jobspy=False, force_seed=mode == "seed", dry_run=mode == "dry_run")
    assert result["sources_queued"] == 0
    assert db.execute("SELECT name FROM sqlite_master WHERE name='application_sources'").fetchone() is None


def test_source_leases_recover_and_retries_stop(setup):
    db, _ = setup
    source_queue.enqueue(db, [job("a", "https://example.test/job")])
    item = source_queue.claim(db)
    assert source_queue.claim(db) is None
    db.execute("UPDATE application_sources SET lease_until=0")
    db.commit()
    assert source_queue.claim(db)["attempts"] == 2
    db.execute("UPDATE application_sources SET lease_until=0,attempts=3")
    db.commit()
    assert source_queue.claim(db) is None
    assert db.execute("SELECT state FROM application_sources").fetchone()[0] == "failed"
    source_queue.resume(db, item["source_job_hash"])
    assert source_queue.claim(db)["attempts"] == 1


def test_disqualified_source_is_retained_as_filtered_before_any_browser(setup):
    db, _ = setup
    denied = {'dedupe_hash':'blocked-source','title':'AI/ML Engineer 1 Top Secret/SCI w/Poly',
              'company':'Synthetic','url':'https://www.linkedin.com/jobs/view/123'}
    source_queue.enqueue(db,[denied])
    assert source_queue.claim(db) is None
    row=db.execute('SELECT * FROM application_sources').fetchone()
    assert row['state']=='filtered' and row['attempts']==0
    assert json.loads(row['job_json'])['eligibility']['findings'][0]['category']=='security_clearance'
    source_queue.resume(db,denied['dedupe_hash'])
    assert source_queue.claim(db) is None


def test_old_source_backlog_and_direct_resolver_call_cannot_bypass_filter(setup):
    db, _ = setup
    source_queue.enqueue(db,[job('legacy','https://www.linkedin.com/jobs/view/123')])
    row=db.execute('SELECT * FROM application_sources').fetchone()
    value=json.loads(row['job_json']);value['description']='Employment visa sponsorship is not available.'
    db.execute('UPDATE application_sources SET job_json=?',(json.dumps(value),));db.commit()
    assert source_queue.claim(db) is None
    async def forbidden(*args,**kwargs): pytest.fail('Disqualified job reached source browser')
    result=asyncio.run(pipeline._resolve_one({'job':value},forbidden,asyncio.Semaphore(1),1,forbidden))
    assert result['state']=='filtered' and result['findings'][0]['category']=='visa_sponsorship'


def test_pipeline_resolves_indirect_greenhouse_only_and_deduplicates(setup):
    db, path = setup
    source_queue.enqueue(db, [job("one", "https://example.test/company/job"),
                              job("two", "https://linkedin.com/jobs/view/123"),
                              job("other", "https://jobs.lever.co/example/abc")])
    async def resolver(candidate, **kwargs):
        return {"state": "not_greenhouse", "board_type": "lever", "application_url": None} if "lever" in candidate["url"] else {
            "state": "greenhouse", "board_type": "greenhouse", "application_url": "https://job-boards.greenhouse.io/example/jobs/123"}
    async def runner(candidate, book, **kwargs):
        assert candidate["source_url"].startswith("https://")
        assert candidate["source_job_hash"] in {j.dedupe_hash for j in [job("one", "https://example.test/company/job"), job("two", "https://linkedin.com/jobs/view/123")]}
        return {"state": "waiting_review", "reason": "Synthetic review", "events": [], "filled": []}, path.parent / "review.html"
    result = pipeline.run_cycle(db, path, resolver=resolver, runner=runner)
    assert result["sources_checked"] == 3
    assert result["applications_queued"] == result["applications_prepared"] == 1
    assert result["boards"] == {"greenhouse": 2, "lever": 1}
    assert db.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM application_sources WHERE state='resolved'").fetchone()[0] == 3
    assert pipeline.run_cycle(db, path, resolver=resolver, runner=runner)["applications_prepared"] == 0


def test_parallel_preparation_is_bounded_and_persists_new_questions(setup):
    db, path = setup
    queue.enqueue(db, [job(str(i), f"https://job-boards.greenhouse.io/example/jobs/{i+1}") for i in range(4)])
    active = peak = 0
    async def runner(candidate, book, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.03)
        active -= 1
        return {"state": "waiting_input", "reason": "Needs candidate answer", "events": [], "filled": [],
                "missing": [{"question": "Employer-specific relocation preference", "answer_key": None}]}, path.parent / "review.html"
    result = pipeline.run_cycle(db, path, resolver=lambda *_: None, runner=runner, application_limit=3, concurrency=2)
    assert peak == 2
    assert result["question_handoffs"] == 3
    assert result["applications_prepared"] == 3
    assert db.execute("SELECT COUNT(*) FROM applications WHERE state='queued'").fetchone()[0] == 1
    book = booklet.load(path)
    assert len(book["question_handoffs"]) == 1
    assert len(next(iter(book["question_handoffs"].values()))["contexts"]) == 3
    assert json.loads((path.parent / "notifications" / "pending-questions.json").read_text())["applications"]


def test_submitted_application_and_capacity_are_preserved(setup):
    db, path = setup
    queue.enqueue(db, [job("submitted", "https://job-boards.greenhouse.io/example/jobs/123")])
    submitted = queue.claim(db)
    queue.finish(db, submitted["job_hash"], "submitted")
    source_queue.enqueue(db, [job("wrapper", "https://example.test/jobs/123")])
    async def resolver(*args, **kwargs):
        return {"state": "greenhouse", "application_url": "https://job-boards.greenhouse.io/example/jobs/123"}
    async def unexpected(*args, **kwargs):
        pytest.fail("Submitted/reviewed applications must not rerun")
    assert pipeline.run_cycle(db, path, resolver=resolver, runner=unexpected)["applications_prepared"] == 0
    queue.enqueue(db, [job("review", "https://job-boards.greenhouse.io/example/jobs/456")])
    review = queue.claim(db)
    queue.finish(db, review["job_hash"], "waiting_review")
    queue.enqueue(db, [job("next", "https://job-boards.greenhouse.io/example/jobs/789")])
    result = pipeline.run_cycle(db, path, resolver=resolver, runner=unexpected, max_active_drafts=1)
    assert result["applications_prepared"] == 0
    assert db.execute("SELECT COUNT(*) FROM applications WHERE state='queued'").fetchone()[0] == 1


def test_source_errors_backoff_and_verification_handoffs_do_not_prepare(setup):
    db, path = setup
    source_queue.enqueue(db, [job("a", "https://example.test/error"), job("b", "https://example.test/login")])
    async def resolver(candidate, **kwargs):
        if candidate["url"].endswith("error"):
            raise RuntimeError("must not expose this private value")
        return {"state": "blocked", "handoff": "waiting_login", "board_type": "linkedin", "reason": "Sign-in required"}
    result = pipeline.run_cycle(db, path, resolver=resolver, runner=lambda *a, **kw: None)
    assert result["applications_prepared"] == 0
    states = {row["state"] for row in db.execute("SELECT state FROM application_sources")}
    assert states == {"retry", "waiting_login"}
    evidence = "".join(p.read_text() for p in (config.ROOT / "private" / "source-checks").glob("*.json"))
    assert "must not expose this private value" not in evidence
    assert pipeline.run_cycle(db, path, resolver=resolver, runner=lambda *a, **kw: None)["sources_checked"] == 0


def test_isolated_source_handoffs_remain_local_without_candidate_email(setup, monkeypatch):
    db, _ = setup
    candidate = job("private", "https://example.test/login")
    source_queue.enqueue(db, [candidate])
    source_queue.finish(db, candidate.dedupe_hash, "waiting_login", board="linkedin")
    attempts = []
    monkeypatch.setattr(pipeline.notify, "send", lambda *args, **kwargs: attempts.append(kwargs) or len(attempts) > 1)
    pipeline.notify_source_handoffs(db, send_email=True)
    assert db.execute("SELECT notified_at FROM application_sources").fetchone()[0] is None
    pipeline.notify_source_handoffs(db, send_email=True)
    pipeline.notify_source_handoffs(db, send_email=True)
    assert attempts == []
    assert db.execute("SELECT notified_at FROM application_sources").fetchone()[0] is None
    assert list((config.ROOT / "private" / "notifications").glob("source-*.json"))


def test_stale_running_leases_do_not_block_recovery_at_capacity(setup):
    db, path = setup
    queue.enqueue(db, [job("crashed", "https://job-boards.greenhouse.io/example/jobs/123")])
    queue.claim(db)
    db.execute("UPDATE applications SET lease_until=0")
    db.commit()
    async def runner(candidate, book, **kwargs):
        return {"state": "waiting_review", "reason": "Synthetic review", "events": [], "filled": []}, path.parent / "review.html"
    result = pipeline.run_cycle(db, path, resolver=lambda *a, **kw: None, runner=runner, max_active_drafts=1)
    assert result["applications_prepared"] == 1


def test_preparation_timeout_is_bounded_and_writes_failure_packet(setup):
    db, path = setup
    queue.enqueue(db, [job("slow", "https://job-boards.greenhouse.io/example/jobs/123")])
    async def runner(candidate, book, **kwargs):
        await asyncio.sleep(30)
    result = pipeline.run_cycle(db, path, resolver=lambda *a, **kw: None, runner=runner, application_timeout=1)
    assert result["states"] == {"retry": 1}
    row = db.execute("SELECT state,packet FROM applications").fetchone()
    assert row["state"] == "retry"
    assert "TimeoutError" in Path(row["packet"]).read_text()


def test_synthetic_demo_discovery_question_answer_resume_and_review(setup, capsys):
    import os
    from pathlib import Path
    from jhb.applications.demo import run_demo
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).resolve().parents[2] / ".local-browsers"))
    assert run_demo() == 0
    output = capsys.readouterr().out
    assert "SYNTHETIC FIXTURE VALIDATION" in output
    assert "source checks queued: 3" in output
    assert '"greenhouse": 2, "lever": 1' in output
    assert "new questions: 1 · resumed: 2" in output
    assert "Review-ready: 2 · final submissions: 0" in output


def test_automation_cannot_mark_an_application_submitted(setup):
    db, path = setup
    queue.enqueue(db, [job("unsafe-result", "https://job-boards.greenhouse.io/example/jobs/123")])
    async def runner(*args, **kwargs):
        return {"state": "submitted"}, "unsafe-receipt.html"
    result = pipeline.run_cycle(db, path, resolver=lambda *a, **kw: None, runner=runner)
    assert result["states"] == {"failed": 1}
    assert db.execute("SELECT state FROM applications").fetchone()[0] == "failed"


def test_malformed_classifier_result_is_a_retryable_failure(setup):
    db, path = setup
    source_queue.enqueue(db, [job("malformed", "https://example.test/job")])
    async def resolver(*args, **kwargs):
        return None
    result = pipeline.run_cycle(db, path, resolver=resolver, runner=lambda *a, **kw: None)
    assert result["sources_checked"] == 1
    assert db.execute("SELECT state FROM application_sources").fetchone()[0] == "retry"


def test_answer_arriving_during_preparation_requeues_a_fresh_snapshot(setup):
    from jhb.applications import questions
    db, path = setup
    queue.enqueue(db, [job("racing-answer", "https://job-boards.greenhouse.io/example/jobs/123")])
    candidate = json.loads(db.execute("SELECT job_json FROM applications").fetchone()[0])
    stale = {"state": "waiting_input", "reason": "New employer question", "events": [], "filled": [],
             "missing": [{"question": "Can you relocate for this role?", "ref": "relocation", "answer_key": None, "required": True}]}
    record = questions.collect(candidate, stale, path)[0]
    async def first_runner(candidate, old_book, **kwargs):
        assert not old_book.get("custom_answers")
        assert db.execute("SELECT state FROM applications").fetchone()[0] == "running"
        questions.answer(record["id"], True, path, connection=db)
        return stale, path.parent / "review.html"
    first = pipeline.run_cycle(db, path, resolver=lambda *a, **kw: None, runner=first_runner)
    assert first["auto_requeued"] == 1
    assert db.execute("SELECT state FROM applications").fetchone()[0] == "queued"
    assert questions.pending(path) == []
    async def resumed_runner(candidate, current_book, **kwargs):
        assert any(item["value"] is True for item in current_book["custom_answers"].values())
        return {"state": "waiting_review", "reason": "Synthetic review", "events": [], "filled": []}, path.parent / "review.html"
    resumed = pipeline.run_cycle(db, path, resolver=lambda *a, **kw: None, runner=resumed_runner)
    assert resumed["states"] == {"waiting_review": 1}


def test_incompatible_answer_handoff_does_not_retry_without_new_input(setup):
    from jhb.applications import questions
    db, path = setup
    queue.enqueue(db, [job("incompatible", "https://job-boards.greenhouse.io/example/jobs/123")])
    candidate = json.loads(db.execute("SELECT job_json FROM applications").fetchone()[0])
    missing = {"state": "waiting_input", "reason": "Required answer", "events": [], "filled": [],
               "missing": [{"question": "Choose an office", "ref": "office", "answer_key": None, "required": True}]}
    record = questions.collect(candidate, missing, path)[0]
    questions.answer(record["id"], "Unlisted office", path)
    key = booklet.load(path)["question_handoffs"][record["id"]]["custom_answer_key"]
    missing["missing"][0].update(answer_key=key, reason="Stored answer unavailable or incompatible with field")
    async def runner(*args, **kwargs):
        return missing, path.parent / "review.html"
    first = pipeline.run_cycle(db, path, resolver=lambda *a, **kw: None, runner=runner)
    assert first["auto_requeued"] == 0
    assert db.execute("SELECT state FROM applications").fetchone()[0] == "waiting_input"
    assert pipeline.run_cycle(db, path, resolver=lambda *a, **kw: None, runner=runner)["applications_prepared"] == 0


def test_transient_preparation_recovers_after_backoff_without_reprocessing_review(setup):
    db, path = setup
    queue.enqueue(db, [job('transient', 'https://job-boards.greenhouse.io/example/jobs/900')])
    seen = []
    async def runner(candidate, book, **kwargs):
        seen.append(candidate['dedupe_hash'])
        if len(seen) == 1:
            raise ConnectionError('Private exception text must never appear')
        return {'state': 'waiting_review', 'filled': [], 'events': []}, path.parent/'review.html'
    first = pipeline.run_cycle(db, path, runner=runner)
    assert first['states'] == {'retry': 1}
    assert first['technical_retries'] == 1
    row = db.execute('SELECT * FROM applications').fetchone()
    assert row['attempts'] == 1 and row['error_kind'] == 'ConnectionError'
    assert 'Private exception text' not in Path(row['packet']).read_text()
    assert pipeline.run_cycle(db, path, runner=runner)['applications_prepared'] == 0
    db.execute('UPDATE applications SET available_at=0')
    db.commit()
    assert pipeline.run_cycle(db, path, runner=runner)['states'] == {'waiting_review': 1}
    assert seen[0] == seen[1]
    assert pipeline.run_cycle(db, path, runner=runner)['applications_prepared'] == 0


def test_technical_retry_budget_is_durable_and_never_reset_by_cron(setup):
    db, path = setup
    queue.enqueue(db, [job('always-transient', 'https://job-boards.greenhouse.io/example/jobs/901')])
    async def runner(*args, **kwargs):
        return {'state': 'failed', 'retryable': True, 'error_kind': 'browser_mechanics',
                'filled': [], 'events': []}, path.parent/'review.html'
    for attempt in range(1, 4):
        summary = pipeline.run_cycle(db, path, runner=runner)
        assert summary['states'] == ({'retry': 1} if attempt < 3 else {'failed': 1})
        assert db.execute('SELECT attempts FROM applications').fetchone()[0] == attempt
        db.execute('UPDATE applications SET available_at=0')
        db.commit()
    assert pipeline.run_cycle(db, path, runner=runner)['applications_prepared'] == 0
    assert db.execute('SELECT state FROM applications').fetchone()[0] == 'failed'


def test_legacy_technical_packet_recovery_does_not_reinterpret_safety_failures(setup):
    db, path = setup
    queue.enqueue(db, [job('old-transient', 'https://job-boards.greenhouse.io/example/jobs/902'),
                       job('old-rejected', 'https://job-boards.greenhouse.io/example/jobs/903')])
    items = [queue.claim(db), queue.claim(db)]
    for item, exception in zip(items, ['FileNotFoundError', 'ValueError']):
        directory = config.ROOT/'private'/'applications'/item['job_hash']
        booklet.write_private(directory/'packet.json', {'state': 'failed', 'reason': f'Preparation failed: {exception}',
                                                       'job': item['job'],
                                                       'filled': [], 'events': [], 'submitted': False})
        queue.finish(db, item['job_hash'], 'failed', directory/'review.html')
    assert pipeline.recover_technical_failures(db) == 1
    assert pipeline.recover_technical_failures(db) == 0
    states = {r['job_hash']:r['state'] for r in db.execute('SELECT * FROM applications')}
    assert states[items[0]['job_hash']] == 'retry'
    assert states[items[1]['job_hash']] == 'failed'
    assert queue.claim(db)['attempts'] == 2


def test_capacity_backlog_is_reported_without_reopening_protected_drafts(setup):
    db, path = setup
    queue.enqueue(db, [job('review', 'https://job-boards.greenhouse.io/example/jobs/904'),
                       job('queued', 'https://job-boards.greenhouse.io/example/jobs/905')])
    item = queue.claim(db)
    queue.finish(db, item['job_hash'], 'waiting_review')
    async def forbidden(*args, **kwargs):
        pytest.fail('Capacity full; no protected draft may be re-opened')
    summary = pipeline.run_cycle(db, path, runner=forbidden, max_active_drafts=1)
    assert summary['capacity_blocked'] is True
    assert summary['backlog']['applications'] == 1
    assert summary['backlog']['active_drafts'] == 1
    assert summary['applications_prepared'] == 0


@pytest.mark.parametrize("extra", [{"missing": [{"question": "A new factual answer"}]},
                                   {"verification": "email-code"}, {"submitted": True},
                                   {"runtime_click_started": True}, {"runtime_click_started": None}])
def test_technical_packet_with_candidate_handoff_or_submission_never_auto_recovers(setup, extra):
    db, _ = setup
    queue.enqueue(db, [job("protected-failure", "https://job-boards.greenhouse.io/example/jobs/906")])
    item = queue.claim(db)
    directory = config.ROOT / "private" / "applications" / item["job_hash"]
    booklet.write_private(directory / "packet.json", {
        "job": item['job'], "state": "failed", "retryable": True, "error_kind": "TimeoutError", **extra,
    })
    queue.finish(db, item["job_hash"], "failed", directory / "review.html")
    assert pipeline.recover_technical_failures(db) == 0
    assert db.execute("SELECT state FROM applications").fetchone()[0] == "failed"


def test_source_null_lease_recovers_without_resetting_budget(setup):
    db, _ = setup
    source_queue.enqueue(db, [job("orphaned-source", "https://example.test/job")])
    source_queue.claim(db)
    db.execute("UPDATE application_sources SET lease_until=NULL")
    db.commit()
    assert source_queue.claim(db)["attempts"] == 2
    db.execute("UPDATE application_sources SET lease_until=NULL,attempts=3")
    db.commit()
    assert source_queue.claim(db) is None
    assert db.execute("SELECT state FROM application_sources").fetchone()[0] == "failed"


def test_pending_overnight_repair_quarantines_pipeline_before_any_work(setup):
    db, path = setup
    source_queue.enqueue(db, [job("quarantined", "https://example.test/job")])
    booklet.write_private(config.ROOT / "private" / "overnight-monitor" / "repair-pending.json",
                          {"state": "repairing"})
    async def forbidden(*args, **kwargs):
        pytest.fail("A quarantined pipeline cannot touch the browser")
    result = pipeline.run_cycle(db, path, resolver=forbidden, runner=forbidden)
    assert result["skipped"] == "Overnight repair requires validated recovery"
    assert db.execute("SELECT attempts FROM application_sources").fetchone()[0] == 0
