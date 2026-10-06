"""Observed veteran semantics survive native filling and final retained audit."""
import pytest

from jhb.applications import booklet, review_inventory
from jhb.applications.manual_runtime import application_scope, dispatch
from jhb.applications.planner import key_for_field
from jhb.applications.submission_runtime import _checks

URL = "https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application"


@pytest.mark.parametrize("kind", ["radio", "select"])
@pytest.mark.parametrize("label", ["Veteran Status", "Unrelated screening"])
def test_only_veteran_context_projects_explicit_false_to_nonprotected_choice(kind, label):
    from playwright.sync_api import sync_playwright
    choices = ["I am a protected veteran", "I am not a protected veteran", "I don't wish to answer"]
    if kind == "radio":
        controls = "".join(f'<label><input type=radio name=veteran id=v{i} value={i}>{choice}</label>'
                           for i, choice in enumerate(choices))
    else:
        controls = '<select id=veteran><option value="">Select...</option>' + "".join(
            f'<option value={i}>{choice}</option>' for i, choice in enumerate(choices)) + '</select>'
    html = ('<form class=ashby-application-form-container><div data-field-path=veteran>'
            f'<label class=ashby-application-form-question-title>{label}</label>{controls}</div>'
            '<button type=submit>Submit</button></form><script>window.submissions=0;'
            "document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};</script>")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        page.goto(URL)
        session = page.context.new_cdp_session(page)
        helpers = {"cdp": lambda method, **params: session.send(method, params), "js": page.evaluate,
                   "wait": lambda seconds: page.wait_for_timeout(seconds * 1000), "click_at_xy": page.mouse.click,
                   "list_tabs": lambda: [{"targetId": "fixture", "url": URL}],
                   "current_tab": lambda: {"targetId": "fixture"}, "switch_tab": lambda target: None}
        def call(op, **payload):
            return dispatch({"operation": op, "scope": application_scope(URL), **payload}, helpers)
        try:
            call("open", url=URL)
            field = call("observe")["fields"][0]
            before = page.evaluate("[...document.querySelectorAll('input,select')].map(e=>({value:e.value,checked:e.checked}))")
            if label != "Veteran Status":
                with pytest.raises(ValueError, match="does not uniquely match|absent from dropdown"):
                    call("fill", field=field, value=False)
                assert page.evaluate("[...document.querySelectorAll('input,select')].map(e=>({value:e.value,checked:e.checked}))") == before
            else:
                assert call("fill", field=field, value=False)["verified"]
                answers = {"disclosure.veteran": booklet.answer(False, "synthetic explicit candidate disclosure")}
                filled = [{"ref": field["ref"], "question": label, "key": "disclosure.veteran",
                           "value": False, "source": answers["disclosure.veteran"]["source"]}]
                packet = {"job": {"url": URL}, "filled": filled,
                          **review_inventory.build([field], filled, answers, key_for_field, complete=True)}
                result = _checks({"target_id": "fixture", "documents": {}}, helpers, packet, {
                    "application_url": URL,
                    "authorization_scope": "one exact application explicitly approved in the local review portal"})
                assert result["retained"][0]["state"]["selected"] == (
                    [choices[1]] if kind == "radio" else choices[1])
            assert page.evaluate("window.submissions") == 0
            assert page.evaluate("window.__jhbGuard") is True
        finally:
            browser.close()
