"""Bounded read-only loopback Chrome availability; never starts a browser."""
from __future__ import annotations

import json
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_BYTES = 65536
TIMEOUT = 2
RECONNECT_TIMEOUT = 20
RECONNECT_COOLDOWN = 300
LOCAL = {"127.0.0.1", "localhost", "::1"}
# The installed browser-use wrapper rejects doctor flags before delegation.
# Its normal CLI access mode pre-imports the official read-only health helpers.
HEALTH_SCRIPT = b"""import json
_health_kind = daemon_browser_kind()
_health_verified = False
_health_count = None
if _health_kind in {'local', 'cdp'}:
    try:
        _health_targets = cdp('Target.getTargets', _response_timeout=1)
        _health_infos = _health_targets.get('targetInfos') if isinstance(_health_targets, dict) else None
        _health_verified = (isinstance(_health_infos, list) and len(_health_infos) <= 10000
            and all(isinstance(t, dict) and isinstance(t.get('targetId'), str) and t['targetId']
                    and isinstance(t.get('type'), str) and t['type'] for t in _health_infos))
        if _health_verified:
            _health_count = len(_health_infos)
    except Exception:
        pass
_health_report = {'schema_version': 2, 'probe': 'browser_level_targets',
    'transport_verified': bool(_health_verified),
    'require_existing_daemon': os.environ.get('BH_REQUIRE_EXISTING_DAEMON') == '1',
    'target_count': _health_count,
    'daemon': {'name': NAME, 'alive': _health_kind in {'local', 'cdp'}, 'browser_kind': _health_kind}}
print(json.dumps(_health_report))
raise SystemExit(0 if _health_verified else 1)
"""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def endpoint_parts(endpoint):
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in {"http", "https", "ws", "wss"} or parsed.hostname not in LOCAL
            or parsed.username is not None or parsed.password is not None or not parsed.port
            or parsed.query or parsed.fragment):
        raise ValueError("Browser endpoint must be credential-free loopback")
    if parsed.scheme in {"ws", "wss"}:
        if not re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]+", parsed.path):
            raise ValueError("Unexpected browser websocket path")
    elif parsed.path not in {"", "/"}:
        raise ValueError("Unexpected browser HTTP path")
    return parsed


def _active_match(parsed, files=None):
    """Bind the port/path to a bounded current Chrome profile record."""
    if files is None:
        home = Path.home()
        files = [home / parent / "DevToolsActivePort" for parent in (
            "Library/Application Support/Google/Chrome", "Library/Application Support/Google/Chrome Canary",
            "Library/Application Support/Chromium", ".config/google-chrome", ".config/chromium")]
    for path in files:
        path = Path(path)
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                continue
            with path.open("rb") as handle:
                content = handle.read(4097)
            if len(content) > 4096:
                continue
            lines = content.decode("utf-8").splitlines()
            if len(lines) != 2 or lines[0] != str(parsed.port):
                continue
            candidate = endpoint_parts(f"ws://127.0.0.1:{parsed.port}"+lines[1])
            if parsed.scheme in {"ws", "wss"} and candidate.path != parsed.path:
                continue
            return (str(path), info.st_dev, info.st_ino, info.st_mtime_ns, tuple(lines))
        except (OSError, ValueError, TypeError):
            continue
    return None


def _existing_daemon(parsed, *, runner=None, files=None):
    before = _active_match(parsed, files)
    if before is None:
        return False
    from .. import config
    env = dict(os.environ)
    env.pop("BU_NAME", None)
    env.update(BH_HOME=str(config.ROOT / "private" / "browser-use-harness"),
               BH_REQUIRE_EXISTING_DAEMON="1", BH_TELEMETRY="0", BH_UPDATE_CHECK="0")
    result = (runner or subprocess.run)(["browser-use"], input=HEALTH_SCRIPT,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=TIMEOUT, check=False, env=env)
    if result.returncode != 0 or not isinstance(result.stdout, bytes) or len(result.stdout) > MAX_BYTES:
        return False
    report = json.loads(result.stdout)
    daemon = report.get("daemon", {}) if isinstance(report, dict) else {}
    return (isinstance(report, dict) and type(report.get("schema_version")) is int and report["schema_version"] == 2
            and report.get("probe") == "browser_level_targets" and report.get("transport_verified") is True
            and report.get("require_existing_daemon") is True
            and type(report.get("target_count")) is int and 0 <= report["target_count"] <= 10000
            and isinstance(daemon, dict) and daemon.get("name") == "default"
            and daemon.get("alive") is True
            and daemon.get("browser_kind") in {"local", "cdp"}
            and _active_match(parsed, files) == before)


def available(endpoint=None, *, opener=None, runner=None, active_files=None):
    """Read-only HTTP or strict existing-daemon CLI health; never starts Chrome."""
    try:
        parsed = endpoint_parts(endpoint if endpoint is not None else
                                os.environ.get("BU_CDP_WS") or os.environ.get("BU_CDP_URL", ""))
        if parsed.scheme in {"ws", "wss"}:
            return _existing_daemon(parsed, runner=runner, files=active_files)
        # Do not delegate localhost resolution to environmental DNS settings.
        host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
        origin = host+":"+str(parsed.port)
        scheme = "https" if parsed.scheme in {"https", "wss"} else "http"
        url = urlunsplit((scheme, origin, "/json/version", "", ""))
        reader = opener or build_opener(ProxyHandler({}), NoRedirect()).open
        try:
            response = reader(Request(url, method="GET", headers={"Accept": "application/json"}), timeout=TIMEOUT)
        except HTTPError as exc:
            if exc.code == 404 and exc.geturl() == url:
                return _existing_daemon(parsed, runner=runner, files=active_files)
            return False
        with response:
            if response.status == 404 and response.geturl() == url:
                return _existing_daemon(parsed, runner=runner, files=active_files)
            if response.status != 200 or response.geturl() != url:
                return False
            content = response.read(MAX_BYTES+1)
            if len(content) > MAX_BYTES:
                return False
            value = json.loads(content)
        if (not isinstance(value, dict) or not isinstance(value.get("Browser"), str)
                or not re.fullmatch(r"(?:Chrome|Chromium|HeadlessChrome)/[0-9][0-9.]*", value["Browser"])):
            return False
        socket = endpoint_parts(value.get("webSocketDebuggerUrl", ""))
        return (socket.scheme in {"ws", "wss"} and socket.port == parsed.port
                and (socket.hostname == "::1") == (parsed.hostname == "::1")
                and (parsed.scheme not in {"ws", "wss"} or socket.path == parsed.path))
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        return False


@contextmanager
def _reconnect_lock(path):
    """Never wait behind active browser work or follow a replaced lock path."""
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Browser reconnect lock must remain private")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Browser reconnect lock must be a regular file")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def reconnect(endpoint, *, runner=None, active_files=None, now=None):
    """Opt-in repair of the same local CLI connection, never a browser launch.

    An explicit WebSocket bypasses harness browser/profile discovery. Only the
    fixed browser-level health script runs; no navigation, tab or form actions.
    A failed or interrupted attempt consumes its cooldown before CLI startup.
    """
    if (os.environ.get("JHB_BROWSER_RECONNECT_ENABLED") != "1"
            or os.environ.get("CI", "").lower() in {"1", "true", "yes"}):
        return False
    from .. import config
    from .booklet import write_private
    try:
        parsed = endpoint_parts(endpoint)
        if parsed.scheme != "ws":
            return False  # No discovery or HTTP-to-WebSocket fallback during repair.
        binding = _active_match(parsed, active_files)
        if binding is None or any(parent.is_symlink() for parent in Path(binding[0]).parents):
            return False
        execute = runner or subprocess.run

        def pinned_run(command, **kwargs):
            env = dict(kwargs.get("env", os.environ))
            for key in ("BU_NAME", "BU_CDP_URL", "BU_BROWSER_ID", "BU_AUTOSPAWN"):
                env.pop(key, None)
            env.update(BU_CDP_WS=endpoint, BH_HOME=str(config.ROOT / "private" / "browser-use-harness"),
                       BH_TELEMETRY="0", BH_UPDATE_CHECK="0")
            return execute(command, **{**kwargs, "env": env})

        def healthy():
            return (_active_match(parsed, active_files) == binding
                    and available(endpoint, runner=pinned_run, active_files=active_files)
                    and _active_match(parsed, active_files) == binding)

        if healthy():
            return True
        private = config.ROOT / "private"
        if private.is_symlink() or any(parent.is_symlink() for parent in private.parents):
            return False
        private.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _reconnect_lock(private / "browser-connection.lock") as connection_owned:
            if not connection_owned:
                return False
            with _reconnect_lock(private / "browser-lane.lock") as lane_owned:
                if not lane_owned or _active_match(parsed, active_files) != binding:
                    return False
                if healthy():
                    return True  # Another CLI operation already repaired the connection.
                path = private / "browser-reconnect.json"
                if path.is_symlink():
                    return False
                instant = time.time() if now is None else now
                if path.exists():
                    if not path.is_file() or path.stat().st_size > MAX_BYTES:
                        return False
                    record = json.loads(path.read_text())
                    attempted = record.get("attempted_at") if isinstance(record, dict) else None
                    if type(attempted) not in {int, float} or not 0 <= instant-attempted:
                        return False
                    if instant-attempted < RECONNECT_COOLDOWN:
                        return False
                record = {"schema_version": 1, "endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest(),
                          "attempted_at": instant, "next_attempt_at": instant+RECONNECT_COOLDOWN,
                          "status": "started", "timeout_seconds": RECONNECT_TIMEOUT}
                write_private(path, record)
                status = "failed"
                try:
                    if _active_match(parsed, active_files) != binding:
                        status = "profile_changed"
                    else:
                        result = pinned_run(["browser-use"], input=HEALTH_SCRIPT, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=RECONNECT_TIMEOUT, check=False,
                            env={**os.environ, "BH_REQUIRE_EXISTING_DAEMON": "0"})
                        if _active_match(parsed, active_files) != binding:
                            status = "profile_changed"
                        elif result.returncode == 0 and healthy():
                            status = "connected"
                except subprocess.TimeoutExpired:
                    status = "timed_out"
                except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
                    pass
                write_private(path, {**record, "status": status, "finished_at": time.time() if now is None else now})
                return status == "connected"
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        return False
