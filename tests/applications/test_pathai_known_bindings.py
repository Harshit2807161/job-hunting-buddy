"""Known facts from different form wording must not prompt the candidate again."""
import asyncio
from copy import deepcopy

import pytest

from jhb.applications import booklet, worker
from jhb.applications.planner import deterministic_plan, key_for_field

FAMILY = ("Are any of your immediate family members employees or directors of PathAI or Poplar Healthcare PLLC, "
          "including a spouse or partner living in same household, parent, child, sibling, grandparent or grandchild "
          "(including step-persons, such as a step-parent or step-child)?*")
AUTHORIZED = "Are you legally authorized to work in the United States for our company?"
SPONSORSHIP = "Will you now or in the future require sponsorship for employment visa status (e.g., H-1B visa status) to work legally for our company in the United States?"


def field(ref, label):
    return {"ref": ref, "label": label, "type": "combobox", "required": True, "options": []}


class SyntheticForm:
    blocked_requests = 0
    def __init__(self, fields): self.fields, self.values = fields, {}
    def allowed_url(self, url): return True
    async def open(self, url): pass
    async def fill(self, field, value): self.values[field["ref"]] = value
    async def observe(self):
        return {"fields": self.fields, "buttons": [{"ref": "terminal", "label": "Submit application"}]}


def prepare(form, answers):
    result, _ = asyncio.run(worker.prepare(None, {"url": "synthetic"}, answers,
        deterministic_plan, None, cli_actions=form))
    return result


def test_observed_pathai_known_questions_all_fill_without_new_factual_handoff():
    fields = [field("family", FAMILY), field("located", "Are you located in the US?"),
              field("authorized", AUTHORIZED), field("sponsorship", SPONSORSHIP)]
    answers = {"identity.country": booklet.answer("United States", "Synthetic verified contact country"),
               "screening.employee_relative": booklet.answer(False, "Synthetic explicit standing family rule"),
               "eligibility.authorized_us": booklet.answer(True, "Synthetic verified work authorization"),
               "eligibility.sponsorship": booklet.answer(True, "Synthetic verified present/future sponsorship")}
    original_country = deepcopy(answers["identity.country"])
    form = SyntheticForm(fields)
    result = prepare(form, answers)
    assert result["state"] == "waiting_review"
    assert not result.get("missing")
    assert form.values == {"family": False, "located": True, "authorized": True, "sponsorship": True}
    assert answers["identity.country"] == original_country
    assert answers["standing.located_us"]["source"] == {
        "method": "verified_contact_country_residence", "derived_from": "identity.country",
        "original_country": "United States", "original_source": original_country["source"],
        "criterion": "Contact country is the United States"}


@pytest.mark.parametrize("country,expected", [("United States", True), ("United States of America", True),
    ("USA", True), ("Canada", False), ("India", False), ("United Kingdom", False)])
def test_us_residence_derivation_is_country_specific_and_does_not_infer_citizenship(country, expected):
    answers = {"identity.country": booklet.answer(country, "Synthetic verified contact country"),
               "eligibility.authorized_us": booklet.answer(True, "Independent synthetic authorization")}
    form = SyntheticForm([field("located", "Are you located in the US?")])
    assert prepare(form, answers)["state"] == "waiting_review"
    assert form.values["located"] is expected
    assert key_for_field(field("citizenship", "Are you a US citizen?"), answers) is None
    assert answers["eligibility.authorized_us"]["value"] is True


@pytest.mark.parametrize("record", [booklet.answer(), booklet.answer("United States", "Unconfirmed", "needs_input"),
    booklet.answer("United States", ""), booklet.answer("Worldwide", "Synthetic source"),
    booklet.answer("Canada or United States", "Synthetic source"), booklet.answer([], "Synthetic source")])
def test_unverified_or_ambiguous_country_does_not_reuse_stale_residence_or_job_location(record):
    answers = {"identity.country": record, "standing.located_us": booklet.answer(True, "Stale derived answer")}
    form = SyntheticForm([field("located", "Are you located in the US?")])
    result = prepare(form, answers)
    assert result["state"] == "waiting_input"
    assert "standing.located_us" not in answers
    assert not form.values


@pytest.mark.parametrize("label,key", [(AUTHORIZED, "eligibility.authorized_us"),
    (SPONSORSHIP, "eligibility.sponsorship"), (FAMILY, "screening.employee_relative")])
def test_exact_observed_aliases_preserve_saved_boolean_and_never_invent_an_answer(label, key):
    assert key_for_field(field("screening", label), {key: booklet.answer(False, "Synthetic verified fact")}) == key
    answers = {key: booklet.answer(source="Missing synthetic fact")}
    result = prepare(SyntheticForm([field("screening", label)]), answers)
    assert result["state"] == "waiting_input"


@pytest.mark.parametrize("label", ["Are you permanently located in the US?", "Are you a United States tax resident?",
    "Are you legally authorized to work in Canada for our company?", "Do you require sponsorship now?",
    "Does your spouse require employment visa sponsorship?", "Are your immediate family members US citizens?",
    FAMILY.replace("employees or directors", "patients or customers"),
    FAMILY.replace("including a spouse or partner living in same household", "including any former business associate")])
def test_broader_or_different_factual_questions_remain_unbound(label):
    answers = {"standing.located_us": booklet.answer(True, "Synthetic derived country"),
               "eligibility.authorized_us": booklet.answer(True, "Synthetic verified authorization"),
               "eligibility.sponsorship": booklet.answer(True, "Synthetic combined sponsorship"),
               "screening.employee_relative": booklet.answer(False, "Synthetic standing family rule")}
    assert key_for_field(field("different", label), answers) is None
