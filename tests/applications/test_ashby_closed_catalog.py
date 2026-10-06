"""Synthetic Ashby catalogs that open on ArrowDown, not input focus/click."""
import os

import pytest

from jhb import config
from jhb.applications.manual_runtime import application_scope, dispatch


URL = "https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application"
HTML = """<form class=ashby-application-form-container>
<div data-field-path=month><label class=ashby-application-form-question-title>
Please select your graduation month</label>
<input role=combobox required aria-expanded=false aria-autocomplete=list
 class=ashby-application-form-input-autocomplete onkeydown="key(event)">
<button type=button onclick="openCatalog()">Open choices</button></div>
<button type=submit>Submit Application</button></form>
<div role=listbox id=foreign><div role=option>Foreign choice</div></div>
<div id=portal></div>
<script>
const input=document.querySelector('input');
window.opens=0;window.commits=0;window.inputEvents=0;window.submissions=0;
window.ownership='owned';
input.addEventListener('input',()=>window.inputEvents++);
document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
function openCatalog(){
  window.opens++;input.setAttribute('aria-expanded','true');
  document.querySelector('#portal').innerHTML='<div role=listbox id=months></div>';
  for(const label of ['May','December']){
    const option=document.createElement('div');option.setAttribute('role','option');
    option.textContent=label;option.onclick=()=>commit(label);
    document.querySelector('#months').append(option);
  }
  if(window.ownership==='owned')input.setAttribute('aria-controls','months');
  if(window.ownership==='missing')input.setAttribute('aria-controls','absent');
  if(window.ownership==='ambiguous'){
    input.setAttribute('aria-controls','portal');
    document.querySelector('#portal').insertAdjacentHTML('beforeend',
      '<div role=listbox><div role=option>Unrelated choice</div></div>');
  }
}
function commit(label){window.commits++;input.value=label;closeCatalog()}
function closeCatalog(){
  document.querySelector('#portal').innerHTML='';
  input.setAttribute('aria-expanded','false');input.removeAttribute('aria-controls');
}
function key(event){
  if(event.key==='ArrowDown'){event.preventDefault();openCatalog()}
  if(event.key==='Enter'){event.preventDefault();commit('May')}
  if(event.key==='Escape')closeCatalog();
}
</script>"""


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", os.environ.get(
        "PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers")))
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.fulfill(
                status=200, content_type="text/html", body=HTML))
            page.goto(URL)
            session = page.context.new_cdp_session(page)
            helpers = {"cdp": lambda method, **params: session.send(method, params),
                       "js": page.evaluate,
                       "wait": lambda seconds: page.wait_for_timeout(seconds * 1000),
                       "click_at_xy": lambda x, y: page.mouse.click(x, y),
                       "list_tabs": lambda: [{"url": URL, "targetId": "fixture"}],
                       "switch_tab": lambda target: None,
                       "current_tab": lambda: {"targetId": "fixture"}}

            def call(operation, **payload):
                return dispatch({"operation": operation, "scope": application_scope(URL),
                                 **payload}, helpers)

            call("open", url=URL)
            yield page, call, call("observe")
        finally:
            browser.close()


def assert_unchanged(page, value):
    assert page.locator("input").input_value() == value
    assert page.locator("input").get_attribute("aria-expanded") == "false"
    assert page.locator("#portal").inner_text() == ""
    assert page.evaluate("[window.commits,window.inputEvents,window.submissions]") == [0, 0, 0]
    assert page.evaluate("window.__jhbGuard") is True


@pytest.mark.parametrize("existing", ["", "December"])
def test_queryless_catalog_opens_without_editing_or_selecting(catalog, existing):
    page, call, snapshot = catalog
    page.locator("input").evaluate("(e,value)=>{e.value=value}", existing)
    field = snapshot["fields"][0]
    assert call("describe", field=field) == {
        "choices": ["May", "December"], "type": "combobox", "truncated": False}
    assert page.evaluate("window.opens") == 1
    assert_unchanged(page, existing)


@pytest.mark.parametrize("ownership", ["absent", "missing", "ambiguous"])
def test_unowned_or_ambiguous_catalog_still_fails_closed(catalog, ownership):
    page, call, snapshot = catalog
    page.evaluate("mode=>{window.ownership=mode}", ownership)
    assert call("describe", field=snapshot["fields"][0])["choices"] == []
    assert snapshot["fields"][0]["options"] == []
    assert "native_question_catalog" not in snapshot["fields"][0]
    assert page.evaluate("window.opens") == 1
    assert_unchanged(page, "")


def test_wrong_job_stops_before_catalog_opens(catalog):
    page, call, snapshot = catalog
    page.goto(URL.replace("555555555555", "555555555556"))
    with pytest.raises(ValueError, match="differs from approved manual job"):
        call("describe", field=snapshot["fields"][0])
    assert page.evaluate("window.opens") == 0
