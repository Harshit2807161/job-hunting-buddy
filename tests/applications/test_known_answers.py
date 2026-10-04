"""Synthetic retained questions: reuse facts without broad screening guesses."""
import asyncio
import copy
from datetime import date

import pytest

from jhb.applications import booklet, known_answers
from jhb.applications.planner import deterministic_plan, key_for_field, validate_plan
from jhb.applications.worker import prepare


def field(label, *, kind="radio", choices=("Yes", "No"), context=None, ref="q"):
    return {"ref": ref, "label": label, "type": kind, "required": True,
            "country_context": context,
            "options": [{"label": option, "value": option} for option in choices]}


def facts():
    return {key: booklet.answer(value, "synthetic explicit standing answer") for key, value in {
        "preferences.relocation": True, "standing.office_willingness": True,
        "eligibility.authorized_us": True, "eligibility.sponsorship": True,
        "screening.non_compete": False, "identity.state": "California",
        "preferences.start_date": "January 2027", "standing.discovery_source": "Simplify",
        "links.github": "https://github.com/synthetic", "links.scholar": "https://scholar.google.com/synthetic",
        "links.portfolio": "https://synthetic.example/"}.items()}


@pytest.mark.parametrize("label,key", [
    ("Are you\u00a0 open to relocation?", "preferences.relocation"),
    ("I am authorized to work in the United States.", "eligibility.authorized_us"),
    ("Are you currently subject to any agreement (such as a non\u2011compete, non\u2011solicitation, non\u2011disclosure, or similar restriction) that could limit your ability to perform this role?", "screening.non_compete"),
])
def test_exact_known_aliases_reuse_verified_values(label, key):
    values = facts()
    assert key_for_field(field(label), values) == key
    values[key]["status"] = "needs_input"
    assert key_for_field(field(label), values) is None
    values[key] = booklet.answer(True, "")
    assert key_for_field(field(label), values) is None


def test_residence_state_does_not_infer_job_country_or_work_authorization():
    values = facts()
    residence = field("State/Country of Residence", kind="combobox", choices=())
    assert key_for_field(residence, values) == "identity.state"
    question = field("Are you legally authorized to work in the country in which this job is located?")
    known_answers.enrich(question, {"work_country": None}, values, as_of=date(2026, 10, 4))
    assert key_for_field(question, values) is None


@pytest.mark.parametrize("context,expected", [(None, None), ("United Kingdom", None),
                                               ("United States", "eligibility.sponsorship")])
def test_combined_visa_assistance_requires_exact_observed_us_context(context, expected):
    question = field("I will now or in the future need assistance with a work visa.", context=context)
    assert key_for_field(question, facts()) == expected


@pytest.mark.parametrize("label", ["Are you open to travel?", "Do you currently need visa assistance?",
    "Earliest residency start date?", "How many months in a row can you commit to?",
    "Does your degree focus on hardware, device physics, stochastic or analog computing, or machine learning systems?",
    "Have you ever had a non-disclosure agreement?", "Are you a United States citizen?",
    "Are you legally authorized to work in the country in which this job is located?"])
def test_new_or_differently_scoped_facts_remain_handoffs(label):
    values = facts()
    assert known_answers.enrich(field(label), {"work_country": None}, values, as_of=date(2026, 10, 4)) is None
    assert key_for_field(field(label), values) is None


OFFICES = ["Austin, Texas", "London, United Kingdom", "New York, New York", "San Francisco, California",
           "Phoenix, Arizona", "Seattle, Washington"]


def test_offices_use_only_observed_us_choices_under_verified_relocation_and_office_preferences():
    values = facts()
    question = field("Office Location", kind="multiselect", choices=OFFICES)
    key = known_answers.enrich(question, {"work_country": "United States"}, values, as_of=date(2026, 10, 4))
    assert values[key]["value"] == [choice for choice in OFFICES if "United Kingdom" not in choice]
    assert key_for_field(question, values) == key
    changed = copy.deepcopy(question);changed["options"].append({"label": "Boston, Massachusetts", "value": "Boston"})
    assert key_for_field(changed, values) is None
    changed = {**question, "ref": "another-office-question"}
    assert key_for_field(changed, values) is None
    snapshot = {"fields": [question], "buttons": []}
    assert validate_plan(deterministic_plan(snapshot, values), snapshot, values)["bindings"][0]["answer_key"] == key


@pytest.mark.parametrize("change,country", [(None, None), (None, "United Kingdom"),
    ("preferences.relocation", "United States"), ("standing.office_willingness", "United States")])
def test_office_selection_does_not_choose_foreign_country_or_unverified_preference(change, country):
    values = facts()
    if change:values[change]["status"] = "needs_input"
    assert known_answers.enrich(field("Office Location", kind="multiselect", choices=OFFICES),
                                {"work_country": country}, values, as_of=date(2026, 10, 4)) is None


@pytest.mark.parametrize("source,choices,expected", [
    ("Simplify", ["LinkedIn", "Job Board (Indeed, Glassdoor, etc.)", "Referral", "Other"], ["Job Board (Indeed, Glassdoor, etc.)"]),
    ("LinkedIn", ["LinkedIn", "Job Board", "Referral"], ["LinkedIn"]),
    ("Unknown source", ["Job Board", "Referral", "Other"], None),
    ("Simplify", ["Job Board", "Job Board (Other)", "Referral"], None),
])
def test_discovery_category_uses_actual_recorded_source_and_unique_observed_option(source, choices, expected):
    values = facts();values["standing.discovery_source"] = booklet.answer(source, {"method": "recorded_phase1_discovery"})
    question = field(known_answers._DISCOVERY, kind="multiselect", choices=choices)
    key = known_answers.enrich(question, {}, values, as_of=date(2026, 10, 4))
    assert (values[key]["value"] if key else None) == expected


@pytest.mark.parametrize("as_of,value,expected", [
    (date(2026, 10, 4), "January 2027", "One month +"),
    (date(2026, 10, 4), "2027-01", "One month +"),
    (date(2026, 12, 15), "January 2027", None),
    (date(2027, 1, 15), "January 2027", None),
    (date(2026, 10, 4), "After graduation", None),
])
def test_relative_start_category_uses_lower_bound_without_inventing_exact_start(as_of, value, expected):
    values = facts();values["preferences.start_date"] = booklet.answer(value, "synthetic approved calendar availability")
    question = field(known_answers._AVAILABILITY, choices=["One week", "Two weeks", "Three weeks", "One month", "One month +"])
    key = known_answers.enrich(question, {}, values, as_of=as_of)
    assert (values[key]["value"] if key else None) == expected
    if key:
        assert values[key]["source"]["as_of"] == as_of.isoformat()
        assert values[key]["source"]["records"]["preferences.start_date"]["value"] == value
    assert key_for_field(field("Earliest residency start date?", kind="text", choices=()), values) is None


def education():
    return booklet.for_role({"answers": {}, "roles": {"ml": {}}, "education_records": [
        {"status": "verified", "source": "synthetic original education", "degree": "Master of Science",
         "major": "Computer Science", "start_date": "2025-09", "end_date": "2026-12", "expected": True}]}, "ml")


def test_current_grad_cs_question_uses_original_indexed_expected_study_and_not_degree_focus():
    values = education()
    question = field(known_answers._GRADUATE)
    key = known_answers.enrich(question, {}, values, as_of=date(2026, 10, 4))
    assert values[key]["value"] is True and key_for_field(question, values) == key
    assert values[key]["source"]["records"]["end_date"]["value"] == "2026-12"
    assert known_answers.enrich(question, {}, values, as_of=date(2027, 1, 4)) is None
    assert key_for_field(question, values) is None


@pytest.mark.parametrize("change,value", [("major", "Physics"), ("degree", "Bachelor of Science"),
    ("start_date", "2026-11"), ("end_date", "2026-10"), ("end_year", None)])
def test_unproven_current_study_stays_unknown(change, value):
    values = education()
    if change == "end_year":values["education.0.end_year"]["source"]["expected"] = False
    else:values["education.0." + change]["value"] = value
    assert known_answers.enrich(field(known_answers._GRADUATE), {}, values, as_of=date(2026, 10, 4)) is None


def test_optional_links_aggregate_verified_existing_urls_without_inventing_publications():
    values = facts()
    values["links.scholar"]["status"] = "needs_input"
    question = field(known_answers._LINKS, kind="text", choices=())
    key = known_answers.enrich(question, {}, values, as_of=date(2026, 10, 4))
    assert values[key]["value"] == "https://github.com/synthetic\nhttps://synthetic.example/"
    assert "publications" not in values[key]["value"]


def test_reobserved_derived_answer_cannot_survive_revoked_preference():
    values = facts()
    question = field("Office Location", kind="multiselect", choices=OFFICES)
    assert known_answers.enrich(question, {"work_country": "United States"}, values)
    values["preferences.relocation"]["value"] = False
    assert known_answers.enrich(question, {"work_country": "United States"}, values) is None
    assert key_for_field(question, values) is None


def test_preparation_fills_verified_preferences_and_parks_only_new_travel_without_terminal_click():
    known = field("Are you open to relocation?", ref="relocate")
    offices = field("Office Location", kind="multiselect", choices=OFFICES, ref="offices")
    travel = field("Are you open to travel?", ref="travel")
    class SyntheticCLI:
        blocked_requests = 0
        async def open(self, url):pass
        async def ensure_education(self, count):pass
        def allowed_url(self, url):return True
        async def observe(self):
            return {"url": "https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application",
                    "fields": [known, offices, travel], "buttons": [{"ref": "submit", "label": "Submit application"}]}
        async def fill(self, question, value):self.filled[question["ref"]] = value
        async def click_next(self, button):raise AssertionError("No terminal click allowed")
        def __init__(self):self.filled = {}
    cli = SyntheticCLI()
    result, _ = asyncio.run(prepare(None, {"url": "https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555",
                                          "work_country": "United States"}, facts(), deterministic_plan, None, cli_actions=cli))
    assert result["state"] == "waiting_input"
    assert [item["question"] for item in result["missing"]] == [travel["label"]]
    assert cli.filled == {"relocate": True, "offices": [choice for choice in OFFICES if "United Kingdom" not in choice]}
