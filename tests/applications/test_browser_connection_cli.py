"""Chrome147 direct-WS compatibility via strict official CLI; injected only."""
import json
import subprocess
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from jhb import config
from jhb.applications import browser_connection as connection
from tests.applications.test_browser_connection import URL, SOCKET, Response


@pytest.fixture
def active(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    path = tmp_path/"DevToolsActivePort"
    path.write_text("12345\n/devtools/browser/synthetic-owned-browser\n")
    return path


def healthy():
    return {"schema_version": 2, "probe": "browser_level_targets", "transport_verified": True,
            "require_existing_daemon": True, "target_count": 1,
            "daemon": {"name": "default", "alive": True, "browser_kind": "local"}}


def process(value=None, code=0):
    return SimpleNamespace(returncode=code, stdout=json.dumps(value if value is not None else healthy()).encode())


def test_direct_ws_uses_official_cli_transport_existing_default_without_http_or_autostart(active, monkeypatch):
    monkeypatch.setenv("BU_NAME", "unrelated")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert command == ["browser-use"]
        assert kwargs["input"] == connection.HEALTH_SCRIPT
        assert kwargs["timeout"] == 2 and kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.DEVNULL and kwargs["check"] is False
        assert "BU_NAME" not in kwargs["env"]
        assert kwargs["env"]["BH_REQUIRE_EXISTING_DAEMON"] == "1"
        assert kwargs["env"]["BH_TELEMETRY"] == "0"
        assert kwargs["env"]["BH_UPDATE_CHECK"] == "0"
        assert kwargs["env"]["BH_HOME"] == str(config.ROOT/"private"/"browser-use-harness")
        return process()
    assert connection.available(SOCKET, active_files=[active], runner=run,
        opener=lambda *a, **kw: pytest.fail("Direct websocket forced unsupported HTTP discovery"))
    assert len(calls) == 1


@pytest.mark.parametrize("variant", ["response", "exception"])
def test_http404_falls_back_only_after_matching_current_port_and_strict_transport(active, variant):
    calls = []
    def reader(*args, **kwargs):
        if variant == "exception": raise HTTPError(URL,404,"NotFound",{},None)
        return Response(status=404)
    assert connection.available("http://127.0.0.1:12345", opener=reader, active_files=[active],
                                runner=lambda *a, **kw: calls.append(1) or process())
    assert calls == [1]


@pytest.mark.parametrize("change", ["missing", "wrong_port", "wrong_path", "symlink", "oversize", "malformed"])
def test_unrelated_or_invalid_profile_record_cannot_pass_healthy_default_daemon(active, change):
    if change == "missing": active.unlink()
    elif change == "wrong_port": active.write_text("22222\n/devtools/browser/synthetic-owned-browser\n")
    elif change == "wrong_path": active.write_text("12345\n/devtools/browser/another-browser\n")
    elif change == "symlink":
        other = active.with_name("reference"); active.rename(other); active.symlink_to(other)
    elif change == "oversize": active.write_text("x"*4097)
    else: active.write_text("12345\n/unsafe/path\n")
    assert not connection.available(SOCKET, active_files=[active],
                                    runner=lambda *a, **kw: pytest.fail("Unbound metadata reached CLI"))


@pytest.mark.parametrize("change", ["transport", "probe", "strict", "name", "alive", "count_bool", "count_missing", "schema", "nonzero", "malformed", "oversize"])
def test_transport_report_requires_exact_default_daemon_and_verified_browser_response(active, change):
    report = healthy()
    if change == "transport": report["transport_verified"] = False
    elif change == "probe": report["probe"] = "doctor_attachment"
    elif change == "strict": report["require_existing_daemon"] = False
    elif change == "name": report["daemon"]["name"] = "other"
    elif change == "alive": report["daemon"]["alive"] = False
    elif change == "count_bool": report["target_count"] = True
    elif change == "count_missing": report.pop("target_count")
    elif change == "schema": report["schema_version"] = True
    result = process(report, code=1 if change == "nonzero" else 0)
    if change == "malformed": result.stdout = b"invalid json"
    elif change == "oversize": result.stdout = b"x"*65537
    assert not connection.available(SOCKET, active_files=[active], runner=lambda *a, **kw: result)


@pytest.mark.parametrize('kind,accepted', [('local',True),('cdp',True),('cloud',False),(None,False),('unknown',False)])
def test_loopback_profile_cannot_authorize_cloud_or_unknown_default_daemon(active, kind, accepted):
    report = healthy(); report['daemon']['browser_kind'] = kind
    assert connection.available(SOCKET, active_files=[active], runner=lambda *a, **kw: process(report)) is accepted


def test_old_report_without_browser_kind_fails_closed(active):
    report = healthy(); report['daemon'].pop('browser_kind')
    assert not connection.available(SOCKET, active_files=[active], runner=lambda *a, **kw: process(report))


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired("browser-use",2)])
def test_dead_daemon_or_missing_cli_never_restarts_or_repairs(active, error):
    def run(*a, **kw): raise error
    assert not connection.available(SOCKET, active_files=[active], runner=run)


def test_profile_record_change_during_transport_probe_rejects_health(active):
    def run(*a, **kw):
        active.write_text("12345\n/devtools/browser/restarted-browser\n")
        return process()
    assert not connection.available(SOCKET, active_files=[active], runner=run)


def test_http404_other_port_or_redirect_does_not_trust_unrelated_transport(active):
    assert not connection.available("http://127.0.0.1:33333", active_files=[active],
        opener=lambda *a, **kw: Response(status=404, url="http://127.0.0.1:33333/json/version"),
        runner=lambda *a, **kw: pytest.fail("Wrong port trusted daemon"))
    def redirected(*a, **kw): raise HTTPError("https://remote.example",404,"NotFound",{},None)
    assert not connection.available("http://127.0.0.1:12345", opener=redirected, active_files=[active],
                                    runner=lambda *a, **kw: pytest.fail("Redirect trusted daemon"))


def test_ws_setting_takes_precedence_like_installed_harness(active, monkeypatch):
    monkeypatch.setenv("BU_CDP_WS", SOCKET)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:33333")
    assert connection.available(active_files=[active], runner=lambda *a, **kw: process(),
                                opener=lambda *a, **kw: pytest.fail("Ignored configured WS"))


@pytest.mark.parametrize('kind', ['local','cdp','cloud',None])
def test_installed_browser_use_entry_supports_fixed_access_mode_health_script_without_startup(monkeypatch, capsys, kind):
    """Exercise real wrapper + harness parsers, with only transport injected."""
    from io import StringIO
    import sys
    dotenv = pytest.importorskip("dotenv")
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)
    monkeypatch.setenv("BH_TELEMETRY", "0")
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    entry = pytest.importorskip("browser_use.cli")
    harness = pytest.importorskip("browser_harness.run")
    calls = []
    monkeypatch.setattr(entry, "_set_harness_client_env", lambda: None)
    monkeypatch.setattr(entry, "_patch_browser_harness_cli_text", lambda: None)
    monkeypatch.setattr(entry, "_delegated_to_harness", False)
    monkeypatch.setattr(harness, "print_update_banner", lambda: None)
    monkeypatch.setattr(harness, "_install_helper_trace", lambda: None)
    monkeypatch.setattr(harness, "require_existing_daemon", lambda: calls.append("existing"))
    monkeypatch.setattr(harness, "ensure_daemon", lambda: pytest.fail("Health CLI attempted daemon startup"))
    monkeypatch.setattr(harness, "run_doctor_json", lambda *a, **kw: pytest.fail("Attachment doctor is not transport health"))
    def read_only_targets(method, **kwargs):
        assert method == 'Target.getTargets' and kwargs == {'_response_timeout': 1}
        calls.append('browser_targets')
        return {'targetInfos': [{'targetId': 'synthetic', 'type': 'page', 'url': 'https://private.example/secret', 'title': 'Private title'}]}
    monkeypatch.setattr(harness, 'cdp', read_only_targets)
    monkeypatch.setattr(harness, 'NAME', 'default')
    def browser_kind():
        calls.append('browser_kind')
        return kind
    monkeypatch.setattr(harness, 'daemon_browser_kind', browser_kind)
    monkeypatch.setenv("BH_REQUIRE_EXISTING_DAEMON", "1")
    monkeypatch.setattr(sys, "argv", ["browser-use"])
    monkeypatch.setattr(sys, "stdin", StringIO(connection.HEALTH_SCRIPT.decode()))
    with pytest.raises(SystemExit) as exitcode:
        entry._run_browser_harness()
    assert exitcode.value.code == (0 if kind in {'local','cdp'} else 1)
    assert calls == ["existing", 'browser_kind'] + (['browser_targets'] if kind in {'local','cdp'} else [])
    stdout = capsys.readouterr().out
    report = json.loads(stdout)
    assert report['transport_verified'] is (kind in {'local','cdp'})
    assert report['daemon']['browser_kind'] == kind
    assert 'private.example' not in stdout and 'Private title' not in stdout
    assert 'healthy' not in report and 'browser_ready' not in report['daemon']


def test_installed_wrapper_rejects_doctor_flags_before_harness_dispatch(monkeypatch, capsys):
    import sys
    dotenv = pytest.importorskip("dotenv")
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)
    monkeypatch.setenv("BH_TELEMETRY", "0")
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    entry = pytest.importorskip("browser_use.cli")
    harness = pytest.importorskip("browser_harness.run")
    monkeypatch.setattr(entry, "_set_harness_client_env", lambda: None)
    monkeypatch.setattr(entry, "_patch_browser_harness_cli_text", lambda: None)
    monkeypatch.setattr(harness, "main", lambda: pytest.fail("Rejected flags entered browser harness"))
    monkeypatch.setattr(sys, "argv", ["browser-use", "doctor", "--json", "--require-existing-daemon"])
    with pytest.raises(SystemExit) as exitcode:
        entry._run_browser_harness()
    assert exitcode.value.code == 2
    assert "doctor [--fix-snap]" in capsys.readouterr().err


@pytest.mark.parametrize('payload', [None, {}, {'targetInfos': None}, {'targetInfos': True},
    {'targetInfos': [{}]}, {'targetInfos': [{'targetId': 't'}]},
    {'targetInfos': [{'targetId': '', 'type': 'page'}]},
    {'targetInfos': [{'targetId': 't', 'type': 'page'}] * 10001}])
def test_cli_transport_script_rejects_missing_or_malformed_cdp_targets_without_sensitive_output(payload, capsys):
    calls = []
    def targets(method, **kwargs):
        calls.append(method)
        assert kwargs == {'_response_timeout': 1}
        return payload
    namespace = {'daemon_browser_kind': lambda: 'cdp', 'cdp': targets, 'NAME': 'default',
                 'os': SimpleNamespace(environ={'BH_REQUIRE_EXISTING_DAEMON': '1'})}
    with pytest.raises(SystemExit) as result:
        exec(connection.HEALTH_SCRIPT, namespace)
    assert result.value.code == 1 and calls == ['Target.getTargets']
    report = json.loads(capsys.readouterr().out)
    assert report['transport_verified'] is False and report['target_count'] is None


@pytest.mark.parametrize('error', [TimeoutError('private endpoint'), ConnectionError('private endpoint'),
                                 RuntimeError('browser transport closed: private endpoint')])
def test_cli_closed_transport_is_unavailable_and_does_not_leak_error_or_repair(error, capsys):
    def closed(*a, **kw):raise error
    namespace = {'daemon_browser_kind': lambda: 'local', 'cdp': closed, 'NAME': 'default',
                 'os': SimpleNamespace(environ={'BH_REQUIRE_EXISTING_DAEMON': '1'})}
    with pytest.raises(SystemExit) as result:
        exec(connection.HEALTH_SCRIPT, namespace)
    assert result.value.code == 1
    stdout = capsys.readouterr().out
    assert json.loads(stdout)['transport_verified'] is False
    assert 'private endpoint' not in stdout


def test_installed_daemon_stale_attachment_does_not_retarg_browser_level_health(active, monkeypatch, capsys):
    """Use actual daemon dispatch with injected CDP, not a candidate browser."""
    import asyncio
    daemon_module = pytest.importorskip('browser_harness.daemon')
    monkeypatch.setattr(daemon_module.ipc, 'expected_token', lambda: None)
    calls = []
    class SyntheticCDP:
        async def send_raw(self, method, params, session_id=None):
            calls.append((method, session_id))
            if method == 'Target.getTargetInfo':raise RuntimeError('No target with given id found')
            assert method == 'Target.getTargets' and session_id is None
            return {'targetInfos': [{'targetId': 'existing-user-tab', 'type': 'page', 'url': 'https://private.example/'}]}
    daemon = object.__new__(daemon_module.Daemon)
    daemon.cdp = SyntheticCDP()
    daemon.target_id, daemon.session = 'closed-scratch-tab', 'stale-session'
    daemon._begin_recovery = lambda: pytest.fail('Read-only health triggered session recovery')
    async def probe():
        doctor_attachment = await daemon.handle({'meta': 'connection_status'})
        assert doctor_attachment == {'error': 'cdp_disconnected'}
        # Even an explicitly stale session is ignored for Target.* operations.
        result = await daemon.handle({'method': 'Target.getTargets', 'params': {}, 'session_id': 'stale-session'})
        return result['result']
    targets = asyncio.run(probe())
    namespace = {'daemon_browser_kind': lambda: 'cdp', 'cdp': lambda *a, **kw: targets, 'NAME': 'default',
                 'os': SimpleNamespace(environ={'BH_REQUIRE_EXISTING_DAEMON': '1'})}
    with pytest.raises(SystemExit) as result:
        exec(connection.HEALTH_SCRIPT, namespace)
    assert result.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert connection.available(SOCKET, active_files=[active], runner=lambda *a, **kw: process(report))
    assert calls == [('Target.getTargetInfo', None), ('Target.getTargets', None)]
    assert daemon.target_id == 'closed-scratch-tab' and daemon.session == 'stale-session'
    assert 'browser_ready' not in report['daemon']
