"""Bounded read-only loopback Chrome availability; never starts a browser."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_BYTES = 65536
TIMEOUT = 2
LOCAL = {"127.0.0.1", "localhost", "::1"}
# browser-use 0.1.13 rejects doctor subcommand flags before delegation, while
# its normal CLI access mode pre-imports this read-only harness diagnostic.
HEALTH_SCRIPT = b"raise SystemExit(run_doctor_json(require_existing_daemon=True))\n"


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
    return (isinstance(report, dict) and type(report.get("schema_version")) is int and report["schema_version"] == 1
            and report.get("healthy") is True and report.get("require_existing_daemon") is True
            and isinstance(daemon, dict) and daemon.get("name") == "default"
            and daemon.get("alive") is True and daemon.get("browser_ready") is True
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
