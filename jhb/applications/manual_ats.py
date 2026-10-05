"""Exact-job board adapters sharing the registered Browser Use CLI transport."""
from urllib.parse import urlsplit, urlunsplit

from .cli_browser import BrowserUseCLI
from .manual_runtime import application_scope, matches_scope


class ManualATSCLI(BrowserUseCLI):
    _dispatch_module = "jhb.applications.manual_runtime"

    def __init__(self, approved_url, *, board="ashby", foreground=False, **kwargs):
        super().__init__(**kwargs)
        if not isinstance(foreground, bool):
            raise ValueError("Foreground preference must be an explicit boolean")
        self.foreground = foreground
        self._residence_query = None
        from .boards import board_type, job_identity
        if board_type(approved_url) != board or job_identity(approved_url) is None:
            raise ValueError(f"Manual scope requires an exact {board.title()} application URL")
        parsed = urlsplit(approved_url)
        if board == "ashby" and not parsed.path.rstrip("/").endswith("/application"):
            parsed = parsed._replace(path=parsed.path.rstrip("/")+"/application")
        if board in {"workable", "lever"} and not parsed.path.rstrip("/").endswith("/apply"):
            parsed = parsed._replace(path=parsed.path.rstrip("/")+"/apply")
        self.application_url = urlunsplit(parsed)
        self._scope = application_scope(self.application_url, board)
        self._identity = job_identity(self.application_url)

    def call(self, operation, *, _cancelled=None, **payload):
        payload["scope"] = dict(self._scope)
        payload["foreground"] = self.foreground
        try:
            return super().call(operation, _cancelled=_cancelled, **payload)
        except ValueError as exc:
            from .cli_browser import BrowserOperationError
            mechanical = {"Manual input did not retain the approved answer",
                          "Residence catalog could not recommit the original selection",
                          "Existing residence differs from the approved state and country",
                          "Manual choices did not retain the approved answer",
                          "Approved file was not retained", "Observed manual control is unavailable",
                          "Observed manual input did not receive focus",
                          "Observed manual field has changed", "Autocomplete did not retain the committed choice",
                          "Lever location choice is absent or ambiguous", "Observed Lever location choice changed",
                          "Lever location did not retain a committed catalog choice"}
            if isinstance(exc, BrowserOperationError) and str(exc) in mechanical:
                self.last_failure = {"operation": operation, "kind": "browser_mechanics"}
                raise BrowserOperationError(str(exc), retryable=True) from None
            raise

    def allowed_url(self, url):
        from .boards import job_identity
        return job_identity(url) == self._identity and self._identity is not None

    async def open(self, url):
        if not self.allowed_url(url):
            raise ValueError("Requested job differs from the approved board scope")
        response = await self.invoke("open", url=self.application_url)
        if not matches_scope(response.get("url", ""), self._scope):
            self.redirected_to = response.get("url")
            raise ValueError("Application redirected outside its approved job scope")
        self.target_id, self.expected_url = response["target_id"], response["url"]
        return response

    async def ensure_education(self, count):
        return {"supported": False, "reason": "Manual adapter does not add education rows"}

    async def observe(self):
        snapshot = await super().observe()
        if self._scope["board"] == "ashby" and self._residence_query and not snapshot.get("handoff"):
            from .booklet import normalize
            for field in snapshot.get("fields", []):
                if field.get("type") == "combobox" and normalize(field.get("label", "")) == "state/country of residence":
                    catalog = await self.invoke("describe", field=field, query=self._residence_query)
                    if not catalog.get("choices") or catalog.get("truncated"):
                        from .cli_browser import BrowserOperationError
                        raise BrowserOperationError("Autocomplete residence catalog is unavailable", retryable=True)
                    field["options"] = [{"label": v, "value": v} for v in catalog["choices"]]
        return snapshot

    async def ensure_profile(self, answers):
        from .booklet import normalize
        self._residence_query = None
        state = answers.get("identity.state", {})
        if (self._scope["board"] == "ashby" and state.get("status") == "verified" and state.get("source")
                and isinstance(state.get("value"), str) and state["value"].strip()):
            self._residence_query = state["value"]
            country=answers.get("identity.country",{})
            if country.get("status")=="verified" and country.get("source") and isinstance(country.get("value"),str):
                snapshot=await super().observe()
                if not snapshot.get("handoff"):
                    for field in snapshot.get("fields",[]):
                        if field.get("type")=="combobox" and normalize(field.get("label",""))=="state/country of residence":
                            await self.invoke("prepare_residence",field=field,query=self._residence_query,country=country["value"])
        if self._scope["board"] != "workable":
            return {"supported": False}
        snapshot = await self.observe()
        if snapshot.get("handoff"):
            return snapshot
        import re
        groups = {}
        for key, item in answers.items():
            match = re.fullmatch(r"(education|experience)\.(\d+)\.(school|degree|major|company|title|summary|start_date|end_date|current)", key)
            if match and item.get("status") == "verified" and item.get("source"):
                group = groups.setdefault((match[1], int(match[2])), {"index": int(match[2]), "status": "verified", "source": item["source"]})
                group[match[3]] = item["value"]
        filled = []
        # Keep each editor inside one bounded CLI operation and release the
        # shared browser lane between records. Five full record editors exceed
        # the transport budget on real remote CDP even when every value is known.
        for key in sorted(groups):
            result = await self.invoke("records", education=[groups[key]] if key[0] == "education" else [],
                                       experience=[groups[key]] if key[0] == "experience" else [])
            filled.extend(result.get("filled", []))
            if result.get("handoff"):
                return {**result, "filled": filled}
        return {"supported": True, "verified": True, "filled": filled}
