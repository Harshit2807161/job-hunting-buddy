"""Generic preparation cannot replace the canonical candidate-reviewed draft."""
import asyncio
import hashlib
import json
import sqlite3
import time

import pytest

from jhb import config, eligibility
from jhb.applications import approvals, boards, booklet, queue, role_fit, worker


@pytest.fixture
def retained(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    url = "https://job-boards.greenhouse.io/synthetic/jobs/12345"
    key = boards.application_hash(url)
    text = "Build Python backend software for commercial customers."
    description = {"status": "verified", "text": text, "retrieved_at": time.time(),
                   "source_url": "https://boards-api.greenhouse.io/v1/boards/synthetic/jobs/12345",
                   "sha256": hashlib.sha256(text.encode()).hexdigest()}
    job = {"dedupe_hash": key, "url": url, "title": "Software Engineer", "company": "Synthetic",
           "role_classes": "swe", "verified_job_description": description}
    book = {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}}
    # The DB's canonical packet may be in a manually prepared multi-board path.
    directory = tmp_path / "private" / "multi-board" / "applications" / key
    packet = {"job": job, "state": "waiting_review", "submitted": False, "selected_role": "sde",
              "missing": [], "filled": [{"ref": "motivation", "question": "Why us?",
                  "key": "custom.motivation", "value": "Candidate manually edited this wording",
                  "source": "Synthetic retained manual edit"}],
              "role_fit": {"state": "eligible", "review_notes": ["Original disclosed experience gap"]}}
    booklet.write_private(directory / "packet.json", packet)
    (directory / "review.html").write_text("Retained candidate review")
    (directory / "browser.png").write_bytes(b"Synthetic retained review screenshot")
    booklet.write_private(directory / "role-fit.json", packet["role_fit"])
    booklet.write_private(directory / "eligibility.json", {"state": "eligible", "description": description})
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(tmp_path / "data" / "jobs.sqlite3");conn.row_factory = sqlite3.Row
    queue.initialize(conn);approvals.initialize(conn)
    conn.execute("INSERT INTO applications(job_hash,job_json,state,updated_at,packet) VALUES(?,?,'waiting_review',?,?)",
                 (key, json.dumps(job), int(time.time()), str(directory / "review.html")))
    conn.commit()
    monkeypatch.setattr(role_fit, "assess", lambda *a, **kw: pytest.fail("Reviewed draft reached role-fit reassessment"))
    monkeypatch.setattr(booklet, "for_role", lambda *a, **kw: pytest.fail("Reviewed draft reached answer planning/refill"))
    before = {p: p.read_bytes() for p in directory.iterdir()}
    yield conn, job, book, directory, before
    conn.close()


@pytest.mark.parametrize("approval_state", [None, "approved", "submitting", "needs_review", "expired"])
@pytest.mark.parametrize("application_state", ["waiting_review", "retry", "running"])
def test_generic_prepare_keeps_canonical_packet_manual_edits_and_authority(retained, approval_state, application_state):
    conn, job, book, directory, before = retained
    conn.execute("UPDATE applications SET state=?", (application_state,))
    if approval_state:
        conn.execute("INSERT INTO application_approvals VALUES('synthetic',?,?,1,2,'revision','unused',NULL)",
                     (job["dedupe_hash"], approval_state))
    conn.commit()
    old_row = dict(conn.execute("SELECT * FROM applications").fetchone())
    result, path = asyncio.run(worker.run_job(job, book, planner_name="deterministic"))
    assert result["preparation_preserved"] is True and result["state"] == "waiting_review"
    assert result["filled"][0]["value"] == "Candidate manually edited this wording"
    assert result["role_fit"]["review_notes"] == ["Original disclosed experience gap"]
    assert path == directory / "review.html"
    assert all(p.read_bytes() == contents for p, contents in before.items())
    assert not (config.ROOT / "private" / "applications" / job["dedupe_hash"]).exists()
    assert dict(conn.execute("SELECT * FROM applications").fetchone()) == old_row
    if approval_state:
        assert conn.execute("SELECT state FROM application_approvals").fetchone()[0] == approval_state


@pytest.mark.parametrize("restriction", ["US citizenship is required.", "Must hold an active TS/SCI clearance.",
                                       "We cannot provide visa sponsorship."])
def test_fresh_objective_restrictions_still_block_reviewed_draft_without_replacing_it(retained, restriction):
    _, job, book, directory, before = retained
    description = job["verified_job_description"]
    description.update(text=restriction, sha256=hashlib.sha256(restriction.encode()).hexdigest())
    result, path = asyncio.run(worker.run_job(job, book))
    assert result["state"] == "skipped" and result["eligibility"]["findings"]
    assert path == directory / "review.html"
    assert all(p.read_bytes() == contents for p, contents in before.items())


@pytest.mark.parametrize("block", ["history", "candidate_exclusion", "description_unavailable"])
def test_review_preservation_does_not_bypass_other_preparation_guards(retained, monkeypatch, block):
    _, job, book, directory, before = retained
    if block == "history":
        from jhb.applications import historical
        monkeypatch.setattr(historical, "cached_match", lambda job: {"disposition": "hold", "reason": "Synthetic historical match"})
    elif block == "candidate_exclusion":
        book["job_exclusions"] = {job["dedupe_hash"]: {"status": "verified", "source": "Explicit synthetic exclusion"}}
    else:
        monkeypatch.setattr(eligibility, "assess_job", lambda job: {"state": "waiting_input",
            "reason": "Official description unavailable", "policy": eligibility.POLICY_ID})
    result, path = asyncio.run(worker.run_job(job, book))
    assert result["state"] == {"history": "history_hold", "candidate_exclusion": "skipped", "description_unavailable": "waiting_input"}[block]
    assert path == directory / "review.html"
    assert all(p.read_bytes() == contents for p, contents in before.items())


def test_invalidated_canonical_packet_cannot_be_replaced_or_silently_revived(retained):
    conn, job, book, directory, _ = retained
    conn.execute("INSERT INTO application_approvals VALUES('synthetic',?,'needs_review',1,2,'revision','unused',NULL)",
                 (job["dedupe_hash"],));conn.execute("UPDATE applications SET state='skipped'");conn.commit()
    packet = json.loads((directory / "packet.json").read_text());packet.update(state="skipped", filled=[])
    booklet.write_private(directory / "packet.json", packet)
    before = (directory / "packet.json").read_bytes()
    result, path = asyncio.run(worker.run_job(job, book))
    assert result["state"] == "unsupported" and result["preparation_preserved"] is True
    assert (directory / "packet.json").read_bytes() == before
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "skipped"
    assert conn.execute("SELECT state FROM application_approvals").fetchone()[0] == "needs_review"


def test_stale_pipeline_queue_cannot_refresh_and_overwrite_a_reviewed_packet(retained, monkeypatch):
    from jhb.applications import pipeline, source_refresh
    conn, job, book, directory, before = retained
    book_path = config.ROOT / "private" / "book.json"
    booklet.write_private(book_path, book)
    conn.execute("UPDATE applications SET state='queued'");conn.commit()
    async def forbidden(*args, **kwargs):
        pytest.fail("Retained draft reached source re-preparation")
    monkeypatch.setattr(source_refresh, "refresh", forbidden)
    monkeypatch.setenv("JHB_REQUIRE_PORTAL_APPROVAL", "1")
    monkeypatch.setenv("JHB_PORTAL_SUBMISSIONS_ENABLED", "0")
    result = asyncio.run(pipeline.cycle(conn, book_path, resolver=forbidden,
                                       source_limit=1, application_limit=1))
    assert result["states"] == {"waiting_review": 1}
    assert result["applications_prepared"] == 0
    assert all(p.read_bytes() == contents for p, contents in before.items())
    assert conn.execute("SELECT packet FROM applications").fetchone()[0] == str(directory / "review.html")
