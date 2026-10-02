"""Live browser access through the installed Browser Use CLI only."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import subprocess
from pathlib import Path

from ..config import ROOT

MARKER = "JHB_BROWSER_RESULT="


class BrowserUseCLI:
    def __init__(self, *, executable="browser-use", timeout=45):
        self.executable, self.timeout = executable, timeout
        self.target_id = None

    def call(self, operation: str, **payload):
        if operation != "open" and self.target_id:
            payload["target_id"] = self.target_id
        request = json.dumps({"operation": operation, **payload}, ensure_ascii=False)
        # Python stdin, not shell interpolation. Values never appear in argv.
        script = (
            "import sys,json\n"
            f"sys.path.insert(0,{str(ROOT)!r})\n"
            "from jhb.applications.cli_runtime import dispatch\n"
            "try:\n"
            f"    result=dispatch(json.loads({request!r}),globals())\n"
            "except ValueError as exc:\n"
            "    result={'error':str(exc)}\n"
            f"print({MARKER!r}+json.dumps(result,ensure_ascii=False))\n"
        )
        env = dict(os.environ)
        env.pop("BU_NAME", None)  # One shared local daemon, never a per-job controller.
        env["BH_TELEMETRY"] = "0"
        env["BH_HOME"] = str(ROOT / "private" / "browser-use-harness")
        if not env.get("BU_CDP_URL") and not env.get("BU_CDP_WS"):
            raise RuntimeError("Set the existing browser's BU_CDP_URL or BU_CDP_WS")
        lane = ROOT / "private" / "browser-lane.lock"
        lane.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lane.parent.chmod(0o700)
        lane.touch(mode=0o600, exist_ok=True)
        with lane.open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = subprocess.run([self.executable], input=script, text=True,
                                    capture_output=True, env=env, timeout=self.timeout)
        # CLI errors may contain private page content; never echo them in routine logs.
        if result.returncode:
            raise RuntimeError("Browser Use CLI failed; run browser-use --doctor")
        for line in reversed(result.stdout.splitlines()):
            if line.startswith(MARKER):
                response = json.loads(line[len(MARKER):])
                if isinstance(response, dict) and response.get("error"):
                    raise ValueError(response["error"])
                return response
        raise RuntimeError("Browser Use CLI returned no structured result")

    async def invoke(self, operation, **payload):
        return await asyncio.to_thread(self.call, operation, **payload)

    async def open(self, url):
        response = await self.invoke("open", url=url)
        self.target_id = response["target_id"]
        return response

    async def observe(self):
        return await self.invoke("observe")

    async def ensure_education(self, count):
        return await self.invoke("education", count=count)

    async def fill(self, field, value):
        return await self.invoke("fill", field=field, value=value)

    async def click_next(self, button):
        return await self.invoke("next", button=button)

    async def screenshot(self, path: Path):
        return await self.invoke("screenshot", path=str(path.resolve()))

    async def human_takeover(self, acknowledgement):
        return await self.invoke("takeover", acknowledgement=acknowledgement)

    def allowed_url(self, url):
        from .queue import is_greenhouse
        return is_greenhouse(url)

    blocked_requests = 0

    async def authenticate(self, answers, vault):
        # Hosted Greenhouse forms normally require no account. Unknown auth is
        # a handoff; no password fallback or Google-account guessing.
        return False
