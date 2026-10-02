"""Read-only access probe; fixture success and live compatibility stay separate."""
from __future__ import annotations

import time
from pathlib import Path

from ..config import ROOT
from .booklet import write_private
from .cli_browser import BrowserUseCLI
from .queue import is_greenhouse


async def probe_url(url, *, headless=False):
    if not is_greenhouse(url): raise ValueError("Probe only supports HTTPS Greenhouse URLs")
    directory = ROOT / "private" / "live-probe"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    actions = BrowserUseCLI()
    await actions.open(url)
    observation = await actions.observe()
    result = {"url": url, "observation": observation, "time": int(time.time()),
              "field_mutations": 0, "live_fill_verified": False,
              "browser_backend": "browser-use CLI", "tab_preserved": True}
    await actions.screenshot(directory / "browser.png")
    write_private(directory / "probe.json", result)
    return directory / "probe.json"
