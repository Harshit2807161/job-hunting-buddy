"""US aliases are display translations of a native phone-country choice."""
import pytest

from jhb.applications import booklet, review_inventory
from jhb.applications.cli_runtime import dispatch, phone_country_value
from jhb.applications.planner import key_for_field
from jhb.applications.submission_runtime import _checks


@pytest.mark.parametrize("value", ["United States of America ", " USA ", "US", "U.S.", "U.S.A."])
def test_phone_alias_needs_owned_phone_context(value):
    assert phone_country_value(value, {"ref": "country", "phone_country": True}) == "United States"
    for field in ({"ref": "country"}, {"ref": "citizenship", "phone_country": True},
                  {"ref": "country", "phone_country": False}):
        assert phone_country_value(value, field) == value
    assert phone_country_value("US citizen", {"ref": "country", "phone_country": True}) == "US citizen"
    assert phone_country_value("Canada", {"ref": "country", "phone_country": True}) == "Canada"


@pytest.mark.parametrize("owned", [True, False])
def test_native_phone_alias_is_retained_in_independent_audit_without_changing_facts(owned):
    from playwright.sync_api import sync_playwright
    url = "https://job-boards.greenhouse.io/synthetic-phone/jobs/1234"
    html = '''<form id=application><fieldset class=phone-input>
    <div class=phone-input__country><label for=country>Country</label>
    <div class=select__value-container><div class=select__single-value></div>
    <input id=country role=combobox aria-controls=options onfocus="openMenu()" onclick="openMenu()">
    <div id=options role=listbox></div></div></div>
    <label for=phone>Phone</label><input id=phone type=tel></fieldset>
    <button type=submit>Submit application</button></form>
    <script>window.selections=0;window.submissions=0;
    document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
    function openMenu(){options.innerHTML='<div role=option onclick="choose()">United States (+1)</div>'}
    function choose(){window.selections++;document.querySelector('.select__single-value').innerHTML='<span class="iti__flag iti__us"></span>+1';country.value='';options.innerHTML=''}
    document.addEventListener('keydown',e=>{if(e.key==='Escape')options.innerHTML=''})</script>'''
    if not owned:
        html = html.replace('class=phone-input__country', 'class=unrelated-country')
    with sync_playwright() as pw:
        browser = pw.chromium.launch(); page = browser.new_page()
        page.route('**/*', lambda r:r.fulfill(status=200, content_type='text/html', body=html))
        page.goto(url); session = page.context.new_cdp_session(page)
        helpers = {'cdp':lambda method, **params:session.send(method, params), 'js':page.evaluate,
                   'wait':lambda s:page.wait_for_timeout(s*1000), 'click_at_xy':lambda x,y:page.mouse.click(x,y),
                   'list_tabs':lambda:[{'targetId':'fixture', 'url':url}],
                   'current_tab':lambda:{'targetId':'fixture'}, 'switch_tab':lambda t:None}
        try:
            dispatch({'operation':'open', 'url':url}, helpers)
            snapshot = dispatch({'operation':'observe'}, helpers)
            field = next(f for f in snapshot['fields'] if f['ref'] == 'country')
            value = 'United States of America '
            if not owned:
                # Caller metadata cannot impersonate native phone ownership.
                with pytest.raises(ValueError, match='absent from dropdown'):
                    dispatch({'operation':'fill', 'field':{**field, 'phone_country':True}, 'value':value}, helpers)
                assert page.evaluate('window.selections') == 0
                assert page.locator('#country').input_value() == ''
                return
            result = dispatch({'operation':'fill', 'field':field, 'value':value}, helpers)
            assert result['verified'] and result['selected'] == 'United States (+1)'
            assert dispatch({'operation':'fill', 'field':field, 'value':value}, helpers)['verified']
            assert page.evaluate('window.selections') == 1
            record = booklet.answer(value, {'provider':'synthetic explicit country answer'})
            row = {'ref':'country', 'question':field['label'], 'key':'identity.country',
                   'value':value, 'source':record['source']}
            packet = {'job':{'url':url}, 'filled':[row], **review_inventory.build(snapshot['fields'], [row],
                      {'identity.country':record}, key_for_field, complete=True)}
            check = _checks({'target_id':'fixture', 'documents':{}}, helpers, packet,
                            {'application_url':url, 'authorization_scope':'one exact application explicitly approved in the local review portal'})
            assert check.get('state') is None and len(check['retained']) == 1
            assert row['value'] == record['value'] == value
            assert page.evaluate('window.selections') == 1
            assert page.evaluate('window.submissions') == 0 and page.evaluate('window.__jhbGuard') is True
        finally:
            browser.close()
