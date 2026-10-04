"""Bounded read-only loopback Chrome availability; never starts a browser."""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_BYTES = 65536
TIMEOUT = 2
LOCAL = {"127.0.0.1", "localhost", "::1"}


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


def available(endpoint=None, *, opener=None):
    """Only GET /json/version; no CDP calls, proxies, redirects or auth."""
    try:
        parsed = endpoint_parts(endpoint if endpoint is not None else
                                os.environ.get("BU_CDP_URL") or os.environ.get("BU_CDP_WS", ""))
        # Do not delegate localhost resolution to environmental DNS settings.
        host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
        origin = host+":"+str(parsed.port)
        scheme = "https" if parsed.scheme in {"https", "wss"} else "http"
        url = urlunsplit((scheme, origin, "/json/version", "", ""))
        reader = opener or build_opener(ProxyHandler({}), NoRedirect()).open
        with reader(Request(url, method="GET", headers={"Accept": "application/json"}), timeout=TIMEOUT) as response:
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
    except (OSError, ValueError, TypeError, AttributeError):
        return False
