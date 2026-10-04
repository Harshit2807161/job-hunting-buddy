"""Offline synthetic Ashby controls, job scoping, and submission protection."""
import os

import pytest

from jhb import config
from jhb.applications.cli_browser import BrowserUseCLI
from jhb.applications.manual_ats import ManualATSCLI
from jhb.applications.manual_runtime import ASHBY_FIELDS, application_scope, dispatch, matches_scope


URL = "https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application"


def test_manual_scope_cannot_widen_greenhouse_or_other_job_access():
    client = ManualATSCLI(URL)
    assert client.allowed_url(URL + "?embed=true&ref=synthetic")
    assert not client.allowed_url(URL.replace("example/", "other/"))
    assert not client.allowed_url(URL.replace("555555555555", "555555555556"))
    assert not client.allowed_url(URL.replace("https://", "http://"))
    assert not client.allowed_url(URL.replace("jobs.ashbyhq.com", "jobs.ashbyhq.com.evil.invalid"))
    assert not BrowserUseCLI().allowed_url(URL)
    with pytest.raises(ValueError, match="exact Ashby"):
        ManualATSCLI(URL.replace("https://", "https://user:secret@"))
    assert not matches_scope(URL, {**application_scope(URL), "origin": "https://evil.invalid"})
    with pytest.raises(ValueError, match="exact Ashby"):
        ManualATSCLI(URL.replace("11111111-2222-3333-4444-555555555555", "-"*36))


HTML = '''<!doctype html><html><title>Synthetic Ashby application</title>
<style>button{padding:12px;margin:8px}.ashby-application-form-field-entry{padding:10px}
input[type=radio],input[type=checkbox]{display:none}label{display:block;padding:10px}</style>
<form id="application"><input type="file" id="autofill">
<div class="ashby-application-form-field-entry" data-field-path="graduation-date">
 <label class="ashby-application-form-question-title _required_123">Graduation Date</label><input required></div>
<div class="ashby-application-form-field-entry" data-field-path="location">
 <label class="ashby-application-form-question-title">Current Location</label><input role="combobox"></div>
<div class="ashby-application-form-field-entry" data-field-path="name">
 <label class="ashby-application-form-question-title _required_123" for="name">Full Name</label>
 <input id="name" required></div>
<div class="ashby-application-form-field-entry" data-field-path="authorized">
 <label class="ashby-application-form-question-title _required_123">Authorized to work?</label>
 <button type="button" class="ashby-application-form-input-yesno-option" data-option="yes" aria-pressed="false" onclick="pick(this)">Yes</button>
 <button type="button" class="ashby-application-form-input-yesno-option" data-option="no" aria-pressed="false" onclick="pick(this)">No</button>
 <input type="checkbox" name="authorized" style="display:none"></div>
<div class="ashby-application-form-field-entry" data-field-path="sponsor">
 <label class="ashby-application-form-question-title _required_123">Need sponsorship?</label>
 <button type="button" class="ashby-application-form-input-yesno-option" data-option="yes" aria-pressed="false" onclick="pick(this)">Yes</button>
 <button type="button" class="ashby-application-form-input-yesno-option" data-option="no" aria-pressed="false" onclick="pick(this)">No</button></div>
<div data-field-path="graduation"><fieldset class="ashby-application-form-input-radio-group">
 <legend class="ashby-application-form-question-title _required_123">Graduation period</legend>
 <label><input type="radio" name="graduation" id="spring">Spring 2027</label>
 <label><input type="radio" name="graduation" id="fall">Fall 2027</label></fieldset></div>
<div data-field-path="teams">
 <label class="ashby-application-form-question-title _required_123">Teams</label>
 <label><input type="checkbox" id="backend">Backend</label>
 <label><input type="checkbox" id="ml">Machine Learning</label></div>
<div class="ashby-application-form-field-entry" data-field-path="resume">
 <label class="ashby-application-form-question-title _required_123" for="_systemfield_resume">Resume</label>
 <input type="file" id="_systemfield_resume" required style="display:none"></div>
<button type="submit">Submit Application</button></form>
<script>window.submissions=0;document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
function pick(button){for(const b of button.parentElement.querySelectorAll('button'))b.setAttribute('aria-pressed',b===button?'true':'false')};</script>
</html>'''


@pytest.mark.parametrize("foreground", [False, True])
def test_manual_ashby_groups_upload_and_submit_guard(tmp_path, foreground):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    from playwright.sync_api import sync_playwright
    from pypdf import PdfWriter
    resume = tmp_path / "synthetic-resume.pdf"
    writer = PdfWriter(); writer.add_blank_page(width=612, height=792); writer.write(resume)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        requests = []
        def serve(route):
            requests.append(route.request.url)
            route.fulfill(status=200, content_type="text/html", body=HTML)
        page.route("**/*", serve)
        page.goto(URL)
        session = page.context.new_cdp_session(page)
        activated = []
        helpers = {"cdp": lambda method, **params: session.send(method, params),
                   "js": page.evaluate, "wait": lambda seconds: page.wait_for_timeout(seconds*1000),
                   "click_at_xy": lambda x, y: page.mouse.click(x, y),
                   "list_tabs": lambda: [{"url": URL, "targetId": "fixture-tab"}],
                   "switch_tab": lambda target: None,
                   "activate_tab": lambda target: activated.append(target),
                   "current_tab": lambda: {"targetId": "fixture-tab"}}
        scope = application_scope(URL)
        def call(operation, **payload):
            return dispatch({"operation": operation, "scope": scope, "foreground": foreground, **payload}, helpers)
        try:
            assert call("open", url=URL)["guarded"] is True
            observed = call("observe")
            fields = {f["ref"]: f for f in observed["fields"]}
            assert "autofill" not in fields
            assert fields["ashby:authorized"]["required"] is True
            assert fields["ashby:teams"]["type"] == "multiselect"
            assert fields["ashby:graduation"]["required"] is True
            assert fields["ashby:teams"]["required"] is True
            assert fields["_systemfield_resume"]["required"] is True
            assert fields["ashby:graduation-date:control:0"]["required"] is True
            assert call("fill", field=fields["ashby:graduation-date:control:0"], value="May 2027")["verified"]
            assert fields["ashby:location:control:0"]["type"] == "combobox"
            with pytest.raises(ValueError, match="approved query and exact choice"):
                call("fill", field=fields["ashby:location:control:0"], value="Synthetic City")
            assert call("fill", field=fields["name"], value="Sam Example")["verified"]
            assert call("fill", field=fields["ashby:authorized"], value=True)["verified"]
            assert call("fill", field=fields["ashby:sponsor"], value=False)["verified"]
            assert page.locator('[data-field-path=authorized] [data-option=yes]').get_attribute("aria-pressed") == "true"
            assert page.locator('[data-field-path=sponsor] [data-option=no]').get_attribute("aria-pressed") == "true"
            assert call("describe", field=fields["ashby:graduation"])["choices"] == ["Spring 2027", "Fall 2027"]
            assert call("fill", field=fields["ashby:graduation"], value="Fall 2027")["verified"]
            assert not page.locator("#fall").is_visible()
            assert page.locator("#fall").is_checked() and not page.locator("#spring").is_checked()
            assert call("fill", field=fields["ashby:teams"], value=["Backend", "Machine Learning"])["verified"]
            assert call("fill", field=fields["ashby:teams"], value=["Machine Learning"])["verified"]
            assert not page.locator("#backend").is_checked() and page.locator("#ml").is_checked()
            with pytest.raises(ValueError, match="approved selection"):
                call("fill", field=fields["ashby:teams"], value=[])
            assert call("fill", field=fields["_systemfield_resume"], value=str(resume))["verified"]
            assert page.locator("#autofill").evaluate("e=>e.files.length") == 0
            with pytest.raises(ValueError, match="uniquely match"):
                call("fill", field=fields["ashby:sponsor"], value="Maybe")
            with pytest.raises(ValueError, match="changed"):
                call("fill", field={**fields["name"], "label": "Arbitrary question"}, value="No")
            with pytest.raises(ValueError, match="Terminal submission"):
                call("next", button={"ref": "123", "label": "Submit Application"})
            page.get_by_role("button", name="Submit Application").click()
            page.evaluate("document.querySelector('form').submit()")
            assert page.evaluate("window.submissions") == 0
            assert activated == ["fixture-tab"]*len(activated)
            assert bool(activated) is foreground
            activation_count = len(activated)
            page.goto(URL.replace("555555555555", "555555555556"))
            with pytest.raises(ValueError, match="differs from approved"):
                call("fill", field=fields["name"], value="Sam")
            assert len(activated) == activation_count
            assert requests == [URL, URL.replace("555555555555", "555555555556")]
        finally:
            browser.close()


def test_async_portaled_autocomplete_uses_only_owned_options_and_verifies_commit():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    from playwright.sync_api import sync_playwright
    choice = "Example City, Example State, United States"
    html = '''<style>[role=option]{padding:12px}</style>
    <div class="ashby-application-form-section-container"><div data-field-path="location">
      <label class="ashby-application-form-question-title">Current Location</label>
      <input role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls=":r0:"
       oninput="loadOptions(this)" onclick="loadOptions(this)">
    </div></div>
    <div role="listbox" id="foreign"><div role="option">CHOICE</div></div>
    <div role="listbox" id=":r0:"></div>
    <script>
    window.commits=0;
    function loadOptions(input){
      const query=input.value;document.getElementById(':r0:').innerHTML='';input.setAttribute('aria-expanded','true');
      setTimeout(()=>{
        const count=query==='Ambiguous'?2:1;
        const label=query==='Missing'?'Other City':'CHOICE';
        for(let i=0;i<count;i++){
          const option=document.createElement('div');option.setAttribute('role','option');option.textContent=label;
          option.onclick=()=>{if(query==='Uncommitted')return;window.commits++;input.value=label;
            input.setAttribute('aria-expanded','false');document.getElementById(':r0:').innerHTML=''};
          document.getElementById(':r0:').append(option);
        }
      },300);
    }
    document.addEventListener('keydown',e=>{if(e.key==='Escape'){
      document.querySelector('input').setAttribute('aria-expanded','false');document.getElementById(':r0:').innerHTML='';
    }});
    </script>'''.replace("CHOICE", choice)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        page.goto(URL)
        session = page.context.new_cdp_session(page)
        helpers = {"cdp": lambda method, **params: session.send(method, params),
                   "js": page.evaluate, "wait": lambda seconds: page.wait_for_timeout(seconds*1000),
                   "click_at_xy": lambda x, y: page.mouse.click(x, y),
                   "list_tabs": lambda: [{"url": URL, "targetId": "fixture-tab"}],
                   "switch_tab": lambda target: None,
                   "current_tab": lambda: {"targetId": "fixture-tab"}}
        scope = application_scope(URL)
        def call(operation, **payload):
            return dispatch({"operation": operation, "scope": scope, **payload}, helpers)
        try:
            call("open", url=URL)
            field = call("observe")["fields"][0]
            assert field["ref"] == "ashby:location:control:0"
            assert call("fill", field=field, value={"query": "Example City", "choice": choice}) == {"verified": True, "selected": choice}
            assert page.locator('input').input_value() == choice
            assert page.locator('input').get_attribute("aria-expanded") == "false"
            assert page.evaluate("window.commits") == 1
            assert call("describe", field=field)["choices"] == [choice]
            assert page.locator('input').input_value() == choice
            assert page.locator('input').get_attribute("aria-expanded") == "false"
            with pytest.raises(ValueError, match="ambiguous"):
                call("fill", field=field, value={"query": "Ambiguous", "choice": choice})
            assert page.evaluate("window.commits") == 1
            with pytest.raises(ValueError, match="absent"):
                call("fill", field=field, value={"query": "Missing", "choice": choice})
            assert page.evaluate("window.commits") == 1
            with pytest.raises(ValueError, match="committed choice"):
                call("fill", field=field, value={"query": "Uncommitted", "choice": choice})
            assert page.locator('input').input_value() == "Uncommitted"
            assert page.evaluate("window.commits") == 1
        finally:
            browser.close()


def test_classless_field_wrappers_without_form_do_not_duplicate_or_absorb_nested_groups():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    from playwright.sync_api import sync_playwright
    html = '''<style>input[type=radio]{display:none}</style>
    <div class="ashby-application-form-section-container">
      <div data-field-path="outer"><div class="ashby-application-form-section-container">
        <div data-field-path="degree"><fieldset><legend class="ashby-application-form-question-title _required_123">Degree</legend>
          <label><input type="radio" name="degree" id="masters">Master's</label></fieldset></div>
        <div data-field-path="internships"><fieldset><legend class="ashby-application-form-question-title _required_123">Internship count</legend>
          <label><input type="radio" name="count" id="count">2</label></fieldset></div>
      </div></div>
    </div>
    <div data-field-path="unrelated"><label class="ashby-application-form-question-title">Other website form</label><input id="foreign"></div>'''
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html)
            fields = page.evaluate(ASHBY_FIELDS)
            assert [f["ref"] for f in fields] == ["ashby:degree", "ashby:internships"]
            assert [f["options"][0]["label"] for f in fields] == ["Master's", "2"]
            assert all(f["required"] for f in fields)
        finally:
            browser.close()
