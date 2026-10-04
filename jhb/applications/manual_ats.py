"""Explicit manual application scope; never used by automatic board routing."""
from .cli_browser import BrowserUseCLI
from .manual_runtime import application_scope, matches_scope


class ManualATSCLI(BrowserUseCLI):
    _dispatch_module = "jhb.applications.manual_runtime"

    def __init__(self, approved_url, *, board="ashby", foreground=False, **kwargs):
        super().__init__(**kwargs)
        if not isinstance(foreground, bool):
            raise ValueError("Foreground preference must be an explicit boolean")
        self.foreground = foreground
        self._scope = application_scope(approved_url, board)

    def call(self, operation, *, _cancelled=None, **payload):
        payload["scope"] = dict(self._scope)
        payload["foreground"] = self.foreground
        return super().call(operation, _cancelled=_cancelled, **payload)

    def allowed_url(self, url):
        return matches_scope(url, self._scope)

    async def ensure_education(self, count):
        return {"supported": False, "reason": "Manual adapter does not add education rows"}
