"""Feedback hooks use synthetic packets; no browser, API, mail or submission."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from jhb import config, eligibility
from jhb.applications import attempt_feedback, capture, cli_browser, pipeline, resume_selection, role_fit, worker


def candidate_job(number=1):
    return {"dedupe_hash": f"{number:064x}", "url": f"https://job-boards.greenhouse.io/example/jobs/{number}",
            "title": "Synthetic Engineer", "company": "Example", "role_classes": ["swe"]}


def result(state="waiting_review"):
    return {"state": state, "reason": "Synthetic preparation outcome", "events": [], "filled": [],
            "missing": [{"ref": "new", "question": "New required fact", "required": True}]
                       if state == "waiting_input" else []}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(eligibility, "assess_job", lambda job: {
        "state": "eligible", "reason": "Synthetic eligibility", "policy": "synthetic"})
    monkeypatch.setattr(role_fit, "assess", lambda *args: {"state": "eligible", "reason": "Synthetic role fit"})
    monkeypatch.setattr(resume_selection, "select", lambda *args, **kwargs: {
        "state": "selected", "selected_role": "sde", "reason": "Synthetic upstream document comparison",
        "selected_resume_sha256": "0" * 64})
    monkeypatch.setattr(cli_browser, "BrowserUseCLI", SimpleNamespace)
    async def fresh(*args, **kwargs):
        return {"verified": True}
    monkeypatch.setattr(capture, "fresh", fresh)
    calls = []
    prepared = result()
    async def prepare(*args, **kwargs):
        assert kwargs["cli_actions"].job_hash == args[1]["dedupe_hash"]
        calls.append(args[1]["dedupe_hash"])
        return deepcopy(prepared), kwargs["cli_actions"]
    monkeypatch.setattr(worker, "prepare", prepare)
    return {"book": {"answers": {}, "roles": {"sde": {}, "ml": {}}}, "prepared": prepared, "calls": calls}


def run_pipeline(job, book, *, runner=worker.run_job):
    return pipeline._prepare_one({"job": job, "job_hash": job["dedupe_hash"]}, runner, book,
                                 asyncio.Semaphore(1), 10, "deterministic")


@pytest.mark.parametrize("state", ["waiting_review", "waiting_input", "waiting_login"])
def test_standalone_worker_records_success_and_handoffs_after_persistence(environment, state, monkeypatch):
    environment["prepared"].update(result(state))
    original = attempt_feedback.record_attempt
    persisted = []
    def observe(job, outcome, **kwargs):
        packet = kwargs["packet_path"]
        saved = json.loads(packet.read_text())
        assert saved["state"] == outcome["state"] == state
        assert saved["capture"]["verified"] is True
        persisted.append(packet)
        return original(job, outcome, **kwargs)
    monkeypatch.setattr(attempt_feedback, "record_attempt", observe)
    outcome, packet = asyncio.run(worker.run_job(candidate_job(), environment["book"], planner_name="deterministic"))
    rows = attempt_feedback.records()
    assert len(rows) == len(persisted) == len(environment["calls"]) == 1
    assert rows[0]["outcome"] == outcome["state"] == state
    assert rows[0]["required_unknown_count"] == (1 if state == "waiting_input" else 0)
    assert packet.is_file()


def test_pipeline_owned_worker_emits_one_record_and_actual_rerun_gets_new_token(environment):
    async def run():
        for _ in range(2):
            outcome, _ = await run_pipeline(candidate_job(), environment["book"])
            assert outcome["state"] == "waiting_review"
            assert worker._FEEDBACK_ATTEMPT.get() is None
    asyncio.run(run())
    rows = attempt_feedback.records()
    assert len(rows) == len(environment["calls"]) == 2
    assert len({row["attempt_id"] for row in rows}) == 2


@pytest.mark.parametrize("use_pipeline", [False, True])
def test_telemetry_write_failure_preserves_outcome_without_replay_or_private_error(environment, monkeypatch, caplog, use_pipeline):
    writes = []
    def fail(*args, **kwargs):
        writes.append(1)
        raise OSError("synthetic-secret-account and sensitive credential")
    monkeypatch.setattr(attempt_feedback, "record_attempt", fail)
    operation = run_pipeline(candidate_job(), environment["book"]) if use_pipeline else worker.run_job(
        candidate_job(), environment["book"], planner_name="deterministic")
    outcome, packet = asyncio.run(operation)
    assert outcome["state"] == json.loads(packet.with_name("packet.json").read_text())["state"] == "waiting_review"
    assert not outcome.get("retryable") and not outcome.get("error_kind")
    assert len(writes) == len(environment["calls"]) == 1
    assert "Application attempt feedback unavailable (OSError)" in caplog.text
    assert "synthetic-secret" not in caplog.text and "sensitive credential" not in caplog.text
    assert worker._FEEDBACK_ATTEMPT.get() is None


def test_outer_preparation_exception_records_sanitized_failure_after_packet(environment):
    async def broken(job, book, **kwargs):
        raise TimeoutError("synthetic-private-candidate-answer")
    outcome, packet = asyncio.run(run_pipeline(candidate_job(), environment["book"], runner=broken))
    rows = attempt_feedback.records()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failed" and rows[0]["error_kind"] == "TimeoutError"
    assert rows[0]["retryable"] is True and packet.is_file()
    assert "synthetic-private" not in json.dumps(rows)
    assert "synthetic-private" not in packet.with_name("packet.json").read_text()
    assert outcome["state"] == "failed" and worker._FEEDBACK_ATTEMPT.get() is None


def test_transport_reclassification_records_final_packet_once(environment, monkeypatch):
    monkeypatch.setattr(eligibility, "assess_job", lambda job: {
        "state": "waiting_input", "reason": "Synthetic unavailable description", "policy": "synthetic",
        "verification": {"kind": "job_description", "error_type": "TimeoutError"}})
    outcome, packet = asyncio.run(run_pipeline(candidate_job(), environment["book"]))
    rows = attempt_feedback.records()
    assert len(rows) == 1 and rows[0]["outcome"] == "failed"
    assert outcome["error_kind"] == "job_description_transport" and rows[0]["retryable"] is True
    assert not environment["calls"]
    assert json.loads(packet.with_name("packet.json").read_text())["state"] == "failed"


def test_capture_failure_is_recorded_after_it_invalidates_review(environment, monkeypatch):
    async def unavailable(*args, **kwargs):
        return {"verified": False, "error_kind": "TimeoutError"}
    monkeypatch.setattr(capture, "fresh", unavailable)
    outcome, packet = asyncio.run(worker.run_job(candidate_job(), environment["book"], planner_name="deterministic"))
    rows = attempt_feedback.records()
    assert len(rows) == 1
    assert rows[0]["outcome"] == outcome["state"] == "failed"
    assert rows[0]["error_kind"] == "browser_capture"
    stored = json.loads((attempt_feedback.directory() / rows[0]["job_hash"] / (rows[0]["attempt_id"] + ".json")).read_text())
    assert stored["screenshot_status"] == "failed"


def test_parallel_attempt_contexts_are_independent_and_restore_callers_context(environment, monkeypatch):
    tokens = {}
    async def prepare(*args, **kwargs):
        job_hash = args[1]["dedupe_hash"]
        tokens[job_hash] = worker._FEEDBACK_ATTEMPT.get()
        await asyncio.sleep(0)
        assert worker._FEEDBACK_ATTEMPT.get() == tokens[job_hash]
        return result(), kwargs["cli_actions"]
    monkeypatch.setattr(worker, "prepare", prepare)
    async def run():
        await asyncio.gather(*(run_pipeline(candidate_job(number), environment["book"]) for number in (1, 2)))
        assert worker._FEEDBACK_ATTEMPT.get() is None
    asyncio.run(run())
    rows = attempt_feedback.records()
    assert len(rows) == len(set(tokens.values())) == 2
    assert all(isinstance(token, str) and len(token) == 32 for token in tokens.values())


def test_outer_packet_write_failure_does_not_leak_attempt_context_or_mask_original_error(environment, monkeypatch):
    async def broken(*args, **kwargs):
        raise TimeoutError("synthetic timeout")
    async def no_disk(*args, **kwargs):
        raise OSError("Synthetic packet persistence failure")
    monkeypatch.setattr(worker, "write_packet", no_disk)
    async def run():
        with pytest.raises(OSError, match="packet persistence"):
            await run_pipeline(candidate_job(), environment["book"], runner=broken)
        assert worker._FEEDBACK_ATTEMPT.get() is None
    asyncio.run(run())
    assert attempt_feedback.records() == []
