import asyncio
import json
import subprocess
from types import SimpleNamespace

import pytest

from jhb.applications import boards, booklet, cli_browser, credentials, workday, worker

URL = "https://broadridge.wd5.myworkdayjobs.com/en-US/Careers/job/Example/Engineer_JR123456"


@pytest.mark.parametrize("origin,verified,source,expected", [
    (workday.BROADDRIDGE_ORIGIN, True, True, True),
    (workday.BROADDRIDGE_ORIGIN, False, True, False),
    (workday.BROADDRIDGE_ORIGIN, True, False, False),
    ("https://other.wd5.myworkdayjobs.com", True, True, False),
])
def test_worker_wires_os_store_only_for_explicit_verified_broadridge_exception(tmp_path, monkeypatch, origin, verified, source, expected):
    from jhb import eligibility
    from jhb.applications import role_fit
    monkeypatch.setattr(eligibility, "assess_job", lambda job: {"state": "eligible", "reason": "Synthetic verified JD", "policy": "synthetic"})
    monkeypatch.setattr(role_fit, "assess", lambda *args: {"state": "eligible", "reason": "Isolated wiring fixture"})
    monkeypatch.setitem(boards.ADAPTERS["workday"], "prep_enabled", True)
    url = URL.replace(workday.BROADDRIDGE_ORIGIN, origin)
    monkeypatch.setattr(workday, "WorkdayCLI", lambda *args, **kwargs: SimpleNamespace())
    stores = []
    class Vault:
        def __init__(self, demo_path=None):
            assert demo_path is None
            stores.append(self)
        def get(self, *args):
            pytest.fail("Wiring must not read a password outside observed login")
    monkeypatch.setattr(credentials, "CredentialStore", Vault)
    async def prepare(page, job, answers, planner, vault, **kwargs):
        assert kwargs["cli_actions"].job_hash == job["dedupe_hash"]
        assert (vault is not None) is expected
        return {"state": "waiting_login", "reason": "Synthetic guarded login handoff", "filled": [], "events": []}, None
    monkeypatch.setattr(worker, "prepare", prepare)
    async def packet(*args, **kwargs): return tmp_path / "review.html"
    monkeypatch.setattr(worker, "write_packet", packet)
    answers = {"auth.password_exception": {"status": "verified" if verified else "needs_input", "source": "Synthetic explicit user exception" if source else "",
                "value": {"origin": origin, "method": "password", "reuse_existing": True}}}
    book = {"answers": answers, "roles": {"sde": {}, "ml": {}}}
    result, _ = asyncio.run(worker.run_job({"dedupe_hash": "a"*64, "url": url, "company": "Synthetic", "title": "Engineer"},
                                          book, role="sde", planner_name="deterministic", artifacts=tmp_path))
    assert result["state"] == "waiting_login"
    assert bool(stores) is expected


def test_workday_namespace_uses_registered_cli_and_exact_owned_target(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_browser, "ROOT", tmp_path)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:12345")
    scripts = []
    def run(self, script, env, deadline, cancelled):
        scripts.append(script)
        assert env.get("BU_NAME") is None
        return subprocess.CompletedProcess([self.executable], 0, cli_browser.MARKER+json.dumps({"verified": True}), "")
    monkeypatch.setattr(cli_browser.BrowserUseCLI, "_run", run)
    client = workday.WorkdayCLI(URL)
    client.target_id = "synthetic-owned-tab"
    client.expected_url = client.application_url
    assert client.call("observe")["verified"]
    assert "from jhb.applications.workday_runtime import dispatch" in scripts[0]
    assert "synthetic-owned-tab" in scripts[0] and "JR123456" in scripts[0]
