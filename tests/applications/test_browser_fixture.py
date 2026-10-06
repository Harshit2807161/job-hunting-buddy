"""Synthetic fixture checks, separate from live Browser Use validation."""
import asyncio
import os

import pytest

from jhb import config
from jhb.applications import booklet
from jhb.applications.browser import BrowserActions
from jhb.applications.credentials import CredentialStore
from jhb.applications.demo import fixture_server, sample_book
from jhb.applications.planner import deterministic_plan
from jhb.applications.worker import prepare


@pytest.mark.parametrize("role,unknown,expected", [
    ("sde", False, "waiting_review"), ("ml", False, "waiting_review"),
    ("sde", True, "waiting_input"),
])
def test_synthetic_discovery_form_and_submission_boundary(tmp_path, role, unknown, expected):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    from playwright.async_api import async_playwright
    book = sample_book(tmp_path)
    with fixture_server() as (server, origin):
        async def run():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                page = await browser.new_page()
                try:
                    job = {"url": origin+f"/job?role={role}&unknown={int(unknown)}"}
                    result, actions = await prepare(page, job, booklet.for_role(book, role),
                                                    deterministic_plan, CredentialStore(tmp_path / "vault.json"), demo_origin=origin)
                    assert result["state"] == expected
                    if unknown:
                        assert result["missing"][0]["question"] == "Explain your experience with our proprietary engine"
                    else:
                        assert await page.locator("#summary").count() == 1
                        await page.get_by_role("button", name="Submit application").click()
                        await page.evaluate("document.getElementById('final').submit()")
                        await page.evaluate("fetch('/submit',{method:'POST'}).catch(()=>{})")
                        assert await page.evaluate("window.demoSubmitted") == 0
                    assert len(server.accounts) == 1
                    assert server.submissions == 0
                finally:
                    await browser.close()
        asyncio.run(run())


def test_visible_challenge_is_a_handoff(tmp_path):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.ROOT / ".local-browsers"))
    from playwright.async_api import async_playwright
    with fixture_server() as (server, origin):
        async def run():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                page = await browser.new_page()
                try:
                    actions = BrowserActions(page, demo_origin=origin)
                    await actions.install()
                    await page.goto(origin+"/job?captcha=1")
                    assert (await actions.observe())["handoff"] == "waiting_captcha"
                    assert not server.accounts and server.submissions == 0
                finally:
                    await browser.close()
        asyncio.run(run())
