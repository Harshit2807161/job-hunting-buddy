import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import shutil
import threading

import pytest

from jhb.applications.greenhouse_source import (
    ApplicationSourceResolver, canonical_greenhouse, classify_ats, resolve_job,
)
from jhb.applications.mcp_client import PlaywrightMCPClient, is_public_url


GH = "https://job-boards.greenhouse.io/example/jobs/123"


def page(url, **values):
    return {"url": url, "title": "Software Engineer", "text": "Software Engineer Apply",
            "headings": ["Software Engineer"], "links": [], "frames": [], "scripts": [],
            "form": False, "fields": 0, "buttons": [], **values}


class FakeMCP:
    def __init__(self, pages):
        self.pages = pages
        self.current = None
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "browser_navigate":
            self.current = self.pages[arguments["url"]]
            return {}
        if name == "browser_snapshot":
            return {"content": [{"type": "text", "text": "Snapshot"}]}
        assert name == "browser_evaluate"
        return self.current


def resolve(pages, url, **kw):
    transport = FakeMCP(pages)
    return asyncio.run(resolve_job({"url": url}, transport=transport, **kw)), transport


def test_http_and_client_redirects_confirm_final_individual_identity():
    source = "https://company.example/careers/engineer"
    result, transport = resolve({source: page(GH, form=True)}, source)
    assert result["state"] == "greenhouse"
    assert result["application_url"] == GH
    assert result["identity"] == ["global", "example", "123"]
    assert result["source_url"] == source
    assert [name for name, _ in transport.calls] == ["browser_navigate", "browser_snapshot", "browser_evaluate"]


def test_linkedin_observed_encoded_apply_link_and_employer_embed():
    source = "https://www.linkedin.com/jobs/view/888"
    employer = "https://company.example/jobs/software?gh_jid=123"
    outbound = "https://www.linkedin.com/redir/redirect?url=https%3A%2F%2Fcompany.example%2Fjobs%2Fsoftware%3Fgh_jid%3D123"
    embed = "https://boards.greenhouse.io/embed/job_app?for=example&token=123"
    result, _ = resolve({source: page(source, text="Software Engineer. Sign in. Join LinkedIn", links=[{"url": outbound, "text": "Apply on company website"}]),
                         employer: page(employer, frames=[{"url": embed}]), embed: page(embed, form=True)}, source)
    assert result["state"] == "greenhouse"
    assert result["application_url"] == embed and result["embedded"]
    assert any(e["kind"] == "embedded_greenhouse" for e in result["evidence"])
    assert len([e for e in result["evidence"] if e["kind"] == "mcp_navigation"]) == 3


def test_job_board_embed_requires_context_job_id():
    embed = "https://boards.eu.greenhouse.io/embed/job_board?for=example"
    assert canonical_greenhouse(embed) is None
    assert canonical_greenhouse(embed, context_url="https://company.example/job?gh_jid=99") == "https://job-boards.eu.greenhouse.io/example/jobs/99"
    assert canonical_greenhouse("https://evil.example/embed/job_app?for=example&token=123") is None
    assert canonical_greenhouse("https://boards.greenhouse.io.evil.example/example/jobs/123") is None
    assert canonical_greenhouse("https://boards.greenhouse.io/embed/job_board/js?for=example", context_url="https://company.example/job?gh_jid=123") == GH


def test_inline_explicit_greenhouse_data_requires_canonical_confirmation():
    source = "https://company.example/job"
    result, _ = resolve({source: page(source, data=[{"job": "123", "board": "example"}]), GH: page(GH, form=True)}, source)
    assert result["state"] == "greenhouse"
    assert any(e["kind"] == "explicit_greenhouse_data_attributes" for e in result["evidence"])


def test_inline_form_individual_api_request_is_confirmed_not_just_branding():
    source = "https://company.example/job"
    result, _ = resolve({source: page(source, resources=["https://boards-api.greenhouse.io/v1/boards/example/jobs/123?questions=true"]),
                         GH: page(GH, form=True)}, source)
    assert result["state"] == "greenhouse"
    assert any(e["kind"] == "observed_greenhouse_job_api_request" for e in result["evidence"])


def test_canonical_company_redirect_verifies_exact_documented_embed_before_queue():
    source = "https://company.example/job?gh_jid=123"
    embed = "https://boards.greenhouse.io/embed/job_app?for=example&token=123"
    posting = page(source, scripts=["https://boards.greenhouse.io/embed/job_board/js?for=example"], buttons=["Apply"])
    result, _ = resolve({source: posting, GH: posting, embed: page(embed, form=True)}, source)
    assert result["state"] == "greenhouse"
    assert result["application_url"] == embed and result["embedded"]
    assert sum(e["kind"] == "mcp_navigation" for e in result["evidence"]) == 3
    result, _ = resolve({source: posting, GH: posting, embed: page(embed, form=False)}, source)
    assert result["state"] == "blocked" and result["application_url"] is None


def test_linkedin_recommended_greenhouse_link_is_not_an_apply_target():
    source = "https://linkedin.com/jobs/view/88"
    result, transport = resolve({source: page(source, links=[{"url": GH, "text": "Recommended: Software Engineer"}])}, source)
    assert result["state"] == "ambiguous"
    assert len(transport.calls) == 3


def test_redirect_to_a_different_individual_greenhouse_job_is_ambiguous():
    result, _ = resolve({GH: page(GH.replace("123", "456"), form=True)}, GH)
    assert result["state"] == "ambiguous"
    assert result["application_url"] is None


def test_inaccessible_outbound_source_and_identity_free_embed_are_uncertain():
    linkedin = "https://linkedin.com/jobs/view/88"
    result, _ = resolve({linkedin: page(linkedin, buttons=["Apply"])}, linkedin)
    assert result["state"] == "ambiguous"
    source = "https://company.example/job"
    result, _ = resolve({source: page(source, frames=[{"url": "https://boards.greenhouse.io/embed/job_board?for=example"}])}, source)
    assert result["state"] == "ambiguous"


def test_several_job_targets_are_ambiguous_not_guessed():
    source = "https://company.example/careers"
    result, _ = resolve({source: page(source, links=[{"url": GH, "text": "Apply"},
                                                    {"url": GH.replace("123", "456"), "text": "Apply"}])}, source)
    assert result["state"] == "ambiguous"
    assert result["application_url"] is None


def test_closed_job_and_unconfirmed_url_never_enqueue():
    result, _ = resolve({GH: page(GH, text="The job you are looking for is no longer open")}, GH)
    assert result["state"] == "not_greenhouse" and result["closed"]
    assert result["ats"] == "greenhouse"
    result, _ = resolve({GH: page(GH)}, GH)
    assert result["state"] == "blocked"
    assert result["application_url"] is None
    result, _ = resolve({GH: page("https://job-boards.greenhouse.io/example?error=true", text="", headings=[])}, GH)
    assert result["state"] == "not_greenhouse" and result["closed"]


def test_linkedin_login_and_verification_are_explicit_handoffs():
    source = "https://www.linkedin.com/jobs/view/888"
    result, _ = resolve({source: page("https://www.linkedin.com/authwall", text="Join LinkedIn Sign in")}, source)
    assert result["state"] == "blocked" and result["handoff"] == "waiting_login"
    result, _ = resolve({source: page(source, text="Verify you are human")}, source)
    assert result["handoff"] == "waiting_captcha"
    result, _ = resolve({source: page("https://linkedin.com/signup/cold-join", text="Create an account")}, source)
    assert result["handoff"] == "waiting_login"
    result, _ = resolve({source: page("https://company.example/job", title="Just a moment...", text="Performing security verification")}, source)
    assert result["handoff"] == "waiting_captcha"


def test_wrapper_resolves_non_greenhouse_ats_without_attempting_application():
    source = "https://www.linkedin.com/jobs/view/888"
    workday = "https://example.wd1.myworkdayjobs.com/en-US/jobs/job/Engineer_123"
    result, _ = resolve({source: page(source, links=[{"url": workday, "text": "Apply"}]),
                         workday: page(workday)}, source)
    assert result["state"] == "not_greenhouse"
    assert result["ats"] == "workday"
    assert result["application_url"] == workday


@pytest.mark.parametrize("url,ats", [
    ("https://jobs.lever.co/company/123", "lever"), ("https://jobs.ashbyhq.com/company/123", "ashby"),
    ("https://jobs.smartrecruiters.com/company/123", "smartrecruiters"),
    ("https://careers-company.icims.com/jobs/123", "icims"), ("https://company.taleo.net/careersection/jobdetail.ftl", "taleo"),
    ("https://indeed.com/viewjob?jk=123", "indeed"), ("https://greenhouse.io.evil.example/", "unknown")])
def test_classifies_observed_host_with_suffix_boundary(url, ats):
    assert classify_ats(url) == ats


@pytest.mark.parametrize("url", ["http://127.0.0.1/a", "https://localhost/a", "http://169.254.169.254/a",
                                  "http://10.0.0.1/a", "file:///tmp/x", "https://user:password@company.example/a",
                                  "http://[::1]/a", "https://company.local/a", "https://company.example:8443/a"])
def test_rejects_unsafe_source_before_navigation(url):
    assert not is_public_url(url)
    result, transport = resolve({}, url)
    assert result["state"] == "blocked"
    assert not transport.calls


def test_redirect_loop_and_hop_budget_are_bounded():
    one, two = "https://company.example/a", "https://company.example/b"
    pages = {one: page(one, links=[{"url": two, "text": "Apply"}]),
             two: page(two, links=[{"url": one, "text": "Apply"}])}
    result, transport = resolve(pages, one)
    assert result["state"] == "blocked" and "cycle" in result["reason"]
    assert len(transport.calls) == 6
    result, _ = resolve(pages, one, max_hops=1)
    assert "hop budget" in result["reason"]


def test_actual_official_mcp_stdio_http_redirect_and_embed_inspection(monkeypatch):
    """An actual isolated MCP process reads synthetic HTTP pages; no site login."""
    if not shutil.which("npx"):
        pytest.skip("Node.js/npx is required for actual MCP fixture")
    class Handler(BaseHTTPRequestHandler):
        writes = 0
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/job")
                self.end_headers()
                return
            body = b'<h1>Synthetic Engineer</h1><iframe src="https://boards.greenhouse.io/embed/job_app?for=synthetic&token=123"></iframe><form><input name="name"></form>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            type(self).writes += 1
            self.send_response(200)
            self.end_headers()
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("JHB_SMTP_PASS", "synthetic-private-credential")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-private-credential")
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:52018")
    actual_spawn = asyncio.create_subprocess_exec
    async def checked_spawn(*args, **kwargs):
        assert args[2] == "@playwright/mcp@0.0.83"
        assert "--isolated" in args and "--headless" in args
        assert "--cdp-endpoint" not in args
        assert all(key not in kwargs["env"] for key in ("JHB_SMTP_PASS", "OPENAI_API_KEY", "BU_CDP_URL"))
        return await actual_spawn(*args, **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", checked_spawn)
    async def run():
        from jhb.applications.greenhouse_source import _json_result, _OBSERVE
        async with PlaywrightMCPClient(allow_localhost=True) as client:
            # All content is synthetic and localhost-only in this test. Show
            # tool diagnostics on CI failures without changing live log policy.
            navigation = await client._request("tools/call", {"name": "browser_navigate", "arguments": {"url": url + "/redirect"}})
            assert not navigation.get("isError"), navigation.get("content")
            await client.call_tool("browser_snapshot", {})
            observed = _json_result(await client.call_tool("browser_evaluate", {"function": _OBSERVE}))
            assert observed["url"] == url + "/job"
            assert observed["headings"] == ["Synthetic Engineer"]
            assert canonical_greenhouse(observed["frames"][0]["url"]) == "https://job-boards.greenhouse.io/synthetic/jobs/123"
            # Attempted fixture POST is blocked by the server's read-only route guard.
            blocked = await client.call_tool("browser_evaluate", {"function": "async () => { try { await fetch('/write', {method:'POST'}); return false; } catch (_) {return true;} }"})
            assert "true" in str(blocked)
            assert Handler.writes == 0
    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
