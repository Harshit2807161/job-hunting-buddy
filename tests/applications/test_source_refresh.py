import asyncio
import hashlib
import json
import time

import pytest

from jhb import config, eligibility
from jhb.applications import boards, booklet, greenhouse_source, pipeline, queue, source_refresh
from tests.applications.test_pipeline import setup, job

URL = "https://jobs.ashbyhq.com/example/19eb22cd-9540-49ed-840b-6422714413b5"


def description(url=URL, **changes):
    text = "Python backend engineering role in United States. Annual base salary range: $114,000 - $148,000. No security clearance required."
    return {"status": "verified", "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "source_url": url, "job_identity": list(boards.job_identity(url)), "retrieved_at": time.time(),
            "country_context": "United States", **changes}


def outcome(**changes):
    return {"state": "not_greenhouse", "source_url": URL, "application_url": URL,
            "final_url": URL, "board_type": "ashby", "verified_job_description": description(), **changes}


@pytest.mark.parametrize("cache", ["job", "persisted"])
def test_fresh_integrity_bound_cache_avoids_both_public_get_and_mcp(setup, cache):
    _, _ = setup
    candidate = {"url": URL}
    proof = description()
    if cache == "job": candidate["verified_job_description"] = proof
    else:
        path = config.ROOT / "private" / "applications" / boards.application_hash(URL) / "public-job-context.json"
        booklet.write_private(path, {"verified_job_description": proof})
    def forbidden(*args, **kwargs): pytest.fail("Fresh proof must not cause network/browser access")
    result = asyncio.run(source_refresh.refresh(candidate, fetcher=forbidden, resolver=forbidden))
    assert result["state"] == "verified" and result["context"]["verified_job_description"] == proof
    assert result["context"]["country_context"] == "United States"
    assert result["context"]["advertised_salary_ranges"]


def test_stale_description_refreshes_once_using_isolated_exact_job_and_no_second_get(setup, monkeypatch):
    calls = []
    candidate = {"url": URL, "verified_job_description": description(retrieved_at=time.time()-86401)}
    def get(*args, **kwargs):
        calls.append("public")
        raise ValueError("Synthetic JavaScript-only job page")
    async def mcp(job, **kwargs):
        calls.append("isolated_mcp")
        assert job["url"] == URL and kwargs == {"timeout": 60}
        return outcome()
    result = asyncio.run(source_refresh.refresh(candidate, fetcher=get, resolver=mcp))
    assert calls == ["public", "isolated_mcp"] and result["state"] == "verified"
    monkeypatch.setattr(eligibility, "fetch_description", lambda *a, **k: pytest.fail("Eligibility repeated the GET"))
    assert eligibility.assess_job({**candidate, **{"verified_job_description": result["context"]["verified_job_description"]}})["state"] == "eligible"


@pytest.mark.parametrize("result,state", [
    (outcome(application_url=URL.replace("example", "another")), "unsupported"),
    (outcome(verified_job_description=description(URL.replace("example", "another"))), "unsupported"),
    (outcome(state="ambiguous"), "unsupported"),
    (outcome(closed=True), "skipped"),
    (outcome(closed=True, source_url="https://unrelated.test"), "unsupported"),
    (outcome(state="blocked", handoff="waiting_login"), "waiting_login"),
    (outcome(state="blocked", handoff="waiting_captcha"), "waiting_captcha"),
    (outcome(verified_job_description=None), "unsupported"),
])
def test_mismatched_closed_blocked_or_unverified_source_never_asks_candidate_facts(setup, result, state):
    async def mcp(*args, **kwargs): return result
    refreshed = asyncio.run(source_refresh.refresh({"url": URL}, fetcher=lambda *a, **k: None, resolver=mcp))
    assert refreshed["state"] == state and refreshed["missing"] == [] and refreshed["filled"] == []
    assert refreshed["retryable"] is False


@pytest.mark.parametrize("error,retry,state", [(TimeoutError, True, "failed"), (ConnectionError, True, "failed"),
                                               (ValueError, False, "unsupported")])
def test_transient_transport_and_technical_handoffs_are_distinct(setup, error, retry, state):
    async def mcp(*args, **kwargs): raise error("Synthetic detail must remain private")
    result = asyncio.run(source_refresh.refresh({"url": URL}, fetcher=lambda *a, **k: None, resolver=mcp))
    assert result["state"] == state and result["retryable"] is retry and result["missing"] == []
    assert "Synthetic detail" not in json.dumps(result)


def test_default_pipeline_refreshes_stale_source_then_passes_verified_country_salary_to_worker(setup, monkeypatch):
    conn, book = setup
    queue.enqueue(conn, [{"url": URL, "dedupe_hash": "old", "title": "Software Engineer",
                          "verified_job_description": description(retrieved_at=time.time()-86401)}])
    monkeypatch.setattr(eligibility, "fetch_description", lambda *a, **k: None)
    seen = []
    async def mcp(*args, **kwargs): return outcome()
    async def worker(candidate, *args, **kwargs):
        seen.append(candidate)
        assert candidate["work_country"] == "United States"
        assert candidate["advertised_salary_ranges"]
        assert eligibility.assess_job(candidate)["state"] == "eligible"
        return {"state": "waiting_review", "events": [], "filled": [], "missing": []}, book.parent / "review.html"
    from jhb.applications import worker as worker_module
    monkeypatch.setattr(worker_module, "run_job", worker)
    result = pipeline.run_cycle(conn, book, resolver=mcp)
    assert result["applications_prepared"] == len(seen) == 1 and result["pending_question_ids"] == []
    directory = config.ROOT / "private" / "applications" / boards.application_hash(URL)
    saved = json.loads((directory / "public-job-context.json").read_text())
    assert saved["country_context"] == "United States" and saved["advertised_salary_ranges"]
    assert json.loads((directory / "source-refresh.json").read_text())["source_refresh"]["method"] == "isolated_playwright_mcp"


def test_ambiguous_refreshed_country_cannot_reuse_stale_us_authorization(setup, monkeypatch):
    conn, book = setup
    queue.enqueue(conn, [{"url": URL, "dedupe_hash": "synthetic-old-source", "title": "Software Engineer", "work_country": "United States",
                          "locations": ["United States"],
                          "verified_job_description": description(retrieved_at=time.time()-86401)}])
    text = "Python backend engineering role available in United States or Canada."
    latest = description(text=text, sha256=hashlib.sha256(text.encode()).hexdigest(), country_context=None)
    monkeypatch.setattr(eligibility, "fetch_description", lambda *a, **k: None)
    async def mcp(*args, **kwargs): return outcome(verified_job_description=latest)
    from jhb.applications import worker as worker_module
    from jhb.applications.planner import deterministic_plan
    class Form:
        blocked_requests = 0
        values = []
        def allowed_url(self, url): return True
        async def open(self, url): pass
        async def observe(self):
            return {"fields": [{"ref": "authorization", "label": "Work authorization", "type": "combobox", "required": True}],
                    "buttons": [{"ref": "submit", "label": "Submit application"}]}
        async def fill(self, field, value): self.values.append(value)
    form = Form()
    seen = []
    async def prepare(candidate, *args, **kwargs):
        seen.append(candidate)
        assert candidate["work_country"] is None
        # Exercise the real observer/planner: even old US location metadata
        # cannot turn an ambiguous current jurisdiction into a verified answer.
        result, _ = await worker_module.prepare(None, candidate, {
            "eligibility.authorized_us": booklet.answer(True, "Synthetic verified US authorization"),
            "eligibility.authorized_canada": booklet.answer(False, "Synthetic verified Canadian authorization")},
            deterministic_plan, None, cli_actions=form)
        return result, book.parent / "review.html"
    monkeypatch.setattr(worker_module, "run_job", prepare)
    result = pipeline.run_cycle(conn, book, resolver=mcp)
    assert result["applications_prepared"] == len(seen) == 1
    assert result["states"] == {"waiting_input": 1} and form.values == []
    handoff = next(iter(booklet.load(book)["question_handoffs"].values()))
    assert handoff["question"] == "Work authorization" and handoff["status"] == "pending"


@pytest.mark.parametrize("result,expected", [(outcome(state="error"), "retry"),
                                           (outcome(verified_job_description=None), "unsupported"),
                                           (outcome(closed=True), "skipped")])
def test_default_pipeline_never_opens_filler_for_source_only_technical_handoffs(setup, monkeypatch, result, expected):
    conn, book = setup
    queue.enqueue(conn, [job("missing", URL)])
    monkeypatch.setattr(eligibility, "fetch_description", lambda *a, **k: None)
    async def mcp(*args, **kwargs): return result
    async def forbidden(*args, **kwargs): pytest.fail("Unverified source opened candidate browser")
    from jhb.applications import worker
    monkeypatch.setattr(worker, "run_job", forbidden)
    summary = pipeline.run_cycle(conn, book, resolver=mcp)
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == expected
    assert summary["pending_question_ids"] == [] and summary["question_handoffs"] == 0


def test_observed_official_jobposting_retains_single_country_metadata():
    observed = greenhouse_source._observed_description({"url": URL, "job_postings": [
        {"url": URL, "title": "Synthetic Engineer", "description": description()["text"],
         "jobLocation": {"address": {"addressCountry": "US"}}}]})
    assert observed["country_context"] == "United States"


@pytest.mark.parametrize("change,requeue", [(None, True), ("candidate_question", False), ("retained_answer", False),
                                          ("exhausted", False), ("wrong_job", False), ("other_verification", False),
                                          ("approval", False), ("attempt", False), ("terminal_marker", False), ("optional_question", False)])
def test_only_old_zero_field_description_handoffs_requeue_once_without_resetting_budget(setup, change, requeue):
    conn, _ = setup
    queue.enqueue(conn, [job("old-description", URL)])
    item = queue.claim(conn)
    packet = {"job": item["job"], "state": "waiting_input", "missing": [], "filled": [],
              "eligibility": {"verification": {"kind": "job_description", "error_type": "ValueError"}}}
    if change == "candidate_question": packet["missing"] = [{"question": "A new factual question"}]
    elif change == "retained_answer": packet["filled"] = [{"ref": "email", "value": "synthetic@example.test"}]
    elif change == "wrong_job": packet["job"] = {**packet["job"], "url": URL.replace("example", "another")}
    elif change == "other_verification": packet["eligibility"]["verification"]["kind"] = "google_account"
    elif change == "terminal_marker": packet["runtime_click_started"] = True
    elif change == "optional_question": packet["optional_questions"] = [{"question": "Why this company?"}]
    elif change == "approval":
        from jhb.applications import approvals
        approvals.initialize(conn)
        conn.execute("INSERT INTO application_approvals VALUES('fixture',?,'revoked',0,0,'fixture','private/auth.json',NULL)", (item["job_hash"],)); conn.commit()
    elif change == "attempt":
        from jhb.applications import overnight
        overnight.initialize(conn)
        conn.execute("INSERT INTO authorized_submission_attempts(job_hash,authorization_id,application_url,state,started_at,updated_at,attempt_path) "
                     "VALUES(?,'fixture',?,'uncertain',0,0,'private/attempt.json')", (item["job_hash"], URL)); conn.commit()
    directory = config.ROOT / "private" / "applications" / item["job_hash"]
    booklet.write_private(directory / "packet.json", packet)
    queue.finish(conn, item["job_hash"], "waiting_input", directory / "review.html")
    if change == "exhausted": conn.execute("UPDATE applications SET attempts=3"); conn.commit()
    assert pipeline.recover_description_handoffs(conn) == int(requeue)
    row = conn.execute("SELECT state,attempts FROM applications").fetchone()
    assert row["state"] == ("queued" if requeue else "waiting_input")
    assert row["attempts"] == (3 if change == "exhausted" else 1)
    if requeue:
        conn.execute("UPDATE applications SET state='waiting_input'"); conn.commit()
        assert pipeline.recover_description_handoffs(conn) == 0
