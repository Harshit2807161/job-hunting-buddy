"""Real synthetic Chromium/CDP fixtures; never attach to the live browser."""
from contextlib import contextmanager
import os

import pytest

from jhb import config
from jhb.applications.cli_runtime import dispatch


@contextmanager
def synthetic_browser(html):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 900, "height": 640})
        requests = []
        def serve(route):
            requests.append(route.request.url)
            route.fulfill(status=200, content_type="text/html", body=html)
        page.route("**/*", serve)
        url = "https://job-boards.greenhouse.io/synthetic-recovery/jobs/1234"
        page.goto(url)
        session = page.context.new_cdp_session(page)
        target = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
        switches, activations = [], []
        def activate(owned):
            activations.append(owned)
            session.send("Target.activateTarget", {"targetId": owned})
        helpers = {"cdp": lambda method, **params: session.send(method, params),
                   "js": page.evaluate, "wait": lambda seconds: page.wait_for_timeout(seconds * 1000),
                   "click_at_xy": page.mouse.click,
                   "list_tabs": lambda: [{"url": url, "targetId": target}],
                   "switch_tab": switches.append,
                   "current_tab": lambda: {"targetId": target, "url": page.url},
                   "activate_tab": activate}
        try:
            dispatch({"operation": "open", "url": url}, helpers)
            switches.clear()
            yield page, session, helpers, target, url, requests, switches, activations
        finally:
            browser.close()


WHEEL_HTML = '''<!doctype html><html lang="en"><title>Synthetic wheel recovery</title>
<style>body{margin:0}form{height:1600px;padding:0 40px}#row{padding-top:550px}
input{width:20px;height:20px;margin:0}footer{position:fixed;bottom:0;left:0;right:0;height:160px;background:#eee}
button{display:block;margin-top:500px}</style>
<form id="application"><div id="row"><label for="agreement">Synthetic approved agreement</label>
<input id="agreement" type="checkbox"></div><button type="submit">Submit application</button></form>
<footer>Sticky synthetic footer</footer><script>
window.submissions=0;window.agreementClicks=0;
document.getElementById('agreement').onclick=()=>window.agreementClicks++;
document.getElementById('application').onsubmit=e=>{e.preventDefault();window.submissions++};
</script></html>'''


@pytest.mark.parametrize("timeouts", [1, 2])
def test_wheel_timeout_wakes_only_owned_target_once_and_retries_bounded(timeouts):
    with synthetic_browser(WHEEL_HTML) as (page, session, helpers, target, url, requests, switches, activations):
        field = next(f for f in dispatch({"operation": "observe"}, helpers)["fields"] if f["ref"] == "agreement")
        raw = helpers["cdp"]
        wheels = []
        def injected(method, **params):
            if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseWheel":
                wheels.append(dict(params))
                if len(wheels) <= timeouts:
                    raise RuntimeError("Synthetic native mouseWheel timed out")
            return raw(method, **params)
        helpers["cdp"] = injected
        request = {"operation": "fill", "field": field, "value": True,
                   "target_id": target, "expected_url": url}
        if timeouts == 1:
            result = dispatch(request, helpers)
            assert result == {"verified": True, "checked": True}
            assert page.locator("#agreement").is_checked()
            assert page.evaluate("window.agreementClicks") == 1
            assert page.evaluate("window.scrollY") > 0
        else:
            with pytest.raises(RuntimeError, match="mouseWheel timed out"):
                dispatch(request, helpers)
            assert not page.locator("#agreement").is_checked()
            assert page.evaluate("window.agreementClicks") == 0
            assert page.evaluate("window.scrollY") == 0
        # The second native timeout propagates instead of waking/retrying again.
        assert len(wheels) == 2
        assert wheels[0] == wheels[1]
        assert activations == [target]
        assert switches == [target]
        assert page.evaluate("window.__jhbGuard") is True
        assert page.evaluate("window.submissions") == 0
        assert requests == [url]


@pytest.mark.parametrize("change", ["target", "job_identity"])
def test_wheel_recovery_rejects_changed_target_or_job_before_activation(change):
    with synthetic_browser(WHEEL_HTML) as (page, session, helpers, target, url, requests, switches, activations):
        field = next(f for f in dispatch({"operation": "observe"}, helpers)["fields"] if f["ref"] == "agreement")
        raw, wheels = helpers["cdp"], []
        def injected(method, **params):
            if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseWheel":
                wheels.append(params)
                raise TimeoutError("Synthetic wheel timeout")
            return raw(method, **params)
        helpers["cdp"] = injected
        helpers["current_tab"] = lambda: {
            "targetId": "unrelated-target" if change == "target" else target,
            "url": url if change == "target" else "https://job-boards.greenhouse.io/another-board/jobs/9876",
        }
        with pytest.raises(ValueError, match="changed during recovery"):
            dispatch({"operation": "fill", "field": field, "value": True,
                      "target_id": target, "expected_url": url}, helpers)
        assert len(wheels) == 1
        assert activations == []
        assert not page.locator("#agreement").is_checked()
        assert page.evaluate("window.agreementClicks") == 0
        assert page.evaluate("window.submissions") == 0
        assert requests == [url]


SALARY_HTML = '''<!doctype html><html lang="en"><title>Synthetic keyboard salary dropdown</title>
<style>.select__value-container{margin:30px}.option{padding:12px}input{padding:8px}</style>
<form id="application"><label id="salary-label">What are your salary expectations?</label>
<div class="select__value-container" id="salary-container"><div class="select__single-value"></div>
<input id="salary" role="combobox" aria-labelledby="salary-label" aria-controls="salary-options"
aria-expanded="false" aria-required="true" autocomplete="off">
<div id="salary-options" role="listbox"></div></div><button type="submit">Submit application</button></form>
<script>
window.submissions=0;window.salaryOpens=0;window.salaryClicks=0;window.typedQueries=0;
const salary=document.getElementById('salary'),options=document.getElementById('salary-options');
salary.onclick=()=>window.salaryClicks++;salary.oninput=()=>window.typedQueries++;
salary.onkeydown=e=>{if(e.key==='ArrowDown'){
e.preventDefault();window.salaryOpens++;salary.setAttribute('aria-expanded','true');options.innerHTML='';
for(const label of ['$75,000 - $100,000','$100,000 - $125,000']){
const item=document.createElement('div');item.role='option';item.className='option';item.textContent=label;
item.onclick=()=>{document.querySelector('#salary-container .select__single-value').textContent=label;
salary.value='';salary.setAttribute('aria-expanded','false');options.innerHTML='';};options.appendChild(item);
}}};
document.getElementById('application').onsubmit=e=>{e.preventDefault();window.submissions++};
</script></html>'''


def test_salary_combobox_opens_with_arrow_down_and_retains_unique_approved_band():
    with synthetic_browser(SALARY_HTML) as (page, session, helpers, target, url, requests, switches, activations):
        field = next(f for f in dispatch({"operation": "observe"}, helpers)["fields"] if f["ref"] == "salary")
        assert field["label"] == "What are your salary expectations?"
        request = {"operation": "fill", "field": field, "value": 100000,
                   "target_id": target, "expected_url": url}
        result = dispatch(request, helpers)
        assert result == {"verified": True, "selected": "$100,000 - $125,000"}
        assert page.locator("#salary-container .select__single-value").inner_text() == "$100,000 - $125,000"
        assert page.locator("#salary").input_value() == ""
        assert page.locator("#salary").get_attribute("aria-expanded") == "false"
        assert page.evaluate("window.salaryOpens") == 1
        assert page.evaluate("window.salaryClicks") == 1
        assert page.evaluate("window.typedQueries") == 0
        # Resuming preserves the selected bucket without re-opening or typing.
        assert dispatch(request, helpers) == {"verified": True}
        assert page.evaluate("window.salaryOpens") == 1
        assert activations == []
        assert switches == [target, target]
        page.get_by_role("button", name="Submit application").click()
        page.evaluate("document.getElementById('application').submit()")
        assert page.evaluate("window.submissions") == 0
        assert requests == [url]


DISPLAY_OVERLAY_HTML = '''<!doctype html><html lang="en"><title>Synthetic selected display overlays</title>
<style>.select__value-container{position:relative;width:280px;height:40px;margin:30px}
.select__value-container input{position:absolute;inset:0;width:100%;height:100%;opacity:0;z-index:1}
.select__single-value{position:absolute;inset:0;display:block;padding:10px;background:#ddd;z-index:2}
#control-b{margin-top:90px}.option{padding:12px}#options-a:not(:empty){position:fixed;inset:0 0 auto 0;
height:300px;background:#ddd;z-index:20}#options-b:not(:empty){position:absolute;top:42px;background:#eee;z-index:5}</style>
<form id="application"><label id="label-a">Previous retained selection</label>
<div class="select__value-container" id="control-a"><span class="select__single-value">California</span>
<input id="a" role="combobox" aria-labelledby="label-a" aria-controls="options-a" aria-expanded="false">
<div id="options-a" role="listbox"></div></div>
<label id="label-b">New approved selection</label>
<div class="select__value-container" id="control-b"><span class="select__single-value">No</span>
<input id="b" role="combobox" aria-labelledby="label-b" aria-controls="options-b" aria-expanded="false">
<div id="options-b" role="listbox"></div></div><button type="submit">Submit application</button></form>
<script>
window.submissions=0;window.escapes=[];window.optionClicks={a:0,b:0};window.inputEdits=[];
for(const id of ['a','b']){
const input=document.getElementById(id),container=document.getElementById('control-'+id),menu=document.getElementById('options-'+id);
container.onclick=e=>{
if(e.target.closest('[role=option]'))return;
input.focus();input.setAttribute('aria-expanded','true');menu.innerHTML='';
for(const label of id==='a'?['California','New York']:['No','Yes']){
const option=document.createElement('div');option.role='option';option.className='option';option.textContent=label;
option.onclick=e=>{e.stopPropagation();window.optionClicks[id]++;container.querySelector('.select__single-value').textContent=label;
input.value='';input.setAttribute('aria-expanded','false');menu.innerHTML='';};menu.appendChild(option);
}};
input.oninput=()=>window.inputEdits.push(id);
input.onkeydown=e=>{if(e.key==='Escape'){window.escapes.push(id);menu.innerHTML='';input.setAttribute('aria-expanded','false');}};
}
document.getElementById('application').onsubmit=e=>{e.preventDefault();window.submissions++};
</script></html>'''


def test_selected_display_overlay_allows_new_approved_selection_on_its_own_combobox():
    with synthetic_browser(DISPLAY_OVERLAY_HTML) as (page, session, helpers, target, url, requests, switches, activations):
        field = next(f for f in dispatch({"operation": "observe"}, helpers)["fields"] if f["ref"] == "b")
        assert page.locator('#b').evaluate("e=>{const r=e.getBoundingClientRect();return document.elementFromPoint(r.x+r.width/2,r.y+r.height/2).className}") == 'select__single-value'
        assert page.locator('#b').evaluate('e=>getComputedStyle(e).opacity') == '0'
        result = dispatch({"operation": "fill", "field": field, "value": True,
                           "target_id": target, "expected_url": url}, helpers)
        assert result == {"verified": True, "selected": "Yes"}
        assert page.locator('#control-b .select__single-value').inner_text() == 'Yes'
        assert page.locator('#control-a .select__single-value').inner_text() == 'California'
        assert page.evaluate('window.optionClicks') == {'a': 0, 'b': 1}
        assert page.evaluate('window.inputEdits') == []
        assert page.evaluate('window.submissions') == 0
        assert requests == [url]


def test_interrupted_other_dropdown_closes_with_escape_without_changing_retained_answer():
    with synthetic_browser(DISPLAY_OVERLAY_HTML) as (page, session, helpers, target, url, requests, switches, activations):
        fields = {f['ref']: f for f in dispatch({"operation": "observe"}, helpers)['fields']}
        page.locator('#control-a .select__single-value').click()
        assert page.locator('#a').get_attribute('aria-expanded') == 'true'
        # The interrupted first menu physically covers the second input.
        assert page.locator('#b').evaluate("e=>{const r=e.getBoundingClientRect();return document.elementFromPoint(r.x+r.width/2,r.y+r.height/2).closest('[role=listbox]').id}") == 'options-a'
        result = dispatch({"operation": "fill", "field": fields['b'], "value": True,
                           "target_id": target, "expected_url": url}, helpers)
        assert result == {"verified": True, "selected": "Yes"}
        assert page.evaluate('window.escapes') == ['a']
        assert page.locator('#a').get_attribute('aria-expanded') == 'false'
        assert page.locator('#control-a .select__single-value').inner_text() == 'California'
        assert page.locator('#a').input_value() == ''
        assert page.locator('#control-b .select__single-value').inner_text() == 'Yes'
        assert page.evaluate('window.optionClicks') == {'a': 0, 'b': 1}
        assert page.evaluate('window.inputEdits') == []
        assert page.evaluate('window.submissions') == 0
        assert requests == [url]
