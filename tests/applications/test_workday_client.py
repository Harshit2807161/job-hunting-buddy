import asyncio

import pytest

from jhb.applications.workday import WorkdayCLI

URL = "https://example.wd5.myworkdayjobs.com/en-US/Careers/job/San-Diego/Engineer_JR123456"


def test_workday_client_identity_scope_and_bounded_records():
    client = WorkdayCLI(URL, experience_count=3)
    assert client.application_url.endswith("/apply")
    assert client.allowed_url(URL+"/apply?source=synthetic")
    assert not client.allowed_url(URL.replace("JR123456", "JR999999"))
    assert not client.allowed_url(URL.replace("example.wd5", "other.wd5"))
    with pytest.raises(ValueError):
        WorkdayCLI("https://example.test/jobs/1")
    with pytest.raises(ValueError):
        WorkdayCLI(URL, experience_count=11)


@pytest.mark.parametrize("approved,stored", [(False, False), (True, False), (True, True)])
def test_workday_authentication_reuses_only_scoped_os_store_without_registration(monkeypatch, approved, stored):
    client = WorkdayCLI(URL)
    origin = "https://example.wd5.myworkdayjobs.com"
    calls = []
    class Vault:
        def get(self, site, email):
            calls.append(("get", site, email))
            return "synthetic-fixture-secret" if stored else None
        def create(self, *args):
            pytest.fail("Workday must never create an account by fallback")
    async def invoke(operation, **payload):
        assert operation == "authenticate" and payload["username"] == "synthetic@example.test"
        assert payload["password"] == "synthetic-fixture-secret"
        calls.append(("authenticate",))
        return {"authenticated": True}
    monkeypatch.setattr(client, "invoke", invoke)
    answers = {"identity.email": {"status": "verified", "value": "synthetic@example.test", "source": "Synthetic profile"}}
    if approved:
        answers["auth.password_exception"] = {"status": "verified", "source": "Synthetic explicit site exception", "value": {
            "origin": origin, "method": "password", "reuse_existing": True}}
    assert asyncio.run(client.authenticate(answers, Vault())) is (approved and stored)
    if not approved:
        assert calls == []
    else:
        assert calls[0] == ("get", origin, "synthetic@example.test")


def test_workday_education_addition_is_deferred_until_its_observed_wizard_step(monkeypatch):
    client = WorkdayCLI(URL, experience_count=3)
    calls = []
    stage = {"experience": False}
    async def invoke(operation, **payload):
        calls.append((operation, payload))
        if operation == "observe":
            return {"fields": [], "buttons": [], "experience_step": stage["experience"]}
        return {"supported": True}
    monkeypatch.setattr(client, "invoke", invoke)
    asyncio.run(client.ensure_education(2))
    assert calls == []
    asyncio.run(client.observe())
    assert [operation for operation, _ in calls] == ["observe"]
    calls.clear(); stage["experience"] = True
    asyncio.run(client.observe())
    assert calls == [("observe", {}), ("records", {"kind": "education", "count": 2}),
                     ("records", {"kind": "workExperience", "count": 3}), ("observe", {})]
