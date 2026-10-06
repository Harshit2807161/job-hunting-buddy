import asyncio

import pytest

from jhb.applications import booklet, worker


@pytest.mark.parametrize("record,excluded", [
    ({"status": "verified", "source": "Synthetic explicit user rejection"}, True),
    ({"status": "needs_input", "source": "Unconfirmed suggestion"}, False),
    ({"status": "verified", "source": ""}, False),
])
def test_exclusion_requires_exact_identity_and_verified_user_evidence(record, excluded):
    job = {"dedupe_hash": "a"*64}
    book = {"job_exclusions": {"a"*64: record}}
    assert booklet.job_excluded(book, job) is excluded
    assert not booklet.job_excluded(book, {"dedupe_hash": "b"*64})


def test_explicit_user_rejection_stops_direct_worker_before_jd_lookup_or_browser(tmp_path, monkeypatch):
    from jhb import eligibility
    monkeypatch.setattr(eligibility, "assess_job", lambda *args: pytest.fail("Excluded job must not read JD or open browser"))
    book = {"job_exclusions": {"a"*64: {"status": "verified", "source": "Synthetic explicit rejection"}}}
    job = {"dedupe_hash": "a"*64, "company": "Synthetic", "title": "Engineer",
           "url": "https://job-boards.greenhouse.io/example/jobs/1"}
    result, packet = asyncio.run(worker.run_job(job, book, artifacts=tmp_path))
    assert result["state"] == "skipped" and result["filled"] == [] and packet.exists()
