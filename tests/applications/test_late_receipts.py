"""A delayed positive receipt cannot grant or replay any terminal action."""
import asyncio
import base64
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import time

import pytest

from jhb import config, store
from jhb.applications import approvals, boards, booklet, late_receipts as late, overnight, queue, service
from jhb.applications.application_review import snapshot_digest
from jhb.applications.authorized_submission import audit_hash

URL = "https://job-boards.greenhouse.io/example/jobs/1234"
PNG = b"\x89PNG\r\n\x1a\nsynthetic screenshot"


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("JHB_TRACKER_CONFIG", raising=False)
    now = time.time()
    db = store.connect(tmp_path / "jobs.sqlite3")
    queue.initialize(db); approvals.initialize(db); overnight.initialize(db)
    job = {"url": URL, "dedupe_hash": boards.application_hash(URL), "company": "Example", "title": "Engineer"}
    directory = tmp_path / "private" / "authorized-submissions" / job["dedupe_hash"]
    authpath = tmp_path / "private" / "expired-approval.json"
    booklet.write_private(authpath, {"enabled": False, "expired": True})
    authhash = hashlib.sha256(authpath.read_bytes()).hexdigest()
    attempt = {"job_hash": job["dedupe_hash"], "application_url": URL, "authorization_id": authhash,
        "runtime_click_started": True, "click_started_at": datetime.fromtimestamp(now-197, timezone.utc).isoformat(),
        "started_at": datetime.fromtimestamp(now-250, timezone.utc).isoformat(), "state": "uncertain",
        "require_independent_review": True, "packet_sha256": "a"*64}
    booklet.write_private(directory / "attempt.json", attempt)
    snapshot = {"fields": [{"ref": "first_name"}], "retained": [{"ref": "first_name", "state": {"value": "Synthetic"}}]}
    docs = {"documents.resume": {"sha256": "b"*64}}
    checks = {"check_count": 2, "checks": [snapshot, deepcopy(snapshot)], "documents": docs,
              "authorization_id": authhash, "job_hash": job["dedupe_hash"], "target_id": "exact-original-target"}
    booklet.write_private(directory / "checks.json", checks)
    review = {"verdict": "approved", "source": "independent_application_review", "reviewer": "codex-readonly",
              "issues": [], "snapshot_sha256": snapshot_digest(snapshot), "job_hash": job["dedupe_hash"], "authorization_id": authhash}
    token = {"verdict": "approved", "source": "independent_application_review", "job_hash": job["dedupe_hash"],
        "authorization_id": authhash, "packet_sha256": "a"*64, "audit_sha256": audit_hash(snapshot, docs), "review": review}
    booklet.write_private(directory / "independent-review.json", token)
    db.execute("INSERT INTO authorized_submission_attempts(job_hash,authorization_id,application_url,state,started_at,updated_at,attempt_path,packet_sha256) VALUES(?,?,?,'uncertain',?,?,?,?)",
               (job["dedupe_hash"], authhash, URL, int(now-250), int(now-197), str(directory / "attempt.json"), "a"*64))
    db.execute("INSERT INTO applications(job_hash,job_json,state,updated_at) VALUES(?,?,'submission_uncertain',?)",
               (job["dedupe_hash"], json.dumps(job), int(now)))
    db.execute("INSERT INTO application_approvals VALUES(?, ?, 'uncertain', ?, ?, 'old-binding', ?, NULL)",
               ("c"*32, job["dedupe_hash"], int(now-300), int(now-10), str(authpath)))
    db.commit()
    yield {"db": db, "directory": directory, "attempt": attempt, "job": job, "now": now, "checks": checks}
    db.close()


def observed(case, **changes):
    return {"state": "observed", "target_id": "exact-original-target",
        "observed_at": datetime.fromtimestamp(case["now"], timezone.utc).isoformat(),
        "screenshot_base64": base64.b64encode(PNG).decode(), "observation": {
            "url": URL+"/confirmation", "title": "Thank you for applying",
            "body": "Thank you for applying. Your application has been received.",
            "active_controls": 0, "application_forms": 0, "terminal_controls": 0, "verification_challenges": 0}, **changes}


def run(case, result=None, **kwargs):
    calls = []
    async def observer(**payload):
        calls.append(payload)
        return result if result is not None else observed(case)
    outcome = asyncio.run(late.reconcile(case["db"], observer=observer, now=case["now"], **kwargs))
    return outcome, calls


def test_delayed_receipt_records_and_tracks_once_after_authority_expiry(case, monkeypatch):
    monkeypatch.setenv("JHB_PORTAL_SUBMISSIONS_ENABLED", "0")
    monkeypatch.setattr(overnight, "load_authorization", lambda *a, **kw: pytest.fail("Record-only path requested submit authority"))
    outcome, calls = run(case)
    assert outcome == {"observed": 1, "receipts": 1, "reconciled": 1}
    assert calls[0]["target_id"] == "exact-original-target"
    db = case["db"]
    assert db.execute("SELECT state FROM applications").fetchone()[0] == "submitted"
    assert db.execute("SELECT state FROM authorized_submission_attempts").fetchone()[0] == "submitted"
    assert db.execute("SELECT state FROM application_approvals").fetchone()[0] == "submitted"
    assert db.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 1
    receipt = json.loads((case["directory"] / "receipt.json").read_text())
    assert receipt["confirmed_at"] == observed(case)["observed_at"]
    assert receipt["confirmed_at"] != case["attempt"]["click_started_at"]
    assert receipt["document_sha256"] == {"documents.resume": "b"*64}
    assert receipt["screenshot_sha256"] == hashlib.sha256(PNG).hexdigest()
    assert Path(receipt["screenshot_path"]).stat().st_mode & 0o777 == 0o600
    assert json.loads((case["directory"] / "attempt.json").read_text())["runtime_click_started"] is True
    assert run(case) == ({"observed": 0, "receipts": 0, "reconciled": 0}, [])


@pytest.mark.parametrize("change", ["no_click", "marker_absent", "different_job", "different_authority", "changed_audit",
    "changed_review", "missing_checks", "expired_window", "future_click", "missing_target", "bad_hash"])
def test_bad_original_evidence_never_reads_browser(case, change):
    directory, attempt, checks = case["directory"], case["attempt"], case["checks"]
    if change == "no_click": attempt["runtime_click_started"] = False
    elif change == "marker_absent": attempt.pop("runtime_click_started")
    elif change == "different_job": attempt["application_url"] = URL.replace("1234", "4321")
    elif change == "different_authority": attempt["authorization_id"] = "d"*64
    elif change == "changed_audit": checks["checks"][1]["retained"][0]["state"]["value"] = "Changed"
    elif change == "changed_review": booklet.write_private(directory / "independent-review.json", {"verdict": "rejected"})
    elif change == "missing_checks": (directory / "checks.json").unlink()
    elif change == "expired_window": attempt["click_started_at"] = datetime.fromtimestamp(case["now"]-901, timezone.utc).isoformat()
    elif change == "future_click": attempt["click_started_at"] = datetime.fromtimestamp(case["now"]+1, timezone.utc).isoformat()
    elif change == "missing_target": checks.pop("target_id")
    elif change == "bad_hash": checks["documents"]["documents.resume"]["sha256"] = "unknown"
    booklet.write_private(directory / "attempt.json", attempt)
    if change != "missing_checks": booklet.write_private(directory / "checks.json", checks)
    assert run(case) == ({"observed": 0, "receipts": 0, "reconciled": 0}, [])
    assert not (directory / "receipt.json").exists()


@pytest.mark.parametrize("change", ["different_target", "wrong_job", "wrong_origin", "http", "userinfo", "thanks_only",
    "active_form", "active_input", "terminal", "no_confirmation_path", "missing_screenshot", "bad_timestamp", "closed", "pending", "rejected", "email_verification", "challenge"])
def test_nonpositive_or_unbound_observation_preserves_uncertainty(case, change):
    result = observed(case); page = result["observation"]
    if change == "different_target": result["target_id"] = "another-target"
    elif change == "wrong_job": page["url"] = page["url"].replace("1234", "4321")
    elif change == "wrong_origin": page["url"] = page["url"].replace("job-boards.greenhouse.io", "boards.greenhouse.io")
    elif change == "http": page["url"] = page["url"].replace("https", "http")
    elif change == "userinfo": page["url"] = page["url"].replace("https://", "https://user@")
    elif change == "thanks_only": page["body"] = "Thank you for applying. Please finish verification."
    elif change == "active_form": page["application_forms"] = 1
    elif change == "active_input": page["active_controls"] = 1
    elif change == "terminal": page["terminal_controls"] = 1
    elif change == "no_confirmation_path": page["url"] = URL
    elif change == "missing_screenshot": result.pop("screenshot_base64")
    elif change == "bad_timestamp": result["observed_at"] = case["attempt"]["click_started_at"]
    elif change == "closed": result = {"state": "target_absent"}
    elif change == "pending": result = {"state": "pending"}
    elif change == "rejected": page["body"] += " We couldn't submit your application: flagged as possible spam."
    elif change == "email_verification": page["body"] += " Please verify your email address."
    elif change == "challenge": page["verification_challenges"] = 1
    outcome, calls = run(case, result)
    assert outcome == {"observed": 1, "receipts": 0, "reconciled": 0}
    assert len(calls) == 1
    assert case["db"].execute("SELECT state FROM applications").fetchone()[0] == "submission_uncertain"
    assert not (case["directory"] / "receipt.json").exists()


def test_cooldown_is_durable_and_does_not_extend_observation_window(case):
    assert run(case, {"state": "pending"})[0]["observed"] == 1
    assert run(case, {"state": "pending"})[1] == []
    case["now"] += 30
    assert run(case, {"state": "pending"})[0]["observed"] == 1
    case["now"] += 901
    assert run(case)[1] == []


def test_transport_failure_also_consumes_cooldown(case):
    async def broken(**kw): raise RuntimeError("Synthetic transport failure")
    result = asyncio.run(late.reconcile(case["db"], observer=broken, now=case["now"]))
    assert result["observed"] == 1
    assert run(case)[1] == []


def runtime(case, page=None, tabs=None, change_after_capture=False):
    evidence = late._evidence(case["directory"] / "attempt.json")
    request = {key: evidence[key] for key in ("attempt_path", "attempt_sha256", "checks_sha256", "target_id")}
    request["operation"] = "observe_receipt"
    page = page or observed(case)["observation"]
    tab = {"targetId": "exact-original-target", "url": page["url"]}
    calls = []
    def cdp(method, **params):
        assert method == "Page.captureScreenshot"  # No Input, navigation, DOM mutation or guard release.
        calls.append(method)
        if change_after_capture: tab["targetId"] = "another-target"
        return {"data": base64.b64encode(PNG).decode()}
    def js(script):
        assert script == late.PAGE_STATE
        calls.append("read")
        return deepcopy(page)
    helpers = {"list_tabs": lambda: [tab] if tabs is None else tabs,
        "switch_tab": lambda target: calls.append(("attach", target)), "current_tab": lambda: tab, "js": js, "cdp": cdp}
    return request, helpers, calls


def test_fixed_runtime_reads_exact_existing_tab_twice_around_one_capture(case, monkeypatch):
    from jhb.applications import submission_runtime
    monkeypatch.setattr(submission_runtime, "load_gate", lambda *a: pytest.fail("Record-only path loaded authority"))
    request, helpers, calls = runtime(case)
    result = submission_runtime.dispatch(request, helpers)
    assert result["state"] == "observed"
    assert calls == [("attach", "exact-original-target"), "read", "Page.captureScreenshot", "read"]


def test_closed_original_tab_never_reopens_or_uses_another_matching_tab(case):
    request, helpers, calls = runtime(case, tabs=[{"targetId": "new-tab", "url": URL+"/confirmation"}])
    assert late.observe(request, helpers) == {"state": "target_absent"}
    assert calls == []


def test_changed_target_after_capture_cannot_confirm(case):
    request, helpers, _ = runtime(case, change_after_capture=True)
    with pytest.raises(ValueError, match="target changed"):
        late.observe(request, helpers)


def test_active_terminal_attempt_lock_prevents_late_read(case):
    request, helpers, calls = runtime(case)
    with (case["directory"] / "attempt.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        assert late.observe(request, helpers) == {"state": "attempt_active"}
    assert calls == []


def test_pause_service_records_receipts_but_does_not_enter_submit_lane(case, monkeypatch):
    booklet.write_private(config.ROOT / "private" / "pipeline-pause.json", {"paused": True})
    calls = []
    async def reconcile(conn): calls.append("read-only reconciliation"); return {"observed": 1, "receipts": 0, "reconciled": 0}
    monkeypatch.setattr(late, "reconcile", reconcile)
    database = config.ROOT / "jobs.sqlite3"
    result = service.once("approved", connector=lambda: store.connect(database),
                          approved=lambda *a, **kw: pytest.fail("Paused service dispatched submission"))
    assert calls == ["read-only reconciliation"]
    assert result == {"state": "paused", "reason_code": "automation_paused"}


def test_ashby_exact_job_confirmation_does_not_accept_other_role_or_merely_thanks():
    url = "https://jobs.ashbyhq.com/example/00000000-1111-2222-3333-444444444444/application"
    page = {"url": url, "body": "We received your application.", "active_controls": 0, "application_forms": 0, "terminal_controls": 0, "verification_challenges": 0}
    assert late.positive(url, page)
    assert not late.positive(url.replace("444444444444", "555555555555"), page)
    assert not late.positive(url, {**page, "body": "Thank you for applying"})


def test_cycle_reads_at_most_two_original_attempts(case):
    db = case["db"]
    for index in (2, 3):
        url = URL.replace("1234", str(index))
        job_hash = boards.application_hash(url)
        directory = case["directory"].with_name(job_hash)
        for name in ("attempt.json", "checks.json", "independent-review.json"):
            raw = (case["directory"] / name).read_text().replace(URL, url).replace(case["job"]["dedupe_hash"], job_hash)
            booklet.write_private(directory / name, json.loads(raw))
        db.execute("INSERT INTO authorized_submission_attempts SELECT ?,authorization_id,?,state,started_at,updated_at,?,NULL,NULL,attempt_count,available_at,packet_sha256 FROM authorized_submission_attempts WHERE job_hash=?",
                   (job_hash, url, str(directory / "attempt.json"), case["job"]["dedupe_hash"]))
    db.commit()
    outcome, calls = run(case, {"state": "pending"})
    assert outcome["observed"] == len(calls) == 2
    assert case["db"].execute("SELECT SUM(observations) FROM late_receipt_observations").fetchone()[0] == 2


def test_ci_never_creates_observation_state_or_uses_live_browser(case, monkeypatch):
    monkeypatch.setenv("CI", "true")
    assert run(case) == ({"observed": 0, "receipts": 0, "reconciled": 0}, [])
    assert not case["db"].execute("SELECT 1 FROM sqlite_master WHERE name='late_receipt_observations'").fetchone()


@pytest.mark.parametrize("marker", ["pipeline-pause.json", "overnight-monitor/repair-pending.json"])
def test_pause_or_quarantine_stops_new_reads_but_records_existing_positive_evidence(case, marker):
    def interrupted_recorder(*args): raise RuntimeError("Synthetic recording interruption")
    assert run(case, recorder=interrupted_recorder)[0] == {"observed": 1, "receipts": 1, "reconciled": 0}
    booklet.write_private(config.ROOT / "private" / marker, {"paused": True})
    assert run(case) == ({"observed": 0, "receipts": 0, "reconciled": 1}, [])


@pytest.mark.parametrize("damage", ["screenshot", "page", "target", "checks"])
def test_persisted_receipt_cannot_reconcile_after_observation_evidence_changes(case, damage):
    def interrupted_recorder(*args): raise RuntimeError("Synthetic recording interruption")
    assert run(case, recorder=interrupted_recorder)[0]["receipts"] == 1
    if damage == "screenshot":
        (case["directory"] / "late-confirmation.png").write_bytes(b"changed")
    else:
        path = case["directory"] / "late-confirmation.json"
        value = json.loads(path.read_text())
        if damage == "page": value["url"] = URL.replace("1234", "4321")+"/confirmation"
        elif damage == "target": value["target_id"] = "different-target"
        else: value["checks_sha256"] = "0"*64
        booklet.write_private(path, value)
    booklet.write_private(config.ROOT / "private" / "pipeline-pause.json", {"paused": True})
    assert run(case) == ({"observed": 0, "receipts": 0, "reconciled": 0}, [])
    assert case["db"].execute("SELECT state FROM applications").fetchone()[0] == "submission_uncertain"


def test_pause_before_any_receipt_keeps_uncertainty_and_makes_no_cli_call(case):
    booklet.write_private(config.ROOT / "private" / "pipeline-pause.json", {"paused": True})
    assert run(case) == ({"observed": 0, "receipts": 0, "reconciled": 0}, [])
    assert not (case["directory"] / "receipt.json").exists()


def test_runtime_rechecks_pause_inside_serialized_lane(case):
    request, helpers, calls = runtime(case)
    booklet.write_private(config.ROOT / "private" / "pipeline-pause.json", {"paused": True})
    with pytest.raises(ValueError, match="paused"):
        late.observe(request, helpers)
    assert calls == []


def test_discarded_job_never_observes_browser(case, monkeypatch):
    from jhb.applications import application_discard
    def cancelled(*args): raise ValueError("Synthetic candidate discard")
    monkeypatch.setattr(application_discard, "check", cancelled)
    assert run(case) == ({"observed": 0, "receipts": 0, "reconciled": 0}, [])


def test_receipt_cli_reuses_only_existing_local_daemon_and_never_remembers_a_new_target(case, monkeypatch):
    from jhb.applications import cli_browser, application_discard
    from jhb.applications.authorized_submission import AuthorizedSubmissionCLI
    monkeypatch.setattr(cli_browser, "ROOT", config.ROOT)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:12345")
    monkeypatch.setenv("BU_NAME", "not-allowed")
    monkeypatch.delenv("BU_CDP_WS", raising=False)
    monkeypatch.setattr(application_discard, "remember_target", lambda *a: pytest.fail("Receipt observer registered a new target"))
    calls = []
    def run_cli(self, script, env, *args):
        calls.append((script, env))
        return subprocess.CompletedProcess(["browser-use"], 0, cli_browser.MARKER+'{"state":"pending"}', "")
    monkeypatch.setattr(cli_browser.BrowserUseCLI, "_run", run_cli)
    client = AuthorizedSubmissionCLI(timeout=25)
    client.expected_url = URL
    assert client.call("observe_receipt", attempt_path=str(case["directory"] / "attempt.json"))["state"] == "pending"
    script, env = calls[0]
    assert env["BH_REQUIRE_EXISTING_DAEMON"] == "1" and "BU_NAME" not in env
    assert 'from jhb.applications.submission_runtime import dispatch' in script
    monkeypatch.setenv("BU_CDP_URL", "https://remote.example:443")
    with pytest.raises(ValueError, match="loopback"):
        client.call("observe_receipt")
    assert len(calls) == 1


@pytest.mark.parametrize("extra,expected", [("", True), ('<form><input value="Synthetic"><button>Submit</button></form>', False),
    ('<form><button type="submit" aria-label="Finish"></button></form>', False),
    ('<div role="button">Submit application</div>', False),
    ('<iframe src="https://synthetic.test/recaptcha/challenge" style="height:150px"></iframe>', False),
    ('<input type="search" placeholder="Search jobs">', True)])
def test_native_page_inventory_distinguishes_receipt_from_remaining_application_controls(extra, expected):
    # Isolated synthetic Chromium verifies DOM inspection only, never a live site.
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=
            '<html><body>Your application has been received.'+extra+'</body></html>'))
        page.goto(URL+"/confirmation")
        observation = page.evaluate(late.PAGE_STATE)
        assert late.positive(URL, observation) is expected
        browser.close()
