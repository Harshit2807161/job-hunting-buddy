"""Approved reusable preferences must stay factual and bounded by context."""
import asyncio
from types import SimpleNamespace

import pytest

from jhb.applications import booklet, worker
from jhb.applications.planner import deterministic_plan, key_for_field
from jhb.applications.salary import advertised_ranges


def field(ref, label, *, required=True, kind="text"):
    return {"ref": ref, "label": label, "type": kind, "required": required}


class SyntheticForm:
    blocked_requests = 0

    def __init__(self, fields, *, salary_ranges=()):
        self.fields = fields
        self.salary_ranges = list(salary_ranges)
        self.values = {}

    def allowed_url(self, _):
        return True

    async def open(self, _):
        pass

    async def fill(self, control, value):
        self.values[control["ref"]] = value

    async def ensure_education(self, count):
        self.education_count = count

    async def observe(self):
        return {"fields": self.fields, "salary_ranges": self.salary_ranges,
                "buttons": [{"ref": "submit", "label": "Submit application"}]}


def prepare(form, answers):
    result, _ = asyncio.run(worker.prepare(None, {"url": "synthetic"}, answers,
                                           deterministic_plan, None, cli_actions=form))
    return result


@pytest.mark.parametrize("text,expected", [
    ("The annual USD base salary range is $114,000 - $148,000.", (114000, 148000)),
    ("Annual base salary: $122k–$141K USD.", (122000, 141000)),
    ("US base salary is $122.5k to $141k annually.", (122500, 141000)),
    ("Target Salary Range: $85000 — 115000 USD per year.", (85000, 115000)),
])
def test_explicit_annual_us_base_ranges_and_k_notation(text, expected):
    ranges = advertised_ranges(text)
    assert len(ranges) == 1
    assert (ranges[0]["lower"], ranges[0]["upper"]) == expected
    assert ranges[0]["currency"] == "USD" and ranges[0]["period"] == "annual"
    assert ranges[0]["evidence"] in text


@pytest.mark.parametrize("text", [
    "Canadian annual base salary is $85,000 - $115,000 CAD.",
    "Australian annual base salary is $85k - $115k AUD.",
    "Base salary: $40 - $65 per hour.",
    "Base salary $25,000 - $35,000 per month.",
    "Hourly compensation range: $25k - $35k.",
    "Annual salary range: $148,000 - $114,000 USD.",
    "The company serves $85,000 - $115,000 worth of equipment.",
    "Annual total compensation range: $180,000 - $260,000, including equity and target bonus.",
])
def test_non_us_non_annual_non_base_or_invalid_ranges_are_not_salary_answers(text):
    assert advertised_ranges(text) == []


def test_multiple_location_ranges_remain_distinct_and_repeated_bounds_deduplicate():
    text = "Annual USD base salary in Region A: $85,000 - $115,000.\n" + ("Job responsibilities. " * 30)
    text += "Annual USD base salary in Region B: $120,000 - $150,000.\n" + ("Benefits. " * 50)
    text += "Annual USD base salary in Region A: $85,000 - $115,000."
    ranges = advertised_ranges(text)
    assert [(r["lower"], r["upper"]) for r in ranges] == [(85000, 115000), (120000, 150000)]


def test_advertised_midpoint_overrides_fallback_and_preserves_the_bounds_source():
    ranges = advertised_ranges("Annual USD base salary range: $114,000 - $148,000.")
    form = SyntheticForm([field("salary", "Desired Salary")], salary_ranges=ranges)
    answers = {"standing.salary_policy": booklet.answer(True, "synthetic explicit approval"),
               "preferences.salary": booklet.answer(125000, "synthetic user fallback")}
    result = prepare(form, answers)
    assert result["state"] == "waiting_review"
    assert form.values == {"salary": 131000}
    assert answers["preferences.salary"]["source"]["advertised_range"] == ranges[0]
    assert result["filled"][0]["source"] == answers["preferences.salary"]["source"]


def test_no_range_keeps_the_user_approved_fallback_instead_of_guessing():
    form = SyntheticForm([field("salary", "What are your salary expectations?")])
    fallback = booklet.answer(125000, "synthetic explicit fallback")
    answers = {"standing.salary_policy": booklet.answer(True, "synthetic explicit approval"),
               "preferences.salary": fallback}
    result = prepare(form, answers)
    assert result["state"] == "waiting_review"
    assert form.values["salary"] == 125000
    assert answers["preferences.salary"] == fallback


def test_multiple_advertised_ranges_require_location_input_even_with_fallback():
    form = SyntheticForm([field("salary", "Desired Salary")], salary_ranges=[
        {"lower": 85000, "upper": 115000, "currency": "USD", "period": "annual"},
        {"lower": 120000, "upper": 150000, "currency": "USD", "period": "annual"},
    ])
    answers = {"standing.salary_policy": booklet.answer(True, "synthetic explicit approval"),
               "preferences.salary": booklet.answer(125000, "synthetic fallback")}
    result = prepare(form, answers)
    assert result["state"] == "waiting_input"
    assert result["missing"][0]["question"] == "Desired Salary"
    assert form.values == {}
    assert answers["preferences.salary"]["status"] == "needs_input"


def test_required_preferred_name_uses_first_name_but_optional_decline_stays_blank():
    form = SyntheticForm([field("required-name", "Preferred First Name"),
                          field("optional-name", "Preferred First Name", required=False)])
    answers = {"standing.required_preferred_name": booklet.answer("Sam", "synthetic explicit policy"),
               "identity.preferred_name": booklet.answer(status="declined", source="synthetic user decline")}
    result = prepare(form, answers)
    assert result["state"] == "waiting_review"
    assert form.values == {"required-name": "Sam"}
    assert result["optional_questions"] == []


def test_unverified_first_name_still_requires_input_when_preferred_name_is_required():
    form = SyntheticForm([field("required-name", "Preferred First Name")])
    result = prepare(form, {"standing.required_preferred_name": booklet.answer(source="synthetic missing name")})
    assert result["state"] == "waiting_input" and not form.values


@pytest.mark.parametrize("label,key", [
    ("Are you interested in working out of our Miami HQ?", "standing.office_willingness"),
    ("Are you willing to work on-site?", "standing.office_willingness"),
    ("Are you open to work at our Seattle office?", "standing.office_willingness"),
    ("Are you willing to relocate to Seattle?", "preferences.relocation"),
    ("Where are you currently located? Are you open to relocating to Los Angeles, CA?", "standing.location_relocation"),
])
def test_office_and_relocation_policies_bind_only_approved_question_templates(label, key):
    answers = {"standing.office_willingness": booklet.answer(True, "synthetic explicit policy"),
               "preferences.relocation": booklet.answer(True, "synthetic explicit policy"),
               "standing.location_relocation": booklet.answer("Based in Example Metro; open to relocation.", "synthetic explicit policy")}
    assert key_for_field(field("location", label), answers) == key
    assert key_for_field(field("restricted", "Can you commute to Miami five days a week beginning tomorrow?"), answers) is None


def test_exact_employer_office_answer_overrides_general_willingness():
    question = "Are you interested in working out of our Miami HQ?"
    answers = {"standing.office_willingness": booklet.answer(True, "synthetic standing approval"),
               "custom.office": {**booklet.answer(False, "synthetic employer-specific approval"), "question": question}}
    assert key_for_field(field("office", question), answers) == "custom.office"


def test_run_job_injects_only_explicit_standing_policies_and_uses_application_city(monkeypatch, tmp_path):
    job = {"dedupe_hash": "b" * 64, "url": "https://job-boards.greenhouse.io/example/jobs/123", "role_classes": "swe"}
    import hashlib, time
    job["verified_job_description"] = {"status": "verified", "source_url": "https://boards-api.greenhouse.io/v1/boards/example/jobs/123",
        "text": "Synthetic software role with no security clearance required.", "retrieved_at": time.time(),
        "sha256": hashlib.sha256(b"Synthetic software role with no security clearance required.").hexdigest()}
    book = {"answers": {"identity.first_name": booklet.answer("Sam", "synthetic explicit name"),
                         "identity.preferred_name": booklet.answer(status="declined", source="synthetic explicit decline"),
                         "preferences.application_city": booklet.answer("Example Metro, CA", "synthetic application-city instruction"),
                         "identity.location": booklet.answer("Different Mailing City, CA", "synthetic mailing location")},
            "roles": {"sde": {"role.skills": booklet.answer("Python backend engineering", "Synthetic resume")}, "ml": {}}, "workflow_preferences": {}}
    captured = {}
    async def fake_prepare(page, current_job, answers, planner, vault, **kwargs):
        captured.clear()
        captured.update(answers)
        return {"state": "waiting_review", "events": [], "filled": []}, kwargs["cli_actions"]
    async def fake_packet(page, directory, current_job, result, **kwargs):
        return directory / "synthetic-review.html"
    monkeypatch.setattr(worker, "prepare", fake_prepare)
    monkeypatch.setattr(worker, "write_packet", fake_packet)
    from jhb.applications import cli_browser
    monkeypatch.setattr(cli_browser, "BrowserUseCLI", SimpleNamespace)
    asyncio.run(worker.run_job(job, book, planner_name="deterministic", artifacts=tmp_path))
    assert not any(key.startswith("standing.") for key in captured)

    book["workflow_preferences"] = {"office_locations": True, "relocation": True,
                                     "career_fair_contact": True,
                                     "preferred_first_name": {"required": "use identity.first_name"}}
    asyncio.run(worker.run_job(job, book, planner_name="deterministic", artifacts=tmp_path))
    assert captured["standing.office_willingness"]["value"] is True
    assert captured["standing.career_fair_contact"]["value"] == "N/A"
    assert captured["standing.required_preferred_name"]["value"] == "Sam"
    assert captured["identity.preferred_name"]["status"] == "declined"
    assert captured["standing.location_relocation"]["value"] == "I am currently based in Example Metro, CA and am open to relocating anywhere."
    assert captured["standing.location_relocation"]["source"]["location_source"] == "synthetic application-city instruction"
    assert "Different Mailing City" not in captured["standing.location_relocation"]["value"]


def test_school_attendance_uses_original_verified_catalog_not_dropdown_other_mapping(monkeypatch, tmp_path):
    job = {"dedupe_hash": "a" * 64, "url": "https://job-boards.greenhouse.io/example/jobs/123", "role_classes": "swe"}
    import hashlib, time
    job["verified_job_description"] = {"status": "verified", "source_url": "https://boards-api.greenhouse.io/v1/boards/example/jobs/123",
        "text": "Synthetic software role with no security clearance required.", "retrieved_at": time.time(),
        "sha256": hashlib.sha256(b"Synthetic software role with no security clearance required.").hexdigest()}
    book = {"answers": {"identity.first_name": booklet.answer("Sam", "synthetic source")},
            "roles": {"sde": {"role.skills": booklet.answer("Python backend engineering", "Synthetic resume")}, "ml": {}}, "workflow_preferences": {"school_attendance": True},
            "education_records": [
                {"school": "University of California San Diego", "status": "verified", "source": "synthetic education",
                 "form_mappings": [{"region": "global", "board": "example", "school_option": "Other"}]},
                {"school": "Unverified University", "status": "needs_input", "source": "synthetic missing education"},
            ]}
    captured = {}
    async def fake_prepare(page, current_job, answers, planner, vault, **kwargs):
        captured.update(answers)
        form = SyntheticForm([field("ucsd", "Are you currently attending or a recent graduate of UCSD?", kind="combobox"),
                              field("miami", "Are you currently attending or a recent graduate of The University of Miami?", kind="combobox")])
        # Exercise real prepare policy matching with a synthetic browser boundary.
        result, _ = await original_prepare(page, current_job, answers, planner, vault, cli_actions=form)
        captured["filled_values"] = form.values
        return result, kwargs["cli_actions"]
    async def fake_packet(page, directory, current_job, result, **kwargs):
        return directory / "synthetic-review.html"
    original_prepare = worker.prepare
    monkeypatch.setattr(worker, "prepare", fake_prepare)
    monkeypatch.setattr(worker, "write_packet", fake_packet)
    from jhb.applications import cli_browser
    monkeypatch.setattr(cli_browser, "BrowserUseCLI", SimpleNamespace)
    result, _ = asyncio.run(worker.run_job(job, book, planner_name="deterministic", artifacts=tmp_path))
    assert result["state"] == "waiting_review"
    assert captured["education.0.school"]["value"] == "Other"
    assert captured["standing.education_schools"]["value"] == ["University of California San Diego"]
    assert captured["filled_values"] == {"ucsd": True, "miami": False}


def test_embedded_form_uses_public_posting_range_even_when_form_has_no_description():
    form = SyntheticForm([field("salary", "Desired Salary")])
    answers = {"standing.salary_policy": booklet.answer(True, "synthetic explicit approval"),
               "preferences.salary": booklet.answer(125000, "synthetic fallback")}
    job = {"url": "synthetic", "advertised_salary_ranges": advertised_ranges(
        "Annual USD base salary range: $100,000 - $140,000.")}
    result, _ = asyncio.run(worker.prepare(None, job, answers, deterministic_plan, None, cli_actions=form))
    assert result["state"] == "waiting_review"
    assert form.values["salary"] == 120000
