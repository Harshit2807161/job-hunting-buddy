"""Bounded same-profile repair through an injected official CLI, never Chrome."""
import fcntl
import json
import subprocess
from types import SimpleNamespace

import pytest

from jhb import config
from jhb.applications import browser_connection as connection, service
from tests.applications.test_browser_connection import SOCKET
from tests.applications.test_browser_connection_cli import healthy


@pytest.fixture
def active(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("JHB_BROWSER_RECONNECT_ENABLED", "1")
    path = tmp_path / "DevToolsActivePort"
    path.write_text("12345\n/devtools/browser/synthetic-owned-browser\n")
    return path


def result(ok=True):
    return SimpleNamespace(returncode=0 if ok else 1, stdout=json.dumps(healthy()).encode())


def test_opt_in_reconnect_pins_same_endpoint_and_requires_independent_strict_health(active, monkeypatch):
    for key, value in {"BU_NAME": "wrong", "BU_CDP_URL": "https://unrelated.invalid", "BU_CDP_WS": "ws://remote.invalid",
                       "BU_BROWSER_ID": "cloud-id", "BU_AUTOSPAWN": "1", "BH_HOME": "/unrelated"}.items():
        monkeypatch.setenv(key, value)
    calls = []
    def run(command, **kwargs):
        strict = kwargs["env"]["BH_REQUIRE_EXISTING_DAEMON"]
        calls.append(strict)
        assert command == ["browser-use"] and kwargs["input"] == connection.HEALTH_SCRIPT
        assert kwargs["timeout"] == (2 if strict == "1" else 20)
        assert kwargs["stderr"] == subprocess.DEVNULL
        env = kwargs["env"]
        assert env["BU_CDP_WS"] == SOCKET
        assert env["BH_HOME"] == str(config.ROOT / "private" / "browser-use-harness")
        assert env["BH_TELEMETRY"] == env["BH_UPDATE_CHECK"] == "0"
        assert not set(env) & {"BU_NAME", "BU_CDP_URL", "BU_BROWSER_ID", "BU_AUTOSPAWN"}
        return result(strict == "0" or "0" in calls)
    assert connection.reconnect(SOCKET, runner=run, active_files=[active], now=1000)
    assert calls == ["1", "1", "0", "1"]
    saved = json.loads((config.ROOT / "private/browser-reconnect.json").read_text())
    assert saved["status"] == "connected" and saved["next_attempt_at"] == 1300
    assert SOCKET not in json.dumps(saved) and len(saved["endpoint_sha256"]) == 64


@pytest.mark.parametrize("mode", ["no_opt_in", "ci", "http", "remote", "missing", "changed", "symlink"])
def test_invalid_scope_never_calls_cli_or_creates_repair_state(active, monkeypatch, mode):
    endpoint = SOCKET
    if mode == "no_opt_in": monkeypatch.delenv("JHB_BROWSER_RECONNECT_ENABLED")
    elif mode == "ci": monkeypatch.setenv("CI", "true")
    elif mode == "http": endpoint = "http://127.0.0.1:12345"
    elif mode == "remote": endpoint = SOCKET.replace("127.0.0.1", "remote.invalid")
    elif mode == "missing": active.unlink()
    elif mode == "changed": active.write_text("12345\n/devtools/browser/unrelated-profile\n")
    elif mode == "symlink":
        target = active.with_name("real-port"); active.rename(target); active.symlink_to(target)
    assert not connection.reconnect(endpoint, runner=lambda *a, **kw: pytest.fail("Unbound repair ran CLI"), active_files=[active])
    assert not (config.ROOT / "private").exists()


def test_already_connected_is_health_only_without_private_state(active):
    calls = []
    def run(*args, **kwargs):
        calls.append(kwargs["env"]["BH_REQUIRE_EXISTING_DAEMON"])
        return result()
    assert connection.reconnect(SOCKET, runner=run, active_files=[active])
    assert calls == ["1"] and not (config.ROOT / "private").exists()


@pytest.mark.parametrize("name", ["browser-connection.lock", "browser-lane.lock"])
def test_existing_lock_prevents_repair_without_wait_or_cooldown_consumption(active, name):
    private = config.ROOT / "private"; private.mkdir()
    calls = []
    with (private / name).open("w") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def run(*args, **kwargs):
            calls.append(kwargs["env"]["BH_REQUIRE_EXISTING_DAEMON"])
            return result(False)
        assert not connection.reconnect(SOCKET, runner=run, active_files=[active])
    assert calls == ["1"] and not (private / "browser-reconnect.json").exists()


def test_failed_attempt_cooldown_is_written_before_cli_and_bounds_future_repairs(active):
    calls = []
    def run(*args, **kwargs):
        strict = kwargs["env"]["BH_REQUIRE_EXISTING_DAEMON"]
        calls.append(strict)
        if strict == "0":
            saved = json.loads((config.ROOT / "private/browser-reconnect.json").read_text())
            assert saved["status"] == "started"
        return result(False)
    for instant in [1000, 1001, 1299]:
        assert not connection.reconnect(SOCKET, runner=run, active_files=[active], now=instant)
    assert calls.count("0") == 1
    assert not connection.reconnect(SOCKET, runner=run, active_files=[active], now=1300)
    assert calls.count("0") == 2


def test_cli_timeout_is_bounded_and_private_record_does_not_leak_output(active):
    def run(*args, **kwargs):
        if kwargs["env"]["BH_REQUIRE_EXISTING_DAEMON"] == "0":
            assert kwargs["timeout"] == connection.RECONNECT_TIMEOUT
            raise subprocess.TimeoutExpired("browser-use", 20, output=b"sensitive output", stderr=b"secret")
        return result(False)
    assert not connection.reconnect(SOCKET, runner=run, active_files=[active], now=1000)
    raw = (config.ROOT / "private/browser-reconnect.json").read_text()
    assert json.loads(raw)["status"] == "timed_out"
    assert "sensitive" not in raw and "secret" not in raw


def test_successful_repair_exit_without_strict_transport_proof_is_rejected(active):
    def run(*args, **kwargs):
        return result(kwargs["env"]["BH_REQUIRE_EXISTING_DAEMON"] == "0")
    assert not connection.reconnect(SOCKET, runner=run, active_files=[active], now=1000)
    assert json.loads((config.ROOT / "private/browser-reconnect.json").read_text())["status"] == "failed"


def test_profile_change_during_repair_cannot_authorize_queue_resume(active):
    calls = []
    def run(*args, **kwargs):
        strict = kwargs["env"]["BH_REQUIRE_EXISTING_DAEMON"]
        calls.append(strict)
        if strict == "0":
            active.write_text("12345\n/devtools/browser/restarted-profile\n")
            return result()
        return result(False)
    assert not connection.reconnect(SOCKET, runner=run, active_files=[active], now=1000)
    assert calls == ["1", "1", "0"]
    assert json.loads((config.ROOT / "private/browser-reconnect.json").read_text())["status"] == "profile_changed"


@pytest.mark.parametrize("kind", ["lock_symlink", "state_symlink", "invalid_state", "future_state"])
def test_untrusted_repair_paths_or_cooldown_records_fail_closed(active, kind):
    private = config.ROOT / "private"; private.mkdir()
    state = private / "browser-reconnect.json"
    if kind == "lock_symlink": (private / "browser-connection.lock").symlink_to(active)
    elif kind == "state_symlink": state.symlink_to(active)
    elif kind == "invalid_state": state.write_text("broken")
    else: state.write_text(json.dumps({"attempted_at": 2000}))
    def run(*args, **kwargs):
        assert kwargs["env"]["BH_REQUIRE_EXISTING_DAEMON"] == "1"
        return result(False)
    assert not connection.reconnect(SOCKET, runner=run, active_files=[active], now=1000)


def test_service_uses_repair_only_after_strict_health_failure(active, monkeypatch):
    monkeypatch.setenv("JHB_APPLICATIONS_ENABLED", "1")
    monkeypatch.setenv("JHB_REQUIRE_PORTAL_APPROVAL", "1")
    monkeypatch.setenv("BU_CDP_WS", SOCKET)
    calls = []
    monkeypatch.setattr(connection, "available", lambda value: calls.append(("health", value)) or False)
    monkeypatch.setattr(connection, "reconnect", lambda value: calls.append(("repair", value)) or True)
    assert service._gate("prepare") is None
    assert calls == [("health", SOCKET), ("repair", SOCKET)]
    calls.clear()
    monkeypatch.setattr(connection, "available", lambda value: True)
    assert service._gate("prepare") is None and calls == []


def test_repair_cannot_bypass_pause_or_ci_gate(active, monkeypatch):
    monkeypatch.setattr(connection, "reconnect", lambda *a, **kw: pytest.fail("Paused/CI service repaired browser"))
    monkeypatch.setenv("CI", "true")
    assert service._gate("prepare") == "ci_disabled"
    monkeypatch.delenv("CI")
    private = config.ROOT / "private"; private.mkdir()
    (private / "pipeline-pause.json").write_text('{}')
    assert service._gate("prepare") == "automation_paused"
