"""Observed native Workable/Lever controls; no arbitrary selectors or clicks.

Transport remains the registered Browser Use CLI. These expressions describe
controls belonging to the application, never login/newsletter/global inputs.
"""
NATIVE_CONTROLS = r"""[...document.querySelectorAll('[data-ui="application-form"],.application-form,form#application-form,form:has(input[type=file][name=resume]),form:has([data-ui="resume"])')].flatMap(root=>[...root.querySelectorAll('input,textarea,select')]).filter((e,i,a)=>a.indexOf(e)===i)"""
NATIVE_FIELDS = r"""(()=>{
 const controls=CONTROLS, fields=[], names=new Set();
 const visible=e=>!e.disabled&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const choiceVisible=e=>visible(e)||[...(e.labels||[])].some(visible);
 const clean=s=>(s||'').trim().replace(/^[*✱]\s*/,'').replace(/[\s*✱]+$/,'').replace(/\s*\(Optional\)\s*$/i,'').replace(/\s+/g,' ').trim();
 const label=e=>clean(e.getAttribute('aria-label')||(e.getAttribute('aria-labelledby')||'').split(/\s+/).map(id=>document.getElementById(id)?.innerText||'').join(' ')||[...(e.labels||[])].map(l=>{const clone=l.cloneNode(true);clone.querySelectorAll('input,select,textarea,button,.required').forEach(c=>c.remove());return clone.textContent}).join(' '));
 for(const [native_index,e] of controls.entries()){
  if(['hidden','password','submit','button','reset'].includes(e.type)||e.disabled)continue;
  if(e.type!=='file'&&!choiceVisible(e))continue;
  const group=e.closest('fieldset,.application-question,[data-ui="question"],.application-field');
  const sibling=group?.previousElementSibling;
  const heading=clean(sibling?.matches('.application-label')?sibling.innerText:group?.querySelector('legend,.application-label,[data-ui="label"]')?.innerText);
  const required=e.required||e.getAttribute('aria-required')==='true'||!!group?.querySelector('[aria-required="true"]')||
      !!sibling?.querySelector('.required')||[...(e.labels||[])].some(l=>!!l.querySelector('.required'));
  if(e.type==='radio'){
   if(!e.name||names.has(e.name))continue;names.add(e.name);
   const options=controls.map((c,i)=>({e:c,native_index:i})).filter(c=>c.e.type==='radio'&&c.e.name===e.name&&choiceVisible(c.e));
   fields.push({ref:'native-radio:'+e.name,label:heading||label(e),type:'radio',widget:'native-radio',required:required||options.some(c=>c.e.required),
      options:options.map(c=>({label:label(c.e),value:c.e.value,native_index:c.native_index,native_name:c.e.name,native_type:c.e.type,checked:c.e.checked}))});continue;
  }
  if(e.type==='checkbox'&&e.name&&group&&heading){
   const options=controls.map((c,i)=>({e:c,native_index:i})).filter(c=>c.e.type==='checkbox'&&c.e.name===e.name&&c.e.closest('fieldset,.application-question,[data-ui="question"],.application-field')===group&&choiceVisible(c.e));
   if(options.length>1){const key='checkbox:'+e.name;if(names.has(key))continue;names.add(key);
    fields.push({ref:'native-multiselect:'+e.name,label:heading,type:'multiselect',widget:'native-checkboxes',required:required||options.some(c=>c.e.required),
     options:options.map(c=>({label:label(c.e),value:c.e.value,native_index:c.native_index,native_name:c.e.name,native_type:c.e.type,checked:c.e.checked}))});continue;}
  }
  let title=heading||label(e);
  if(e.type==='file'){
   const owner=e.closest('[data-ui="resume"],[data-ui="avatar"]');
   if(owner?.getAttribute('data-ui')==='avatar')continue;
   if(owner?.getAttribute('data-ui')==='resume'||e.name==='resume'||e.name==='resumeFile')title='Resume';
  }
  if(!title){if(!required)continue;title='Unlabeled required application field';}
  const uniqueId=e.id&&controls.filter(c=>c.id===e.id).length===1;
  const uniqueName=e.name&&controls.filter(c=>c.name===e.name).length===1;
  const leverLocation=e.id==='location-input'&&e.name==='location'&&!!group?.querySelector('#selected-location[name="selectedLocation"]');
  fields.push({ref:uniqueId?e.id:uniqueName?'native-name:'+e.name:'native:'+native_index,label:title,type:leverLocation||e.getAttribute('role')==='combobox'?'combobox':e.tagName==='SELECT'?'select':e.tagName==='TEXTAREA'?'textarea':e.type,
    widget:leverLocation?'lever-location':'native',native_index,required,separate_phone_country:e.type==='tel'&&!!e.closest('.iti')?.querySelector('.iti__country-container'),
    options:e.tagName==='SELECT'?[...e.options].map(o=>({label:o.label,value:o.value,disabled:o.disabled})):[]});
 }
 return fields;
})()""".replace('CONTROLS','('+NATIVE_CONTROLS+')')
