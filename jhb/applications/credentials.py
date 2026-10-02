"""OS keyring for live credentials; isolated local vault for fixture demos."""
from __future__ import annotations

import json
import secrets
import string
from pathlib import Path

from .booklet import write_private


class CredentialStore:
    def __init__(self, demo_path: Path | None = None):
        self.demo_path = demo_path

    def _key(self, origin):
        return "job-hunting-buddy:" + origin

    def get(self, origin, username):
        if self.demo_path:
            data = json.loads(self.demo_path.read_text()) if self.demo_path.exists() else {}
            return data.get(self._key(origin), {}).get(username)
        import keyring
        return keyring.get_password(self._key(origin), username)

    def save(self, origin, username, password):
        if self.demo_path:
            data = json.loads(self.demo_path.read_text()) if self.demo_path.exists() else {}
            data.setdefault(self._key(origin), {})[username] = password
            write_private(self.demo_path, data)
        else:
            import keyring
            # Fail if OS credential storage is unavailable. No plaintext fallback.
            keyring.set_password(self._key(origin), username, password)

    def create(self, origin, username):
        password = "Jhb!" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(28))
        self.save(origin, username, password)
        return password
