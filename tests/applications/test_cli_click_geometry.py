"""Synthetic coordinate-click races; no live browser or network access."""
import os
import pytest

from jhb.applications.cli_runtime import _settled_click


def quad(y):
    return [100, y, 120, y, 120, y+20, 100, y+20]


def clear_hit_response(method, params):
    if method == "DOM.resolveNode":
        assert params == {"backendNodeId": 42}
        return {"object": {"objectId": "observed-control"}}
    if method == "Runtime.callFunctionOn":
        assert params["objectId"] == "observed-control"
        assert len(params["arguments"]) == 4
        assert "elementFromPoint" in params["functionDeclaration"]
        return {"result": {"value": {"hit": True}}}
    if method == "Runtime.releaseObject":
        assert params == {"objectId": "observed-control"}
        return {}


def test_click_waits_until_animated_geometry_settles():
    samples = iter([quad(y) for y in [100, 120, 150, 170, 170, 170, 170]])
    methods, waits, clicks = [], [], []
    def cdp(method, **params):
        methods.append(method)
        if method == "DOM.getBoxModel":
            return {"model": {"content": next(samples)}}
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"clientWidth": 600, "clientHeight": 600}}
        response = clear_hit_response(method, params)
        if response is not None:
            return response
        assert method == "DOM.scrollIntoViewIfNeeded"
    _settled_click(42, cdp, waits.append, lambda x, y: clicks.append((x, y)))
    assert clicks == [(110, 180)]
    assert len(waits) == 6
    assert methods.count("DOM.scrollIntoViewIfNeeded") == 1


def test_continuously_moving_control_fails_without_click():
    counter = 0
    waits, clicks = [], []
    def cdp(method, **params):
        nonlocal counter
        if method == "DOM.getBoxModel":
            counter += 1
            return {"model": {"content": quad(10+counter*5)}}
        assert method == "DOM.scrollIntoViewIfNeeded"
    with pytest.raises(ValueError, match="did not settle"):
        _settled_click(42, cdp, waits.append, lambda *args: clicks.append(args))
    assert clicks == []
    assert sum(waits) == pytest.approx(1)


def test_stable_offscreen_control_is_scrolled_again_before_click():
    samples = iter([quad(y) for y in [900, 900, 900, 150, 150, 150, 150]])
    scrolls, clicks = [], []
    def cdp(method, **params):
        if method == "DOM.getBoxModel":
            return {"model": {"content": next(samples)}}
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"clientWidth": 600, "clientHeight": 600}}
        response = clear_hit_response(method, params)
        if response is not None:
            return response
        assert method == "DOM.scrollIntoViewIfNeeded"
        scrolls.append(params)
    _settled_click(42, cdp, lambda seconds: None, lambda x, y: clicks.append((x, y)))
    assert len(scrolls) == 2
    assert clicks == [(110, 160)]


def test_final_geometry_is_rechecked_after_viewport_sampling():
    samples = iter([quad(y) for y in [100, 100, 100, 180, 180, 180, 180, 180]])
    clicks = []
    def cdp(method, **params):
        if method == "DOM.getBoxModel":
            return {"model": {"content": next(samples)}}
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"clientWidth": 600, "clientHeight": 600}}
        response = clear_hit_response(method, params)
        if response is not None:
            return response
        assert method == "DOM.scrollIntoViewIfNeeded"
    _settled_click(42, cdp, lambda seconds: None, lambda x, y: clicks.append((x, y)))
    assert clicks == [(110, 190)]


def test_overlay_wheel_recovery_rechecks_hit_before_click_and_releases_objects():
    position, events = [550], []
    def cdp(method, **params):
        events.append((method, params))
        if method == "DOM.getBoxModel":
            return {"model": {"content": quad(position[0])}}
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"clientWidth": 600, "clientHeight": 600}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": {"hit": position[0] < 400, "wheel": {"x": 300, "y": 300}}}}
        if method == "Input.dispatchMouseEvent":
            assert params["type"] == "mouseWheel" and params["deltaY"] > 0
            position[0] = 250
            return {}
        return clear_hit_response(method, params)
    _settled_click(42, cdp, lambda seconds: None, lambda x, y: events.append(("click", (x, y))))
    assert events[-1] == ("click", (110, 260))
    methods = [e[0] for e in events]
    assert methods.count("Runtime.callFunctionOn") == 2
    assert methods.count("Runtime.releaseObject") == 2
    assert methods.index("Runtime.releaseObject") < methods.index("Input.dispatchMouseEvent")


@pytest.mark.parametrize("wheel", [None, {"x": 300, "y": 300}, {"x": float("nan"), "y": 300}])
def test_persistent_obstruction_never_clicks_and_bounds_wheel_attempts(wheel):
    events, clicks = [], []
    def cdp(method, **params):
        events.append(method)
        if method == "DOM.getBoxModel":
            return {"model": {"content": quad(550)}}
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"clientWidth": 600, "clientHeight": 600}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": {"hit": False, "wheel": wheel}}}
        if method == "Input.dispatchMouseEvent":
            return {}
        return clear_hit_response(method, params)
    with pytest.raises(ValueError, match="obstructed"):
        _settled_click(42, cdp, lambda seconds: None, lambda *args: clicks.append(args))
    assert clicks == []
    assert events.count("Input.dispatchMouseEvent") == (3 if wheel and wheel["x"] == 300 else 0)
    assert events.count("Runtime.releaseObject") == events.count("Runtime.callFunctionOn")


@pytest.mark.parametrize("settle", [False, True])
def test_delayed_reflow_misses_immediate_click_but_settled_click_succeeds(monkeypatch, settle):
    """Model scroll-triggered reflow plus realistic helper/input latency."""
    from playwright.sync_api import sync_playwright
    from jhb import config
    from jhb.applications import cli_runtime
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    html = '''<!doctype html><html lang="en"><title>Synthetic reflow</title>
    <style>body{margin:0}.gap{height:2500px}.target{transition:transform 180ms linear}
    .target.moved{transform:translateY(180px)}</style>
    <div class="gap"></div><form id="application"><div class="target">
    <label for="certify">Synthetic explicit consent</label><input id="certify" type="checkbox">
    </div><button type="submit">Submit application</button></form><div class="gap"></div>
    <script>window.changed=false;window.submissions=0;window.checks=0;
    document.getElementById('application').onsubmit=e=>{e.preventDefault();window.submissions++};
    document.getElementById('certify').onchange=()=>window.checks++;
    addEventListener('scroll',()=>{if(!window.changed&&scrollY>0){window.changed=true;
      setTimeout(()=>document.querySelector('.target').classList.add('moved'),35)}});
    </script></html>'''
    if not settle:
        def immediate(backend, cdp, wait, click_at_xy):
            cdp("DOM.scrollIntoViewIfNeeded", backendNodeId=backend)
            q = cdp("DOM.getBoxModel", backendNodeId=backend)["model"]["content"]
            click_at_xy(sum(q[0::2])/4, sum(q[1::2])/4)
        monkeypatch.setattr(cli_runtime, "_settled_click", immediate)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 900, "height": 600})
        requests = []
        def serve(route):
            requests.append(route.request.url)
            route.fulfill(status=200, content_type="text/html", body=html)
        page.route("**/*", serve)
        url = "https://job-boards.greenhouse.io/synthetic-fixture/jobs/6789"
        page.goto(url)
        session = page.context.new_cdp_session(page)
        def delayed_click(x, y):
            page.wait_for_timeout(100)
            page.mouse.click(x, y)
        helpers = {"cdp": lambda method, **params: session.send(method, params),
                   "js": page.evaluate, "wait": lambda seconds: page.wait_for_timeout(seconds*1000),
                   "click_at_xy": delayed_click, "list_tabs": lambda: [{"url": url, "targetId": "fixture-tab"}],
                   "switch_tab": lambda target: None, "current_tab": lambda: {"targetId": "fixture-tab"}}
        try:
            cli_runtime.dispatch({"operation": "open", "url": url}, helpers)
            fields = cli_runtime.dispatch({"operation": "observe"}, helpers)["fields"]
            request = {"operation": "fill", "field": fields[0], "value": True}
            if settle:
                assert cli_runtime.dispatch(request, helpers)["verified"]
            else:
                with pytest.raises(ValueError, match="Checkbox did not retain"):
                    cli_runtime.dispatch(request, helpers)
            assert page.locator("#certify").is_checked() is settle
            assert page.evaluate("window.checks") == int(settle)
            assert page.evaluate("window.submissions") == 0
            assert requests == [url]
        finally:
            browser.close()


@pytest.mark.parametrize("settle", [False, True])
def test_sticky_footer_receives_old_click_but_hit_checked_wheel_recovery_selects_target(settle):
    from playwright.sync_api import sync_playwright
    from jhb import config
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    html = '''<!doctype html><style>body{margin:0}.gap{height:540px}
    label{display:block;width:400px;height:40px;background:#eee}input{display:none}
    footer{position:fixed;bottom:0;height:150px;left:0;right:0;background:#ccc}
    .tail{height:1500px}</style><div class="gap"></div>
    <label id="target"><span>Synthetic approved degree choice</span><input id="degree" type="checkbox"></label>
    <div class="tail"></div><footer onclick="window.wrongClicks++">Wrong footer control</footer>
    <script>window.wrongClicks=0;window.correctClicks=0;document.querySelector('input').onchange=()=>window.correctClicks++;</script>'''
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 900, "height": 600})
            page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
            page.goto("https://fixture.invalid/sticky-footer")
            session = page.context.new_cdp_session(page)
            node = session.send("DOM.querySelector", {"nodeId": session.send("DOM.getDocument")["root"]["nodeId"], "selector": "#target"})["nodeId"]
            backend = session.send("DOM.describeNode", {"nodeId": node})["node"]["backendNodeId"]
            cdp = lambda method, **params: session.send(method, params)
            if settle:
                _settled_click(backend, cdp, lambda seconds: page.wait_for_timeout(seconds*1000), page.mouse.click)
            else:
                cdp("DOM.scrollIntoViewIfNeeded", backendNodeId=backend)
                q = cdp("DOM.getBoxModel", backendNodeId=backend)["model"]["content"]
                page.mouse.click(sum(q[0::2])/4, sum(q[1::2])/4)
            assert page.locator("#degree").is_checked() is settle
            assert page.evaluate("window.wrongClicks") == int(not settle)
            assert page.evaluate("window.correctClicks") == int(settle)
            if settle:
                assert page.evaluate("scrollY") > 0
        finally:
            browser.close()


def test_full_overlay_causes_explicit_handoff_without_wrong_click():
    from playwright.sync_api import sync_playwright
    from jhb import config
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content('''<button id="target" onclick="window.correct++">Approved option</button>
              <div style="position:fixed;inset:0" onclick="window.wrong++">Blocking overlay</div>
              <script>window.correct=0;window.wrong=0;</script>''')
            session = page.context.new_cdp_session(page)
            node = session.send("DOM.querySelector", {"nodeId": session.send("DOM.getDocument")["root"]["nodeId"], "selector": "#target"})["nodeId"]
            backend = session.send("DOM.describeNode", {"nodeId": node})["node"]["backendNodeId"]
            with pytest.raises(ValueError, match="obstructed"):
                _settled_click(backend, lambda method, **params: session.send(method, params),
                               lambda seconds: page.wait_for_timeout(seconds*1000), page.mouse.click)
            assert page.evaluate("window.correct") == 0
            assert page.evaluate("window.wrong") == 0
        finally:
            browser.close()


def test_native_control_hit_on_associated_label_descendant_is_safe():
    from playwright.sync_api import sync_playwright
    from jhb import config
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content('''<label style="position:relative;display:block;height:40px;width:300px">
              <span style="position:absolute;left:0;top:0;width:30px;height:30px">Choice</span>
              <input id="target" type="checkbox" style="position:absolute;left:0;top:0;width:24px;height:24px;margin:0;pointer-events:none">
            </label>''')
            session = page.context.new_cdp_session(page)
            node = session.send("DOM.querySelector", {"nodeId": session.send("DOM.getDocument")["root"]["nodeId"], "selector": "#target"})["nodeId"]
            backend = session.send("DOM.describeNode", {"nodeId": node})["node"]["backendNodeId"]
            _settled_click(backend, lambda method, **params: session.send(method, params),
                           lambda seconds: page.wait_for_timeout(seconds*1000), page.mouse.click)
            assert page.locator("#target").is_checked()
        finally:
            browser.close()
