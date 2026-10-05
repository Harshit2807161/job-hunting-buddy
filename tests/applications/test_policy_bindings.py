"""Explicit policy bindings preserve factual and jurisdiction boundaries."""
import asyncio

import pytest

from jhb.applications import booklet
from jhb.applications.planner import deterministic_plan, key_for_field
from jhb.applications.worker import prepare


def field(label, kind="combobox", **context):
    return {"ref": "synthetic-question", "label": label, "type": kind, "required": True, **context}


def compliance_answers():
    return {
        "standing.compliance": booklet.answer(True, "Explicit synthetic user compliance policy"),
        "standing.interview_expectations": booklet.answer("I agree", "Explicit synthetic user interview policy"),
        "standing.legal_signature": booklet.answer("Sam Example", "Explicit synthetic user signature policy"),
    }


@pytest.mark.parametrize("question", [
    field("I agree to this employer's interview terms", "checkbox"),
    field("Do you consent to us using AI to transcribe and summarize your interview?"),
    field("Please sign by typing your full legal name as your electronic signature", "text"),
])
def test_compliance_policy_has_no_invented_default(question):
    assert key_for_field(question, {}) is None


@pytest.mark.parametrize("question,key", [
    (field("By checking this box, I consent to processing my voluntary demographic answers", "checkbox"),
     "standing.compliance"),
    (field("How we interview: Please agree to respectful interview conduct"),
     "standing.interview_expectations"),
    (field("Do you consent to us using AI to transcribe and summarize your interview?"),
     "standing.compliance"),
    (field("Please sign by typing your full legal name as your electronic signature", "text"),
     "standing.legal_signature"),
])
def test_explicit_compliance_policy_uses_correct_response_type(question, key):
    answers = compliance_answers()
    assert key_for_field(question, answers) == key
    if key == "standing.compliance":
        assert answers[key]["value"] is True
    elif key == "standing.interview_expectations":
        assert answers[key]["value"] == "I agree"
    else:
        assert answers[key]["value"] == "Sam Example"


def test_arbitration_accept_checkbox_requires_its_observed_description():
    # Runtime combines a generic checkbox label with its observed description.
    question = field("By clicking Accept, I acknowledge receipt of and agree to this employer's arbitration agreement. (Accept)",
                     "checkbox")
    assert key_for_field(question, compliance_answers()) == "standing.compliance"
    assert key_for_field(field("Accept", "checkbox"), compliance_answers()) is None


def test_compliance_does_not_invent_factual_history():
    assert key_for_field(field("Have you violated compliance requirements?"), compliance_answers()) is None
    assert key_for_field(field("Have you agreed to arbitration with a previous employer?"), compliance_answers()) is None


def test_exact_employer_custom_answer_wins_over_standing_compliance():
    question = field("I agree to this employer's interview terms", "checkbox")
    answers = {**compliance_answers(), "custom.employer": {
        **booklet.answer(False, "Explicit synthetic employer-specific response"),
        "question": question["label"], "scope": {"region": "global", "board": "example"},
    }}
    assert key_for_field(question, answers) == "custom.employer"


@pytest.mark.parametrize("label,key", [
    ("Have you ever been employed full-time at Example Corp?", "standing.previous_employment"),
    ("Have you ever provided any contract work for Example Corp?", "standing.previous_contract"),
])
def test_explicit_previous_employment_policy_is_false_for_exact_templates(label, key):
    answers = {key: booklet.answer(False, "Explicit synthetic user standing history answer")}
    assert key_for_field(field(label), answers) == key
    assert answers[key]["value"] is False
    assert key_for_field(field(label), {}) is None
    assert key_for_field(field("Have you been employed by any company?"), answers) is None


def test_generic_location_authorization_uses_explicit_country_context():
    answers = {"eligibility.authorized_us": booklet.answer(True, "Explicit synthetic user authorization"),
               "eligibility.authorized_canada": booklet.answer(False, "Explicit synthetic user authorization")}
    label = "Are you authorized to work lawfully in the location posted for this position?"
    assert key_for_field(field(label, country_context="united states"), answers) == "eligibility.authorized_us"
    assert key_for_field(field(label, country_context="canada"), answers) == "eligibility.authorized_canada"
    assert key_for_field(field(label), answers) is None
    assert key_for_field(field(label, country_context="unknown"), answers) is None


def test_canadian_custom_answer_never_overrides_a_us_location_field():
    label = "Are you authorized to work lawfully in the location posted for this position?"
    answers = {"custom.canada": {
        **booklet.answer(False, "Explicit synthetic employer and country response"),
        "question": label, "country_context": "canada",
        "scope": {"region": "global", "board": "example"},
    }, "eligibility.authorized_us": booklet.answer(True, "Explicit synthetic US authorization")}
    assert key_for_field(field(label, country_context="canada"), answers) == "custom.canada"
    assert key_for_field(field(label, country_context="united states"), answers) == "eligibility.authorized_us"
    assert key_for_field(field(label), answers) is None


class CatalogForm:
    blocked_requests = 0

    def __init__(self, question, original, error):
        self.question, self.original, self.error = question, original, error
        self.attempts = []

    def allowed_url(self, url):
        return True

    async def open(self, url):
        pass

    async def ensure_education(self, count):
        assert count <= 2

    async def observe(self):
        return {"fields": [self.question], "buttons": [{"ref": "submit", "label": "Submit application"}]}

    async def fill(self, question, value):
        self.attempts.append(value)
        if value == self.original:
            raise ValueError(self.error)

    async def describe(self, question):
        if self.error == "Stored answer is absent from dropdown options":
            return {"choices": ["Other"], "truncated": False}
        if self.error == "Observed field is no longer available":
            raise ValueError(self.error)
        return {"choices": [self.original], "truncated": False}


@pytest.mark.parametrize("ref,label,key,fallback_key,original,fallback", [
    ("school--0", "School", "education.0.school", "standing.catalog.0.school", "Example Missing College", "Other"),
    ("discipline--1", "Discipline", "education.1.major", "standing.catalog.1.major", "Mathematics and Computing", "Mathematics"),
])
def test_approved_catalog_fallback_applies_only_after_original_option_is_absent(ref, label, key, fallback_key, original, fallback):
    question = field(label, ref=ref)
    actions = CatalogForm(question, original, "Stored answer is absent from dropdown options")
    answers = {key: booklet.answer(original, "Verified synthetic original education fact"),
               fallback_key: booklet.answer(fallback, "Explicit synthetic user catalog fallback")}
    result, _ = asyncio.run(prepare(None, {"url": "synthetic"}, answers, deterministic_plan, None, cli_actions=actions))
    assert result["state"] == "waiting_review"
    assert actions.attempts == [original, fallback]
    assert result["filled"][0]["key"] == fallback_key
    assert result["filled"][0]["value"] == fallback
    assert answers[key]["value"] == original


@pytest.mark.parametrize("error", [
    "Stored answer matches multiple dropdown options", "Observed field is no longer available",
    "Dropdown did not retain the selected answer",
])
def test_education_catalog_fallback_cannot_hide_ambiguity_or_technical_failure(error):
    from jhb.applications.cli_browser import BrowserOperationError
    original = "Example Missing College"
    actions = CatalogForm(field("School", ref="school--0"), original, error)
    answers = {"education.0.school": booklet.answer(original, "Verified synthetic school"),
               "standing.catalog.0.school": booklet.answer("Other", "Explicit synthetic user catalog fallback")}
    with pytest.raises(BrowserOperationError) as failure:
        asyncio.run(prepare(None, {"url": "synthetic"}, answers, deterministic_plan, None, cli_actions=actions))
    assert failure.value.retryable
    assert actions.attempts == [original]


def test_catalog_fallback_requires_explicit_user_policy():
    original = "Example Missing College"
    actions = CatalogForm(field("School", ref="school--0"), original, "Stored answer is absent from dropdown options")
    answers = {"education.0.school": booklet.answer(original, "Verified synthetic original education fact")}
    result, _ = asyncio.run(prepare(None, {"url": "synthetic"}, answers, deterministic_plan, None, cli_actions=actions))
    assert result["state"] == "waiting_input"
    assert actions.attempts == [original]
    assert not result["filled"]


def test_short_authorization_label_requires_country_context():
    answers = {"eligibility.authorized_us": booklet.answer(True, "synthetic approved fact"),
               "eligibility.authorized_canada": booklet.answer(False, "synthetic approved fact")}
    field = {"ref": "work_auth", "label": "Work authorization", "type": "combobox", "required": True}
    assert key_for_field(field, answers) is None
    assert key_for_field({**field, "country_context": "united states"}, answers) == "eligibility.authorized_us"
    assert key_for_field({**field, "country_context": "canada"}, answers) == "eligibility.authorized_canada"
