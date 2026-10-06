#!/usr/bin/env python3
"""Keep a private local Chrome available for the official Browser Use CLI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def chrome_ready(endpoint: str) -> bool:
    try:
        with urllib.request.urlopen(endpoint + "/json/version", timeout=1) as response:
            version = json.load(response)
        return "Chrome/" in version.get("Browser", "") and bool(version.get("webSocketDebuggerUrl"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def save_connection(endpoint: str) -> None:
    # Preserve existing SMTP and other settings without printing their values.
    destination = ROOT / ".env"
    lines = destination.read_text().splitlines() if destination.exists() else []
    updates = {"BU_CDP_URL": endpoint, "BH_HOME": str(ROOT / "private" / "browser-use-harness")}
    lines = [line for line in lines if line.partition("=")[0].strip() not in {*updates, "BU_CDP_WS"}]
    lines.extend(f"{key}={value}" for key, value in updates.items())
    temporary = destination.with_name(".env.browser-use.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("\n".join(lines) + "\n")
    temporary.chmod(0o600)
    temporary.replace(destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=52018)
    parser.add_argument("--chrome", type=Path, default=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use a local port between 1024 and 65535")
    endpoint = f"http://127.0.0.1:{args.port}"
    if not chrome_ready(endpoint):
        if not args.chrome.is_file():
            parser.error("Chrome executable unavailable; supply --chrome")
        private = ROOT / "private"
        profile = private / "browser-use-user-data-dir-glassdoor"
        profile.mkdir(parents=True, exist_ok=True)
        private.chmod(0o700)
        profile.chmod(0o700)
        log_fd = os.open(private / "browser-use-chrome.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(log_fd, "a") as log:
            process = subprocess.Popen(
                [str(args.chrome), f"--remote-debugging-port={args.port}", "--remote-debugging-address=127.0.0.1",
                 f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check"],
                stdout=log, stderr=log, start_new_session=True,
            )
        deadline = time.monotonic() + 15
        while not chrome_ready(endpoint):
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("Chrome did not become ready; inspect private/browser-use-chrome.log")
            time.sleep(0.2)
    save_connection(endpoint)
    print(f"Local Chrome ready at {endpoint}; connection saved privately. Chrome stays open until you close it.")


if __name__ == "__main__":
    main()
