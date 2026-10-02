"""Exercise CLI runtime actions on synthetic DOM; no live-site requests."""
import os
import pytest

from jhb import config
from jhb.applications.cli_runtime import dispatch

HTML = '''<!doctype html><html lang="en"><title>Synthetic application</title>
<style>input{margin:12px;padding:8px}.select__value-container{margin:20px}.option{padding:10px}</style>
<form id="application"><label for="first_name">First Name</label><input id="first_name" required>
<label id="state-label">State</label><div class="select__value-container">
<div class="select__single-value"></div><input id="state" role="combobox" aria-labelledby="state-label" aria-required="true" aria-expanded="false" aria-autocomplete="list" onfocus="openOptions()">
<div id="options" role="listbox"></div></div>
<div class="file-upload"><label for="resume">Resume/CV</label><input id="resume" type="file" accept="application/pdf" required style="display:none" onchange="showUpload()"></div>
<button type="submit">Submit application</button></form>
<script>
window.submissions=0;document.getElementById('application').onsubmit=e=>{e.preventDefault();window.submissions++};
function openOptions(){document.getElementById('state').setAttribute('aria-expanded','true');document.getElementById('options').innerHTML='<div role="option" class="option" onclick="chooseState()">California</div><div role="option" class="option">New York</div>';}
function chooseState(){document.querySelector('.select__single-value').textContent='California';document.getElementById('options').innerHTML='';document.getElementById('state').value='';document.getElementById('state').setAttribute('aria-expanded','false');}
function showUpload(){let p=document.createElement('p');p.textContent=document.getElementById('resume').files[0].name;let wrapper=document.createElement('div');wrapper.className='file-upload__filename';wrapper.appendChild(p);document.querySelector('.file-upload').appendChild(wrapper);}
</script></html>'''


def test_cli_runtime_verifies_custom_dropdown_upload_and_blocks_submit(tmp_path):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    from playwright.sync_api import sync_playwright
    from pypdf import PdfWriter
    resume = tmp_path / "synthetic-ml.pdf"
    writer = PdfWriter(); writer.add_blank_page(width=612, height=792); writer.write(resume)
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
            assert dispatch({"operation": "fill", "field": fields["first_name"], "value": "Sam"}, helpers)["verified"]
            assert page.locator("#first_name").input_value() == "Sam"
            assert dispatch({"operation": "fill", "field": fields["state"], "value": "CA"}, helpers)["selected"] == "California"
            with pytest.raises(ValueError, match="uniquely match"):
                dispatch({"operation": "fill", "field": fields["state"], "value": "Unknown State"}, helpers)
            assert page.locator("#state").input_value() == ""
            assert dispatch({"operation": "fill", "field": fields["resume"], "value": str(resume)}, helpers)["filename"] == resume.name
            page.get_by_role("button", name="Submit application").click()
            page.evaluate("document.getElementById('application').submit()")
            assert page.evaluate("window.submissions") == 0
            with pytest.raises(ValueError, match="acknowledgement"):
                dispatch({"operation": "takeover"}, helpers)
            assert page.evaluate("window.__jhbGuard") is True
            assert requests == [url]
        finally:
            browser.close()
