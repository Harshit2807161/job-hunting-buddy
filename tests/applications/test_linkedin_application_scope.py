"""Transport scope only; no invented LinkedIn UI or live browser interaction."""
import asyncio
import pytest
from jhb.applications.linkedin_application import LinkedInApplicationCLI

JOB={'url':'https://www.linkedin.com/jobs/view/1234567890/','board_type':'linkedin_easy_apply','title':'Engineer','company':'Example'}


def test_easy_apply_capability_must_be_observed_not_inferred_from_linkedin_host():
    with pytest.raises(ValueError,match='observed capability'):
        LinkedInApplicationCLI({**JOB,'board_type':'linkedin'})
    with pytest.raises(ValueError,match='exact LinkedIn'):
        LinkedInApplicationCLI({**JOB,'url':'https://www.linkedin.com/jobs/'})
    client=LinkedInApplicationCLI(JOB)
    assert client.allowed_url('https://www.linkedin.com/jobs/view/engineer-example-1234567890/?tracking=synthetic')
    assert not client.allowed_url(JOB['url'].replace('1234567890','1234567891'))
    assert not client.allowed_url('https://www.linkedin.com.evil.invalid/jobs/view/1234567890/')


def test_easy_apply_transport_rejects_wrong_job_and_unconfirmed_guard(monkeypatch):
    client=LinkedInApplicationCLI(JOB)
    async def invoke(operation,**payload):return {'target_id':'synthetic','url':JOB['url'],'guarded':False}
    monkeypatch.setattr(client,'invoke',invoke)
    with pytest.raises(ValueError,match='submission guard'):
        asyncio.run(client.open(JOB['url']))
    with pytest.raises(ValueError,match='differs'):
        asyncio.run(client.open(JOB['url'].replace('1234567890','1234567891')))
