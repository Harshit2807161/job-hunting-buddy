import hashlib
import io
import json
import time

import pytest

from jhb import eligibility
from jhb.applications import boards, job_context

URL = "https://jobs.ashbyhq.com/example/19eb22cd-9540-49ed-840b-6422714413b5"


def response(record):
    return io.BytesIO(('<html><script type="application/ld+json">'+json.dumps(record)+'</script></html>').encode())


def metadata(**changes):
    return {"@type": "JobPosting", "title": "Synthetic Engineer", "identifier": {"value": boards.job_identity(URL)[-1]},
            "description": "<h2>Requirements</h2><p>Strong Python engineering skills. No clearance required.</p>", **changes}


def test_exact_official_jobposting_get_produces_integrity_bound_description():
    calls = []
    def opener(request, **kwargs):
        calls.append((request, kwargs))
        return response(metadata())
    description = job_context.fetch_public_description(URL+"/application?ref=x", opener=opener)
    assert description["source_url"] == URL
    assert description["job_identity"] == list(boards.job_identity(URL))
    assert description["sha256"] == hashlib.sha256(description["text"].encode()).hexdigest()
    assert job_context.valid_description(description, URL) is True
    job = {"url": URL, "verified_job_description": description}
    assert eligibility.assess_job(job)["state"] == "eligible"
    assert calls[0][0].get_method() == "GET" and calls[0][0].data is None
    assert dict(calls[0][0].header_items()) == {"Accept": "text/html"}


@pytest.mark.parametrize("record", [
    metadata(identifier={"value": "a different exact job"}),
    metadata(url=URL.replace("example", "another")),
    metadata(description=""), {"@type": "Organization", "description": "Company branding"},
    [metadata(), metadata()],
])
def test_missing_mismatched_or_ambiguous_job_metadata_stops(record):
    with pytest.raises(ValueError):
        job_context.fetch_public_description(URL, opener=lambda *a, **k: response(record))


@pytest.mark.parametrize("change", [
    {"retrieved_at": "stale"}, {"retrieved_at": "future"}, {"retrieved_at": True},
    {"text": "Modified"}, {"sha256": "bad"}, {"job_identity": ["ashby", "wrong", "id"]},
    {"source_url": "https://attacker.test/job"}, {"status": "needs_input"},
])
def test_description_provenance_never_crosses_job_or_integrity_boundaries(change):
    change = dict(change)
    if change.get("retrieved_at") == "future":
        change["retrieved_at"] = time.time()+3600
    elif change.get("retrieved_at") == "stale":
        change["retrieved_at"] = time.time()-86401
    description = job_context.fetch_public_description(URL, opener=lambda *a, **k: response(metadata()))
    assert job_context.valid_description({**description, **change}, URL) is False


def test_clearance_filter_applies_before_any_non_greenhouse_preparation():
    description = job_context.fetch_public_description(URL, opener=lambda *a, **k: response(metadata(description="Must be able to obtain TS/SCI clearance.")))
    result = eligibility.assess_job({"url": URL, "verified_job_description": description})
    assert result["state"] == "skipped" and result["findings"]


def test_official_response_redirect_to_other_job_is_rejected():
    class Redirected(io.BytesIO):
        def geturl(self):
            return URL.replace("example", "another")
    with pytest.raises(ValueError):
        job_context.fetch_public_description(URL, opener=lambda *a, **k: Redirected(b"{}"))


def test_unknown_url_never_fetches_description():
    with pytest.raises(ValueError):
        job_context.fetch_public_description("https://unknown.test/jobs/1", opener=lambda *a, **k: pytest.fail("HTTP called"))


@pytest.mark.parametrize("country,applicants,expected", [
    ("US", [], "United States"), ("CA", [], "Canada"), ("MX", [], None),
    ("United States", [{"@type": "Country", "name": "Canada"}], None),
    (None, [{"@type": "Country", "name": "United States"}], "United States"),
])
def test_metadata_jurisdiction_uses_explicit_country_fields_only(country, applicants, expected):
    record = metadata(jobLocation={"address": {"addressCountry": country}}, applicantLocationRequirements=applicants)
    description = job_context.fetch_public_description(URL, opener=lambda *a, **k: response(record))
    assert description["country_context"] == expected
