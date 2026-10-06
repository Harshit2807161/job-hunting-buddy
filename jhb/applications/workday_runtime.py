"""Fixed Workday wizard mechanics; guarded preparation has no terminal action."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .boards import job_identity
from .booklet import normalize
from .browser import GUARD_SCRIPT
from .cli_runtime import _settled_click, option_matches
from .planner import safe_next

FIELDS = r"""(()=>{
 const visible=e=>!e.disabled&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const label=e=>{let text=([...(e.labels||[])].map(l=>l.innerText).join(' ')||
  (e.getAttribute('aria-labelledby')||'').split(' ').map(id=>document.getElementById(id)?.innerText||'').join(' ').trim()||e.getAttribute('aria-label')||'').trim().replace(/\s*(?:\*|Required)\s*$/,'');
  if(e.tagName==='BUTTON'){const selected=e.innerText.trim();if(selected&&text.endsWith(' '+selected))text=text.slice(0,-selected.length).trim();}
  return text;};
 const fields=[],dates=new Set(),radios=new Set();
 const schoolIds=[...document.querySelectorAll('input[id^="education-"][id$="--schoolName"]')].map(e=>e.id.split('--')[0]);
 const experienceIds=[...document.querySelectorAll('input[id^="workExperience-"][id$="--jobTitle"]')].map(e=>e.id.split('--')[0]);
 for(const e of document.querySelectorAll('input,textarea,select,button[aria-haspopup="listbox"],button[aria-haspopup="true"]')){
  const choiceVisible=visible(e)||[...(e.labels||[])].some(visible);
  if((!e.id&&e.type!=='file')||(!visible(e)&&e.type!=='file'&&!(e.type==='radio'&&choiceVisible))||['hidden','password','submit','reset'].includes(e.type))continue;
  if(e.tagName==='BUTTON'&&!e.getAttribute('aria-haspopup'))continue;
  let ref=e.id,title=label(e),kind=e.tagName==='SELECT'?'select':e.tagName==='TEXTAREA'?'textarea':e.tagName==='BUTTON'?'combobox':e.type;
  const catalog=e.getAttribute('type')==='selectinput'||e.getAttribute('role')==='combobox'||e.getAttribute('data-uxi-widget-type')==='selectinput';
  if(catalog)kind='combobox';
  if(e.id==='phoneNumber--phoneNumber'&&e.name==='phoneNumber'&&/^phone number$/i.test(title)&&document.getElementById('phoneNumber--countryPhoneCode'))kind='tel';
  let file_index=null,file_required=false;
  if(e.type==='file'){
   file_index=[...document.querySelectorAll('input[type=file]')].indexOf(e);ref=e.id||'workday:file:'+file_index;
   const owner=e.closest('[data-fkit-id="resumeAttachments--attachments"]'),group=e.closest('[role=group][aria-labelledby]');
   const headings=(group?.getAttribute('aria-labelledby')||'').split(' ').map(id=>document.getElementById(id)).filter(Boolean);
   const upload=e.closest('[data-automation-id="attachments-FileUpload"]');
   if(owner&&group&&group.contains(owner)&&headings.length===1&&group.contains(headings[0])&&headings[0].innerText.trim()==='Resume/CV'&&group.querySelectorAll('input[type=file]').length===1&&upload&&owner.contains(upload)){
    const labels=(upload.getAttribute('aria-labelledby')||'').split(' ').map(id=>document.getElementById(id)).filter(Boolean);
    if(labels.length===1&&owner.contains(labels[0])&&labels[0].tagName==='LABEL'){
     title='Resume/CV';file_required=[...labels[0].querySelectorAll('abbr')].some(a=>a.innerText.trim()==='*');
    }
   }
   if(!title){for(let p=e.parentElement,depth=0;p&&depth<5;p=p.parentElement,depth++){
    const text=p.innerText.trim();if(text.length<1500&&/^Resume\s*\/\s*CV\b/i.test(text)&&!/(photo|headshot|cover letter)/i.test(text)){title='Resume/CV';break;}
   }}
   if(!title)title='Unlabeled document upload';
  }
  if(!title)continue;
  if(e.type==='radio'){
   if(!e.name||radios.has(e.name))continue;radios.add(e.name);
   const choices=[...document.querySelectorAll('input[type=radio]')].filter(r=>r.name===e.name&&r.form===e.form&&!r.disabled);
   const legend=e.closest('fieldset')?.querySelector('legend')?.innerText?.trim();
   const heading=legend||(e.name==='disability'?'Disability status':null);
   if(!heading)continue;
   const group=e.closest('[aria-required][aria-labelledby]');
   const ownedGroup=group&&e.closest('fieldset')?.contains(group)&&choices.every(r=>group.contains(r))&&
    [...group.querySelectorAll('input[type=radio]')].every(r=>r.name===e.name&&r.form===e.form);
   fields.push({ref:'workday-radio:'+e.name,actual_id:e.id,label:heading,type:'radio',required:choices.some(r=>r.required||r.getAttribute('aria-required')==='true')||!!(ownedGroup&&group.getAttribute('aria-required')==='true'),
    options:choices.map(r=>({label:label(r),value:r.value,id:r.id,checked:r.checked}))});continue;
  }
  const ed=schoolIds.findIndex(id=>e.id.startsWith(id+'--'));
  const exp=experienceIds.findIndex(id=>e.id.startsWith(id+'--'));
  const suffix=e.id.split('--').slice(1).join('--');
  const education={schoolName:'school',degree:'degree',fieldOfStudy:'discipline',gradeAverage:'gpa'};
  if(ed>=0&&education[suffix])ref=education[suffix]+'--'+ed;
  let date=null,date_required=false;
  const segmented=e.id.match(/^(.*)-(dateSectionMonth|dateSectionYear|dateSectionDay)-input$/);
  if(segmented){
   const base=segmented[1];if(dates.has(base))continue;dates.add(base);
   const part=s=>document.getElementById(base+'-dateSection'+s+'-input');
   const owner=e.closest('[data-fkit-id]'),owned=owner?.getAttribute('data-fkit-id')===base;
   const legend=owned?owner.querySelector('fieldset > legend'):null;
   date_required=!!legend&&[...legend.querySelectorAll('abbr')].some(a=>a.innerText.trim()==='*');
   if(!part('Month')||!part('Year')){
    const column=base.endsWith('--firstYearAttended')?'start':base.endsWith('--lastYearAttended')?'end':null;
    if(ed<0||segmented[2]!=='dateSectionYear'||part('Month')||part('Day')||!column||!legend)continue;
    kind='number';date={base,year_only:true};ref='workday-year:'+base;title=legend.innerText.trim()+' year';
    if((column==='start'&&legend.innerText.trim()==='From')||(column==='end'&&legend.innerText.trim()==='To (Actual or Expected)')){
     ref=column+'-year--'+ed;title=(column==='start'?'Start':'End')+' date year';
    }
   }else{
    kind='date';date={base,day:!!part('Day')};ref='workday-date:'+base;
    if(ed>=0&&/--(startDate|endDate)$/.test(base))ref=(base.endsWith('--startDate')?'start_date':'end_date')+'--'+ed;
    title=base.endsWith('--startDate')?'Start date':base.endsWith('--endDate')?'End date':title;
   }
  }
  const columns={jobTitle:'title',companyName:'company',location:'location',roleDescription:'summary',currentlyWorkHere:'current'};
  let record_column=ed>=0?({schoolName:'school',degree:'degree',fieldOfStudy:'major',gradeAverage:'gpa'}[suffix]||null):exp>=0?(columns[suffix]||null):null;
  if(date&&(ed>=0||exp>=0))record_column=date.base.endsWith('--startDate')?'start_date':date.base.endsWith('--endDate')?'end_date':null;
  fields.push({ref,actual_id:e.id,file_index,label:title,type:kind,widget:catalog?'catalog':'native',required:e.required||file_required||date_required||e.getAttribute('aria-required')==='true'||/\bRequired\s*$/.test(e.getAttribute('aria-label')||''),
   education_index:ed>=0?ed:null,experience_index:exp>=0?exp:null,date,
   record_kind:ed>=0?'education':exp>=0?'experience':null,record_index:ed>=0?ed:exp>=0?exp:null,record_column,
   separate_phone_country:e.id==='phoneNumber--phoneNumber'&&!!document.getElementById('phoneNumber--countryPhoneCode'),
   options:e.tagName==='SELECT'?[...e.options].map(o=>({label:o.label,value:o.value,disabled:o.disabled})):[]});
 }
 return fields;
})()"""


def dispatch(request, helpers):
    raw_cdp, js, wait = helpers["cdp"], helpers["js"], helpers["wait"]
    woke_for_input = False
    def cdp(method, **params):
        nonlocal woke_for_input
        if method.startswith("Input.") and helpers.get("jhb_cdp_timeout"):
            params["_response_timeout"] = helpers["jhb_cdp_timeout"]
        try:
            return raw_cdp(method, **params)
        except TimeoutError:
            if (woke_for_input or method != "Input.dispatchMouseEvent"
                    or params.get("type") not in {"mouseMoved", "mouseWheel"}
                    or not callable(helpers.get("activate_tab"))):
                raise
            owned_guard()
            helpers["activate_tab"](request["target_id"]); wait(0.3)
            owned_guard(); woke_for_input = True
            return raw_cdp(method, **params)
    expected = job_identity(request.get("approved_url"))
    if not expected or expected[0] != "workday":
        raise ValueError("Invalid exact Workday job scope")
    if request.get("target_id"):
        helpers["switch_tab"](request["target_id"])
    operation = request.get("operation")
    if operation == "open":
        if job_identity(request.get("url")) != expected:
            raise ValueError("Requested Workday job differs from approved scope")
        tabs = [tab for tab in helpers["list_tabs"]() if job_identity(tab.get("url")) == expected]
        if len(tabs) > 1:
            raise ValueError("Several Workday drafts match the same job; review tab ownership")
        if tabs:
            helpers["switch_tab"](tabs[0]["targetId"])
        else:
            helpers["new_tab"](request["url"]); helpers["wait_for_load"](); wait(0.5)
        if job_identity(js("location.href")) != expected:
            raise ValueError("Workday navigation changed approved job scope")
        cdp("Page.addScriptToEvaluateOnNewDocument", source=GUARD_SCRIPT); js(GUARD_SCRIPT)
        return {"target_id": helpers["current_tab"]()["targetId"], "url": js("location.href"), "guarded": True}
    if (helpers["current_tab"]().get("targetId") != request.get("target_id")
            or job_identity(js("location.href")) != expected or js("window.__jhbGuard===true") is not True):
        raise ValueError("Owned Workday job or submission guard changed")

    def owned_guard():
        if (helpers["current_tab"]().get("targetId") != request.get("target_id")
                or job_identity(js("location.href")) != expected or js("window.__jhbGuard===true") is not True):
            raise ValueError("Owned Workday job or submission guard changed during native input")
    def click_node(backend):
        def native_press(x, y):
            owned_guard()
            try:
                cdp("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", buttons=1, clickCount=1)
            finally:
                cdp("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", buttons=0, clickCount=1)
        def prime(x, y):
            owned_guard()
            cdp("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", buttons=0, clickCount=1)
            cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, buttons=0)
            _settled_click(backend, cdp, wait, native_press)
        _settled_click(backend, cdp, wait, prime)

    def nodes():
        return [n for n in cdp("Accessibility.getFullAXTree")["nodes"] if not n.get("ignored")]
    def buttons():
        result = []
        for node in nodes():
            if node.get("role", {}).get("value") != "button" or not node.get("backendDOMNodeId"):
                continue
            label = node.get("name", {}).get("value", "")
            remote = cdp("DOM.resolveNode", backendNodeId=node["backendDOMNodeId"])["object"]["objectId"]
            try:
                # Workday exposes a named click_filter overlay as its canonical
                # accessible control. Its covered native sibling is not a second
                # action; arbitrary overlays remain obstructed handoffs.
                covered = cdp("Runtime.callFunctionOn", objectId=remote,
                    functionDeclaration="function(name){if(this.tagName!=='BUTTON')return false;return [...(this.parentElement?.querySelectorAll('[data-automation-id=click_filter][role=button]')||[])].some(e=>e!==this&&e.getAttribute('aria-label')===name);}",
                    arguments=[{"value": label}], returnByValue=True)["result"].get("value")
            finally:
                cdp("Runtime.releaseObject", objectId=remote)
            if not covered:
                result.append({"ref": str(node["backendDOMNodeId"]), "label": label})
        return result
    def click(button):
        current = [b for b in buttons() if b == button]
        if len(current) != 1 or not button["ref"].isdigit():
            raise ValueError("Observed Workday button changed")
        click_node(int(button["ref"])); wait(0.15)
    def node_for(selector):
        root = cdp("DOM.getDocument")["root"]["nodeId"]
        node = cdp("DOM.querySelector", nodeId=root, selector=selector).get("nodeId")
        if not node:
            raise ValueError("Observed Workday control is unavailable")
        return node
    def selector(field):
        return '[id='+json.dumps(field["actual_id"])+']'
    def node_for_field(field):
        if field["type"] != "file" or field.get("actual_id"):
            return node_for(selector(field))
        root = cdp("DOM.getDocument")["root"]["nodeId"]
        choices = cdp("DOM.querySelectorAll", nodeId=root, selector="input[type=file]").get("nodeIds", [])
        index = field.get("file_index")
        if not isinstance(index, int) or not 0 <= index < len(choices):
            raise ValueError("Observed Workday document input changed")
        return choices[index]
    def read(field):
        return js("(()=>{const e=document.getElementById("+json.dumps(field["actual_id"])+
                  ");if(!e)return null;let selected='';const owner=e.closest('[data-automation-id=multiselectInputContainer]');"
                  "if(owner){const key=e.getAttribute('data-uxi-multiselect-id');const lists=[...owner.querySelectorAll('[data-automation-id=selectedItemList]')].filter(p=>p.closest('[data-automation-id=multiselectInputContainer]')===owner&&key&&p.getAttribute('data-uxi-multiselect-id')===key);"
                  "if(lists.length===1){const chips=[...lists[0].querySelectorAll('[data-automation-id=selectedItem] [data-automation-id=promptOption]')];selected=chips.map(c=>c.innerText.trim()).filter(Boolean).join(', ');}}"
                  "else{for(let p=e.parentElement,depth=0;p&&depth<5;p=p.parentElement,depth++){const controls=[...p.querySelectorAll('input')].filter(x=>x.getAttribute('type')==='selectinput'||x.getAttribute('role')==='combobox'||x.getAttribute('data-uxi-widget-type')==='selectinput');if(controls.length!==1||controls[0]!==e)break;const chips=[...p.querySelectorAll('[data-automation-id=promptSelectionLabel]')].filter(c=>c.innerText.trim());if(chips.length){selected=chips.map(c=>c.innerText.trim()).join(', ');break;}}}"
                  "return {value:e.value||'',text:e.innerText||'',selected,checked:e.checked,invalid:e.getAttribute('aria-invalid')==='true'}})()")
    def press(key, code=None, **params):
        cdp("Input.dispatchKeyEvent", type="keyDown", key=key, code=code or key, **params)
        cdp("Input.dispatchKeyEvent", type="keyUp", key=key, code=code or key)
    def type_value(node, value, *, segmented=False):
        if segmented:
            remote = cdp("DOM.resolveNode", nodeId=node)["object"]["objectId"]
            try:
                shape = cdp("Runtime.callFunctionOn", objectId=remote,
                    functionDeclaration="""function(){
                        const r=this.getBoundingClientRect(),m=this.id.match(/^(.*)-dateSection(Month|Year|Day)-input$/);
                        const tiny=r.width<2||r.height<2;
                        if(!tiny)return {tiny:false};
                        const owner=this.closest('[data-automation-id=dateInputWrapper]');
                        const id=m&&m[1]+'-dateSection'+m[2]+'-display';
                        const displays=id?[...document.querySelectorAll('[id]')].filter(e=>e.id===id):[];
                        const d=displays.length===1?displays[0]:null;
                        return {tiny:true,id:this.id,display:owner&&m&&owner.id===m[1]&&d&&
                            d.closest('[data-automation-id=dateInputWrapper]')===owner&&
                            d.getAttribute('data-automation-id')==='dateSection'+m[2]+'-display'?id:null};
                    }""", returnByValue=True)["result"].get("value")
                if shape and shape.get("tiny"):
                    # Modern Workday hides the editable spinbutton behind a
                    # visible segment display. Focusing the hidden input and
                    # typing digits can move focus and corrupt another segment.
                    if not shape.get("display") or not str(value).isdigit():
                        raise ValueError("Workday date did not retain an approved segment")
                    owned_guard()
                    display = node_for('[id='+json.dumps(shape["display"])+']')
                    click_node(cdp("DOM.describeNode", nodeId=display)["node"]["backendNodeId"])
                    def current_segment():
                        owned_guard()
                        state = cdp("Runtime.callFunctionOn", objectId=remote,
                            functionDeclaration="function(){return {ready:this.isConnected&&document.activeElement===this&&!this.disabled&&!this.readOnly,value:this.value};}",
                            returnByValue=True)["result"].get("value")
                        if not state or state.get("ready") is not True:
                            raise ValueError("Workday date did not retain an approved segment")
                        return state["value"]
                    actual = current_segment()
                    if actual == "":
                        press("ArrowUp"); actual = current_segment()
                    if not str(actual).isdigit() or abs(int(value)-int(actual)) > 100:
                        raise ValueError("Workday date did not retain an approved segment")
                    for _ in range(101):
                        actual = current_segment()
                        if not str(actual).isdigit():
                            raise ValueError("Workday date did not retain an approved segment")
                        if int(actual) == int(value):
                            press("Tab"); wait(0.1)
                            return
                        step = 1 if int(actual) < int(value) else -1
                        press("ArrowUp" if step == 1 else "ArrowDown")
                        after = current_segment()
                        if not str(after).isdigit() or int(after) != int(actual)+step:
                            raise ValueError("Workday date did not retain an approved segment")
                    raise ValueError("Workday date did not retain an approved segment")
            finally:
                cdp("Runtime.releaseObject", objectId=remote)
        cdp("DOM.focus", nodeId=node)
        press("a", "KeyA", modifiers=4, commands=["selectAll"])
        if segmented:
            # An empty native date segment can interpret Backspace as moving
            # focus to its sibling. Replace the selected text with digit keys.
            remote = cdp("DOM.resolveNode", nodeId=node)["object"]["objectId"]
            try:
                for char in str(value):
                    focused = cdp("Runtime.callFunctionOn", objectId=remote,
                        functionDeclaration="function(){return this.isConnected&&document.activeElement===this&&!this.disabled&&!this.readOnly;}",
                        returnByValue=True)["result"].get("value")
                    if focused is not True:
                        raise ValueError("Workday date did not retain an approved segment")
                    press(char, "Digit"+char, text=char, windowsVirtualKeyCode=ord(char))
            finally:
                cdp("Runtime.releaseObject", objectId=remote)
        else:
            press("Backspace")
            cdp("Input.insertText", text=str(value))
        press("Tab"); wait(0.1)

    if operation == "screenshot":
        path = Path(request["path"]).resolve()
        from ..config import ROOT
        if not path.is_relative_to((ROOT/"private").resolve()):
            raise ValueError("Workday screenshots must remain private")
        from .screenshot_runtime import capture_from_top
        return capture_from_top({**request, 'path':str(path)}, helpers)
    if operation == "begin":
        manual = [b for b in buttons() if normalize(b["label"]) == "apply manually"]
        if len(manual) == 1:
            click(manual[0])
        elif len(manual) > 1:
            raise ValueError("Workday manual-start choice is ambiguous")
        return {"guarded": True}
    if operation == "observe":
        body = js("document.body.innerText")
        if re.search(r"verify (?:you are human|your email)|verification code|checking your browser", body, re.I):
            return {"url": js("location.href"), "fields": [], "buttons": [], "handoff": "waiting_captcha", "reason": "Workday verification requires review"}
        if js("[...document.querySelectorAll('input[type=password]')].some(e=>e.getClientRects().length)"):
            return {"url": js("location.href"), "fields": [], "buttons": [], "handoff": "waiting_login", "reason": "Workday requires its existing approved account session"}
        fields = js(FIELDS)
        observed_buttons = buttons()
        if not fields and any(normalize(button["label"]) == "sign in" for button in observed_buttons):
            return {"url": js("location.href"), "fields": [], "buttons": observed_buttons, "handoff": "waiting_login",
                    "reason": "Workday Sign In navigation requires inspection of its owned existing-account login form"}
        if not fields and any(normalize(button["label"]) in {"submit", "submit application"} for button in observed_buttons):
            return {"url": js("location.href"), "fields": [], "buttons": observed_buttons, "handoff": "unsupported",
                    "reason": "Saved Workday review cards require retained-answer and document auditing before final review"}
        return {"url": js("location.href"), "fields": fields, "buttons": observed_buttons,
                "experience_step": js("!!document.querySelector('input[id^=workExperience-],input[id^=education-]')||[...document.querySelectorAll('[data-automation-id=applyFlowMyExpPage]')].some(p=>p.getClientRects().length&&['Work Experience','Education'].every(title=>[...p.querySelectorAll('[role=group][aria-labelledby]')].some(g=>{const ids=(g.getAttribute('aria-labelledby')||'').split(' ');return ids.some(id=>{const h=document.getElementById(id);return h&&g.contains(h)&&h.innerText.trim()===title;});})))") is True}
    if operation == "next":
        button = request["button"]
        if not safe_next(button):
            raise ValueError("Workday adapter cannot submit or choose an unknown continuation")
        click(button)
        return {"continued": True, "guarded": js("window.__jhbGuard===true")}
    if operation == "records":
        kind, count = request.get("kind"), request.get("count")
        if kind not in {"education", "workExperience"} or not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 10:
            raise ValueError("Invalid bounded Workday record request")
        suffix = "schoolName" if kind == "education" else "jobTitle"
        title = "Education" if kind == "education" else "Work Experience"
        count_script = "document.querySelectorAll("+json.dumps('input[id^="'+kind+'-"][id$="--'+suffix+'"]')+").length"
        current = js(count_script)
        if current > count:
            raise ValueError("Existing Workday rows exceed the approved record count; preserve them for review")
        while current < count:
            candidates = []
            for button in buttons():
                if normalize(button["label"]) not in {"add", "add another"}:
                    continue
                remote = cdp("DOM.resolveNode", backendNodeId=int(button["ref"]))["object"]["objectId"]
                try:
                    owned = cdp("Runtime.callFunctionOn", objectId=remote,
                        functionDeclaration="function(title,kind){const group=this.closest('[role=group][aria-labelledby]');if(group){const labels=(group.getAttribute('aria-labelledby')||'').split(' ').map(id=>document.getElementById(id)).filter(Boolean);return labels.length===1&&group.contains(labels[0])&&labels[0].innerText.trim()===title;}for(let p=this.parentElement;p&&p!==document.body;p=p.parentElement){const headings=[...p.querySelectorAll('h1,h2,h3,h4')];if(headings.length>1)return false;if(headings.length===1)return headings[0].innerText.trim()===title&&!p.querySelector('input[id^=\"'+(kind==='education'?'workExperience':'education')+'-\"]');}return false;}",
                        arguments=[{"value": title}, {"value": kind}], returnByValue=True)["result"].get("value")
                finally:
                    cdp("Runtime.releaseObject", objectId=remote)
                if owned:
                    candidates.append(button)
            if len(candidates) != 1:
                raise ValueError("Workday add-record control is ambiguous")
            click(candidates[0])
            updated = js(count_script)
            for _ in range(10):
                if updated != current:
                    break
                wait(0.15); owned_guard(); updated = js(count_script)
            if updated != current + 1:
                raise ValueError("Another Workday record did not appear")
            current = updated
        return {"supported": True, "count": current}

    if operation == "authenticate":
        policy = request.get("approved_exception", {})
        value = policy.get("value", {})
        origin = "https://"+urlsplit(request["approved_url"]).hostname
        if (policy.get("status") != "verified" or not policy.get("source") or value.get("origin") != origin
                or value.get("method") != "password" or value.get("reuse_existing") is not True):
            raise ValueError("Workday password authentication lacks an approved site-specific exception")
        auth = js("(()=>{const pw=[...document.querySelectorAll('input[type=password]')].filter(e=>e.getClientRects().length);if(pw.length!==1)return null;const owner=pw[0].closest('form,[role=dialog]');if(!owner)return null;const mail=[...owner.querySelectorAll('input[type=email],input[data-automation-id=email]')].filter(e=>e.getClientRects().length);const required=[...owner.querySelectorAll('input[required]')].filter(e=>e!==pw[0]&&!mail.includes(e)&&e.getClientRects().length);return mail.length===1&&!required.length?{email:mail[0].id,password:pw[0].id}:null})()")
        if not auth or not auth.get("email") or not auth.get("password"):
            return {"authenticated": False}
        choices = []
        for button in buttons():
            if normalize(button["label"]) != "sign in":
                continue
            remote = cdp("DOM.resolveNode", backendNodeId=int(button["ref"]))["object"]["objectId"]
            try:
                owned = cdp("Runtime.callFunctionOn", objectId=remote, functionDeclaration="function(passwordId){return !!this.closest('form,[role=dialog]')?.contains(document.getElementById(passwordId));}", arguments=[{"value": auth["password"]}], returnByValue=True)["result"].get("value")
            finally:
                cdp("Runtime.releaseObject", objectId=remote)
            if owned:
                choices.append(button)
        if len(choices) != 1:
            return {"authenticated": False}
        type_value(node_for('[id='+json.dumps(auth["email"])+']'), request["username"])
        type_value(node_for('[id='+json.dumps(auth["password"])+']'), request["password"])
        if js("(()=>{const p=document.getElementById("+json.dumps(auth["password"])+");const owner=p?.closest('form,[role=dialog]');return !!owner&&![...owner.querySelectorAll('input[required]')].some(e=>e.id!=="+json.dumps(auth["email"])+"&&e.id!=="+json.dumps(auth["password"])+"&&e.getClientRects().length)})()") is not True:
            return {"authenticated": False}
        click(choices[0]); wait(0.5)
        authenticated = not js("[...document.querySelectorAll('input[type=password]')].some(e=>e.getClientRects().length)")
        current_url = js("location.href")
        if authenticated and job_identity(current_url) != expected:
            parsed = urlsplit(current_url)
            site = re.escape(expected[2])
            if ("https://"+parsed.hostname != origin or not re.fullmatch(r"/(?:[a-z]{2}-[A-Z]{2}/)?"+site+r"/userHome/?", parsed.path, re.I)
                    or not callable(helpers.get("goto_url"))):
                return {"authenticated": False, "reason": "Authentication changed to an unrecognized application route"}
            # Workday can return its owned tab to Candidate Home after sign-in.
            # Resume the exact approved job; no application status is inferred.
            helpers["goto_url"](request["approved_url"]); helpers["wait_for_load"](); wait(0.2)
            if job_identity(js("location.href")) != expected:
                return {"authenticated": False}
            js(GUARD_SCRIPT)
        return {"authenticated": authenticated}

    if operation not in {"fill", "describe"}:
        raise ValueError("Unsupported Workday browser operation")
    requested = request["field"]
    matches = [field for field in js(FIELDS) if field["ref"] == requested.get("ref") and field["label"] == requested.get("label")]
    if len(matches) != 1:
        raise ValueError("Observed Workday field changed")
    field = matches[0]
    if field["type"] == "combobox":
        if operation == "fill" and field.get("widget") == "catalog":
            retained = read(field)
            if (isinstance(request.get("value"), str) and retained and not retained.get("invalid")
                    and retained.get("selected") and not retained.get("value")
                    and option_matches(retained["selected"], request["value"], field_label=field["label"])):
                return {"verified": True, "already_retained": True}
        node = node_for(selector(field)); backend = cdp("DOM.describeNode", nodeId=node)["node"]["backendNodeId"]
        press("Escape")
        if field.get("widget") == "catalog":
            if operation == "fill":
                if not isinstance(request.get("value"), str):
                    raise ValueError("Workday search catalog requires an approved exact text option")
                # Search text alone is never accepted as a committed answer.
                type_value(node, request["value"])
            cdp("DOM.focus", nodeId=node); press("ArrowDown")
        else:
            click_node(backend)
        wait(0.2)
        ownership = js("(()=>{const e=document.getElementById("+json.dumps(field["actual_id"])+");const visible=x=>x.getClientRects().length&&getComputedStyle(x).visibility!=='hidden'&&x.getAttribute('data-automation-id')!=='selectedItemList';const owned=e?.getAttribute('aria-controls');if(owned){const popup=document.getElementById(owned);return !!popup&&visible(popup);}return [...document.querySelectorAll('[role=listbox],[role=menu]')].filter(visible).length===1;})()")
        if ownership is not True:
            press("Escape")
            raise ValueError("Workday dropdown ownership is ambiguous")
        choices = []
        for node in nodes():
            if node.get("role", {}).get("value") != "option" or not node.get("backendDOMNodeId"):
                continue
            remote = cdp("DOM.resolveNode", backendNodeId=node["backendDOMNodeId"])["object"]["objectId"]
            try:
                owned = cdp("Runtime.callFunctionOn", objectId=remote,
                    functionDeclaration="function(id){const e=document.getElementById(id);const visible=p=>p.getClientRects().length&&getComputedStyle(p).visibility!=='hidden'&&p.getAttribute('data-automation-id')!=='selectedItemList';const controlled=e?.getAttribute('aria-controls');const popups=[...document.querySelectorAll('[role=listbox],[role=menu]')].filter(visible);const popup=controlled?document.getElementById(controlled):popups.length===1?popups[0]:null;return !!popup&&visible(popup)&&popup.contains(this);}",
                    arguments=[{"value": field["actual_id"]}], returnByValue=True)["result"].get("value")
            finally:
                cdp("Runtime.releaseObject", objectId=remote)
            if owned:
                choices.append(node)
        def clean(text):
            return re.sub(r"\s+(?:not )?(?:selected|checked)$", "", text, flags=re.I).strip()
        catalog = [{"label": clean(n.get("name", {}).get("value", "")), "ref": n.get("backendDOMNodeId")}
                   for n in choices if not any(p.get("name") == "disabled" and p.get("value", {}).get("value") is True
                                               for p in n.get("properties", []))]
        if operation == "describe":
            press("Escape")
            return {"choices": [o["label"] for o in catalog], "type": "combobox"}
        value = request["value"]
        matched = [o for o in catalog if option_matches(o["label"], value, field_label=field["label"])]
        if len(matched) != 1:
            press("Escape")
            raise ValueError("Stored answer is absent or ambiguous in Workday dropdown options")
        click_node(matched[0]["ref"]); press("Escape"); wait(0.1)
        retained = read(field)
        actual = retained.get("selected") if field.get("widget") == "catalog" and retained else retained.get("text") if retained else ""
        if not retained or not option_matches(actual, value, field_label=field["label"]):
            raise ValueError("Workday dropdown did not retain the selected answer")
        return {"verified": True}
    if operation == "describe":
        return {"choices": [o["label"] for o in field.get("options", [])], "type": field["type"]}
    value = request["value"]
    if field["type"] == "number" and (field.get("date") or {}).get("year_only"):
        if not isinstance(value, str) or not re.fullmatch(r"[1-9]\d{3}", value):
            raise ValueError("Workday year-only education control requires an approved four-digit year")
        type_value(node_for_field(field), value, segmented=True)
        retained = read(field)
        if not retained or retained.get("value") != value or retained.get("invalid"):
            raise ValueError("Workday year-only education control did not retain its approved year")
        return {"verified": True}
    if field["type"] == "radio":
        matches = [o for o in field["options"] if option_matches(o["label"], value, field_label=field["label"])]
        if len(matches) != 1:
            raise ValueError("Workday radio answer is absent or ambiguous")
        choice = matches[0]
        actual = js("document.getElementById("+json.dumps(choice["id"])+")?.checked")
        if actual is not True:
            options = [n for n in nodes() if n.get("role", {}).get("value") == "radio"
                       and normalize(n.get("name", {}).get("value", "")) == normalize(choice["label"])]
            if len(options) != 1:
                raise ValueError("Observed Workday radio control is ambiguous")
            click_node(options[0]["backendDOMNodeId"])
        if js("document.getElementById("+json.dumps(choice["id"])+")?.checked") is not True:
            raise ValueError("Workday radio did not retain its approved answer")
        return {"verified": True}
    if field["type"] == "date" and field.get("date"):
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", value):
            raise ValueError("Workday segmented date requires an approved ISO date")
        from datetime import date
        date.fromisoformat(value if len(value) == 10 else value+"-01")
        parts = [("Month", int(value[5:7])), ("Year", int(value[:4]))]
        if field["date"]["day"]:
            if len(value) != 10:
                raise ValueError("Workday day field needs an explicitly approved day")
            parts.append(("Day", int(value[8:10])))
        for part, number in parts:
            actual_id = field["date"]["base"]+"-dateSection"+part+"-input"
            type_value(node_for('[id='+json.dumps(actual_id)+']'), number, segmented=True)
            retained = js("document.getElementById("+json.dumps(actual_id)+").value")
            if not str(retained).isdigit() or int(retained) != number:
                raise ValueError("Workday date did not retain an approved segment")
        ids = [field["date"]["base"]+"-dateSection"+part+"-input" for part, _ in parts]
        active_segment = json.dumps(ids)+".includes(document.activeElement?.id)"
        # A later segment or composite blur can change a previously checked
        # month. Finish native blur, then audit the entire approved date.
        for _ in range(len(ids)+1):
            if js(active_segment) is not True:
                break
            press("Tab"); wait(0.1)
        if js(active_segment) is True:
            raise ValueError("Workday date did not retain an approved segment")
        owned_guard()
        retained = js(json.dumps(ids)+".map(id=>{const nodes=[...document.querySelectorAll('input')].filter(e=>e.id===id);const e=nodes.length===1?nodes[0]:null;return e?{value:e.value,invalid:e.getAttribute('aria-invalid')==='true'||e.validity.valid===false,unavailable:e.disabled||e.readOnly}:null})")
        if (not isinstance(retained, list) or len(retained) != len(parts) or
                any(not item or item.get("invalid") or item.get("unavailable") or
                    not str(item.get("value", "")).isdigit() or int(item["value"]) != number
                    for item, (_, number) in zip(retained, parts))):
            raise ValueError("Workday date did not retain an approved segment")
        return {"verified": True}
    if field["type"] == "file":
        path = Path(str(value)).resolve()
        if not path.is_file() or path.suffix.lower() != ".pdf" or normalize(field["label"]) not in {"resume", "resume/cv", "cover letter"}:
            raise ValueError("Workday upload requires an approved existing PDF in a known document control")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        cdp("DOM.setFileInputFiles", nodeId=node_for_field(field), files=[str(path)])
        wait(0.2)
        names = js("(()=>{const e=[...document.querySelectorAll('input[type=file]')]["+str(field["file_index"])+"];return e?[...e.files].map(f=>f.name):[]})()")
        if names != [path.name] or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Workday uploaded document did not retain its approved bytes")
        return {"verified": True, "upload_receipt": {"sha256": digest, "filename": path.name, "nonce": uuid.uuid4().hex}}
    if field["type"] == "checkbox":
        if not isinstance(value, bool):
            raise ValueError("Workday checkbox requires an approved boolean")
        if read(field)["checked"] != value:
            backend = cdp("DOM.describeNode", nodeId=node_for(selector(field)))["node"]["backendNodeId"]
            click_node(backend)
        if read(field)["checked"] != value:
            raise ValueError("Workday checkbox did not retain its approved answer")
    elif field["type"] in {"text", "email", "tel", "url", "textarea", "number"}:
        type_value(node_for(selector(field)), value)
        if read(field)["value"] != str(value):
            raise ValueError("Workday field did not retain its approved answer")
    else:
        raise ValueError("Workday field type requires a reviewed mechanic")
    return {"verified": True}
