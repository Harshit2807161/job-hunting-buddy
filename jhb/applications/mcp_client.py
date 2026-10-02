"""Small, bounded JSON-RPC stdio client for Microsoft's isolated Playwright MCP.

This is an MCP transport, not a direct Playwright adapter. Source checks never
attach to the candidate's browser or inherit its cookies or CDP configuration.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

PLAYWRIGHT_MCP_VERSION = "0.0.83"

# Runs inside the official server's init-page hook. It blocks writes and local
# network navigation before HTTP redirects or page scripts can reach them.
_INIT_PAGE = r"""
const dns = require('node:dns').promises;
const net = require('node:net');
const privateAddress = address => {
  if (net.isIPv4(address)) {
    const a = address.split('.').map(Number);
    return a[0] === 0 || a[0] === 10 || a[0] === 127 || a[0] >= 224 ||
      (a[0] === 169 && a[1] === 254) || (a[0] === 172 && a[1] >= 16 && a[1] <= 31) ||
      (a[0] === 192 && (a[1] === 168 || a[1] === 0)) || (a[0] === 100 && a[1] >= 64 && a[1] <= 127);
  }
  const s = address.toLowerCase();
  if (s.startsWith('::ffff:')) return privateAddress(s.slice(7));
  return s === '::' || s === '::1' || s.startsWith('fc') || s.startsWith('fd') ||
    /^fe[89ab]/.test(s) || s.startsWith('ff');
};
module.exports.default = async ({ page }) => {
  const fixture = process.env.JHB_MCP_FIXTURE_LOCAL === '1';
  await page.context().route('**/*', async route => {
    const req = route.request();
    try {
      const u = new URL(req.url());
      if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password ||
          !['GET', 'HEAD', 'OPTIONS'].includes(req.method())) return route.abort();
      const host = u.hostname.replace(/^\[|\]$/g, '');
      if (fixture) return ['127.0.0.1', 'localhost', '::1'].includes(host) ? route.continue() : route.abort();
      const rows = net.isIP(host) ? [{address: host}] : await dns.lookup(host, {all: true});
      if (!rows.length || rows.some(r => privateAddress(r.address))) return route.abort();
      return route.continue();
    } catch (_) { return route.abort(); }
  });
};
"""


def fixture_launch_options(allow_localhost):
    # GitHub's Ubuntu AppArmor policy prevents Chromium sandbox startup.
    # This opt-in applies only to trusted, localhost-only synthetic fixtures.
    if allow_localhost and os.environ.get("JHB_MCP_FIXTURE_NO_SANDBOX") == "1":
        return {"chromiumSandbox": False}
    return {}


def chromium_executable() -> str:
    override = os.environ.get("JHB_SOURCE_CHROMIUM")
    if override:
        if not Path(override).is_file():
            raise RuntimeError("JHB_SOURCE_CHROMIUM does not name an installed executable")
        return override
    # Prefer the binary revision belonging to the pinned official MCP package.
    # Python fixture Playwright may install a different Chromium revision.
    if shutil.which("node"):
        try:
            lookup = subprocess.run(["node", "-p", "require('playwright').chromium.executablePath()"],
                                    capture_output=True, text=True, timeout=5,
                                    env={key: value for key, value in os.environ.items()
                                         if key in {"PATH", "HOME", "SYSTEMROOT", "WINDIR", "PLAYWRIGHT_BROWSERS_PATH"}})
            candidate = Path(lookup.stdout.strip())
            if lookup.returncode == 0 and candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
        except (OSError, subprocess.TimeoutExpired):
            pass
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ".local-browsers"))
    patterns = ("chromium-*/**/Contents/MacOS/*", "chromium-*/chrome-linux/chrome",
                "chromium-*/chrome-linux64/chrome", "chromium_headless_shell-*/**/chrome-headless-shell")
    for pattern in patterns:
        for file in sorted(root.glob(pattern), reverse=True):
            if file.is_file() and os.access(file, os.X_OK):
                return str(file.resolve())
    raise RuntimeError("Source checker Chromium is missing; install local Chromium first")


class PlaywrightMCPClient:
    """One ephemeral browser/server per client; requests are serialized."""

    def __init__(self, *, allow_localhost=False, timeout=35, executable_path=None):
        self.allow_localhost = allow_localhost
        self.timeout = timeout
        self.executable_path = executable_path
        self.process = None
        self._lock = asyncio.Lock()
        self._id = 0
        self._stderr_task = None
        self._temp = None
        self.fixture_stderr = ""

    async def __aenter__(self):
        try:
            await self.start()
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *_):
        await self.close()

    async def start(self):
        if self.process:
            return
        if not shutil.which("npx"):
            raise RuntimeError("Playwright MCP requires Node.js and npx")
        self._temp = tempfile.TemporaryDirectory(prefix="jhb-source-mcp-")
        init = Path(self._temp.name) / "source-guard.cjs"
        init.write_text(_INIT_PAGE)
        # Node needs runtime paths, not candidate/SMTP/API/browser credentials.
        runtime_keys = {"PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR",
                        "LANG", "LC_ALL", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR"}
        env = {k: v for k, v in os.environ.items() if k in runtime_keys}
        env["JHB_MCP_FIXTURE_LOCAL"] = "1" if self.allow_localhost else "0"
        if self.allow_localhost:
            env["DEBUG"] = "pw:browser"
        cmd = ["npx", "-y", f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}", "--headless", "--isolated",
               "--executable-path", self.executable_path or chromium_executable(),
               "--block-service-workers", "--image-responses", "omit", "--init-page", str(init),
               "--output-dir", self._temp.name, "--timeout-navigation", "20000",
               "--timeout-action", "5000"]
        launch_options = fixture_launch_options(self.allow_localhost)
        if launch_options:
            fixture_config = Path(self._temp.name) / "fixture-config.json"
            fixture_config.write_text(json.dumps({"browser": {"launchOptions": launch_options}}))
            cmd.extend(["--config", str(fixture_config)])
        self.process = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, limit=8 * 1024 * 1024)
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._request("initialize", {"protocolVersion": "2024-11-05",
                            "capabilities": {}, "clientInfo": {"name": "jhb-source-checker", "version": "1"}})
        await self._notify("notifications/initialized", {})

    async def _drain_stderr(self):
        # Never echo browser URLs, npm environment details, or server stderr.
        while self.process:
            chunk = await self.process.stderr.read(8192)
            if not chunk:
                break
            if self.allow_localhost:
                self.fixture_stderr = (self.fixture_stderr + chunk.decode("utf-8", errors="replace"))[-16000:]

    async def _notify(self, method, params):
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n").encode())
        await self.process.stdin.drain()

    async def _request(self, method, params):
        async with self._lock:
            self._id += 1
            request_id = self._id
            self.process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": request_id,
                                                  "method": method, "params": params}) + "\n").encode())
            await self.process.stdin.drain()
            async def receive():
                while True:
                    line = await self.process.stdout.readline()
                    if not line:
                        raise RuntimeError("Playwright MCP server stopped")
                    response = json.loads(line)
                    if response.get("id") != request_id:
                        continue  # Server notifications are not tool responses.
                    if "error" in response:
                        raise RuntimeError("Playwright MCP rejected the request")
                    return response.get("result", {})
            return await asyncio.wait_for(receive(), self.timeout)

    async def call_tool(self, name, arguments):
        if name not in {"browser_navigate", "browser_snapshot", "browser_evaluate"}:
            raise ValueError("Source checking permits navigation and read-only inspection only")
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError("Playwright MCP source inspection failed")
        return result

    async def close(self):
        process, self.process = self.process, None
        if process:
            if process.stdin:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        if self._stderr_task:
            await asyncio.gather(self._stderr_task, return_exceptions=True)
        if self._temp:
            self._temp.cleanup()


def is_public_url(url: str, *, allow_localhost=False) -> bool:
    """Static navigation gate; server route hook also checks DNS/redirects."""
    from urllib.parse import urlsplit
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        if p.scheme not in {"http", "https"} or not host or p.username or p.password:
            return False
        if p.port not in {None, 80, 443} and not allow_localhost:
            return False
        if allow_localhost and host in {"localhost", "127.0.0.1", "::1"}:
            return True
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except (ValueError, TypeError):
        return False
