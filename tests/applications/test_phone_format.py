"""National phone formatting requires both approved policy and real widget context."""
import asyncio
import os

import pytest

from jhb import config
from jhb.applications import booklet
from jhb.applications.cli_runtime import dispatch
from jhb.applications.planner import deterministic_plan, key_for_field, validate_plan
from jhb.applications.worker import _apply_phone_format, prepare


def phone_answers():
    return {"identity.phone": booklet.answer("+1(555)010-1234", "Synthetic user full number"),
            "identity.phone_national": booklet.answer("(555)010-1234", "Synthetic explicit national number")}


def phone_field(**context):
    return {"ref": "phone", "label": "Phone", "type": "tel", "required": True, **context}


def test_national_phone_requires_explicit_policy_and_verified_number():
    question = phone_field(separate_phone_country=True)
    for policy in ({}, {"phone_format": {"separate_country": "international"}}):
        answers = phone_answers()
        _apply_phone_format(answers, policy)
        assert key_for_field(question, answers) == "identity.phone"
    answers = phone_answers()
    original = answers["identity.phone_national"]
    _apply_phone_format(answers, {"phone_format": {"separate_country": "national"}})
    assert answers["identity.phone_national"] is original
    assert key_for_field(question, answers) == "identity.phone_national"
    for record in (booklet.answer(), booklet.answer("", "Synthetic empty value")):
        answers = {**phone_answers(), "identity.phone_national": record}
        _apply_phone_format(answers, {"phone_format": {"separate_country": "national"}})
        assert key_for_field(question, answers) == "identity.phone"


@pytest.mark.parametrize("context", [{}, {"separate_phone_country": False},
                                     {"separate_phone_country": "true"}])
def test_unified_phone_keeps_full_number_and_rejects_national_planner_guess(context):
    answers = phone_answers()
    question = phone_field(**context)
    assert key_for_field(question, answers) == "identity.phone"
    with pytest.raises(ValueError, match="unsupported or sensitive"):
        validate_plan({"bindings": [{"ref": "phone", "answer_key": "identity.phone_national"}],
                       "next_ref": None, "reason": "Synthetic invalid guess"},
                      {"fields": [question], "buttons": []}, answers)


def test_latest_split_phone_rule_supersedes_old_employer_phone_answer():
    answers = {**phone_answers(), "custom.phone": {
        **booklet.answer("555-010-9999", "Synthetic employer-specific instruction"),
        "question": "Phone", "field_ref": "phone"}}
    assert key_for_field(phone_field(separate_phone_country=True), answers) == "identity.phone_national"
    assert key_for_field(phone_field(separate_phone_country=False), answers) == "custom.phone"
    assert key_for_field(phone_field(type="text", separate_phone_country=True), phone_answers()) == "identity.phone"


def test_new_calling_code_widget_context_replans_same_phone_ref():
    class Form:
        blocked_requests = 0
        values = []

        def allowed_url(self, url): return True
        async def open(self, url): pass
        async def fill(self, field, value): self.values.append(value)
        async def observe(self):
            return {"fields": [phone_field(separate_phone_country=bool(self.values))],
                    "buttons": [{"ref": "submit", "label": "Submit application"}]}

    actions = Form()
    result, _ = asyncio.run(prepare(None, {"url": "synthetic"}, phone_answers(),
                                    deterministic_plan, None, cli_actions=actions))
    assert result["state"] == "waiting_review"
    assert actions.values == ["+1(555)010-1234", "(555)010-1234"]
    assert result["filled"][0]["key"] == "identity.phone_national"
    assert any(event["event"] == "fields_revealed" for event in result["events"])


def test_observe_distinguishes_visible_calling_code_from_hidden_search_and_address_country():
    """Synthetic GH structure mirrors the separate Country sibling in .phone-input."""
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    from playwright.sync_api import sync_playwright
    html = '''<!doctype html><title>Synthetic phone widgets</title>
    <div class="phone-input"><label for="country">Country</label><input id="country" role="combobox">
      <div class="iti"><input id="iti-search" role="combobox" aria-label="Country" style="display:none">
      <label for="split">Phone</label><input id="split" type="tel"></div></div>
    <div class="phone-input"><label for="hidden-country">Country</label><input id="hidden-country" role="combobox" style="display:none">
      <div class="iti"><label for="hidden">Phone</label><input id="hidden" type="tel"></div></div>
    <div class="phone-input"><label for="disabled-country">Country</label><input id="disabled-country" role="combobox" disabled>
      <div class="iti"><label for="disabled">Phone</label><input id="disabled" type="tel"></div></div>
    <div class="phone-input"><label for="address-country">Country</label><input id="address-country" role="combobox">
      <label for="no-intl-widget">Phone</label><input id="no-intl-widget" type="tel"></div>
    <div class="iti"><input id="internal-search" role="combobox" aria-label="Country" style="display:none">
      <label for="unified">Phone</label><input id="unified" type="tel"></div>
    <div class="iti"><input id="iti-1__search-input" class="iti__search-input" role="combobox" aria-label="Country">
      <label for="search-open">Phone</label><input id="search-open" type="tel"></div>
    <label for="mailing-country">Country</label><input id="mailing-country" role="combobox">
    <label for="plain">Phone</label><input id="plain" type="tel">
    <button type="submit">Submit application</button>'''
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        requests = []
        def serve(route):
            requests.append(route.request.url)
            route.fulfill(status=200, content_type="text/html", body=html)
        page.route("**/*", serve)
        url = "https://job-boards.greenhouse.io/synthetic-fixture/jobs/7890"
        page.goto(url)
        session = page.context.new_cdp_session(page)
        helpers = {"cdp": lambda method, **params: session.send(method, params),
                   "js": page.evaluate, "wait": lambda seconds: page.wait_for_timeout(seconds*1000),
                   "click_at_xy": lambda x, y: page.mouse.click(x, y),
                   "list_tabs": lambda: [{"url": url, "targetId": "fixture-tab"}],
                   "switch_tab": lambda target: None,
                   "current_tab": lambda: {"targetId": "fixture-tab"}}
        try:
            dispatch({"operation": "open", "url": url}, helpers)
            snapshot = dispatch({"operation": "observe"}, helpers)
            fields = {field["ref"]: field for field in snapshot["fields"]}
            assert fields["split"]["separate_phone_country"] is True
            for ref in ("hidden", "disabled", "no-intl-widget", "unified", "search-open", "plain"):
                assert fields[ref]["separate_phone_country"] is False
            assert "iti-search" not in fields and "internal-search" not in fields
            answers = phone_answers()
            for ref in ("split", "unified", "plain"):
                key = key_for_field(fields[ref], answers)
                dispatch({"operation": "fill", "field": fields[ref], "value": answers[key]["value"]}, helpers)
            assert page.locator("#split").input_value() == "(555)010-1234"
            assert page.locator("#unified").input_value() == "+1(555)010-1234"
            assert page.locator("#plain").input_value() == "+1(555)010-1234"
            assert page.locator("#country").input_value() == ""
            assert page.locator("#mailing-country").input_value() == ""
            assert requests == [url]
        finally:
            browser.close()
