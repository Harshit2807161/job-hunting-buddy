"""Synthetic native selected-residence repair; never candidate Chrome."""
import os
import pytest
from jhb import config
from jhb.applications.manual_runtime import application_scope,dispatch

URL='https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application'
HTML='''<style>[role=option]{padding:10px}input{margin:10px}</style>
<form class=ashby-application-form-container><div data-field-path=state>
<label class=ashby-application-form-question-title>State/Country of Residence</label>
<div><input role=combobox class=ashby-application-form-input-autocomplete aria-haspopup=listbox aria-expanded=false value="California, United States"
oninput="window.edits++;window.token='';menu(this)" onkeydown="if(event.key==='ArrowDown')menu(this)">
<button type=button onclick="menu(document.querySelector('input'))">Toggle</button></div>
<div id=owned role=listbox></div></div><button type=submit>Submit</button></form>
<div id=foreign role=listbox><div role=option>California, United States</div></div>
<script>window.edits=0;window.commits=0;window.submitted=0;window.token='California, United States';
document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submitted++};
function menu(e){e.setAttribute('aria-controls','owned');e.setAttribute('aria-expanded','true');
const box=document.querySelector('#owned');box.innerHTML='';
if(e.value==='CA' && AVAILABLE){box.innerHTML='<div role="option" onclick="choose()">California, United States</div><div role="option">Canada</div>'}}
function choose(){const e=document.querySelector('input');window.token='California, United States';window.commits++;
e.value=window.token;e.setAttribute('aria-expanded','false');document.querySelector('#owned').innerHTML=''}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){document.querySelector('input').setAttribute('aria-expanded','false');document.querySelector('#owned').innerHTML=''}});
</script>'''

@pytest.mark.parametrize('available',[True,False])
def test_recommits_only_exact_owned_original_choice_then_describes_without_mutation(available):
 os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.local-browsers'))
 from playwright.sync_api import sync_playwright
 with sync_playwright() as pw:
  browser=pw.chromium.launch();page=browser.new_page()
  page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=HTML.replace('AVAILABLE','true' if available else 'false')))
  page.goto(URL);session=page.context.new_cdp_session(page)
  activated=[]
  helpers={'activate_tab':lambda target:activated.append(target),'cdp':lambda method,**params:session.send(method,params),'js':page.evaluate,'wait':lambda t:page.wait_for_timeout(t*1000),
    'click_at_xy':lambda x,y:page.mouse.click(x,y),'list_tabs':lambda:[{'url':URL,'targetId':'fixture'}],
    'current_tab':lambda:{'targetId':'fixture'},'switch_tab':lambda t:None}
  def call(op,**payload):return dispatch({'operation':op,'scope':application_scope(URL),**payload},helpers)
  try:
   call('open',url=URL);field=call('observe')['fields'][0]
   # Current full selected display label produces no results; foreign option
   # cannot be used, and read-only inspection never clears the old selection.
   assert call('describe',field=field,query='CA')['choices']==[]
   assert page.evaluate('window.edits')==0 and page.evaluate('window.token')=='California, United States'
   if available:
    assert call('prepare_residence',field=field,query='CA',country='United States',foreground=True)=={'prepared':True,'retained':True}
    assert activated==['fixture']
    assert page.evaluate('window.commits')==1 and page.evaluate('window.token')=='California, United States'
    count=page.evaluate('window.edits')
    assert call('describe',field=field,query='CA')['choices']==['California, United States','Canada']
    assert page.evaluate('window.edits')==count and page.evaluate('window.commits')==1
    assert call('prepare_residence',field=field,query='CA',country='United States')['retained']
    assert page.evaluate('window.commits')==1
    page.evaluate("(()=>{const note=document.createElement('div');note.className='ashby-application-form-question-description';note.textContent='Select the state where you currently reside.';document.querySelector('[data-field-path=state]').append(note)})()")
    assert call('observe')['fields'][0]['description']=='Select the state where you currently reside.'
    assert call('describe',field=field,query='CA')['choices']==[]  # Changed owned help cannot reuse old proof.
    assert page.evaluate('window.edits')==count and page.evaluate('window.commits')==1
    page.locator('.ashby-application-form-question-description').evaluate('(e)=>e.remove()')
    # New native editing invalidates the prior actual catalog proof; raw typed
    # display text alone cannot inherit a committed-choice receipt.
    page.locator('input').fill('CA');page.locator('input').fill('California, United States')
    assert page.evaluate('window.token')==''
    assert call('describe',field=field,query='CA')['choices']==[]
    assert page.evaluate('document.querySelector("input").__jhbResidenceCatalog') is None
   else:
    with pytest.raises(ValueError,match='could not recommit'):
     call('prepare_residence',field=field,query='CA',country='United States')
    assert page.evaluate('window.commits')==0 and page.evaluate('window.token')==''
    assert page.locator('input').input_value()=='California, United States'
    assert page.evaluate('document.querySelector("input").__jhbResidenceCatalog') is None
   assert page.evaluate('window.submitted')==0
   before=page.evaluate('window.edits')
   with pytest.raises(ValueError,match='differs from the approved'):
    call('prepare_residence',field=field,query='NY',country='United States')
   assert page.evaluate('window.edits')==before
  finally:browser.close()
