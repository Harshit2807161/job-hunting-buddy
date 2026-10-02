"""Live browser access through the installed Browser Use CLI only."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from ..config import ROOT

MARKER = "JHB_BROWSER_RESULT="


class BrowserUseCLI:
    def __init__(self, *, executable="browser-use", timeout=45):
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
            raise ValueError("Browser operation timeout must be between 0 and 300 seconds")
        self.executable, self.timeout = executable, timeout
        self.target_id = None
        self.last_failure = None
        self._uploads = {}

    @staticmethod
    def _stop(process):
        # The CLI executes the action script itself. Its own session isolates
        # cleanup from Chrome and the persistent Browser Use daemon.
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=2)

    def _run(self, script, env, deadline, cancelled):
        process = subprocess.Popen([self.executable], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
        first = True
        try:
            while True:
                if cancelled.is_set():
                    raise RuntimeError("Browser Use operation cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Browser Use CLI operation timed out")
                try:
                    stdout, stderr = process.communicate(input=script if first else None,
                                                         timeout=min(remaining, 0.2))
                    return subprocess.CompletedProcess([self.executable], process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    first = False
        finally:
            # Reap before releasing the global browser lane, even on timeout,
            # cancellation or a parser exception. Never echo captured content.
            self._stop(process)

    def call(self, operation: str, *, _cancelled=None, **payload):
        started = time.monotonic()
        deadline = started + self.timeout
        cancelled = _cancelled or threading.Event()
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
        if lane.is_symlink() or any(parent.is_symlink() for parent in lane.parents):
            raise RuntimeError("Browser lane lock must remain in its private directory")
        fd = os.open(lane, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(fd, 0o600)
        try:
            while True:
                if cancelled.is_set():
                    raise RuntimeError("Browser Use operation cancelled")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Browser Use browser lane timed out")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    cancelled.wait(min(0.05, max(0, deadline-time.monotonic())))
            result = self._run(script, env, deadline, cancelled)
        except (TimeoutError, RuntimeError):
            self.last_failure = {"operation": operation, "kind": "cancelled" if cancelled.is_set() else "timeout",
                                 "elapsed_seconds": round(time.monotonic()-started, 3)}
            raise
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
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
        cancelled = threading.Event()
        work = asyncio.create_task(asyncio.to_thread(self.call, operation, _cancelled=cancelled, **payload))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            cancelled.set()
            # to_thread does not cancel its worker. Keep waiting for process
            # cleanup before an outer worker can release its application lock.
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if work.done() and not work.cancelled():
                work.exception()
            raise

    async def open(self, url):
        response = await self.invoke("open", url=url)
        self.target_id = response["target_id"]
        return response

    async def observe(self):
        return await self.invoke("observe")

    async def ensure_education(self, count):
        return await self.invoke("education", count=count)

    async def fill(self, field, value):
        cache_key, receipt = None, None
        if field["type"] == "file":
            path = Path(str(value))
            if path.is_file():
                semantic = "cover_letter" if field["label"].casefold() == "cover letter" else field["label"].casefold()
                if semantic in {"resume", "resume/cv"}:
                    semantic = "resume"
                cache_key = (self.target_id, semantic, str(path.resolve()), hashlib.sha256(path.read_bytes()).hexdigest())
                receipt = self._uploads.get(cache_key)
        result = await self.invoke("fill", field=field, value=value, upload_receipt=receipt)
        if cache_key and result.get("verified") and result.get("upload_receipt"):
            self._uploads[cache_key] = result["upload_receipt"]
        return result

    async def describe(self, field):
        return await self.invoke("describe", field=field)

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
