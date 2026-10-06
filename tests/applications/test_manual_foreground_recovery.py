"""Bounded foreground recovery through the CLI lane, with synthetic controls."""
import ast
import fcntl
import json
import re
import subprocess
import threading

import pytest

from jhb.applications.cli_browser import BrowserOperationError, BrowserUseCLI, MARKER
from jhb.applications.manual_ats import ManualATSCLI
from jhb.applications.manual_runtime import application_scope, dispatch

URL = "https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application"
MESSAGE = "Manual choices did not retain the approved answer"


def client():
    result = ManualATSCLI(URL)
    result.target_id, result.expected_url = "owned-tab", URL
    return result


@pytest.mark.parametrize("operation,kind,error", [
    ("fill", "text", BrowserOperationError("Manual input did not retain the approved answer")),
    ("fill", "radio", RuntimeError("Browser Use CLI failed; run browser-use --doctor")),
    ("fill", "combobox", BrowserOperationError("Autocomplete choice is absent", retryable=True)),
    ("fill", "radio", BrowserOperationError("Answer does not uniquely match an observed choice")),
    ("next", "radio", BrowserOperationError(MESSAGE)),
])
def test_recovery_is_not_a_generic_mutation_retry(monkeypatch, operation, kind, error):
    calls = []
    def call(self, op, **payload):
        calls.append(payload)
        raise error
    monkeypatch.setattr(BrowserUseCLI, "call", call)
    with pytest.raises(type(error)):
        client().call(operation, field={"type": kind}, value=True)
    assert len(calls) == 1
    assert calls[0]["foreground"] is False


@pytest.mark.parametrize("state", ["unknown_target", "changed_target", "cancelled", "already_foreground"])
def test_recovery_requires_unchanged_attached_target_and_uncancelled_background_attempt(monkeypatch, state):
    c = client()
    cancel = threading.Event()
    if state == "unknown_target": c.target_id = None
    if state == "already_foreground": c.foreground = True
    calls = []
    def call(self, op, **payload):
        calls.append(payload)
        if state == "changed_target": self.target_id = "another-tab"
        if state == "cancelled": cancel.set()
        raise BrowserOperationError(MESSAGE)
    monkeypatch.setattr(BrowserUseCLI, "call", call)
    with pytest.raises(BrowserOperationError):
        c.call("fill", _cancelled=cancel, field={"type": "radio"}, value=True)
    assert len(calls) == 1


def test_successful_recovery_is_remembered_only_for_that_exact_target(monkeypatch):
    c = client()
    calls = []
    def call(self, op, **payload):
        calls.append(payload)
        if len(calls) == 1:
            raise BrowserOperationError(MESSAGE)
        if payload.get("_before_run"): payload["_before_run"]()
        return {"verified": True}
    monkeypatch.setattr(BrowserUseCLI, "call", call)
    assert c.call("fill", field={"type": "radio"}, value=True)["verified"]
    assert c.last_recovery["recovered"] is True
    c.call("fill", field={"type": "radio"}, value=False)
    c.target_id = "new-owned-tab"
    c.call("fill", field={"type": "radio"}, value=True)
    assert [p["foreground"] for p in calls] == [False, True, True, False]
    assert sum(bool(p.get("recover_background_choice")) for p in calls) == 1


def test_target_is_checked_again_after_reacquiring_browser_lane(monkeypatch):
    c = client()
    calls = []
    def call(self, op, **payload):
        calls.append(payload)
        if len(calls) == 1: raise BrowserOperationError(MESSAGE)
        self.target_id = "changed-while-waiting-for-lane"
        payload["_before_run"]()
        pytest.fail("Recovery ran after its target changed")
    monkeypatch.setattr(BrowserUseCLI, "call", call)
    with pytest.raises(BrowserOperationError, match="target changed"):
        c.call("fill", field={"type": "radio"}, value=True)
    assert len(calls) == 2


@pytest.mark.parametrize("mode", ["success", "repeat_failure", "changed_question", "changed_guard",
                                  "wrong_target", "verification", "changed_on_activation"])
@pytest.mark.parametrize("widget", ["yesno", "radio"])
def test_synthetic_native_retention_recovery_revalidates_before_only_owned_tab_activation(monkeypatch, tmp_path, mode, widget):
    from playwright.sync_api import sync_playwright
    monkeypatch.setattr("jhb.applications.cli_browser.ROOT", tmp_path)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:12345")
    html = '''<form class=ashby-application-form-container><div data-field-path=authorized>
<label class=ashby-application-form-question-title>Authorized?</label>
<button type=button class=ashby-application-form-input-yesno-option data-option=yes aria-pressed=false onclick="pick(this)">Yes</button>
<button type=button class=ashby-application-form-input-yesno-option data-option=no aria-pressed=false onclick="pick(this)">No</button>
</div><button type=submit>Submit</button></form><script>
window.foreground=false;window.commits=0;window.submissions=0;
document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
function pick(e){if(!window.foreground)return;window.commits++;for(const b of e.parentNode.querySelectorAll('button'))b.setAttribute('aria-pressed',b===e?'true':'false')}
</script>'''
    if widget == "radio":
        start = html.index('<button type=button class=')
        end = html.index('</div><button type=submit>')
        html = html[:start] + '''<label><input type=radio name=authorized id=yes style="display:none"
          onclick="if(!window.foreground)event.preventDefault();else window.commits++">Yes</label>
          <label><input type=radio name=authorized id=no style="display:none"
          onclick="if(!window.foreground)event.preventDefault();else window.commits++">No</label>''' + html[end:]
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        page.goto(URL)
        cdp = page.context.new_cdp_session(page)
        calls, activated, selected = [], [], []
        def activate(target):
            activated.append(target)
            if mode != "repeat_failure": page.evaluate("window.foreground=true")
            if mode == "changed_on_activation": page.locator(".ashby-application-form-question-title").evaluate("e=>e.textContent='A different question'")
        helpers = {"cdp": lambda method, **params: cdp.send(method, params), "js": page.evaluate,
                   "wait": lambda seconds: page.wait_for_timeout(seconds * 1000), "click_at_xy": page.mouse.click,
                   "list_tabs": lambda: [{"targetId": "owned-tab", "url": URL}],
                   "current_tab": lambda: {"targetId": "different-tab" if mode == "wrong_target" and len(calls) > 1 else "owned-tab"},
                   "switch_tab": lambda target: selected.append(target), "activate_tab": activate}
        def run(self, script, env, deadline, cancelled):
            # Exercise the real transport's private lock; no subprocess/browser
            # connection is made by this fixture transport.
            with (tmp_path / "private/browser-lane.lock").open("a") as lane:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lane.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            match = re.search(r"dispatch_owned\(json.loads\((.+)\),helpers,dispatch,", script)
            request = json.loads(ast.literal_eval(match[1]))
            calls.append(request)
            try:
                response = dispatch(request, helpers)
            except ValueError as exc:
                response = {"error": str(exc)}
                if len(calls) == 1:
                    if mode == "changed_question": page.locator(".ashby-application-form-question-title").evaluate("e=>e.textContent='Changed screening question'")
                    if mode == "changed_guard": page.evaluate("window.__jhbGuard=false")
                    if mode == "verification": page.evaluate("(()=>{const e=document.createElement('iframe');e.src='https://fixture.test/challenge';e.style.height='150px';document.body.append(e)})()")
            return subprocess.CompletedProcess(["browser-use"], 0, MARKER + json.dumps(response) + "\n", "")
        monkeypatch.setattr(BrowserUseCLI, "_run", run)
        try:
            dispatch({"operation": "open", "scope": application_scope(URL), "url": URL}, helpers)
            field = dispatch({"operation": "observe", "scope": application_scope(URL)}, helpers)["fields"][0]
            c = client()
            if mode == "success":
                assert c.call("fill", field=field, value=True)["verified"]
                assert c.last_recovery["recovered"] is True
                assert page.evaluate("window.commits") == 1
            else:
                with pytest.raises(BrowserOperationError):
                    c.call("fill", field=field, value=True)
                assert c.last_recovery["recovered"] is False
                assert page.evaluate("window.commits") == 0
            assert len(calls) == 2
            assert all(p["target_id"] == "owned-tab" for p in calls)
            assert activated == (["owned-tab"] if mode in {"success", "repeat_failure", "changed_on_activation"} else [])
            assert page.evaluate("window.submissions") == 0
            if mode != "changed_guard": assert page.evaluate("window.__jhbGuard") is True
        finally:
            browser.close()
