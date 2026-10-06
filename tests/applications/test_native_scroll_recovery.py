"""Real synthetic CDP actions; no candidate browser or network access."""
from contextlib import contextmanager

import pytest

from jhb.applications import cli_runtime, manual_runtime


@contextmanager
def fixture(board):
    from playwright.sync_api import sync_playwright
    url = ("https://job-boards.greenhouse.io/synthetic/jobs/1234" if board == "greenhouse" else
           "https://jobs.ashbyhq.com/synthetic/11111111-2222-3333-4444-555555555555/application")
    common = '''<script>window.commits=0;window.submissions=0;
    document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};</script>'''
    html = ('''<form><div class=education--container><input id=school--0>
    <button type=button onclick="window.commits++;const e=document.createElement('input');e.id='school--1';this.before(e)">Add another</button>
    </div><button type=submit>Submit</button></form>''' if board == "greenhouse" else
    '''<form class=ashby-application-form-container><div data-field-path=synthetic>
    <label class=ashby-application-form-question-title>Synthetic choice</label>
    <label><input id=yes name=choice type=radio value=yes style="display:none" onchange="window.commits++">Yes</label>
    <label><input id=no name=choice type=radio value=no style="display:none">No</label>
    </div><button type=submit>Submit</button></form>''') + common
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
            page.goto(url)
            session = page.context.new_cdp_session(page)
            events, activations = [], []
            state = {"targetId": "owned-tab", "url": url}
            def native_click(x, y):
                # Use the same transport so an unknown mouse-press outcome can
                # be injected for both board adapters without replaying it.
                helpers["cdp"]("Input.dispatchMouseEvent", type="mousePressed", button="left", x=x, y=y, clickCount=1)
                helpers["cdp"]("Input.dispatchMouseEvent", type="mouseReleased", button="left", x=x, y=y, clickCount=1)
            helpers = {"cdp": lambda method, **params: session.send(method, params),
                       "js": page.evaluate, "wait": lambda seconds: page.wait_for_timeout(seconds*1000),
                       "current_tab": lambda: dict(state), "switch_tab": lambda target: None,
                       "list_tabs": lambda: [dict(state)], "activate_tab": activations.append,
                       "click_at_xy": native_click}
            dispatch = cli_runtime.dispatch if board == "greenhouse" else manual_runtime.dispatch
            scope = {} if board == "greenhouse" else {"scope": manual_runtime.application_scope(url)}
            dispatch({"operation": "open", "url": url, **scope}, helpers)
            request = {"target_id": "owned-tab", "expected_url": url, **scope}
            if board == "greenhouse":
                request.update(operation="education", count=2)
            else:
                field = dispatch({"operation": "observe", **scope}, helpers)["fields"][0]
                request.update(operation="fill", field=field, value=True)
            yield page, helpers, dispatch, request, state, events, activations
        finally:
            browser.close()


@pytest.mark.parametrize("board", ["greenhouse", "ashby"])
@pytest.mark.parametrize("mode", ["normal", "recover", "repeat_timeout", "target_changed", "job_changed",
                                  "guard_changed", "target_after_activation", "guard_after_activation",
                                  "click_timeout", "non_timeout"])
def test_native_scroll_recovery_is_owned_bounded_and_never_replays_clicks(board, mode):
    with fixture(board) as (page, helpers, dispatch, request, state, events, activations):
        raw = helpers["cdp"]
        scrolls, presses = [], []
        def transport(method, **params):
            events.append(method)
            if method == "DOM.scrollIntoViewIfNeeded":
                scrolls.append(dict(params))
                if mode != "normal" and mode != "click_timeout" and (len(scrolls) == 1 or mode == "repeat_timeout"):
                    if mode == "target_changed": state["targetId"] = "unrelated-tab"
                    if mode == "job_changed": state["url"] = "https://example.invalid/another-job"
                    if mode == "guard_changed": page.evaluate("window.__jhbGuard=false")
                    if mode == "non_timeout": raise RuntimeError("Node is detached")
                    raise RuntimeError("DOM.scrollIntoViewIfNeeded timed out after 5s waiting for daemon")
            if method == "Input.dispatchMouseEvent" and params.get("type") == "mousePressed":
                presses.append(dict(params))
                if mode == "click_timeout":
                    raw(method, **params)  # Delivery occurred; its result is unknown.
                    raise TimeoutError("Native click result timed out")
            return raw(method, **params)
        helpers["cdp"] = transport
        def activate(target):
            activations.append(target)
            if mode == "target_after_activation": state["targetId"] = "another-tab"
            if mode == "guard_after_activation": page.evaluate("window.__jhbGuard=false")
        helpers["activate_tab"] = activate
        if mode in {"normal", "recover"}:
            result = dispatch(request, helpers)
            if board == "greenhouse":
                assert result["rows"] == 2
            else:
                assert result["verified"] is True
            assert page.evaluate("window.commits") == 1
            assert len(presses) == 1
            # A recovered scroll still traverses fresh geometry and hit tests.
            assert "DOM.getBoxModel" in events and "Page.getLayoutMetrics" in events
        else:
            with pytest.raises((ValueError, RuntimeError, TimeoutError)):
                dispatch(request, helpers)
            assert page.evaluate("window.commits") == 0
            assert len(presses) == int(mode == "click_timeout")
        retry = mode in {"recover", "repeat_timeout"}
        assert len(scrolls) == (2 if retry else 1)
        if retry: assert scrolls[0] == scrolls[1]
        assert activations == (["owned-tab"] if mode in {
            "recover", "repeat_timeout", "target_after_activation", "guard_after_activation"} else [])
        assert page.evaluate("window.submissions") == 0


@pytest.mark.parametrize("missing", ["target_id", "expected_url"])
def test_scroll_recovery_requires_explicit_owned_job_binding(missing):
    with fixture("greenhouse") as (page, helpers, dispatch, request, state, events, activations):
        del request[missing]
        raw = helpers["cdp"]
        def transport(method, **params):
            if method == "DOM.scrollIntoViewIfNeeded": raise TimeoutError("native scroll timeout")
            return raw(method, **params)
        helpers["cdp"] = transport
        with pytest.raises((TimeoutError, ValueError)):
            dispatch(request, helpers)
        assert activations == []
        assert page.evaluate("window.commits") == 0
