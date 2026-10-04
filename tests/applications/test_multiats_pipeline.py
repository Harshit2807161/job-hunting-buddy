import asyncio
import hashlib
import json
import time

import pytest

from jhb.applications import boards, pipeline, queue, source_queue
from tests.applications.test_pipeline import setup, job

ASHBY = "https://jobs.ashbyhq.com/example/19eb22cd-9540-49ed-840b-6422714413b5"
LEVER = "https://jobs.lever.co/example/19eb22cd-9540-49ed-840b-6422714413b5"


def description(url=ASHBY):
    text = "Requirements: strong Python and backend engineering skills."
    return {"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest(), "status": "verified",
            "source_url": url, "job_identity": list(boards.job_identity(url)), "retrieved_at": int(time.time())}


def test_supported_board_dispatch_preserves_exact_description_and_deduplicates_wrappers(setup, monkeypatch):
    db, path = setup
    monkeypatch.setitem(boards.ADAPTERS, "lever", {**boards.ADAPTERS["lever"], "prep_enabled": False})
    verified = description()
    source_queue.enqueue(db, [job("one", "https://example.test/one"), job("two", "https://example.test/two"),
                              job("unimplemented", LEVER)])
    async def resolver(candidate, **kwargs):
        return {"state": "not_greenhouse", "board_type": "lever", "application_url": LEVER} if "lever" in candidate["url"] else {
            "state": "not_greenhouse", "board_type": "ashby", "application_url": ASHBY,
            "verified_job_description": verified}
    seen = []
    async def runner(candidate, book, **kwargs):
        seen.append(candidate)
        assert candidate["board_type"] == "ashby"
        assert candidate["verified_job_description"] == verified
        assert candidate["dedupe_hash"] == boards.application_hash(ASHBY)
        return {"state": "waiting_review", "events": [], "filled": [], "missing": []}, path.parent / "review.html"
    summary = pipeline.run_cycle(db, path, resolver=resolver, runner=runner)
    assert summary["applications_queued"] == summary["applications_prepared"] == len(seen) == 1
    assert summary["boards"] == {"ashby": 2, "lever": 1}
    assert db.execute("SELECT COUNT(*) FROM source_application_routes").fetchone()[0] == 2
    assert pipeline.run_cycle(db, path, resolver=resolver, runner=runner)["applications_prepared"] == 0


@pytest.mark.parametrize("state", ["submitted", "waiting_review", "submission_uncertain", "skipped"])
def test_replaying_old_supported_classification_never_resets_protected_application(setup, state):
    db, path = setup
    original = job("old", "https://example.test/old")
    source_queue.enqueue(db, [original])
    item = source_queue.claim(db)
    outcome = {"state": "not_greenhouse", "board_type": "ashby", "application_url": ASHBY}
    evidence = pipeline._source_artifact(item, outcome)
    source_queue.finish(db, item["source_job_hash"], "resolved", board="ashby", application_url=ASHBY, evidence_path=evidence)
    queue.enqueue(db, [job("existing", ASHBY)])
    claimed = queue.claim(db); queue.finish(db, claimed["job_hash"], state)
    async def forbidden(*args, **kwargs):
        pytest.fail("A protected application was replayed")
    summary = pipeline.run_cycle(db, path, resolver=forbidden, runner=forbidden)
    assert summary["applications_prepared"] == 0
    assert db.execute("SELECT state FROM applications").fetchone()[0] == state
    assert db.execute("SELECT COUNT(*) FROM source_application_routes").fetchone()[0] == 1


def test_adapter_enablement_consumes_previously_resolved_sources_once(setup, monkeypatch):
    db, path = setup
    monkeypatch.setitem(boards.ADAPTERS, "lever", {**boards.ADAPTERS["lever"], "prep_enabled": False})
    source_queue.enqueue(db, [job("classified-earlier", LEVER)])
    item = source_queue.claim(db)
    outcome = {"state": "not_greenhouse", "board_type": "lever", "application_url": LEVER}
    evidence = pipeline._source_artifact(item, outcome)
    source_queue.finish(db, item["source_job_hash"], "resolved", board="lever", application_url=LEVER, evidence_path=evidence)
    async def runner(candidate, book, **kwargs):
        assert candidate["board_type"] == "lever"
        return {"state": "waiting_review", "events": [], "filled": [], "missing": []}, path.parent / "review.html"
    assert pipeline.run_cycle(db, path, runner=runner)["applications_prepared"] == 0
    monkeypatch.setitem(boards.ADAPTERS, "lever", {**boards.ADAPTERS["lever"], "prep_enabled": True})
    summary = pipeline.run_cycle(db, path, runner=runner)
    assert summary["sources_replayed"] == summary["applications_queued"] == summary["applications_prepared"] == 1
    assert pipeline.run_cycle(db, path, runner=runner)["applications_prepared"] == 0


@pytest.mark.parametrize("outcome", [
    {"state": "not_greenhouse", "board_type": "ashby", "application_url": ASHBY, "closed": True},
    {"state": "not_greenhouse", "board_type": "lever", "application_url": ASHBY},
    {"state": "not_greenhouse", "board_type": "ashby", "application_url": "https://jobs.ashbyhq.com/example"},
])
def test_closed_mismatched_board_and_non_job_sources_do_not_enter_preparation(setup, outcome):
    db, path = setup
    source_queue.enqueue(db, [job("unsafe", "https://example.test/unsafe")])
    async def resolver(*args, **kwargs):
        return outcome
    async def forbidden(*args, **kwargs):
        pytest.fail("Unsafe source dispatched")
    assert pipeline.run_cycle(db, path, resolver=resolver, runner=forbidden)["applications_queued"] == 0


def test_authenticated_linkedin_hook_runs_first_only_for_exact_linkedin_source(setup):
    db, path = setup
    source_queue.enqueue(db, [job("linkedin", "https://www.linkedin.com/jobs/view/1234567890"),
                              job("other", "https://example.test/company")])
    async def isolated(candidate, **kwargs):
        assert "linkedin.com" not in candidate["url"]
        return {"state": "blocked", "board_type": "linkedin" if "linkedin.com" in candidate["url"] else "unknown",
                "handoff": "waiting_login"}
    local_calls = []
    async def authenticated(candidate, *, isolated_outcome, timeout):
        local_calls.append(candidate["url"])
        assert isolated_outcome is None and timeout == 90
        return {"state": "not_greenhouse", "board_type": "ashby", "application_url": ASHBY,
                "verified_job_description": description()}
    async def runner(*args, **kwargs):
        return {"state": "waiting_review", "events": [], "filled": [], "missing": []}, path.parent / "review.html"
    result = pipeline.run_cycle(db, path, resolver=isolated, runner=runner, authenticated_resolver=authenticated)
    assert local_calls == ["https://www.linkedin.com/jobs/view/1234567890"]
    assert result["applications_prepared"] == 1
    assert db.execute("SELECT COUNT(*) FROM application_sources WHERE state='waiting_login'").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["error", "ambiguous", "exception"])
def test_authenticated_linkedin_failure_falls_back_to_isolated_source_checker(setup, failure):
    db, path = setup
    source_queue.enqueue(db, [job("linkedin-fallback", "https://www.linkedin.com/jobs/view/1234567890")])
    calls = []
    async def authenticated(candidate, **kwargs):
        calls.append("local")
        if failure == "exception":
            raise TimeoutError("Synthetic local source timeout")
        return {"state": failure}
    async def isolated(candidate, **kwargs):
        calls.append("isolated")
        return {"state": "not_greenhouse", "board_type": "ashby", "application_url": ASHBY}
    async def runner(*args, **kwargs):
        return {"state": "waiting_review", "events": [], "filled": [], "missing": []}, path.parent / "review.html"
    result = pipeline.run_cycle(db, path, resolver=isolated, runner=runner, authenticated_resolver=authenticated)
    assert calls == ["local", "isolated"] and result["applications_prepared"] == 1


@pytest.mark.parametrize("error,retry", [("URLError", True), ("TimeoutError", True), ("OSError", True),
                                        ("ValueError", False), ("HTTPError", False)])
def test_only_description_network_failures_retry_without_candidate_question(setup, error, retry):
    db, path = setup
    queue.enqueue(db, [job("description-network", ASHBY)])
    async def runner(*args, **kwargs):
        return {"state": "waiting_input", "missing": [], "events": [], "filled": [],
                "eligibility": {"verification": {"kind": "job_description", "error_type": error}}}, path.parent / "review.html"
    result = pipeline.run_cycle(db, path, runner=runner)
    assert db.execute("SELECT state FROM applications").fetchone()[0] == ("retry" if retry else "waiting_input")
    assert result["technical_retries"] == int(retry)
    assert result["pending_question_ids"] == []


def test_local_linkedin_enablement_reconsiders_old_isolated_handoff_once(setup, monkeypatch):
    db, path = setup
    source_queue.enqueue(db, [job("old-linkedin", "https://www.linkedin.com/jobs/view/1234567890")])
    item = source_queue.claim(db)
    source_queue.finish(db, item["source_job_hash"], "waiting_login", board="linkedin")
    assert pipeline.recover_authenticated_linkedin_sources(db) == 0
    monkeypatch.setenv("JHB_LINKEDIN_LOCAL_RESOLUTION", "1")
    assert pipeline.recover_authenticated_linkedin_sources(db) == 1
    item = source_queue.claim(db)
    assert item["attempts"] == 1
    source_queue.finish(db, item["source_job_hash"], "waiting_login", board="linkedin")
    assert pipeline.recover_authenticated_linkedin_sources(db) == 0
