"""Real isolated Chromium catalogs; fixtures never attach to candidate Chrome."""
from contextlib import contextmanager
import os

import pytest

from jhb import config
from jhb.applications.cli_runtime import dispatch

HTML = '''<!doctype html><html lang="en"><title>Synthetic background catalog</title>
<style>.select__container{margin:20px}.option{padding:8px}input{padding:8px}</style>
<form id="application"><label for="retained">First Name</label><input id="retained" value="Synthetic retained name">
<label id="catalog-label">School</label><div class="select__container" id="owned-catalog">
<div class="select__control"><div class="select__value-container"><span class="select__single-value"></span>
<input id="school--0" role="combobox" aria-labelledby="catalog-label" aria-controls="own-options" aria-expanded="false"></div>
<div class="select__indicators"><span class="select__loading-indicator" style="display:none">Loading</span></div></div>
<div id="own-options" role="listbox"></div></div>
<div class="select__container"><span class="select__loading-indicator">Unrelated loading indicator</span>
<span class="select__single-value">Synthetic retained college</span><input id="school--1" role="combobox" aria-label="Other school"></div>
<button type="submit">Submit application</button></form>
<script>
window.activated=false;window.releaseChoices=true;window.ownLoading=true;window.queries=[];window.submissions=0;window.optionClicks=0;
const input=document.querySelector('#owned-catalog input'),menu=document.getElementById('own-options'),spinner=document.querySelector('#owned-catalog .select__loading-indicator');
input.onclick=()=>input.setAttribute('aria-expanded','true');
input.oninput=()=>{window.queries.push(input.value);menu.innerHTML='';spinner.style.display=window.ownLoading?'inline':'none';
if(window.activated&&window.releaseChoices&&input.value){
spinner.style.display='none';const option=document.createElement('div');option.role='option';option.className='option';
option.textContent=input.id==='candidate-location'?'San Diego, California, United States':'University of California - San Diego';
option.onclick=()=>{window.optionClicks++;document.querySelector('#owned-catalog .select__single-value').textContent=option.textContent;
input.value='';menu.innerHTML='';input.setAttribute('aria-expanded','false')};menu.appendChild(option)}};
input.onkeydown=e=>{if(e.key==='Escape'){menu.innerHTML='';input.setAttribute('aria-expanded','false')}};
document.getElementById('application').onsubmit=e=>{e.preventDefault();window.submissions++};
</script></html>'''


@contextmanager
def stalled_catalog():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(); page = browser.new_page()
        url = "https://job-boards.greenhouse.io/synthetic-catalog/jobs/1234"
        requests = []
        def serve(route):
            requests.append(route.request.url)
            route.fulfill(status=200, content_type="text/html", body=HTML)
        page.route("**/*", serve); page.goto(url)
        session = page.context.new_cdp_session(page)
        target = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
        activations, waits = [], []
        def wait(seconds):
            waits.append(seconds)
            # Poll counts stay real; fixture latency is compressed for tests.
            page.wait_for_timeout(1 if seconds == .25 else seconds*1000)
        def activate(owned):
            assert owned == target
            activations.append(owned)
            session.send("Target.activateTarget", {"targetId": owned})
            page.evaluate("window.activated=true")
        def switch(owned): assert owned == target
        helpers = {"cdp": lambda method, **params: session.send(method, params), "js": page.evaluate,
                   "wait": wait, "click_at_xy": page.mouse.click, "activate_tab": activate,
                   "list_tabs": lambda: [{"targetId": target, "url": url}], "switch_tab": switch,
                   "current_tab": lambda: {"targetId": target, "url": page.url}}
        try:
            dispatch({"operation": "open", "url": url}, helpers)
            yield page, helpers, target, url, activations, waits, requests
        finally:
            browser.close()


@pytest.mark.parametrize("catalog", ["school", "city"])
def test_own_loading_catalog_wakes_exact_target_once_and_retries_same_query(catalog):
    with stalled_catalog() as (page, helpers, target, url, activations, waits, requests):
        ref, value, query = "school--0", "University of California San Diego", "University of California San Diego"
        if catalog == "city":
            page.evaluate("document.getElementById('school--0').id='candidate-location';document.getElementById('catalog-label').textContent='Location (City)'")
            ref, value, query = "candidate-location", "San Diego, CA", "San Diego"
        field = next(f for f in dispatch({"operation": "observe"}, helpers)["fields"] if f["ref"] == ref)
        result = dispatch({"operation": "fill", "field": field, "value": value,
                           "target_id": target, "expected_url": url}, helpers)
        assert result["verified"] is True
        assert activations == [target]
        assert [v for v in page.evaluate("window.queries") if v] == [query, query]
        assert waits.count(.25) == (24 if catalog == "school" else 32) + 1
        assert page.evaluate("window.optionClicks") == 1
        assert page.locator('#retained').input_value() == 'Synthetic retained name'
        assert page.locator('.select__container').nth(1).locator('.select__single-value').inner_text() == 'Synthetic retained college'
        assert page.evaluate("window.__jhbGuard") is True
        assert page.evaluate("window.submissions") == 0
        assert requests == [url]


@pytest.mark.parametrize("changed", ["target", "tab_job", "document_job", "guard"])
def test_loading_recovery_refuses_changed_owned_target_identity_or_guard(changed):
    with stalled_catalog() as (page, helpers, target, url, activations, waits, requests):
        field = next(f for f in dispatch({"operation": "observe"}, helpers)["fields"] if f["ref"] == "school--0")
        original_wait = helpers["wait"]
        changed_once = []
        def wait(seconds):
            original_wait(seconds)
            if seconds == .25 and not changed_once:
                changed_once.append(True)
                if changed == "target": helpers["current_tab"] = lambda: {"targetId": "unrelated", "url": url}
                elif changed == "tab_job": helpers["current_tab"] = lambda: {"targetId": target, "url": url.replace('1234', '9999')}
                elif changed == "document_job": page.evaluate("history.replaceState({},'',location.href.replace('1234','9999'))")
                else: page.evaluate("window.__jhbGuard=false")
        helpers["wait"] = wait
        with pytest.raises(ValueError, match="(?:tab changed|guard changed)"):
            dispatch({"operation": "fill", "field": field, "value": "University of California San Diego",
                      "target_id": target, "expected_url": url}, helpers)
        assert activations == []
        assert page.evaluate("window.optionClicks") == 0
        assert page.locator('#retained').input_value() == 'Synthetic retained name'
        assert page.evaluate("window.submissions") == 0


@pytest.mark.parametrize("state", ["own_hidden", "other_loading_only", "other_loading_same_container", "ordinary_field"])
def test_unrelated_or_hidden_indicator_and_ordinary_dropdown_do_not_wake(state):
    with stalled_catalog() as (page, helpers, target, url, activations, waits, requests):
        ref = 'school--0'
        if state == 'own_hidden': page.evaluate("document.querySelector('#owned-catalog .select__loading-indicator').style.visibility='hidden'")
        elif state == 'other_loading_only': page.evaluate("window.ownLoading=false")
        elif state == 'other_loading_same_container':
            page.evaluate("window.ownLoading=false;document.getElementById('owned-catalog').append(document.querySelectorAll('.select__loading-indicator')[1])")
        else:
            page.evaluate("document.getElementById('school--0').id='ordinary';document.getElementById('catalog-label').textContent='Ordinary dropdown'")
            ref = 'ordinary'
        field = next(f for f in dispatch({"operation": "observe"}, helpers)["fields"] if f["ref"] == ref)
        with pytest.raises(ValueError, match='absent from dropdown options'):
            dispatch({"operation": "fill", "field": field, "value": "University of California San Diego",
                      "target_id": target, "expected_url": url}, helpers)
        assert activations == []
        assert page.evaluate('window.optionClicks') == 0
        assert page.evaluate('window.__jhbGuard') is True
        assert page.evaluate('window.submissions') == 0


def test_persistently_loading_catalog_wakes_at_most_once_and_stays_technical_failure():
    with stalled_catalog() as (page, helpers, target, url, activations, waits, requests):
        page.evaluate('window.releaseChoices=false')
        field = next(f for f in dispatch({"operation": "observe"}, helpers)["fields"] if f["ref"] == 'school--0')
        with pytest.raises(ValueError, match='Dropdown catalog is still loading'):
            dispatch({"operation": "fill", "field": field, "value": "University of California San Diego",
                      "target_id": target, "expected_url": url}, helpers)
        assert activations == [target]
        assert [v for v in page.evaluate('window.queries') if v] == ['University of California San Diego', 'University of California San Diego', 'Diego']
        assert waits.count(.25) <= 24+32+24
        assert page.evaluate('window.optionClicks') == 0
        assert page.evaluate('window.__jhbGuard') is True
        assert page.evaluate('window.submissions') == 0


def test_activation_navigation_is_rechecked_before_retyping_catalog_query():
    with stalled_catalog() as (page, helpers, target, url, activations, waits, requests):
        original = helpers['activate_tab']
        def activate(owned):
            original(owned)
            page.evaluate("history.replaceState({},'',location.href.replace('1234','9999'))")
        helpers['activate_tab'] = activate
        field = next(f for f in dispatch({'operation': 'observe'}, helpers)['fields'] if f['ref'] == 'school--0')
        with pytest.raises(ValueError, match='tab changed during recovery'):
            dispatch({'operation': 'fill', 'field': field, 'value': 'University of California San Diego',
                      'target_id': target, 'expected_url': url}, helpers)
        assert activations == [target]
        assert [v for v in page.evaluate('window.queries') if v] == ['University of California San Diego']
        assert page.evaluate('window.optionClicks') == 0
        assert page.evaluate('window.__jhbGuard') is True
        assert page.evaluate('window.submissions') == 0
