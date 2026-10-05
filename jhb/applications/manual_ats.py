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
        self._location_query = None
        self._school_query = None
        self._foreground_target = None
        self.last_recovery = None
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
        from .cli_browser import BrowserOperationError
        original_target = (self.target_id, self.expected_url)
        payload["scope"] = dict(self._scope)
        payload["foreground"] = self.foreground or (
            bool(self.target_id) and self._foreground_target == original_target)
        try:
            return super().call(operation, _cancelled=_cancelled, **payload)
        except ValueError as exc:
            kind = payload.get("field", {}).get("type")
            retention_errors = {
                "radio": "Manual choices did not retain the approved answer",
                "multiselect": "Manual choices did not retain the approved answer",
                "checkbox": "Checkbox did not retain the approved answer",
                "select": "Native select did not retain the approved answer",
                "combobox": "Autocomplete did not retain the committed choice",
            }
            if (operation == "fill" and isinstance(exc, BrowserOperationError)
                    and str(exc) == retention_errors.get(kind) and not payload["foreground"]
                    and original_target[0] and original_target[1]
                    and original_target == (self.target_id, self.expected_url)
                    and matches_scope(self.expected_url, self._scope)
                    and not (_cancelled and _cancelled.is_set())):
                # A demonstrated native retention failure permits one bounded
                # foreground recovery. The dispatcher revalidates the exact
                # field, target and guard under the same browser-lane lock.
                retry = {**payload, "foreground": True, "recover_background_choice": True}
                previous_check = payload.get("_before_run")
                def check_recovery_target():
                    if original_target != (self.target_id, self.expected_url):
                        raise BrowserOperationError("Foreground recovery target changed", retryable=True)
                    if previous_check is not None:
                        previous_check()
                retry["_before_run"] = check_recovery_target
                self.last_recovery = {"operation": "fill", "kind": "foreground_native_choice",
                                      "target_id": self.target_id, "recovered": False}
                try:
                    response = super().call(operation, _cancelled=_cancelled, **retry)
                except ValueError as retry_exc:
                    exc = retry_exc
                else:
                    if response.get("verified") is not True:
                        raise BrowserOperationError("Foreground native choice recovery was not verified", retryable=True)
                    self._foreground_target = original_target
                    self.last_recovery["recovered"] = True
                    self.last_failure = None
                    return response
            mechanical = {"Manual input did not retain the approved answer",
                          "Residence catalog could not recommit the original selection",
                          "Existing residence differs from the approved state and country",
                          "Manual choices did not retain the approved answer",
                          "Approved file was not retained", "Observed manual control is unavailable",
                          "Ashby server did not acknowledge the uploaded document",
                          "Observed manual input did not receive focus",
                          "Observed manual field has changed", "Autocomplete did not retain the committed choice",
                          "Foreground recovery field, target or guard changed",
                          "Foreground recovery requires an authentication or verification handoff",
                          "Native school selection metadata did not retain",
                          "Lever location choice is absent or ambiguous", "Observed Lever location choice changed",
                          "Lever location did not retain a committed catalog choice"}
            if isinstance(exc, BrowserOperationError) and str(exc) in mechanical:
                self.last_failure = {"operation": operation, "kind": "browser_mechanics"}
                raise BrowserOperationError(str(exc), retryable=True) from None
            raise exc

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
        if self._scope["board"] == "ashby" and self._location_query and not snapshot.get("handoff"):
            from .known_answers import plain_contact_location
            for field in snapshot.get("fields", []):
                if plain_contact_location(field):
                    catalog = await self.invoke("describe", field=field, query=self._location_query)
                    if not catalog.get("choices") or catalog.get("truncated"):
                        from .cli_browser import BrowserOperationError
                        raise BrowserOperationError("Autocomplete location catalog is unavailable", retryable=True)
                    field["options"] = [{"label": v, "value": v} for v in catalog["choices"]]
        if self._scope["board"] == "ashby" and self._school_query and not snapshot.get("handoff"):
            from .ashby_education import school_control
            for field in snapshot.get("fields", []):
                if school_control(field):
                    catalog = await self.invoke("describe", field=field, query=self._school_query)
                    if not catalog.get("choices") or catalog.get("truncated"):
                        from .cli_browser import BrowserOperationError
                        raise BrowserOperationError("Autocomplete school catalog is unavailable", retryable=True)
                    details = catalog.get("choice_details", [])
                    field["options"] = []
                    for label in catalog["choices"]:
                        matches = [d for d in details if isinstance(d, dict) and d.get("label") == label
                                   and isinstance(d.get("school_metadata"), dict)]
                        option = {"label": label, "value": label}
                        if len(matches) == 1:
                            option["school_metadata"] = matches[0]["school_metadata"]
                        field["options"].append(option)
        return snapshot

    async def ensure_profile(self, answers):
        from .booklet import normalize
        self._residence_query = None
        from .known_answers import contact_location_basis
        basis = contact_location_basis(answers)
        self._location_query = basis[0] if self._scope["board"] == "ashby" and basis else None
        from .ashby_education import school_basis
        current = school_basis(answers)
        self._school_query = current["value"] if self._scope["board"] == "ashby" and current else None
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
