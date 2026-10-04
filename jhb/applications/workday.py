"""Exact-job Workday wizard access through the registered Browser Use CLI."""
from urllib.parse import urlsplit, urlunsplit

from .boards import job_identity
from .cli_browser import BrowserUseCLI


BROADDRIDGE_ORIGIN = "https://broadridge.wd5.myworkdayjobs.com"


def approved_credential_store(url, answers):
    """Wire the reviewed existing-account exception, without reading a secret.

    This rollout only enables the explicitly authorized Broadridge origin.
    Other tenants retain the Google/default authentication handoff.
    """
    identity = job_identity(url)
    record = answers.get("auth.password_exception", {})
    value = record.get("value")
    origin = "https://" + (urlsplit(url).hostname or "")
    if (not identity or identity[0] != "workday" or origin != BROADDRIDGE_ORIGIN
            or record.get("status") != "verified" or not record.get("source")
            or not isinstance(value, dict) or value.get("origin") != origin
            or value.get("method") != "password" or value.get("reuse_existing") is not True):
        return None
    from .credentials import CredentialStore
    return CredentialStore()


class WorkdayCLI(BrowserUseCLI):
    _dispatch_module = "jhb.applications.workday_runtime"

    def __init__(self, approved_url, *, experience_count=0, **kwargs):
        super().__init__(**kwargs)
        identity = job_identity(approved_url)
        if not identity or identity[0] != "workday":
            raise ValueError("Workday scope requires an exact official job URL")
        self._identity = identity
        parsed = urlsplit(approved_url)
        path = parsed.path.rstrip("/")
        self.application_url = urlunsplit(parsed._replace(path=path if path.endswith("/apply") else path+"/apply"))
        self._education_count = 0
        self._experience_count = experience_count
        if not isinstance(experience_count, int) or isinstance(experience_count, bool) or not 0 <= experience_count <= 10:
            raise ValueError("Workday experience count must be between zero and ten")

    def call(self, operation, *, _cancelled=None, **payload):
        payload["approved_url"] = self.application_url
        return super().call(operation, _cancelled=_cancelled, **payload)

    def allowed_url(self, url):
        return job_identity(url) == self._identity

    async def open(self, url):
        if not self.allowed_url(url):
            raise ValueError("Requested Workday job differs from approved scope")
        response = await self.invoke("open", url=self.application_url)
        if not self.allowed_url(response.get("url")):
            self.redirected_to = response.get("url")
            raise ValueError("Workday navigation changed the approved job")
        self.target_id, self.expected_url = response["target_id"], response["url"]
        await self.invoke("begin")
        return response

    async def ensure_education(self, count):
        if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 5:
            raise ValueError("Workday education count must be between zero and five")
        self._education_count = count
        return {"supported": True, "deferred_until_experience_step": True}

    async def observe(self):
        snapshot = await self.invoke("observe")
        if snapshot.get("experience_step") and not snapshot.get("handoff"):
            if self._education_count:
                await self.invoke("records", kind="education", count=self._education_count)
            if self._experience_count:
                await self.invoke("records", kind="workExperience", count=self._experience_count)
            snapshot = await self.invoke("observe")
        return snapshot

    async def authenticate(self, answers, vault):
        # Reuse an existing account only with a verified exact-site exception.
        # Google preference remains the default; no registration fallback exists.
        from .booklet import normalize
        exception = answers.get("auth.password_exception", {})
        policy = exception.get("value")
        origin = "https://" + urlsplit(self.application_url).hostname
        email = answers.get("identity.email", {})
        if (exception.get("status") != "verified" or not exception.get("source") or not isinstance(policy, dict)
                or policy.get("origin") != origin or policy.get("method") != "password"
                or policy.get("reuse_existing") is not True or email.get("status") != "verified" or not vault):
            return False
        username = email.get("value")
        if not isinstance(username, str) or not normalize(username):
            return False
        password = vault.get(origin, username)
        if not isinstance(password, str) or not password:
            return False
        result = await self.invoke("authenticate", username=username, password=password, approved_exception=exception)
        return result.get("authenticated") is True
