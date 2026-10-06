"""Synthetic delayed geocoder; candidate Chrome and remote APIs are never used."""
from jhb.applications.cli_runtime import dispatch
from test_cli_runtime_recovery_fixture import synthetic_browser

HTML = '''<!doctype html><html lang="en"><title>Synthetic delayed locations</title>
<style>.select__value-container{margin:25px}input,.option{padding:10px}</style>
<form id="application"><label for="candidate-location">Location (City)</label>
<div class="select__value-container"><div class="select__single-value"></div>
<input id="candidate-location" role="combobox" aria-controls="locations" aria-expanded="false" aria-required="true">
<div id="locations" role="listbox"></div></div><button type="submit">Submit application</button></form>
<script>
window.submissions=0;window.selected=[];window.pending=null;
const input=document.getElementById('candidate-location'),menu=document.getElementById('locations');
input.oninput=()=>{clearTimeout(window.pending);menu.innerHTML='';
if(input.value==='San Diego')window.pending=setTimeout(()=>{
input.setAttribute('aria-expanded','true');
for(const name of ['San Diego, Texas, United States','Rancho San Diego, California, United States','San Diego, California, United States']){
const item=document.createElement('div');item.role='option';item.className='option';item.textContent=name;
item.onclick=()=>{window.selected.push(name);document.querySelector('.select__single-value').textContent=name;input.value='';menu.innerHTML='';input.setAttribute('aria-expanded','false')};menu.appendChild(item);
}},3400)};
document.getElementById('application').onsubmit=e=>{e.preventDefault();window.submissions++};
</script></html>'''


def test_delayed_location_catalog_waits_and_selects_exact_city_and_state():
    with synthetic_browser(HTML) as (page, session, helpers, target, url, requests, switches, activations):
        field = next(f for f in dispatch({'operation': 'observe'}, helpers)['fields'] if f['ref'] == 'candidate-location')
        result = dispatch({'operation': 'fill', 'field': field, 'value': 'San Diego, CA',
                           'target_id': target, 'expected_url': url}, helpers)
        assert result == {'verified': True, 'selected': 'San Diego, California, United States'}
        assert page.evaluate('window.selected') == ['San Diego, California, United States']
        assert page.locator('#candidate-location').input_value() == ''
        assert page.evaluate('window.__jhbGuard') is True
        assert page.evaluate('window.submissions') == 0
        assert requests == [url]
