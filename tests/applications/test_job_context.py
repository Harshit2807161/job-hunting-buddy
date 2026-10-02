import io
import json

import pytest

from jhb.applications import job_context

URL = "https://job-boards.greenhouse.io/example/jobs/123?gh_src=ignored"


def response(data):
    return io.BytesIO(json.dumps(data).encode())


@pytest.mark.parametrize("location,expected", [
    ("Remote, United States", "United States"),
    ("United States of America", "United States"),
    ("Remote - USA", "United States"),
    ("Boston, U.S.", "United States"),
    ("Toronto, Canada", "Canada"),
    ("London, United Kingdom", "United Kingdom"),
    ("Remote - UK", "United Kingdom"),
    ("San Diego, CA", None),
    ("London", None),
    ("Remote", None),
    ("USA / Canada", None),
    ("Canada and United Kingdom", None),
    ("Canada / Mexico", None),
    ("UK or Germany", None),
    ("US, Canada", None),
    ("Remote outside Canada", None),
    ("UKG company", None),
    (None, None),
])
def test_country_context_requires_explicit_single_country(location, expected):
    assert job_context.country_context(location) == expected


def test_fetch_validates_identity_and_uses_fixed_unauthenticated_get():
    calls = []
    def opener(request, **kwargs):
        calls.append((request, kwargs))
        return response({"id": 123, "title": "Synthetic Engineer", "location": {"name": "Remote, Canada"}})
    assert job_context.fetch(URL, opener=opener) == {
        "location": "Remote, Canada", "name": "Synthetic Engineer", "country_context": "Canada",
        "source_url": "https://boards-api.greenhouse.io/v1/boards/example/jobs/123",
    }
    request, options = calls[0]
    assert request.full_url == "https://boards-api.greenhouse.io/v1/boards/example/jobs/123"
    assert request.get_method() == "GET"
    assert request.data is None
    assert dict(request.header_items()) == {"Accept": "application/json"}
    assert options == {"timeout": 10}


@pytest.mark.parametrize("url", [
    "https://linkedin.com/jobs/view/123", "http://job-boards.greenhouse.io/example/jobs/123",
    "https://job-boards.greenhouse.io.attacker.example/example/jobs/123",
    "https://user:password@job-boards.greenhouse.io/example/jobs/123",
    "https://job-boards.eu.greenhouse.io/example/jobs/123", "https://127.0.0.1/example/jobs/123",
    "https://job-boards.greenhouse.io/example/jobs/not-a-number", None,
])
def test_invalid_or_non_global_url_never_calls_http(url):
    assert job_context.fetch(url, opener=lambda *a, **kw: pytest.fail("HTTP called")) == {}


@pytest.mark.parametrize("data", [
    {"id": 124, "title": "Wrong job", "location": {"name": "USA"}},
    {"id": True, "location": {"name": "USA"}},
    {"title": "No job identity"}, [],
    {"id": 123, "location": "Canada"},
    {"id": 123, "location": {"name": ["USA", "Canada"]}},
    {"id": 123, "title": None, "location": {"name": "USA"}},
])
def test_identity_mismatch_and_malformed_payload_fail_closed(data):
    assert job_context.fetch(URL, opener=lambda *a, **kw: response(data)) == {}


def test_body_cap_is_enforced_before_json_parsing():
    class Oversized(io.BytesIO):
        def read(self, size=-1):
            assert size == job_context.MAX_BYTES + 1
            return b" " * size
    assert job_context.fetch(URL, opener=lambda *a, **kw: Oversized()) == {}


def test_http_and_encoding_failures_provide_no_metadata():
    def fail(*a, **kw):
        raise OSError("synthetic network failure")
    assert job_context.fetch(URL, opener=fail) == {}
    assert job_context.fetch(URL, opener=lambda *a, **kw: io.BytesIO(b"invalid JSON")) == {}
    assert job_context.fetch(URL, opener=lambda *a, **kw: io.BytesIO(b"\xff")) == {}


def test_city_only_metadata_does_not_assume_a_country():
    data = {"id": 123, "title": "Synthetic Engineer", "location": {"name": "San Diego, CA"}}
    assert job_context.fetch(URL, opener=lambda *a, **kw: response(data))["country_context"] is None


def test_redirected_response_metadata_is_rejected():
    class Redirected(io.BytesIO):
        def geturl(self):
            return "https://unrelated.example/jobs/123"
    assert job_context.fetch(URL, opener=lambda *a, **kw: Redirected(b'{}')) == {}


def test_salary_range_from_encoded_public_job_description_is_preserved():
    def opener(request, **kwargs):
        return response({"id": 123, "title": "Synthetic Engineer", "location": {"name": "USA"},
                         "content": "&lt;p&gt;Annual base salary: $100,000 - $140,000&lt;/p&gt;"})
    result = job_context.fetch(URL, opener=opener)
    assert result["advertised_salary_ranges"][0]["lower"] == 100000
    assert result["advertised_salary_ranges"][0]["upper"] == 140000
