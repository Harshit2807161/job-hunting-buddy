"""Exercise CLI runtime actions on synthetic DOM; no live-site requests."""
import os
import pytest

from jhb import config
from jhb.applications.cli_runtime import dispatch

HTML = '''<!doctype html><html lang="en"><title>Synthetic application</title>
<style>input{margin:12px;padding:8px}.select__value-container{margin:20px}.option{padding:10px}</style>
<form id="application"><label for="first_name">First Name</label><input id="first_name" required>
<label for="certify">Synthetic explicit certification</label><input id="certify" type="checkbox">
<label for="native_select">Native sponsorship question</label><select id="native_select"><option value="" disabled selected>Select</option><option value="yes">Yes</option><option value="no">No</option></select>
<label for="essay">Essay</label><textarea id="essay"></textarea>
<label id="state-label">State</label><div class="select__value-container">
<div class="select__single-value"></div><input id="state" role="combobox" aria-labelledby="state-label" aria-controls="options" aria-required="true" aria-expanded="false" aria-autocomplete="list" onfocus="openOptions()" onclick="openOptions()">
<div id="options" role="listbox"></div></div>
<label id="country-label">Country</label><div class="select__value-container" id="country-container"><div class="select__single-value"></div><input id="country" role="combobox" aria-labelledby="country-label" aria-controls="countries" onfocus="openCountry()" onclick="openCountry()"><div id="countries" role="listbox"></div></div>
<div class="file-upload" role="group" aria-labelledby="upload-label-resume" aria-required="true"><div class="upload-label" id="upload-label-resume">Resume/CV</div><label for="resume">Attach</label><input id="resume" type="file" accept="application/pdf" style="display:none" onchange="showUpload(this)"></div>
<div class="file-upload" role="group" aria-labelledby="upload-label-cover_letter" aria-required="false"><div class="upload-label" id="upload-label-cover_letter">Cover Letter</div><label for="cover_letter">Attach</label><input id="cover_letter" type="file" accept="application/pdf" style="display:none" onchange="showUpload(this)"></div>
<button type="submit">Submit application</button></form>
<script>
window.submissions=0;document.getElementById('application').onsubmit=e=>{e.preventDefault();window.submissions++};
function openOptions(){document.getElementById('state').setAttribute('aria-expanded','true');document.getElementById('options').innerHTML='<div role="option" class="option" onclick="chooseState()">California</div><div role="option" class="option">New York</div>';}
function chooseState(){document.querySelector('.select__single-value').textContent='California';document.getElementById('options').innerHTML='';document.getElementById('state').value='';document.getElementById('state').setAttribute('aria-expanded','false');}
function openCountry(){document.getElementById('countries').innerHTML='<div role="option" class="option" onclick="chooseCountry()">United States +1</div>';}
function chooseCountry(){document.querySelector('#country-container .select__single-value').innerHTML='<div class="iti__flag iti__us"></div><span>+1</span>';document.getElementById('countries').innerHTML='';document.getElementById('country').value='';}
window.removals=[];window.uploads=[];
function showUpload(input){window.uploads.push(input.id);const parent=input.closest('.file-upload');parent.querySelector('.file-upload__filename')?.remove();let p=document.createElement('p');p.textContent=input.files[0].name;let wrapper=document.createElement('div');wrapper.className='file-upload__filename';wrapper.appendChild(p);let remove=document.createElement('button');remove.type='button';remove.textContent='Remove file';remove.onclick=()=>{window.removals.push(input.id);input.value='';wrapper.remove()};wrapper.appendChild(remove);parent.appendChild(wrapper);}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){document.getElementById('options').innerHTML='';document.getElementById('state').setAttribute('aria-expanded','false')}});
</script></html>'''


@pytest.mark.parametrize("changed", [False, True])
def test_frozen_owned_tab_wakes_once_without_touching_other_tabs(changed):
    from jhb.applications.cli_runtime import GUARD_SCRIPT
    url = "https://job-boards.greenhouse.io/example/jobs/123"
    activated, scripts, reads = [], [], []
    def js(expression):
        if expression == "document.readyState":
            reads.append(expression)
            if not activated:
                raise RuntimeError("Runtime.evaluate timed out; expression: document.readyState")
            return "complete"
        if expression == GUARD_SCRIPT:
            scripts.append(expression)
        if expression == "window.__jhbGuard === true":
            return bool(scripts)
        if expression == "location.href":
            return url
    helpers = {"cdp":lambda *args,**kwargs:{}, "js":js, "wait":lambda seconds:None,
               "click_at_xy":lambda *args:pytest.fail("Unexpected click"),
               "list_tabs":lambda:[{"targetId":"owned-job","url":url},{"targetId":"unrelated","url":"https://example.test"}],
               "switch_tab":lambda target:None, "activate_tab":lambda target:activated.append(target),
               "current_tab":lambda:{"targetId":"owned-job","url":url if not changed else "https://example.test"}}
    if changed:
        with pytest.raises(ValueError,match="changed during recovery"):
            dispatch({"operation":"open","url":url},helpers)
        assert activated == scripts == []
    else:
        result=dispatch({"operation":"open","url":url},helpers)
        assert result["guarded"] and result["woke_tab"] and result["reused_tab"]
        assert activated == ["owned-job"] and len(reads)==2 and len(scripts)==1


def test_cli_runtime_verifies_custom_dropdown_upload_and_blocks_submit(tmp_path):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    from playwright.sync_api import sync_playwright
    from pypdf import PdfWriter
    resume = tmp_path / "synthetic-ml.pdf"
    writer = PdfWriter(); writer.add_blank_page(width=612, height=792); writer.write(resume)
    cover = tmp_path / "synthetic-cover.pdf"
    writer.write(cover)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        requests = []
        def serve(route):
            requests.append(route.request.url)
            route.fulfill(status=200, content_type="text/html", body=HTML)
        page.route("**/*", serve)
        url = "https://job-boards.greenhouse.io/synthetic-fixture/jobs/1234"
        page.goto(url)
        cdp = page.context.new_cdp_session(page)
        helpers = {"cdp": lambda method, **params: cdp.send(method, params),
                   "js": page.evaluate, "wait": lambda seconds: page.wait_for_timeout(seconds*1000),
                   "click_at_xy": lambda x, y: page.mouse.click(x, y),
                   "list_tabs": lambda: [{"url": url, "targetId": "fixture-tab"}],
                   "switch_tab": lambda target: None,
                   "current_tab": lambda: {"targetId": "fixture-tab"}}
        try:
            assert dispatch({"operation": "open", "url": url}, helpers)["reused_tab"]
            fields = {f["ref"]: f for f in dispatch({"operation": "observe"}, helpers)["fields"]}
            assert fields["resume"]["label"] == "Resume/CV"
            assert fields["resume"]["required"] is True
            assert fields["cover_letter"]["label"] == "Cover Letter"
            assert fields["cover_letter"]["required"] is False
            assert dispatch({"operation": "fill", "field": fields["first_name"], "value": "Sam"}, helpers)["verified"]
            assert page.locator("#first_name").input_value() == "Sam"
            assert dispatch({"operation": "fill", "field": fields["essay"], "value": "Explicit synthetic user essay"}, helpers)["verified"]
            assert page.locator("#essay").input_value() == "Explicit synthetic user essay"
            assert dispatch({"operation": "fill", "field": fields["certify"], "value": True}, helpers)["checked"] is True
            assert page.locator("#certify").is_checked()
            assert dispatch({"operation": "fill", "field": fields["certify"], "value": False}, helpers)["checked"] is False
            assert not page.locator("#certify").is_checked()
            with pytest.raises(ValueError, match="explicit boolean"):
                dispatch({"operation": "fill", "field": fields["certify"], "value": "false"}, helpers)
            assert dispatch({"operation": "describe", "field": fields["native_select"]}, helpers)["choices"] == ["Yes", "No"]
            assert dispatch({"operation": "fill", "field": fields["native_select"], "value": False}, helpers)["selected"] == "No"
            assert page.locator("#native_select").input_value() == "no"
            assert dispatch({"operation": "fill", "field": fields["native_select"], "value": True}, helpers)["selected"] == "Yes"
            assert page.locator("#native_select").input_value() == "yes"
            described = dispatch({"operation": "describe", "field": fields["state"]}, helpers)
            assert described["choices"] == ["California", "New York"]
            assert page.locator("#state").get_attribute("aria-expanded") == "false"
            assert page.locator("#state").input_value() == ""
            assert dispatch({"operation": "fill", "field": fields["state"], "value": "CA"}, helpers)["selected"] == "California"
            with pytest.raises(ValueError, match="absent from dropdown options"):
                dispatch({"operation": "fill", "field": fields["state"], "value": "Unknown State"}, helpers)
            assert page.locator("#state").input_value() == ""
            assert dispatch({"operation": "fill", "field": fields["country"], "value": "United States"}, helpers)["selected"] == "United States (+1)"
            assert dispatch({"operation": "fill", "field": fields["country"], "value": "United States"}, helpers)["verified"]
            uploaded = dispatch({"operation": "fill", "field": fields["resume"], "value": str(resume)}, helpers)
            assert uploaded["filename"] == resume.name
            assert dispatch({"operation": "fill", "field": fields["cover_letter"], "value": str(cover)}, helpers)["filename"] == cover.name
            virtual_resume = {"ref": "uploaded:Resume/CV", "label": "Resume/CV", "type": "file"}
            cached = dispatch({"operation": "fill", "field": virtual_resume, "value": str(resume), "upload_receipt": uploaded["upload_receipt"]}, helpers)
            assert cached["cached"] is True
            assert page.evaluate("window.removals") == []
            assert page.evaluate("window.uploads") == ["resume", "cover_letter"]
            # No receipt means a new client cannot trust a reused filename.
            replacement = dispatch({"operation": "fill", "field": virtual_resume, "value": str(resume)}, helpers)
            assert replacement["verified"]
            assert page.evaluate("window.removals") == ["resume"]
            assert page.locator("#cover_letter").evaluate("e=>e.files[0].name") == cover.name
            page.get_by_role("button", name="Submit application").click()
            page.evaluate("document.getElementById('application').submit()")
            assert page.evaluate("window.submissions") == 0
            with pytest.raises(ValueError, match="acknowledgement"):
                dispatch({"operation": "takeover"}, helpers)
            assert page.evaluate("window.__jhbGuard") is True
            assert requests == [url]
        finally:
            browser.close()


def test_cli_runtime_waits_for_delayed_education_rows_without_duplicate_clicks():
    """A delayed React-style row update must not fail after the old 250ms wait."""
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    from playwright.sync_api import sync_playwright
    html = '''<!doctype html><html lang="en"><title>Synthetic delayed education</title>
    <form id="application"><div class="education--container">
      <div class="education-row"><label for="school--0">School</label><input id="school--0"></div>
      <button type="button" onclick="addEducation()">Add another</button>
    </div><div class="experience--container">
      <button type="button" onclick="window.wrongAdds++">Add another</button>
    </div><button type="submit">Submit application</button></form>
    <script>
    window.educationAdds=0;window.wrongAdds=0;window.submissions=0;
    document.getElementById('application').onsubmit=e=>{e.preventDefault();window.submissions++};
    function addEducation(){
      window.educationAdds++;
      setTimeout(()=>{
        const section=document.querySelector('.education--container');
        const index=section.querySelectorAll('input[id^="school--"]').length;
        const row=document.createElement('div');row.className='education-row';
        const label=document.createElement('label');label.htmlFor='school--'+index;label.textContent='School';
        const input=document.createElement('input');input.id='school--'+index;
        row.append(label,input);section.insertBefore(row,section.querySelector('button'));
      },450);
    }
    </script></html>'''
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        requests = []
        def serve(route):
            requests.append(route.request.url)
            route.fulfill(status=200, content_type="text/html", body=html)
        page.route("**/*", serve)
        url = "https://job-boards.greenhouse.io/synthetic-fixture/jobs/4321"
        page.goto(url)
        cdp = page.context.new_cdp_session(page)
        helpers = {"cdp": lambda method, **params: cdp.send(method, params),
                   "js": page.evaluate, "wait": lambda seconds: page.wait_for_timeout(seconds * 1000),
                   "click_at_xy": lambda x, y: page.mouse.click(x, y),
                   "list_tabs": lambda: [{"url": url, "targetId": "fixture-tab"}],
                   "switch_tab": lambda target: None,
                   "current_tab": lambda: {"targetId": "fixture-tab"}}
        try:
            dispatch({"operation": "open", "url": url}, helpers)
            result = dispatch({"operation": "education", "count": 3}, helpers)
            assert result == {"supported": True, "rows": 3}
            assert page.locator('.education--container input[id^="school--"]').count() == 3
            assert page.evaluate("window.educationAdds") == 2
            assert page.evaluate("window.wrongAdds") == 0
            # A resumed preparation must preserve the rows already added.
            assert dispatch({"operation": "education", "count": 3}, helpers) == result
            assert page.evaluate("window.educationAdds") == 2
            assert page.evaluate("window.submissions") == 0
            assert requests == [url]
        finally:
            browser.close()


def test_cli_runtime_retains_approved_multiselect_and_exposes_checkbox_terms():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    from playwright.sync_api import sync_playwright
    from jhb.applications import booklet
    from jhb.applications.planner import deterministic_plan
    arbitration = "I agree to resolve employment disputes through binding arbitration and waive a jury trial."
    privacy = "I agree to the employer's separate applicant data processing terms."
    html = '''<!doctype html><html lang="en"><title>Synthetic disclosure controls</title>
    <style>.select__value-container{margin:20px}.option{padding:10px}</style>
    <form id="application"><label id="ethnicity-label">Ethnicity</label>
    <div class="select__value-container" id="ethnicity-container">
      <input id="ethnicity" role="combobox" aria-labelledby="ethnicity-label"
       aria-controls="ethnicity-options" aria-expanded="false" onfocus="openEthnicity()" onclick="openEthnicity()">
      <div id="ethnicity-options" role="listbox"></div>
    </div>
    <label for="arbitration">Accept</label><input id="arbitration" type="checkbox" required description="''' + arbitration + '''">
    <label for="privacy">Accept</label><input id="privacy" type="checkbox" required description="''' + privacy + '''">
    <button type="submit">Submit application</button></form>
    <script>
    window.multiSelections=0;window.submissions=0;
    document.getElementById('application').onsubmit=e=>{e.preventDefault();window.submissions++};
    function openEthnicity(){
      document.getElementById('ethnicity').setAttribute('aria-expanded','true');
      document.getElementById('ethnicity-options').innerHTML='<div role="option" class="option" onclick="selectEthnicity()">South Asian</div><div role="option" class="option">Prefer not to answer</div>';
    }
    function selectEthnicity(){
      window.multiSelections++;
      const selected=document.createElement('div');selected.className='select__multi-value';
      const label=document.createElement('span');label.className='select__multi-value__label';label.textContent='South Asian';
      selected.appendChild(label);document.getElementById('ethnicity-container').prepend(selected);
      document.getElementById('ethnicity-options').innerHTML='';
      document.getElementById('ethnicity').value='';document.getElementById('ethnicity').setAttribute('aria-expanded','false');
    }
    </script></html>'''
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        requests = []
        def serve(route):
            requests.append(route.request.url)
            route.fulfill(status=200, content_type="text/html", body=html)
        page.route("**/*", serve)
        url = "https://job-boards.greenhouse.io/synthetic-fixture/jobs/5678"
        page.goto(url)
        cdp = page.context.new_cdp_session(page)
        helpers = {"cdp": lambda method, **params: cdp.send(method, params),
                   "js": page.evaluate, "wait": lambda seconds: page.wait_for_timeout(seconds * 1000),
                   "click_at_xy": lambda x, y: page.mouse.click(x, y),
                   "list_tabs": lambda: [{"url": url, "targetId": "fixture-tab"}],
                   "switch_tab": lambda target: None,
                   "current_tab": lambda: {"targetId": "fixture-tab"}}
        try:
            dispatch({"operation": "open", "url": url}, helpers)
            snapshot = dispatch({"operation": "observe"}, helpers)
            fields = {control["ref"]: control for control in snapshot["fields"]}
            assert fields["arbitration"]["label"] == arbitration + " (Accept)"
            assert fields["privacy"]["label"] == privacy + " (Accept)"
            assert fields["arbitration"]["required"] is True
            # Generic consent approval cannot authorize unrelated legal terms.
            plan = deterministic_plan(snapshot, {"consent.truthfulness": booklet.answer(True, "synthetic unrelated approval")})
            assert not any(binding["ref"] in {"arbitration", "privacy"} for binding in plan["bindings"])
            assert not page.locator("#arbitration").is_checked()
            assert not page.locator("#privacy").is_checked()

            request = {"operation": "fill", "field": fields["ethnicity"], "value": "South Asian"}
            selected = dispatch(request, helpers)
            assert selected == {"verified": True, "selected": "South Asian"}
            assert page.locator("#ethnicity").input_value() == ""
            assert page.locator(".select__multi-value__label").all_text_contents() == ["South Asian"]
            # Retention must be detected on retry, preventing duplicate tags.
            assert dispatch(request, helpers)["verified"] is True
            assert page.evaluate("window.multiSelections") == 1
            assert page.locator(".select__multi-value__label").count() == 1
            assert page.evaluate("window.submissions") == 0
            assert requests == [url]
        finally:
            browser.close()
