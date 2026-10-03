"""Synthetic coordinate-click races; no live browser or network access."""
import os
import pytest

from jhb.applications.cli_runtime import _settled_click


def quad(y):
    return [100, y, 120, y, 120, y+20, 100, y+20]


def test_click_waits_until_animated_geometry_settles():
    samples = iter([quad(y) for y in [100, 120, 150, 170, 170, 170, 170]])
    methods, waits, clicks = [], [], []
    def cdp(method, **params):
        methods.append(method)
        if method == "DOM.getBoxModel":
            return {"model": {"content": next(samples)}}
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"clientWidth": 600, "clientHeight": 600}}
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
    _settled_click(42, cdp, lambda seconds: None, lambda x, y: clicks.append((x, y)))
    assert clicks == [(110, 190)]


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
