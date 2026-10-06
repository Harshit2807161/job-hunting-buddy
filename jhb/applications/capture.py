"""Fresh, atomically promoted private screenshot evidence for review packets."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
import struct
import time
import uuid
import zlib

PNG = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 15*1024*1024


def png(content):
    """Validate bounded PNG framing/checksums without decoding untrusted pixels."""
    if not isinstance(content, bytes) or not content.startswith(PNG) or len(content) > MAX_BYTES:
        return False
    index, types = len(PNG), []
    while index+12 <= len(content):
        size = struct.unpack(">I", content[index:index+4])[0]
        kind, data = content[index+4:index+8], content[index+8:index+8+size]
        end = index+12+size
        if end > len(content) or zlib.crc32(kind+data) & 0xffffffff != struct.unpack(">I", content[end-4:end])[0]:
            return False
        if not types:
            if kind != b"IHDR" or size != 13:
                return False
            width, height = struct.unpack(">II", data[:8])
            if not 0 < width <= 50000 or not 0 < height <= 50000:
                return False
        types.append(kind)
        index = end
        if kind == b"IEND":
            return size == 0 and index == len(content) and b"IDAT" in types
    return False


def valid(packet, content):
    """Legacy packets remain inspectable; new manifests must match exact bytes."""
    if not isinstance(content, bytes):
        return False
    if "capture" not in packet:
        return isinstance(content, bytes) and content.startswith(PNG)
    item = packet["capture"]
    return (isinstance(item, dict) and item.get("schema_version") == 1 and item.get("verified") is True
            and item.get("filename") == "browser.png" and isinstance(item.get("capture_id"), str)
            and re.fullmatch(r"[a-f0-9]{32}", item["capture_id"]) is not None
            and item.get("job_hash") == packet.get("job", {}).get("dedupe_hash")
            and item.get("packet_created_at") == packet.get("created_at")
            and isinstance(item.get("captured_at"), str) and bool(item["captured_at"])
            and item.get("sha256") == hashlib.sha256(content).hexdigest() and png(content))


async def fresh(directory: Path, job, *, page=None, cli_actions=None):
    """Capture to one never-used file; an old browser.png cannot count as success."""
    capture_id = uuid.uuid4().hex
    temporary = directory / (".capture-"+capture_id+".png")
    item = {"schema_version": 1, "capture_id": capture_id, "verified": False,
            "job_hash": job.get("dedupe_hash"), "filename": "browser.png"}
    try:
        if temporary.exists() or temporary.is_symlink() or any(parent.is_symlink() for parent in temporary.parents):
            raise ValueError("Capture path is not fresh and private")
        started = time.time_ns()
        if cli_actions is not None and cli_actions.target_id:
            item.update(method="browser_use_cli", target_id=cli_actions.target_id)
            await cli_actions.screenshot(temporary)
        elif page is not None:
            item["method"] = "fixture_browser"
            await page.screenshot(path=str(temporary), full_page=True)
        else:
            raise ValueError("No current capture target")
        status = temporary.lstat()
        if (not stat.S_ISREG(status.st_mode) or status.st_size > MAX_BYTES or status.st_mtime_ns < started):
            raise ValueError("Current screenshot is not a fresh regular file")
        temporary.chmod(0o600)
        content = temporary.read_bytes()
        if not png(content):
            raise ValueError("Current screenshot is not a valid PNG")
        item.update(verified=True, sha256=hashlib.sha256(content).hexdigest(),
                    captured_at=datetime.now(timezone.utc).isoformat())
        os.replace(temporary, directory / "browser.png")
    except Exception as exc:
        item.update(verified=False, error_kind=type(exc).__name__)
    finally:
        # Only this attempt's temporary link/file is disposable. The previous
        # retained screenshot remains untouched if current capture failed.
        try:
            if temporary.is_file() or temporary.is_symlink():
                temporary.unlink()
        except OSError:
            pass
    return item
