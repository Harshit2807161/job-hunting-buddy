"""Observed Workable delayed public descriptions and exact-job ownership."""
import asyncio
import os

import pytest

from jhb import config
from jhb.applications.greenhouse_source import _OBSERVE, _observed_description, resolve_job
from tests.applications.test_greenhouse_source import FakeMCP, page

URL='https://apply.workable.com/example/j/ABC1234567'
TEXT='Build production Python APIs and deployed AI prototypes. '*5

class DelayedMCP(FakeMCP):
    def __init__(self,late):
        super().__init__({URL:page(URL)})
        self.late=late
    async def call_tool(self,name,arguments):
        if name=='browser_evaluate' and any(n=='browser_evaluate' for n,_ in self.calls):
            self.current=self.late
        return await super().call_tool(name,arguments)


def test_delayed_workable_description_waits_once_and_verifies_same_job():
    transport=DelayedMCP(page(URL,descriptions=[{'url':URL,'title':'Software Engineer','text':TEXT}]))
    result=asyncio.run(resolve_job({'url':URL},transport=transport))
    assert result['verified_job_description']['text']==TEXT.strip()
    assert result['application_url']==URL
    assert sum(name=='browser_evaluate' for name,_ in transport.calls)==2


def test_changed_job_during_description_wait_never_supplies_application():
    other=URL.replace('ABC1234567','ABC1234568')
    transport=DelayedMCP(page(other,descriptions=[{'url':other,'title':'Other Engineer','text':TEXT}]))
    result=asyncio.run(resolve_job({'url':URL},transport=transport))
    assert result['state']=='ambiguous' and result['application_url'] is None
    assert 'verified_job_description' not in result


def test_workable_description_collection_includes_same_main_requirements():
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.local-browsers'))
    from playwright.sync_api import sync_playwright
    html='''<h1>Software Engineer</h1><main><section data-ui="job-description">Build production Python APIs.</section>
    <section data-ui="job-requirements">Must hold an active US security clearance. Experience with cloud deployment required.</section></main>
    <aside><section data-ui="job-requirements">Unrelated newsletter terms</section></aside>'''
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page_=browser.new_page()
        try:
            page_.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=html))
            page_.goto(URL)
            observation=page_.evaluate(_OBSERVE)
            assert len(observation['descriptions'])==1
            description=_observed_description(observation)
            assert 'Must hold an active US security clearance' in description['text']
            assert 'newsletter' not in description['text']
        finally:browser.close()
