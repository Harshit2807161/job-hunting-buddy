"""Synthetic Workday wizard controls; these are not live-site validation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os

import pytest

from jhb.applications.browser import GUARD_SCRIPT
from jhb.applications.workday_runtime import dispatch

URL = "https://example.wd5.myworkdayjobs.com/en-US/Careers/job/San-Diego/Engineer_JR123456/apply"
HTML = """<!doctype html><html><body><style>input,button,textarea{display:block;margin:9px;padding:5px} [hidden]{display:none!important}</style>
<h1>My Experience</h1><section id=experience><h2>Work Experience</h2>
<label for=workExperience-6--jobTitle>Job Title</label><input id=workExperience-6--jobTitle required>
<label for=workExperience-6--companyName>Company</label><input id=workExperience-6--companyName required>
<label for=workExperience-6--roleDescription>Role Description</label><textarea id=workExperience-6--roleDescription></textarea>
<label for=workExperience-6--startDate-dateSectionMonth-input>Start Date Month</label><input id=workExperience-6--startDate-dateSectionMonth-input aria-label='Start Date Month'>
<input id=workExperience-6--startDate-dateSectionYear-input aria-label='Start Date Year'>
<button type=button onclick="this.insertAdjacentHTML('beforebegin','<input id=workExperience-7--jobTitle aria-label=JobTitle>')">Add Another</button></section>
<section id=education><h2>Education</h2><label for=education-10--schoolName>School</label><input id=education-10--schoolName required>
<button id=education-10--degree type=button aria-haspopup=listbox aria-label='Degree Select One Required' onclick="document.querySelector('#degree-options').hidden=false;this.setAttribute('aria-expanded','true')">Select One</button>
<div id=degree-options role=listbox hidden><div role=option tabindex=0 onclick="const b=document.querySelector('[id=education-10--degree]');b.innerText='M.S.';b.setAttribute('aria-label','Degree M.S. Required');this.parentElement.hidden=true;b.setAttribute('aria-expanded','false')">M.S.</div></div>
<button type=button onclick="this.insertAdjacentHTML('beforebegin','<input id=education-11--schoolName aria-label=School>')">Add Another</button></section>
<div><h3>Resume/CV</h3><input type=file data-automation-id=file-upload-input-ref></div>
<label for=name--legalName--firstName>First Name</label><input id=name--legalName--firstName required>
<fieldset><legend>Disability status</legend><label><input id=disability-no name=disability type=radio value=no>No, I do not have a disability</label><label><input id=disability-decline name=disability type=radio value=decline>I do not want to answer</label></fieldset>
<form id=final><button type=submit>Submit</button></form>
<button type=button id=continue onclick="window.continued++">Save and Continue</button>
<script>window.continued=0;window.submissions=0;document.querySelector('#final').addEventListener('submit',e=>{e.preventDefault();window.submissions++});</script>
</body></html>"""


@contextmanager
def fixture_runtime(html=HTML):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(__import__('pathlib').Path(__file__).resolve().parents[2]/".local-browsers"))
    with ThreadPoolExecutor(max_workers=1) as lane:
        def start():
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start(); browser = pw.chromium.launch(); page = browser.new_page()
            page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
            page.goto(URL); page.evaluate(GUARD_SCRIPT)
            session = page.context.new_cdp_session(page)
            target = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
            def cdp(method, **params):
                params.pop("_response_timeout", None)
                return session.send(method, params)
            helpers = {"cdp": cdp, "js": page.evaluate, "wait": lambda seconds: page.wait_for_timeout(seconds*1000),
                       "click_at_xy": page.mouse.click, "switch_tab": lambda owned: None if owned==target else pytest.fail("Wrong tab"),
                       "current_tab": lambda: {"targetId": target, "url": page.url},
                       "list_tabs": lambda: [{"targetId": target, "url": page.url}], "jhb_cdp_timeout": 15}
            return pw, browser, page, target, helpers
        pw, browser, page, target, helpers = lane.submit(start).result()
        def call(operation, **payload):
            return lane.submit(dispatch, {"operation": operation, "approved_url": URL, "target_id": target, **payload}, helpers).result()
        def inspect(expression):
            return lane.submit(page.evaluate, expression).result()
        try:
            yield call, inspect, helpers, lane
        finally:
            lane.submit(browser.close).result(); lane.submit(pw.stop).result()


def test_workday_observed_repeater_labels_dates_upload_and_safe_continuation(tmp_path):
    with fixture_runtime() as (call, inspect, helpers, lane):
        snapshot = call("observe")
        assert snapshot["experience_step"] is True
        fields = {field["ref"]: field for field in snapshot["fields"]}
        assert fields["school--0"]["label"] == "School" and fields["degree--0"]["label"] == "Degree"
        assert fields["degree--0"]["required"] is True
        for ref, value in [("name--legalName--firstName", "Synthetic"), ("workExperience-6--jobTitle", "Software Engineer"),
                           ("workExperience-6--roleDescription", "First verified bullet\nSecond verified bullet"),
                           ("workday-date:workExperience-6--startDate", "2025-02")]:
            assert call("fill", field=fields[ref], value=value)["verified"] is True
        assert inspect("document.getElementById('workExperience-6--startDate-dateSectionMonth-input').value") == "2"
        assert inspect("document.getElementById('workExperience-6--startDate-dateSectionYear-input').value") == "2025"
        assert call("describe", field=fields["degree--0"])["choices"] == ["M.S."]
        assert call("fill", field=fields["degree--0"], value="M.S.")["verified"] is True
        assert next(f for f in call("observe")["fields"] if f["ref"]=="degree--0")["label"] == "Degree"
        resume = tmp_path/"synthetic-resume.pdf"; resume.write_bytes(b"%PDF-1.4\nSynthetic resume")
        uploaded = call("fill", field=fields["workday:file:0"], value=str(resume))
        assert uploaded["verified"] is True and uploaded["upload_receipt"]["filename"] == resume.name
        assert call("fill", field=fields["workday-radio:disability"], value="I do not want to answer")["verified"] is True
        continuation = next(b for b in snapshot["buttons"] if b["label"] == "Save and Continue")
        call("next", button=continuation)
        assert inspect("window.continued") == 1 and inspect("window.submissions") == 0
        submit = next(b for b in snapshot["buttons"] if b["label"] == "Submit")
        with pytest.raises(ValueError, match="cannot submit"):
            call("next", button=submit)
        assert inspect("window.__jhbGuard") is True


def test_workday_repeater_addition_is_section_scoped_and_never_deletes_existing_rows():
    with fixture_runtime() as (call, inspect, helpers, lane):
        assert call("records", kind="education", count=2)["count"] == 2
        assert inspect("document.querySelectorAll('input[id^=workExperience-][id$=--jobTitle]').length") == 1
        assert call("records", kind="workExperience", count=2)["count"] == 2
        with pytest.raises(ValueError, match="preserve them"):
            call("records", kind="education", count=1)
        assert inspect("document.querySelectorAll('input[id^=education-][id$=--schoolName]').length") == 2


def test_workday_scope_guard_and_document_labels_are_not_guessed(tmp_path):
    with fixture_runtime(HTML.replace("<h3>Resume/CV</h3>", "<h3>Photo</h3>")) as (call, inspect, helpers, lane):
        field = next(f for f in call("observe")["fields"] if f["type"] == "file")
        assert field["label"] == "Unlabeled document upload"
        resume = tmp_path/"resume.pdf"; resume.write_bytes(b"%PDF-1.4\nSynthetic")
        with pytest.raises(ValueError, match="known document"):
            call("fill", field=field, value=str(resume))
        with pytest.raises(ValueError, match="Invalid exact"):
            call("observe", approved_url=URL.replace("JR123456", "not-a-job"))
        inspect("window.__jhbGuard=false")
        with pytest.raises(ValueError, match="guard changed"):
            call("observe")


def test_workday_unknown_login_consent_is_handoff_and_no_registration():
    html = "<form role=dialog><input type=email id=email data-automation-id=email><input type=password id=password><input type=checkbox required id=new-consent><button>Sign In</button></form>"
    with fixture_runtime(html) as (call, inspect, helpers, lane):
        assert call("observe")["handoff"] == "waiting_login"
        with pytest.raises(ValueError, match="site-specific"):
            call("authenticate", username="synthetic@example.test", password="fixture-only")
        policy = {"status": "verified", "source": "Synthetic explicit exception", "value": {
            "origin": "https://example.wd5.myworkdayjobs.com", "method": "password", "reuse_existing": True}}
        assert call("authenticate", username="synthetic@example.test", password="fixture-only", approved_exception=policy)["authenticated"] is False
        assert inspect("document.getElementById('email').value") == ""


@pytest.mark.parametrize("overlay", ["click_filter", "unknown_overlay"])
def test_workday_existing_login_uses_only_canonical_named_overlay_in_approved_form(overlay):
    html = """<body><style>input{display:block;margin:15px} .control{position:relative;width:140px;height:45px} .control button,.control div{position:absolute;inset:0}</style>
      <button type=button data-automation-id=utilityButtonSignIn>Sign In</button>
      <form role=dialog id=login><input id=email type=email data-automation-id=email required><input id=password type=password required>
      <div class=control><button type=button aria-label='Sign In' onclick='window.nativeClicks++'>Sign In</button>
      <div data-automation-id='OVERLAY' aria-label='Sign In' role=button tabindex=0 onclick="window.signins++;document.getElementById('login').remove()">Sign In</div></div></form>
      <script>window.signins=0;window.nativeClicks=0;</script></body>""".replace("OVERLAY", overlay)
    with fixture_runtime(html) as (call, inspect, helpers, lane):
        policy = {"status": "verified", "source": "Synthetic explicit exception", "value": {
            "origin": "https://example.wd5.myworkdayjobs.com", "method": "password", "reuse_existing": True}}
        result = call("authenticate", username="synthetic@example.test", password="fixture-only", approved_exception=policy)
        assert result["authenticated"] is (overlay == "click_filter")
        assert inspect("window.nativeClicks") == 0
        assert inspect("window.signins") == int(overlay == "click_filter")
        if overlay == "unknown_overlay":
            assert inspect("document.getElementById('email').value") == ""
            assert inspect("document.getElementById('password').value") == ""
        assert inspect("window.__jhbGuard") is True


@pytest.mark.parametrize("committed", [True, False])
def test_workday_search_catalog_requires_retained_committed_chip_not_query_text(committed):
    option = "<div role=option tabindex=0 onclick=\"document.querySelector('#selected').innerText='Computer Science';document.querySelector('#major').value='';this.parentElement.hidden=true\">Computer Science</div>" if committed else ""
    html = "<label for=major>Field of Study</label><div><span id=selected data-automation-id=promptSelectionLabel></span><input id=major type=selectinput required onkeydown=\"if(event.key==='ArrowDown')document.querySelector('#options').hidden=false\"><div id=options role=listbox hidden>"+option+"</div></div>"
    with fixture_runtime(html) as (call, inspect, helpers, lane):
        field = call("observe")["fields"][0]
        assert field["type"] == "combobox" and field["widget"] == "catalog"
        if committed:
            assert call("fill", field=field, value="Computer Science")["verified"] is True
            assert inspect("document.querySelector('#major').value") == ""
            assert inspect("document.querySelector('#selected').innerText") == "Computer Science"
        else:
            with pytest.raises(ValueError, match="absent or ambiguous"):
                call("fill", field=field, value="Computer Science")
            assert inspect("document.querySelector('#major').value") == "Computer Science"
            assert inspect("document.querySelector('#selected').innerText") == ""


MODERN_CATALOG_HTML = """<body><style>input,button{display:block;padding:8px;margin:8px} [hidden]{display:none!important}</style>
<label for=source--source>How Did You Hear About Us?</label>
<div data-automation-id=multiselectInputContainer>
 <div><input id=source--source data-uxi-widget-type=selectinput data-uxi-multiselect-id=source aria-required=true
 onkeydown="if(event.key==='ArrowDown')document.querySelector('#choices').hidden=false">
 <div data-automation-id=promptSelectionLabel></div></div>
 <ul role=listbox data-automation-id=selectedItemList data-uxi-multiselect-id=source>
  <li><div role=option data-automation-id=selectedItem><p id=source-chip data-automation-id=promptOption></p></div></li>
 </ul>
</div>
<label for=phoneNumber--countryPhoneCode>Country Phone Code</label>
<div data-automation-id=multiselectInputContainer>
 <input id=phoneNumber--countryPhoneCode data-uxi-widget-type=selectinput data-uxi-multiselect-id=phone aria-required=true>
 <div data-automation-id=promptSelectionLabel></div>
 <ul role=listbox data-automation-id=selectedItemList data-uxi-multiselect-id=phone>
  <li><div role=option data-automation-id=selectedItem><p data-automation-id=promptOption>United States of America (+1)</p></div></li>
 </ul>
</div>
<label for=phoneNumber--phoneNumber>Phone Number</label><input type=text id=phoneNumber--phoneNumber name=phoneNumber aria-required=true>
<label for=other-phone>Phone Number</label><input type=text id=other-phone name=phoneNumber>
<div role=listbox id=choices hidden><div role=option tabindex=0 onclick="document.querySelector('#source-chip').innerText='Employer Website';document.querySelector('[id=source--source]').value='';this.parentElement.hidden=true;window.selections++">Employer Website</div></div>
<script>window.selections=0;window.inputEvents=0;document.addEventListener('input',()=>window.inputEvents++)</script></body>"""


def test_workday_modern_catalog_retains_own_chip_and_national_phone_binding():
    from jhb.applications.booklet import answer
    from jhb.applications.planner import deterministic_plan
    with fixture_runtime(MODERN_CATALOG_HTML) as (call, inspect, helpers, lane):
        snapshot = call("observe")
        fields = {f["ref"]: f for f in snapshot["fields"]}
        source, country = fields["source--source"], fields["phoneNumber--countryPhoneCode"]
        assert source["type"] == country["type"] == "combobox"
        assert source["widget"] == country["widget"] == "catalog"
        assert fields["phoneNumber--phoneNumber"]["type"] == "tel"
        assert fields["other-phone"]["type"] == "text"
        answers = {"identity.phone": answer("+1 555 123 4567", "Synthetic verified source"),
                   "identity.phone_national": answer("(555) 123-4567", "Synthetic verified source")}
        plan = deterministic_plan({"fields": [fields["phoneNumber--phoneNumber"]], "buttons": []}, answers)
        assert plan["bindings"][0]["answer_key"] == "identity.phone_national"
        assert call("fill", field=country, value="United States of America (+1)") == {"verified": True, "already_retained": True}
        assert inspect("window.inputEvents") == 0 and inspect("window.selections") == 0
        assert call("fill", field=source, value="Employer Website")["verified"] is True
        assert inspect("window.selections") == 1
        assert inspect("document.querySelector('[id=source--source]').value") == ""
        assert call("fill", field=source, value="Employer Website")["already_retained"] is True
        assert inspect("window.selections") == 1


@pytest.mark.parametrize("damage", ["foreign_key", "query_only", "invalid"])
def test_workday_catalog_reuse_rejects_mismatched_owner_search_query_or_invalid_chip(damage):
    with fixture_runtime(MODERN_CATALOG_HTML) as (call, inspect, helpers, lane):
        country = next(f for f in call("observe")["fields"] if f["ref"] == "phoneNumber--countryPhoneCode")
        if damage == "foreign_key":
            inspect("document.querySelector('[data-automation-id=selectedItemList][data-uxi-multiselect-id=phone]').setAttribute('data-uxi-multiselect-id','foreign')")
        elif damage == "query_only":
            inspect("document.querySelector('[id=phoneNumber--countryPhoneCode]').value='Uncommitted query'")
        else:
            inspect("document.querySelector('[id=phoneNumber--countryPhoneCode]').setAttribute('aria-invalid','true')")
        with pytest.raises(ValueError, match="ownership is ambiguous"):
            call("fill", field=country, value="United States of America (+1)")


def test_workday_required_radio_group_is_owned_and_does_not_leak_to_optional_neighbors():
    html = """<fieldset><legend id=prior-label>Previously employed?</legend>
      <div aria-required=true aria-labelledby=prior-label>
       <input id=prior-yes name=prior type=radio value=true><label for=prior-yes>Yes</label>
       <input id=prior-no name=prior type=radio value=false><label for=prior-no>No</label>
      </div></fieldset>
      <fieldset><legend>Optional survey</legend><div aria-required=true aria-labelledby=other>
       <input id=optional-yes name=optional type=radio value=true><label for=optional-yes>Yes</label>
       <input id=optional-no name=optional type=radio value=false><label for=optional-no>No</label>
       <input id=unrelated name=unrelated type=radio value=x><label for=unrelated>Different question</label>
      </div></fieldset>"""
    with fixture_runtime(html) as (call, inspect, helpers, lane):
        fields = {f["ref"]: f for f in call("observe")["fields"]}
        assert fields["workday-radio:prior"]["required"] is True
        assert fields["workday-radio:optional"]["required"] is False
        assert fields["workday-radio:unrelated"]["required"] is False


def test_workday_empty_education_section_adds_approved_rows_without_guessing_other_add_buttons():
    html = "<section><h2>Education</h2><button onclick=\"this.insertAdjacentHTML('beforebegin','<input id=education-0--schoolName aria-label=School>')\">Add</button></section><section><h2>Work Experience</h2><button onclick='window.wrong++'>Add</button></section><script>window.wrong=0</script>"
    with fixture_runtime(html) as (call, inspect, helpers, lane):
        assert call("records", kind="education", count=1)["count"] == 1
        assert inspect("window.wrong") == 0


def test_workday_hidden_renderer_wakes_only_owned_guarded_tab_before_safe_press():
    with fixture_runtime() as (call, inspect, helpers, lane):
        snapshot = call("observe")
        continuation = next(b for b in snapshot["buttons"] if b["label"] == "Save and Continue")
        original = helpers["cdp"]
        awake = {"value": False}; activations = []
        def cdp(method, **params):
            if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseMoved" and not awake["value"]:
                raise TimeoutError("Input.dispatchMouseEvent timed out")
            return original(method, **params)
        def activate(target):
            assert helpers["current_tab"]()["targetId"] == target
            assert helpers["js"]("window.__jhbGuard===true") is True
            awake["value"] = True; activations.append(target)
        helpers.update(cdp=cdp, activate_tab=activate)
        call("next", button=continuation)
        assert len(activations) == 1 and inspect("window.continued") == 1
        assert inspect("window.submissions") == 0 and inspect("window.__jhbGuard") is True


def test_real_worker_prepares_synthetic_workday_wizard_and_hands_off_closed_review_cards(tmp_path, monkeypatch):
    import asyncio
    from jhb.applications.booklet import answer
    from jhb.applications.planner import deterministic_plan
    from jhb.applications.worker import prepare
    from jhb.applications.workday import WorkdayCLI
    html = HTML.replace('onclick="window.continued++"', 'onclick="window.continued++;document.body.innerHTML=\'<h1>Review</h1><button type=button onclick=window.submissions++>Submit</button>\'"')
    resume = tmp_path/"synthetic-sde.pdf"; resume.write_bytes(b"%PDF-1.4\nSynthetic SDE resume")
    values = {"identity.first_name": "Synthetic", "documents.resume": str(resume),
              "education.0.school": "Synthetic University", "education.0.degree": "M.S.",
              "experience.0.title": "Software Engineer", "experience.0.company": "Synthetic Company",
              "experience.0.summary": "Verified first bullet\nVerified second bullet", "experience.0.start_date": "2025-02"}
    answers = {key: answer(value, "Synthetic original source") for key, value in values.items()}
    answers["custom.fixture_disability"] = {**answer("I do not want to answer", "Synthetic explicit disclosure choice"),
        "question": "Disability status", "field_ref": "workday-radio:disability"}
    with fixture_runtime(html) as (call, inspect, helpers, lane):
        client = WorkdayCLI(URL, experience_count=1)
        async def invoke(operation, **payload):
            return call(operation, **payload)
        monkeypatch.setattr(client, "invoke", invoke)
        result, actions = asyncio.run(prepare(None, {"url": URL}, answers, deterministic_plan, None, max_steps=5, cli_actions=client))
        assert result["state"] == "unsupported" and result.get("missing", []) == []
        assert "retained-answer and document auditing" in result["reason"]
        assert {record["key"] for record in result["filled"]} == set(values)|{"custom.fixture_disability"}
        uploaded = next(record for record in result["filled"] if record["key"] == "documents.resume")
        assert uploaded["document_sha256"] == __import__('hashlib').sha256(resume.read_bytes()).hexdigest()
        assert uploaded["upload_receipt"]["sha256"] == uploaded["document_sha256"]
        assert uploaded["upload_receipt"]["filename"] == resume.name
        assert inspect("window.continued") == 1 and inspect("window.submissions") == 0
        assert inspect("window.__jhbGuard") is True


@pytest.mark.parametrize("damage", ["missing", "hash", "filename", "nonce"])
def test_workday_worker_rejects_invalid_structured_upload_proof(tmp_path, monkeypatch, damage):
    import asyncio
    from jhb.applications.booklet import answer
    from jhb.applications.cli_browser import BrowserOperationError
    from jhb.applications.planner import deterministic_plan
    from jhb.applications.worker import prepare
    from jhb.applications.workday import WorkdayCLI
    resume = tmp_path / "synthetic-sde.pdf"
    resume.write_bytes(b"%PDF-1.4\nSynthetic resume")
    answers = {"documents.resume": answer(str(resume), "Synthetic exact-role document")}
    html = '<h3>Resume/CV</h3><input id=resume type=file aria-label="Resume/CV"><button type=button>Submit</button>'
    with fixture_runtime(html) as (call, inspect, helpers, lane):
        client = WorkdayCLI(URL)
        async def invoke(operation, **payload):
            result = call(operation, **payload)
            if operation == "fill":
                if damage == "missing":
                    result.pop("upload_receipt")
                else:
                    key = {"hash": "sha256", "filename": "filename", "nonce": "nonce"}[damage]
                    result["upload_receipt"][key] = "wrong-proof"
            return result
        monkeypatch.setattr(client, "invoke", invoke)
        with pytest.raises(BrowserOperationError, match="field repair") as error:
            asyncio.run(prepare(None, {"url": URL}, answers, deterministic_plan, None, cli_actions=client))
        assert str(error.value.__cause__) == "Workday upload proof differs from the approved document binding"
        assert client._preparation_progress["snapshot"]({"events": []})["filled"] == []
        assert inspect("window.__jhbGuard") is True


def test_workday_original_expected_ms_and_completed_bs_dates_remain_distinct():
    with fixture_runtime() as (call, inspect, helpers, lane):
        inspect("""document.querySelector('#education').insertAdjacentHTML('beforeend',`
          <label for=education-11--schoolName>School</label><input id=education-11--schoolName>
          <input id=education-10--endDate-dateSectionMonth-input aria-label='End Date Month'>
          <input id=education-10--endDate-dateSectionYear-input aria-label='End Date Year'>
          <input id=education-11--endDate-dateSectionMonth-input aria-label='End Date Month'>
          <input id=education-11--endDate-dateSectionYear-input aria-label='End Date Year'>`)
        """)
        fields = {field["ref"]: field for field in call("observe")["fields"]}
        for ref, value in [("school--0", "Synthetic MS University"), ("school--1", "Synthetic BS University"),
                           ("end_date--0", "2026-12"), ("end_date--1", "2025-05")]:
            assert call("fill", field=fields[ref], value=value)["verified"] is True
        assert fields["end_date--0"]["record_index"] == 0
        assert fields["end_date--1"]["record_index"] == 1
        assert inspect("document.getElementById('education-10--endDate-dateSectionYear-input').value") == "2026"
        assert inspect("document.getElementById('education-11--endDate-dateSectionYear-input').value") == "2025"
        assert inspect("window.submissions") == 0


@pytest.mark.parametrize("label,state", [("Sign In", "waiting_login"), ("Submit", "unsupported")])
def test_empty_workday_landing_or_saved_review_cannot_be_reported_complete(label, state):
    html = f"<html><body><h1>Workday</h1><button onclick='window.clicks++'>{label}</button><script>window.clicks=0</script></body></html>"
    with fixture_runtime(html) as (call, inspect, helpers, lane):
        snapshot = call("observe")
        assert snapshot["fields"] == [] and snapshot["handoff"] == state
        assert inspect("window.clicks") == 0 and inspect("window.__jhbGuard") is True


SEGMENTED_DATE_HTML = '''<style>input{display:block;margin:8px;padding:5px}</style>
<label for=workExperience-0--jobTitle>Job Title</label><input id=workExperience-0--jobTitle>
<div id=date>
<input id=workExperience-0--startDate-dateSectionMonth-input aria-label='Start Date Month' maxlength=2>
<input id=workExperience-0--startDate-dateSectionYear-input aria-label='Start Date Year' maxlength=4>
</div><button type=button>Next section</button><form><button type=submit>Submit</button></form>
<script>
window.focusMoves=0;window.submissions=0;
const month=document.querySelector('[id$=Month-input]'),year=document.querySelector('[id$=Year-input]');
year.addEventListener('keydown',e=>{if(e.key==='Backspace'&&year.value===''){window.focusMoves++;month.focus();}});
document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++;};
</script>'''


def test_segmented_date_empty_year_backspace_moves_native_focus_but_replacement_does_not():
    with fixture_runtime(SEGMENTED_DATE_HTML) as (call, inspect, helpers, lane):
        # Demonstrate the widget's native failure: deleting an empty year moves
        # focus to Month. This is an actual key event, not a mocked runtime call.
        def legacy_empty_delete():
            cdp = helpers['cdp']
            root = cdp('DOM.getDocument')['root']['nodeId']
            node = cdp('DOM.querySelector', nodeId=root, selector='[id$=Year-input]')['nodeId']
            cdp('DOM.focus', nodeId=node)
            cdp('Input.dispatchKeyEvent', type='keyDown', key='a', code='KeyA', modifiers=4, commands=['selectAll'])
            cdp('Input.dispatchKeyEvent', type='keyUp', key='a', code='KeyA')
            cdp('Input.dispatchKeyEvent', type='keyDown', key='Backspace', code='Backspace')
            cdp('Input.dispatchKeyEvent', type='keyUp', key='Backspace', code='Backspace')
        lane.submit(legacy_empty_delete).result()
        assert inspect("document.activeElement.id.endsWith('Month-input')") is True
        assert inspect('window.focusMoves') == 1
        inspect("window.focusMoves=0")
        field = next(f for f in call('observe')['fields'] if f['type']=='date')
        assert call('fill', field=field, value='2025-09')['verified'] is True
        assert inspect("document.querySelector('[id$=Month-input]').value") == '9'
        assert inspect("document.querySelector('[id$=Year-input]').value") == '2025'
        assert inspect('window.focusMoves') == 0
        assert inspect('window.submissions') == 0 and inspect('window.__jhbGuard') is True


@pytest.mark.parametrize('corruption', ['sibling_value', 'blur_invalid'])
def test_segmented_date_rechecks_earlier_segments_after_year_and_whole_widget_blur(corruption):
    script = ("year.addEventListener('input',()=>{if(year.value.length===4)month.value='2';});" if corruption=='sibling_value'
              else "year.addEventListener('blur',()=>{if(year.value.length===4)month.setAttribute('aria-invalid','true');});")
    html = SEGMENTED_DATE_HTML.replace('</script>', script+'</script>')
    with fixture_runtime(html) as (call, inspect, helpers, lane):
        field = next(f for f in call('observe')['fields'] if f['type']=='date')
        with pytest.raises(ValueError, match='date did not retain an approved segment'):
            call('fill', field=field, value='2025-09')
        # Year retained correctly, but the later edit/blur invalidated Month.
        # Per-segment immediate checks alone would falsely approve the date.
        assert inspect("document.querySelector('[id$=Year-input]').value") == '2025'
        assert inspect("document.querySelector('[id$=Month-input]').value") == ('2' if corruption=='sibling_value' else '9')
        assert inspect('window.submissions') == 0 and inspect('window.__jhbGuard') is True


def test_segmented_date_does_not_invent_day_for_month_precision_source():
    html = SEGMENTED_DATE_HTML.replace('</div>', "<input id=workExperience-0--startDate-dateSectionDay-input aria-label='Start Date Day' maxlength=2></div>")
    with fixture_runtime(html) as (call, inspect, helpers, lane):
        field = next(f for f in call('observe')['fields'] if f['type']=='date')
        with pytest.raises(ValueError, match='explicitly approved day'):
            call('fill', field=field, value='2025-09')
        assert inspect("[...document.querySelectorAll('#date input')].map(e=>e.value)") == ['', '', '']
        assert call('fill', field=field, value='2025-09-14')['verified'] is True
        assert inspect("document.querySelector('[id$=Day-input]').value") == '14'
        assert inspect('window.submissions') == 0


def test_segmented_date_stops_before_typing_into_a_sibling_when_native_focus_moves():
    html = SEGMENTED_DATE_HTML.replace('</script>', "year.addEventListener('input',()=>{if(year.value.length===1)month.focus();});</script>")
    with fixture_runtime(html) as (call, inspect, helpers, lane):
        field = next(f for f in call('observe')['fields'] if f['type']=='date')
        with pytest.raises(ValueError, match='date did not retain an approved segment'):
            call('fill', field=field, value='2025-09')
        assert inspect("document.querySelector('[id$=Month-input]').value") == '9'
        assert inspect("document.querySelector('[id$=Year-input]').value") == '2'
        assert inspect('window.submissions') == 0 and inspect('window.__jhbGuard') is True
