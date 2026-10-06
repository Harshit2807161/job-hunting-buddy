"""Injected loopback HTTP responses: never contact Chrome or candidate accounts."""
import json
from urllib.error import HTTPError

import pytest

from jhb import config, notify, store
from jhb.applications import browser_connection as connection, cli, service
from tests.applications.test_service import context

URL = "http://127.0.0.1:12345/json/version"
SOCKET = "ws://127.0.0.1:12345/devtools/browser/synthetic-owned-browser"


class Response:
    status = 200
    def __init__(self, body=None, *, url=URL, status=200):
        self.body = json.dumps(body or {"Browser": "Chrome/143.0.1", "webSocketDebuggerUrl": SOCKET}).encode()
        self.url, self.status, self.read_limit = url, status, None
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def geturl(self): return self.url
    def read(self, limit): self.read_limit = limit; return self.body[:limit]


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:12345", "http://localhost:12345/"])
def test_readonly_check_is_bounded_and_accepts_exact_loopback_chrome(endpoint):
    calls = []
    response = Response()
    def reader(request, **kwargs):
        calls.append((request.full_url, request.get_method(), kwargs))
        assert request.get_header("Authorization") is None
        return response
    assert connection.available(endpoint, opener=reader)
    assert calls == [(URL, "GET", {"timeout": 2})]
    assert response.read_limit == 65537


@pytest.mark.parametrize("endpoint", ["http://remote.example:12345", "http://secret@127.0.0.1:12345",
    "http://127.0.0.1:12345?token=secret", "http://127.0.0.1:12345/#fragment", "http://127.0.0.1:12345/launch",
    "ws://127.0.0.1:12345/devtools/page/tab", "http://127.0.0.1:0", "", None])
def test_invalid_endpoint_never_opens_http(endpoint, monkeypatch):
    monkeypatch.delenv("BU_CDP_URL", raising=False); monkeypatch.delenv("BU_CDP_WS", raising=False)
    assert not connection.available(endpoint, opener=lambda *a, **kw: pytest.fail("Invalid endpoint was opened"))


@pytest.mark.parametrize("body", [
    {"Browser": "Chrome/143", "webSocketDebuggerUrl": "ws://remote.example:12345/devtools/browser/id"},
    {"Browser": "Chrome/143", "webSocketDebuggerUrl": "ws://secret@127.0.0.1:12345/devtools/browser/id"},
    {"Browser": "Chrome/143", "webSocketDebuggerUrl": "ws://127.0.0.1:4444/devtools/browser/id"},
    {"Browser": "Chrome/143", "webSocketDebuggerUrl": "ws://127.0.0.1:12345/devtools/page/id"},
    {"Browser": "Chrome/143", "webSocketDebuggerUrl": "ws://[::1]:12345/devtools/browser/id"},
    {"Browser": "Unrelated service", "webSocketDebuggerUrl": SOCKET},
    {"webSocketDebuggerUrl": SOCKET}, [],
])
def test_response_must_identify_chrome_and_scoped_local_browser_socket(body):
    response = Response(); response.body = json.dumps(body).encode()
    assert not connection.available("http://127.0.0.1:12345", opener=lambda *a, **kw: response)


def test_configured_websocket_identity_must_still_exist():
    assert not connection.available(SOCKET+"-stale", active_files=[], runner=lambda *a, **kw: pytest.fail("Stale path started CLI"))


@pytest.mark.parametrize("change", ["redirect", "non200", "oversize", "malformed"])
def test_redirected_or_invalid_version_data_is_rejected(change):
    response = Response()
    if change == "redirect": response.url = "https://remote.example/version"
    elif change == "non200": response.status = 302
    elif change == "oversize": response.body = b"x"*65537
    else: response.body = b"not json"
    assert not connection.available("http://127.0.0.1:12345", opener=lambda *a, **kw: response)


@pytest.mark.parametrize("error", [ConnectionRefusedError(), TimeoutError(), HTTPError(URL,302,"redirect",{},None)])
def test_closed_or_timed_out_chrome_is_unavailable_without_exception_text(error):
    def reader(*args, **kwargs): raise error
    assert connection.available("http://127.0.0.1:12345", opener=reader) is False


def test_default_reader_disables_environment_proxy_and_redirects(monkeypatch):
    captured = []
    class Opener:
        def open(self, request, **kwargs): return Response()
    def build(*handlers): captured.extend(handlers); return Opener()
    monkeypatch.setattr(connection, "build_opener", build)
    assert connection.available("http://127.0.0.1:12345")
    assert captured[0].proxies == {} and isinstance(captured[1], connection.NoRedirect)
    assert captured[1].redirect_request(None,None,302,"Moved",{},"https://remote.example") is None


@pytest.mark.parametrize("mode,file", [("prepare", "pipeline-status.json"), ("approved", "pipeline-submit-status.json")])
def test_service_closed_browser_opens_no_database_claims_or_email_and_replaces_stale_status(context, monkeypatch, mode, file):
    monkeypatch.setattr(connection, "available", lambda *a, **kw: False)
    status = config.ROOT/"private"/file; status.parent.mkdir(parents=True,exist_ok=True)
    status.write_text(json.dumps({"status": "running", "stage": "preparation"}))
    def forbidden(*a, **kw): pytest.fail("Disconnected worker touched queue or browser")
    monkeypatch.setattr(notify, "send", forbidden)
    result = service.once(mode, connector=forbidden, prepare=forbidden, approved=forbidden)
    assert result == {"state": "blocked", "reason_code": "local_browser_disconnected"}
    saved = json.loads(status.read_text())
    assert saved["status"] == "blocked" and saved["reason_codes"] == ["local_browser_disconnected"]
    assert not (config.ROOT/"synthetic.sqlite3").exists()


def test_disconnected_gate_preserves_existing_queued_job_and_attempt_budget(context, monkeypatch):
    from jhb.applications import queue
    connector, book = context
    conn = connector(); queue.initialize(conn)
    queue.enqueue(conn, [{"dedupe_hash": "a"*64, "url": "https://job-boards.greenhouse.io/synthetic/jobs/123",
                          "company": "Synthetic", "title": "Software Engineer"}])
    conn.execute("UPDATE applications SET attempts=2"); conn.commit()
    before = tuple(conn.execute("SELECT state,attempts,available_at,lease_until FROM applications").fetchone())
    monkeypatch.setattr(connection, "available", lambda *a, **kw: False)
    def forbidden(*a, **kw): pytest.fail("Closed browser tried to consume a job attempt or send email")
    monkeypatch.setattr(notify, "send", forbidden)
    assert service.once("prepare", connector=forbidden, prepare=forbidden, book_path=book)["state"] == "blocked"
    assert tuple(conn.execute("SELECT state,attempts,available_at,lease_until FROM applications").fetchone()) == before
    conn.close()


def test_direct_scheduled_pipeline_cli_gates_before_database_and_publishes_blocked_status(context, monkeypatch, capsys):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setattr(config, "refresh_from_env", lambda: None)
    monkeypatch.setattr(connection, "available", lambda *a, **kw: False)
    monkeypatch.setattr(store, "connect", lambda *a, **kw: pytest.fail("Closed browser pipeline connected database"))
    assert cli.main(["pipeline", "--if-enabled"]) == 0
    assert json.loads(capsys.readouterr().out) == {"state": "blocked", "reason_code": "local_browser_disconnected"}
    assert json.loads((config.ROOT/"private"/"pipeline-status.json").read_text())["status"] == "blocked"


def test_direct_pipeline_ci_does_not_read_private_env_http_or_database(context, monkeypatch, capsys):
    monkeypatch.setenv("CI", "true")
    def forbidden(*a, **kw): pytest.fail("CI accessed live context")
    monkeypatch.setattr(config, "load_dotenv", forbidden)
    monkeypatch.setattr(store, "connect", forbidden)
    monkeypatch.setattr(connection, "available", forbidden)
    assert cli.main(["pipeline"]) == 0
    assert json.loads(capsys.readouterr().out)["reason_code"] == "ci_disabled"
    assert not (config.ROOT/"private").exists()


def test_direct_pipeline_pause_precedes_connection_probe(context, monkeypatch, capsys):
    from jhb.applications import booklet
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setattr(config, "refresh_from_env", lambda: None)
    booklet.write_private(config.ROOT/"private"/"pipeline-pause.json", {"paused": True})
    monkeypatch.setattr(connection, "available", lambda *a, **kw: pytest.fail("Paused pipeline probed Chrome"))
    monkeypatch.setattr(store, "connect", lambda *a, **kw: pytest.fail("Paused pipeline opened queue"))
    assert cli.main(["pipeline"]) == 0
    assert json.loads(capsys.readouterr().out)["reason_code"] == "automation_paused"


def test_isolated_classify_command_remains_available_without_chrome(context, monkeypatch, capsys):
    from jhb.applications import greenhouse_source
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setattr(config, "refresh_from_env", lambda: None)
    monkeypatch.setattr(connection, "available", lambda *a, **kw: pytest.fail("Isolated source check probed Chrome"))
    async def resolve(*args, **kwargs): return {"state": "ambiguous", "board_type": "unknown"}
    monkeypatch.setattr(greenhouse_source, "resolve_job", resolve)
    assert cli.main(["classify", "https://example.invalid/job"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "ambiguous"
