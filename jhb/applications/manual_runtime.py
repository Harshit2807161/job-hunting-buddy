"""Observed Ashby mechanics behind a per-job, explicit manual URL scope."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .booklet import normalize
from .browser import GUARD_SCRIPT
from .cli_runtime import _settled_click, option_matches
from .planner import safe_next


def application_scope(url, board="ashby"):
    parsed = urlsplit(url)
    if (board != "ashby" or parsed.scheme != "https" or parsed.hostname != "jobs.ashbyhq.com"
            or parsed.username or parsed.password or parsed.port not in {None, 443}
            or not re.fullmatch(r"/[A-Za-z0-9_-]+/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/application/?", parsed.path)):
        raise ValueError("Manual scope requires an exact Ashby application URL")
    return {"board": board, "origin": "https://jobs.ashbyhq.com", "path": parsed.path.rstrip("/")}


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
  const controls=[...group.querySelectorAll('input,textarea,select')].filter(owned);
  const required=group.getAttribute('aria-required')==='true'||group.getAttribute('data-required')==='true'||
    controls.some(e=>e.required||e.getAttribute('aria-required')==='true')||
    (!!title&&([...title.classList].some(c=>c.includes('_required_'))||/\*$/.test(heading)));
  const yesno=[...group.querySelectorAll('button.ashby-application-form-input-yesno-option')].filter(e=>owned(e)&&visible(e));
  const radios=controls.filter(e=>e.type==='radio'&&choiceVisible(e)&&e.name!=='communicationConsent');
  const checks=controls.filter(e=>e.type==='checkbox'&&choiceVisible(e));
  if(yesno.length)fields.push({ref:'ashby:'+path,label:heading,type:'radio',widget:'yesno',required,owner_index,
   options:yesno.map(e=>({label:e.innerText.trim(),value:e.getAttribute('data-option')}))});
  else if(radios.length||checks.length){const options=radios.length?radios:checks;
   fields.push({ref:'ashby:'+path,label:heading,type:radios.length?'radio':'multiselect',widget:radios.length?'radio':'checkboxes',required,owner_index,
    options:options.map(e=>({label:label(e),value:e.value,id:e.id,checked:e.checked}))});}
  for(const [index,e] of controls.entries()){
   if(e.type==='radio'||e.type==='checkbox'||['hidden','password','submit','button','reset'].includes(e.type))continue;
   if(e.type!=='file'&&!visible(e))continue;
   fields.push({ref:e.id||'ashby:'+path+':control:'+index,widget:'native',control_index:index,owner_index,
    label:label(e)||heading,type:e.getAttribute('role')==='combobox'?'combobox':e.tagName==='SELECT'?'select':e.type,required:e.required||required,
    options:e.tagName==='SELECT'?[...e.options].map(o=>({label:o.label,value:o.value,disabled:o.disabled})):[]});
  }
  const consent=controls.filter(e=>e.type==='radio'&&e.name==='communicationConsent'&&choiceVisible(e));
  if(consent.length)fields.push({ref:'ashby:'+path+':communicationConsent',label:group.querySelector('[class*="consentBody"]')?.innerText?.trim()||'Consent to receive application text message updates',
   type:'radio',widget:'communicationConsent',required:consent.some(e=>e.required),owner_index,options:consent.map(e=>({label:label(e),value:e.value,checked:e.checked}))});
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
    if request.get("foreground") is True and operation in {"fill", "describe"}:
        # An opt-in response to demonstrated background-input failure. Reuse
        # this client's attached, scope-checked target inside the browser lane.
        helpers["activate_tab"](helpers["current_tab"]()["targetId"])
        wait(0.1)

    def fields():
        return js(ASHBY_FIELDS)

    def group_expr(field):
        return "("+ASHBY_GROUPS+")["+str(field["owner_index"])+"]"

    def element_expr(field, option=None):
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
        return {"url": js("location.href"), "title": js("document.title"), "fields": fields(),
                "buttons": [{"ref": str(n["backendDOMNodeId"]), "label": n.get("name", {}).get("value", "")}
                            for n in nodes if n.get("role", {}).get("value") == "button" and n.get("backendDOMNodeId")]}
    if operation in {"describe", "fill"}:
        requested = request["field"]
        current = [f for f in fields() if f["ref"] == requested["ref"]]
        if len(current) != 1 or any(current[0][k] != requested[k] for k in ("label", "type")):
            raise ValueError("Observed manual field has changed")
        field = current[0]
        if operation == "describe":
            if field["type"] == "combobox":
                expr = element_expr(field)
                before = state(expr)
                try:
                    click(expr)
                    labels = []
                    for _ in range(8):
                        wait(0.15)
                        labels = [n.get("name", {}).get("value", "") for n in combobox_options(expr)]
                        if labels:
                            break
                finally:
                    escape()
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
                    return {"verified": True, "selected": value["choice"]}
            raise ValueError("Autocomplete did not retain the committed choice")
        if kind == "file":
            path = Path(str(value))
            if not path.is_file() or path.suffix.casefold() != ".pdf":
                raise ValueError("Approved PDF is unavailable")
            cdp("DOM.setFileInputFiles", backendNodeId=backend(expr), files=[str(path.resolve())])
            for _ in range(30):
                wait(0.2)
                retained = js("(()=>{const e="+expr+";return e?.files?.[0]?.name||''})()")
                if retained == path.name:
                    return {"verified": True, "filename": retained}
            raise ValueError("Approved file was not retained")
        if kind in {"text", "email", "tel", "textarea", "url", "number", "date"}:
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
        path = Path(request["path"])
        helpers["capture_screenshot"](str(path))
        path.chmod(0o600)
        return {"screenshot": str(path)}
    raise ValueError("Unsupported manual CLI operation")
