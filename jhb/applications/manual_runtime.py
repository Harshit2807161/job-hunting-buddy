"""Observed Ashby mechanics behind a per-job, explicit manual URL scope."""
from __future__ import annotations

import json
import hashlib
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .booklet import normalize
from .browser import GUARD_SCRIPT
from .cli_runtime import _settled_click, option_matches
from .planner import safe_next


def application_scope(url, board="ashby"):
    from .boards import board_type, job_identity
    parsed = urlsplit(url)
    identity = job_identity(url)
    suffix = "application" if board == "ashby" else "apply"
    if (board not in {"ashby", "workable", "lever"} or board_type(url) != board
            or not identity or not parsed.path.rstrip("/").endswith("/"+suffix)):
        raise ValueError(f"Manual scope requires an exact {board.title()} application URL")
    return {"board": board, "origin": "https://"+parsed.hostname, "path": parsed.path.rstrip("/")}



def matches_scope(url, scope):
    try:
        candidate = application_scope(url, scope["board"])
        return candidate == scope
    except (ValueError, KeyError, TypeError):
        return False


ASHBY_GROUPS = r"""[...new Set([...document.querySelectorAll('.ashby-application-form-section-container,.ashby-application-form-container,form')]
 .flatMap(root=>[...root.querySelectorAll('[data-field-path]')]))]"""

ASHBY_FIELDS = r"""(()=>{
 const visible=e=>!e.disabled&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const choiceVisible=e=>!e.disabled&&(visible(e)||[...(e.labels||[])].some(visible));
 const label=e=>(e.getAttribute('aria-label')||[...(e.labels||[])].map(l=>l.innerText).join(' ')||'').trim();
 const fields=[];
 for(const [owner_index,group] of GROUPS.entries()){
  const owned=e=>e.closest('[data-field-path]')===group;
  const path=group.getAttribute('data-field-path'), title=[...group.querySelectorAll('.ashby-application-form-question-title,legend')].find(owned);
  const heading=(title?.innerText||'').trim();
  const description=[...group.querySelectorAll('.ashby-application-form-question-description')].filter(e=>owned(e)&&visible(e))
    .map(e=>(e.innerText||'').trim()).filter(Boolean).join('\n');
  const context=description?{description:description.slice(0,4096),description_truncated:description.length>4096}:{};
  const controls=[...group.querySelectorAll('input,textarea,select')].filter(owned);
  const required=group.getAttribute('aria-required')==='true'||group.getAttribute('data-required')==='true'||
    controls.some(e=>e.required||e.getAttribute('aria-required')==='true')||
    (!!title&&([...title.classList].some(c=>c.includes('_required_'))||/\*$/.test(heading)));
  const yesno=[...group.querySelectorAll('button.ashby-application-form-input-yesno-option')].filter(e=>owned(e)&&visible(e));
  const radios=controls.filter(e=>e.type==='radio'&&choiceVisible(e)&&e.name!=='communicationConsent');
  const checks=controls.filter(e=>e.type==='checkbox'&&choiceVisible(e));
  if(yesno.length)fields.push({ref:'ashby:'+path,label:heading,type:'radio',widget:'yesno',required,owner_index,...context,
   options:yesno.map(e=>({label:e.innerText.trim(),value:e.getAttribute('data-option')}))});
  else if(radios.length||checks.length){const options=radios.length?radios:checks;
   fields.push({ref:'ashby:'+path,label:heading,type:radios.length?'radio':'multiselect',widget:radios.length?'radio':'checkboxes',required,owner_index,...context,
    options:options.map(e=>({label:label(e),value:e.value,id:e.id,checked:e.checked}))});}
  for(const [index,e] of controls.entries()){
   if(e.type==='radio'||e.type==='checkbox'||['hidden','password','submit','button','reset'].includes(e.type))continue;
   if(e.type!=='file'&&!visible(e))continue;
   fields.push({ref:e.id||'ashby:'+path+':control:'+index,widget:'native',control_index:index,owner_index,...context,
    ...(e.type==='text'&&e.classList.contains('ashby-application-form-input-date')&&
        e.closest('.react-datepicker__input-container')?.closest('.react-datepicker-wrapper')?.closest('[data-field-path]')===group
        ?{calendar_format:'MM/DD/YYYY'}:{}),
    label:label(e)||heading,type:e.getAttribute('role')==='combobox'?'combobox':e.tagName==='SELECT'?'select':e.type,required:e.required||required,
    options:e.tagName==='SELECT'?[...e.options].map(o=>({label:o.label,value:o.value,disabled:o.disabled})):[]});
  }
  const consent=controls.filter(e=>e.type==='radio'&&e.name==='communicationConsent'&&choiceVisible(e));
  if(consent.length)fields.push({ref:'ashby:'+path+':communicationConsent',label:group.querySelector('[class*="consentBody"]')?.innerText?.trim()||'Consent to receive application text message updates',
   type:'radio',widget:'communicationConsent',required:consent.some(e=>e.required),owner_index,...context,options:consent.map(e=>({label:label(e),value:e.value,checked:e.checked}))});
 }
 return [...new Map(fields.map(f=>[f.ref,f])).values()];
})()""".replace("GROUPS", "("+ASHBY_GROUPS+")")


def dispatch(request, helpers):
    cdp, js, wait = helpers["cdp"], helpers["js"], helpers["wait"]
    scope = request.get("scope", {})
    # Revalidate the supplied scope, including its board and fixed origin.
    approved = scope.get("origin", "") + scope.get("path", "")
    if not matches_scope(approved, scope):
        raise ValueError("Invalid manual application scope")
    if request.get("target_id"):
        helpers["switch_tab"](request["target_id"])
    operation = request["operation"]
    if operation == "open":
        if not matches_scope(request["url"], scope):
            raise ValueError("Requested URL differs from approved manual job")
        tabs = [t for t in helpers["list_tabs"]() if matches_scope(t["url"], scope)]
        if tabs:
            helpers["switch_tab"](tabs[0]["targetId"])
        else:
            helpers["new_tab"](request["url"])
            helpers["wait_for_load"]()
            wait(1)
        if not matches_scope(js("location.href"), scope):
            raise ValueError("Application redirected outside its approved manual scope")
        cdp("Page.addScriptToEvaluateOnNewDocument", source=GUARD_SCRIPT)
        js(GUARD_SCRIPT)
        return {"target_id": helpers["current_tab"]()["targetId"], "url": js("location.href"),
                "guarded": True, "reused_tab": bool(tabs)}
    if not matches_scope(js("location.href"), scope):
        raise ValueError("Current page differs from approved manual job")
    if request.get("foreground") is True and operation in {"fill", "describe", "prepare_residence"}:
        # An opt-in response to demonstrated background-input failure. Reuse
        # this client's attached, scope-checked target inside the browser lane.
        helpers["activate_tab"](helpers["current_tab"]()["targetId"])
        wait(0.1)

    if operation == "records":
        if scope["board"] != "workable":
            raise ValueError("Saved-record editors are unsupported for this board")
        from .workable_records import prepare_records
        try:
            return prepare_records(request, helpers)
        except ValueError:
            return {"handoff": "unsupported", "reason": "Workable's active record editor did not retain or save a verified Education/Experience record; preserve the draft for a scoped widget repair"}

    native = scope["board"] in {"workable", "lever"}
    if native:
        from .native_ats_runtime import NATIVE_FIELDS, NATIVE_CONTROLS
    def fields():
        return js(NATIVE_FIELDS if native else ASHBY_FIELDS)

    def group_expr(field):
        return "("+ASHBY_GROUPS+")["+str(field["owner_index"])+"]"

    def element_expr(field, option=None):
        selected = option if option is not None else field
        if "native_index" in selected:
            if option is None:
                if field["ref"].startswith("native-name:"):
                    condition = "e.name==="+json.dumps(field["ref"][len("native-name:"):])
                elif not field["ref"].startswith("native:"):
                    condition = "e.id==="+json.dumps(field["ref"])
                else:
                    condition = None
                if condition:
                    # Upload handlers insert hidden metadata and can replace
                    # the input. Resolve the same unique owned id/name afresh;
                    # never let an old numeric index point at another control.
                    return "(()=>{const a=("+NATIVE_CONTROLS+").filter(e=>"+condition+");return a.length===1?a[0]:null})()"
            elif selected.get("native_name") and selected.get("native_type"):
                condition = "e.name==="+json.dumps(selected["native_name"])+"&&e.type==="+json.dumps(selected["native_type"])+"&&e.value==="+json.dumps(selected["value"])
                return "(()=>{const a=("+NATIVE_CONTROLS+").filter(e=>"+condition+");return a.length===1?a[0]:null})()"
            return "("+NATIVE_CONTROLS+")["+str(selected["native_index"])+"]"
        if option is None:
            if field["ref"].startswith("ashby:") and field.get("widget") == "native":
                return "[...("+group_expr(field)+").querySelectorAll('input,textarea,select')].filter(e=>e.closest('[data-field-path]')==="+group_expr(field)+")["+str(field["control_index"])+"]"
            return "document.getElementById("+json.dumps(field["ref"])+")"
        group = group_expr(field)
        if field["widget"] == "yesno":
            return "[...("+group+").querySelectorAll('button.ashby-application-form-input-yesno-option')].find(e=>e.closest('[data-field-path]')==="+group+"&&e.getAttribute('data-option')==="+json.dumps(option["value"])+")"
        if option.get("id"):
            return "document.getElementById("+json.dumps(option["id"])+")"
        return "[...("+group+").querySelectorAll('input[type=radio]')].find(e=>e.closest('[data-field-path]')==="+group+"&&e.name==='communicationConsent'&&e.value==="+json.dumps(option["value"])+")"

    def backend(expression):
        # Prefer the AX-backed node, use the DOM fallback for hidden file input.
        obj = cdp("Runtime.evaluate", expression=expression)["result"].get("objectId")
        if not obj:
            raise ValueError("Observed manual control is unavailable")
        try:
            node = cdp("DOM.describeNode", objectId=obj)["node"]["backendNodeId"]
            return node
        finally:
            cdp("Runtime.releaseObject", objectId=obj)

    def click(expression):
        _settled_click(backend(expression), cdp, wait, helpers["click_at_xy"])

    def click_choice(expression):
        # Ashby hides the native radio/checkbox. Its visible associated label
        # provides real coordinates and a trusted click toggles the input.
        target = "(()=>{const e="+expression+";if(!e)return null;return [...(e.labels||[])].find(l=>l.getClientRects().length&&getComputedStyle(l).visibility!=='hidden')||e})()"
        click(target)

    def state(expression):
        return js("(()=>{const e="+expression+";return e?{value:e.value,checked:e.checked,pressed:e.getAttribute('aria-pressed'),expanded:e.getAttribute('aria-expanded'),invalid:e.getAttribute('aria-invalid')==='true'}:null})()")

    def type_text(expression, value):
        cdp("DOM.focus", backendNodeId=backend(expression))
        if not js("document.activeElement===("+expression+")"):
            raise ValueError("Observed manual input did not receive focus")
        cdp("Input.dispatchKeyEvent", type="keyDown", key="a", code="KeyA", modifiers=4, commands=["selectAll"])
        cdp("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA")
        cdp("Input.dispatchKeyEvent", type="keyDown", key="Backspace", code="Backspace")
        cdp("Input.dispatchKeyEvent", type="keyUp", key="Backspace", code="Backspace")
        cdp("Input.insertText", text=str(value))
        wait(0.15)

    def escape():
        cdp("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
        cdp("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape", windowsVirtualKeyCode=27)

    def combobox_options(expression):
        ids = js("(()=>{const e="+expression+";return (e?.getAttribute('aria-controls')||e?.getAttribute('aria-owns')||'').split(' ').filter(Boolean)})()")
        if not ids:
            return []
        nodes = cdp("Accessibility.getFullAXTree")["nodes"]
        eligible = []
        for node in nodes:
            if node.get("ignored") or node.get("role", {}).get("value") != "listbox" or not node.get("backendDOMNodeId"):
                continue
            obj = cdp("DOM.resolveNode", backendNodeId=node["backendDOMNodeId"])["object"]["objectId"]
            try:
                owned = cdp("Runtime.callFunctionOn", objectId=obj,
                            functionDeclaration="function(){return "+json.dumps(ids)+".some(id=>{const e=document.getElementById(id);return e===this||!!e?.contains(this)})&&!!this.getClientRects().length&&getComputedStyle(this).visibility!=='hidden'}",
                            returnByValue=True)["result"].get("value")
                if owned:
                    eligible.append(node["nodeId"])
            finally:
                cdp("Runtime.releaseObject", objectId=obj)
        if len(eligible) != 1:
            return []
        by_id = {node["nodeId"]: node for node in nodes}
        options = []
        for node in nodes:
            if node.get("ignored") or node.get("role", {}).get("value") != "option" or not node.get("backendDOMNodeId"):
                continue
            parent, visited = node.get("parentId"), set()
            while parent in by_id and parent not in visited:
                if parent == eligible[0]:
                    options.append(node)
                    break
                visited.add(parent)
                parent = by_id[parent].get("parentId")
        return options

    if operation == "observe":
        if js("[...document.querySelectorAll('input[type=password]')].some(e=>e.getClientRects().length)"):
            return {"handoff": "waiting_login", "reason": "Authentication requires a Google SSO handoff"}
        if js("[...document.querySelectorAll('iframe')].some(e=>/recaptcha|hcaptcha|challenge/i.test(e.src)&&e.getClientRects().length&&e.getBoundingClientRect().height>90)"):
            return {"handoff": "waiting_captcha", "reason": "A visible verification challenge requires a handoff"}
        nodes = cdp("Accessibility.getFullAXTree")["nodes"]
        observed_fields = fields()
        if not observed_fields:
            return {"handoff": "unsupported", "reason": "The exact job has no recognized application-owned native controls; open its application form"}
        return {"url": js("location.href"), "title": js("document.title"), "fields": observed_fields,
                "buttons": [{"ref": str(n["backendDOMNodeId"]), "label": n.get("name", {}).get("value", "")}
                            for n in nodes if n.get("role", {}).get("value") == "button" and n.get("backendDOMNodeId")]}
    if operation in {"describe", "fill", "prepare_residence"}:
        requested = request["field"]
        current = [f for f in fields() if f["ref"] == requested["ref"]]
        if len(current) != 1 or any(current[0][k] != requested[k] for k in ("label", "type")):
            raise ValueError("Observed manual field has changed")
        field = current[0]
        residence = scope["board"] == "ashby" and normalize(field["label"]) == "state/country of residence" and field["type"] == "combobox"
        def residence_catalog(expr):
            proof = js("(()=>{const e="+expr+";return e?.__jhbResidenceCatalog||null})()")
            retained = state(expr)
            binding = {"scope": scope, "ref": field["ref"], "label": field["label"], "required": field.get("required"),
                       "description": field.get("description", ""), "description_truncated": field.get("description_truncated", False)}
            if (residence and isinstance(proof, dict) and proof.get("binding") == binding and retained
                    and retained["value"] == proof.get("selected") and retained["expanded"] == "false" and not retained["invalid"]
                    and isinstance(proof.get("choices"), list) and 0 < len(proof["choices"]) <= 50
                    and all(isinstance(x, str) and 0 < len(x) <= 500 for x in proof["choices"])
                    and proof["choices"].count(proof.get("selected")) == 1):
                return proof["choices"]
            return None
        def remember_residence(expr, labels, selected):
            if not residence or len(labels)>50 or any(not x or len(x)>500 for x in labels):
                return
            proof = {"scope": scope, "ref": field["ref"], "label": field["label"], "required": field.get("required"),
                     "description": field.get("description", ""), "description_truncated": field.get("description_truncated", False)}
            js("(()=>{const e="+expr+";if(!e)return; e.__jhbResidenceCatalog="+json.dumps({"binding":proof,"choices":labels,"selected":selected})+
               ";if(!e.__jhbResidenceListener){const clear=()=>{delete e.__jhbResidenceCatalog};"
               "e.addEventListener('input',clear,{capture:true});e.addEventListener('change',clear,{capture:true});e.__jhbResidenceListener=true}})()")
        if operation == "prepare_residence":
            query, country = request.get("query"), request.get("country")
            if not residence or not isinstance(query,str) or not query.strip() or len(query)>200 or not isinstance(country,str):
                raise ValueError("Residence preparation is outside its approved scope")
            expr=element_expr(field);before=state(expr)
            if not before or not before["value"]:
                return {"prepared":False}
            parts=before["value"].rsplit(", ",1)
            canonical="united states" if normalize(country) in {"us","usa","united states","united states of america"} else normalize(country)
            if len(parts)!=2 or not option_matches(parts[0],query,field_id="state") or normalize(parts[1])!=canonical:
                raise ValueError("Existing residence differs from the approved state and country")
            if residence_catalog(expr):
                return {"prepared":True,"retained":True}
            type_text(expr,query)
            options=[]
            for _ in range(20):
                options=combobox_options(expr)
                if options:break
                wait(.25)
            matches=[n for n in options if n.get("name",{}).get("value")==before["value"]]
            if len(matches)!=1:
                type_text(expr,before["value"]);escape()
                raise ValueError("Residence catalog could not recommit the original selection")
            _settled_click(matches[0]["backendDOMNodeId"],cdp,wait,helpers["click_at_xy"])
            for _ in range(10):
                wait(.15);after=state(expr)
                if after and after["value"]==before["value"] and after["expanded"]=="false" and not after["invalid"]:
                    labels=[n.get("name",{}).get("value","") for n in options]
                    remember_residence(expr,labels,after["value"])
                    return {"prepared":True,"retained":True}
            raise ValueError("Autocomplete did not retain the committed choice")
        if operation == "describe":
            if field["type"] == "combobox":
                expr = element_expr(field)
                before = state(expr)
                query = request.get("query")
                if query is not None:
                    if (scope["board"] != "ashby" or normalize(field["label"]) != "state/country of residence"
                            or not isinstance(query, str) or not query.strip() or len(query) > 200):
                        raise ValueError("Residence catalog query is outside its approved scope")
                    if not before:
                        raise ValueError("Observed manual control is unavailable")
                cached = residence_catalog(expr)
                if cached:
                    return {"choices":cached,"type":"combobox","truncated":False,"source":"retained_owned_native_catalog"}
                existing_query = query is not None and bool(before["value"])
                try:
                    if query is not None and not existing_query:
                        type_text(expr, query)
                    else:
                        # Expand the current query without clearing/retyping a
                        # potentially committed selection. Native ArrowDown
                        # opens the owned catalog; it never presses Enter.
                        click(expr)
                        if existing_query:
                            cdp("DOM.focus", backendNodeId=backend(expr))
                            if not js("document.activeElement===("+expr+")"):
                                raise ValueError("Observed manual input did not receive focus")
                            cdp("Input.dispatchKeyEvent", type="keyDown", key="ArrowDown", code="ArrowDown", windowsVirtualKeyCode=40)
                            cdp("Input.dispatchKeyEvent", type="keyUp", key="ArrowDown", code="ArrowDown", windowsVirtualKeyCode=40)
                    labels = []
                    for _ in range(20 if query is not None else 8):
                        wait(0.25 if query is not None else 0.15)
                        labels = [n.get("name", {}).get("value", "") for n in combobox_options(expr)]
                        if labels:
                            break
                finally:
                    if query is not None and not existing_query:
                        type_text(expr, before["value"])
                    escape()
                    if query is not None and not existing_query:
                        cdp("Input.dispatchKeyEvent", type="keyDown", key="Tab", code="Tab", windowsVirtualKeyCode=9)
                        cdp("Input.dispatchKeyEvent", type="keyUp", key="Tab", code="Tab", windowsVirtualKeyCode=9)
                after = state(expr)
                if not before or not after or before["value"] != after["value"]:
                    raise ValueError("Read-only choices inspection changed the draft value")
                return {"choices": labels[:50], "type": "combobox", "truncated": len(labels)>50}
            return {"choices": [o["label"] for o in field.get("options", [])], "type": field["type"], "truncated": False}
        value, kind = request["value"], field["type"]
        if kind in {"radio", "multiselect"}:
            values = value if isinstance(value, list) else [value]
            if kind == "radio" and len(values) != 1:
                raise ValueError("Radio answers require one approved choice")
            if kind == "multiselect" and field["required"] and not values:
                raise ValueError("Required multiple-choice answers need an approved selection")
            wanted = []
            for answer in values:
                matches = [o for o in field["options"] if option_matches(o["label"], answer)]
                if len(matches) != 1:
                    raise ValueError("Answer does not uniquely match an observed choice")
                wanted.append(matches[0])
            for option in field["options"]:
                expr = element_expr(field, option)
                before = state(expr)
                selected = before and (before["pressed"] == "true" if field["widget"] == "yesno" else before["checked"])
                if kind == "radio" and option not in wanted:
                    continue
                expected = option in wanted
                if bool(selected) != expected:
                    click_choice(expr) if field["widget"] != "yesno" else click(expr)
                    wait(0.15)
            for option in field["options"]:
                after = state(element_expr(field, option))
                selected = after and (after["pressed"] == "true" if field["widget"] == "yesno" else after["checked"])
                if bool(selected) != (option in wanted) or not after or after["invalid"]:
                    raise ValueError("Manual choices did not retain the approved answer")
            return {"verified": True, "selected": [o["label"] for o in wanted]}
        expr = element_expr(field)
        if field.get("widget") == "lever-location":
            if scope["board"] != "lever" or not isinstance(value, str) or not value.strip():
                raise ValueError("Lever location requires an approved city")
            container = "("+expr+").closest('.application-field')"
            committed = lambda: js("(()=>{const root="+container+";return {value:("+expr+").value,token:root.querySelector('#selected-location[name=selectedLocation]')?.value||''}})()")
            before = committed()
            if before.get("token") and option_matches(before["value"], value, field_id="candidate-location"):
                return {"verified": True, "selected": before["value"]}
            type_text(expr, value)
            menu = "[...("+container+").querySelectorAll('.dropdown-results > *')].filter(e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden')"
            matches = []
            for _ in range(32):
                observed = js("("+menu+").map((e,index)=>({index,label:e.innerText.trim()}))")
                matches = [o for o in observed if option_matches(o["label"], value, field_id="candidate-location")]
                if matches:
                    break
                wait(.25)
            if len(matches) != 1:
                escape()
                raise ValueError("Lever location choice is absent or ambiguous")
            chosen = matches[0]
            option = "("+menu+")["+str(chosen["index"])+"]"
            if js("("+option+")?.innerText.trim()") != chosen["label"]:
                raise ValueError("Observed Lever location choice changed")
            click(option)
            for _ in range(12):
                wait(.15)
                after = committed()
                if (after.get("token") and after.get("value") == chosen["label"] and
                        not (before.get("token") and before["value"] != after["value"] and before["token"] == after["token"])):
                    return {"verified": True, "selected": after["value"], "committed": True}
            raise ValueError("Lever location did not retain a committed catalog choice")
        if kind == "checkbox":
            if not isinstance(value, bool):
                raise ValueError("Checkbox requires an approved boolean")
            before = state(expr)
            if not before:
                raise ValueError("Observed native checkbox is unavailable")
            if before["checked"] != value:
                click_choice(expr)
                wait(0.15)
            after = state(expr)
            if not after or after["checked"] != value or after["invalid"]:
                raise ValueError("Checkbox did not retain the approved answer")
            return {"verified": True, "checked": value}
        if kind == "select":
            options = field.get("options", [])
            matches = [o for o in options if not o.get("disabled") and option_matches(o["label"], value)]
            if len(matches) != 1:
                raise ValueError("Stored answer is absent from dropdown options" if not matches else "Answer does not uniquely match an observed choice")
            wanted = matches[0]
            cdp("DOM.focus", backendNodeId=backend(expr))
            for key in ["Home"]+["ArrowDown"]*sum(not o.get("disabled") for o in options[:options.index(wanted)]):
                virtual = {"Home": 36, "ArrowDown": 40}[key]
                cdp("Input.dispatchKeyEvent", type="keyDown", key=key, code=key, windowsVirtualKeyCode=virtual)
                cdp("Input.dispatchKeyEvent", type="keyUp", key=key, code=key, windowsVirtualKeyCode=virtual)
            wait(0.15)
            interim = state(expr)
            if not interim or interim["value"] != wanted["value"]:
                # macOS native select widgets accept trusted typeahead even
                # when Home/ArrowDown do not commit a selection.
                wait(1.05)
                for char in wanted["label"]:
                    virtual = ord(char.upper()) if char.isascii() and len(char.upper()) == 1 else 0
                    cdp("Input.dispatchKeyEvent", type="keyDown", key=char, text=char, unmodifiedText=char, windowsVirtualKeyCode=virtual)
                    cdp("Input.dispatchKeyEvent", type="keyUp", key=char, windowsVirtualKeyCode=virtual)
            wait(0.15)
            after = state(expr)
            if not after or after["value"] != wanted["value"] or after["invalid"]:
                raise ValueError("Native select did not retain the approved answer")
            return {"verified": True, "selected": wanted["label"]}
        if kind == "combobox":
            if (not isinstance(value, dict) or set(value) != {"query", "choice"}
                    or not all(isinstance(value[key], str) and value[key].strip() for key in ("query", "choice"))):
                raise ValueError("Autocomplete requires an approved query and exact choice")
            type_text(expr, value["query"])
            matches = []
            for _ in range(20):
                options = combobox_options(expr)
                matches = [n for n in options if n.get("name", {}).get("value", "") == value["choice"]]
                if matches:
                    break
                wait(0.25)
            if len(matches) != 1:
                escape()
                raise ValueError("Autocomplete choice is absent" if not matches else "Autocomplete choice is ambiguous")
            _settled_click(matches[0]["backendDOMNodeId"], cdp, wait, helpers["click_at_xy"])
            for _ in range(10):
                wait(0.15)
                after = state(expr)
                if after and after["value"] == value["choice"] and after["expanded"] == "false" and not after["invalid"]:
                    remember_residence(expr,[n.get("name",{}).get("value","") for n in options],after["value"])
                    return {"verified": True, "selected": value["choice"]}
            raise ValueError("Autocomplete did not retain the committed choice")
        if kind == "file":
            path = Path(str(value))
            if not path.is_file() or path.suffix.casefold() != ".pdf" or not path.read_bytes().startswith(b"%PDF-"):
                raise ValueError("Approved PDF is unavailable")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            existing = js("(()=>{const e="+expr+";return e?{filename:e.files?.[0]?.name,receipt:e.__jhbUploadReceipt,sha256:e.__jhbUploadSha256}:null})()")
            if (request.get("upload_receipt") and existing and existing.get("receipt") == request["upload_receipt"]
                    and existing.get("sha256") == digest and existing.get("filename") == path.name):
                return {"verified": True, "filename": path.name, "upload_receipt": existing["receipt"], "sha256": digest, "cached": True}
            cdp("DOM.setFileInputFiles", backendNodeId=backend(expr), files=[str(path.resolve())])
            for _ in range(30):
                wait(0.2)
                retained = js("(()=>{const e="+expr+";return e?.files?.[0]?.name||''})()")
                if retained == path.name:
                    receipt = uuid.uuid4().hex
                    js("(()=>{const e="+expr+";if(!e)return; e.__jhbUploadReceipt="+json.dumps(receipt)+
                       ";e.__jhbUploadSha256="+json.dumps(digest)+";e.addEventListener('change',()=>{delete e.__jhbUploadReceipt;delete e.__jhbUploadSha256},{once:true,capture:true})})()")
                    return {"verified": True, "filename": retained, "upload_receipt": receipt, "sha256": digest}
            raise ValueError("Approved file was not retained")
        if kind in {"text", "email", "tel", "textarea", "url", "number", "date"}:
            if kind == "text" and scope["board"] == "ashby" and field.get("calendar_format") == "MM/DD/YYYY":
                from .calendar_dates import approved_day, native_value, retained_day
                type_text(expr, native_value(value))
                # The observed React datepicker commits/formats on blur. ISO
                # text can parse at UTC midnight and shift a US calendar day.
                cdp("Input.dispatchKeyEvent", type="keyDown", key="Tab", code="Tab", windowsVirtualKeyCode=9)
                cdp("Input.dispatchKeyEvent", type="keyUp", key="Tab", code="Tab", windowsVirtualKeyCode=9)
                for _ in range(2):
                    wait(0.2)
                    after = state(expr)
                    if not after or after["invalid"] or not retained_day(after.get("value"), value):
                        raise ValueError("Calendar input did not retain the approved day")
                return {"verified": True, "calendar_format": "MM/DD/YYYY", "calendar_day": approved_day(value).isoformat()}
            type_text(expr, value)
            after = state(expr)
            actual, expected = (after or {}).get("value"), str(value)
            if kind == "tel":
                actual, expected = re.sub(r"\D", "", actual or ""), re.sub(r"\D", "", expected)
            if not after or after["invalid"] or actual != expected:
                raise ValueError("Manual input did not retain the approved answer")
            return {"verified": True}
        raise ValueError("Unsupported manual control")
    if operation == "next":
        button = request["button"]
        if not safe_next(button):
            raise ValueError("Terminal submission is prohibited")
        nodes = cdp("Accessibility.getFullAXTree")["nodes"]
        matches = [n for n in nodes if str(n.get("backendDOMNodeId")) == button["ref"]
                   and n.get("role", {}).get("value") == "button" and n.get("name", {}).get("value") == button["label"]]
        if len(matches) != 1:
            raise ValueError("Observed continuation is unavailable")
        _settled_click(matches[0]["backendDOMNodeId"], cdp, wait, helpers["click_at_xy"])
        wait(0.3)
        return {"continued": True}
    if operation == "screenshot":
        from .screenshot_runtime import capture_from_top
        return capture_from_top(request, helpers)
    raise ValueError("Unsupported manual CLI operation")
