import asyncio
import hashlib
import json
import sqlite3
import time

import pytest

from jhb import eligibility
from jhb.applications import booklet, questions, queue, worker
from jhb.sources import simplify


@pytest.mark.parametrize("text", [
    "Applicants must be a U.S. citizen.", "U.S. citizenship is required.",
    "US citizens only.", "British citizenship required for this role.",
    "Must have an active security clearance.",
    "Candidates must be eligible to obtain and maintain a Secret clearance.",
    "Ability to obtain and maintain TS/SCI with a polygraph.",
    "Must be willing to undergo a polygraph examination.",
    "Security clearance: Secret", "Current TS/SCI clearance",
    "No security clearance required to apply, but must be eligible to obtain TS/SCI.",
    "Citizenship optional, security clearance required.",
    "<p>Minimum requirements:</p><ul><li>US citizenship required</li></ul>",
    "No clearance required and citizenship required.",
    "US citizenship is required; all applicants must disclose status.",
    "Must be a US citizen or permanent resident and must obtain TS/SCI.",
])
def test_required_conditions_are_excluded(text):
    assert eligibility.restrictions(text)


@pytest.mark.parametrize("text", [
    "No security clearance is required.", "Security clearance is not required.",
    "Security clearance: None", "Citizenship optional.", "Citizenship disclosure required.",
    "Do you hold U.S. citizenship? Optional demographic survey.",
    "We hire without regard to citizenship, race, gender, or national origin.",
    "All applicants must be authorized to work in the United States.",
    "The successful applicant must pass a background check.",
    "No polygraph required.", "Applicants need not hold an active clearance.",
    "We do not require citizenship or clearance.", "Security clearance preferred.",
    "Medical clearance required for field work.",
    "Candidates will work with our security-cleared customers.",
    "All applicants must disclose citizenship status.",
    "Must be US citizen or permanent resident.",
    "US citizens or lawful permanent residents only.",
    "No US citizenship required.",
    "No citizenship or security clearance is required.",
])
def test_nonrequirements_are_not_excluded(text):
    assert eligibility.restrictions(text) == []


def test_negated_citizenship_does_not_hide_separate_clearance_requirement():
    findings = eligibility.restrictions("No citizenship required and ability to obtain TS/SCI is required.")
    assert {item["category"] for item in findings} == {"security_clearance"}


def job(description="Ordinary application development position."):
    item = {"dedupe_hash": "a"*64, "title": "Software Engineer", "company": "Synthetic",
            "url": "https://job-boards.greenhouse.io/example/jobs/123", "role_classes": "swe"}
    item["verified_job_description"] = {"status": "verified", "text": description,
        "source_url": eligibility.description_url(item), "retrieved_at": time.time(),
        "sha256": hashlib.sha256(description.encode()).hexdigest()}
    return item


@pytest.mark.parametrize("text", ["US citizenship required.", "Ability to obtain TS/SCI.", "Must pass a polygraph."])
def test_direct_live_worker_never_constructs_browser_for_excluded_jobs(tmp_path, monkeypatch, text):
    def forbidden(*args, **kwargs):
        pytest.fail("Excluded job must not access the candidate browser")
    monkeypatch.setattr("jhb.applications.cli_browser.BrowserUseCLI", forbidden)
    result, packet = asyncio.run(worker.run_job(job(text), {"roles": {"sde": {}, "ml": {}}}, artifacts=tmp_path))
    assert result["state"] == "skipped"
    assert result["filled"] == result["missing"] == []
    assert packet.exists()
    assert json.loads((packet.parent / "eligibility.json").read_text())["findings"]


def test_unavailable_description_handoffs_before_browser_without_candidate_question(tmp_path, monkeypatch):
    item = job()
    item.pop("verified_job_description")
    def unavailable(*args, **kwargs):
        raise RuntimeError("Do not print sensitive exception contents")
    monkeypatch.setattr(eligibility, "fetch_description", unavailable)
    monkeypatch.setattr("jhb.applications.cli_browser.BrowserUseCLI", lambda: pytest.fail("JD must be verified first"))
    result, packet = asyncio.run(worker.run_job(item, {}, artifacts=tmp_path))
    assert result["state"] == "waiting_input"
    assert result["missing"] == []
    assert "sensitive" not in packet.read_text()


def test_old_wrong_source_and_tampered_snapshots_cannot_bypass_fetch(monkeypatch):
    calls = []
    def fetch(item):
        calls.append(item)
        return job("US citizenship required.")["verified_job_description"]
    monkeypatch.setattr(eligibility, "fetch_description", fetch)
    for change in [{"retrieved_at": 0}, {"source_url": "https://example.test/fake"}, {"text": "Injected replacement"}]:
        item = job()
        item["verified_job_description"].update(change)
        assert eligibility.assess_job(item)["state"] == "skipped"
    assert len(calls) == 3


def test_queue_durably_skips_clearance_title_and_cannot_resume():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    item = job()
    item["title"] = "Software Engineer (TS/SCI with Polygraph)"
    assert queue.enqueue(conn, [item]) == 1
    row = conn.execute("SELECT * FROM applications").fetchone()
    assert row["state"] == "skipped"
    queue.resume(conn, row["job_hash"])
    assert queue.claim(conn) is None
    assert queue.enqueue(conn, [item]) == 0
    conn.close()


def test_answering_old_question_cannot_resume_a_skipped_application(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    item = job()
    queue.enqueue(conn, [item])
    item = queue.claim(conn)["job"]
    queue.finish(conn, item["dedupe_hash"], "skipped", "private/eligibility-review.html")
    path = tmp_path / "book.json"
    booklet.write_private(path, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}, "custom_answers": {}})
    question = questions.collect(item, {"state": "waiting_input", "missing": [
        {"question": "Synthetic employer question?", "ref": "question", "required": True}
    ]}, path)[0]
    questions.answer(question["id"], "Synthetic approved answer", path, connection=conn)
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "skipped"
    assert queue.claim(conn) is None
    conn.close()


def test_phase1_structured_citizenship_rule_and_title_description_exclusions():
    base = {"active": True, "is_visible": True, "company_name": "Synthetic Company",
            "id": "synthetic", "title": "Software Engineer", "locations": ["San Diego, CA"],
            "url": "https://job-boards.greenhouse.io/example/jobs/123"}
    for change in [{"sponsorship": "U.S. Citizenship is Required"},
                   {"title": "Software Engineer TS/SCI"},
                   {"description": "Ability to obtain and maintain a security clearance"}]:
        assert simplify.to_jobs([{**base, **change}]) == []
    assert len(simplify.to_jobs([{**base, "description": "No security clearance required; work authorization required."}])) == 1


def test_official_description_identity_and_redirect_host_are_bounded(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, maximum):
            assert maximum == 1_048_577
            return json.dumps({"id": 999, "content": "Unrelated role"}).encode()
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "https://boards-api.greenhouse.io/v1/boards/example/jobs/123"
            return Response()
    monkeypatch.setattr(eligibility.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError, match="different job"):
        eligibility.fetch_description(job())
    request = eligibility.urllib.request.Request(eligibility.description_url(job()))
    with pytest.raises(ValueError, match="outside"):
        eligibility._OfficialRedirects().redirect_request(request, None, 302, "", {}, "https://example.test/redirect")


def test_html_only_description_is_unavailable_not_verified(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, maximum):
            return json.dumps({"id": 123, "content": "<p> </p><div></div>"}).encode()
    class Opener:
        def open(self, request, timeout): return Response()
    monkeypatch.setattr(eligibility.urllib.request, "build_opener", lambda *args: Opener())
    item = job()
    item.pop("verified_job_description")
    result = eligibility.assess_job(item)
    assert result["state"] == "waiting_input"
    assert result["verification"]["kind"] == "job_description"
    assert result["findings"] == []
