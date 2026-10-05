"""Headless dashboard fixtures only, never live job-site browser validation."""
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import base64
import os
from pathlib import Path
import threading

import pytest
pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "frontend" / "out"
pytestmark = pytest.mark.skipif(not (BUILD / "index.html").is_file(), reason="Build dashboard before UI fixture tests")
KEY = "1"*64
QID = "q_"+"a"*24


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@contextmanager
def workspace(fragment="", state=None, configure=None):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".local-browsers"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(BUILD)))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    actions, errors = [], []
    question = {"id": QID, "question": "May we contact your current employer?", "kind": "field", "updated_at": "fixture-revision",
        "required": True, "country_context": None, "contexts": [{"job_hash": KEY, "company": "Synthetic Employer", "title": "Software Engineer",
        "required": True, "type": "checkbox", "choices": [], "reason": "An explicit choice is required"}]}
    app = {"id": KEY, "company": "Synthetic Employer", "title": "Software Engineer", "location": "San Diego, CA", "url": None,
        "board": "greenhouse", "state": "waiting_review", "date": "2026-10-04", "updated_at": 1791144000, "attempts": 1,
        "filled_count": 1, "missing_count": 0, "has_screenshot": False, "has_incident": False, "inventory_ready": True, "screenshot_at": None,
        "confirmed_at": None, "confirmed_date": None, "sheet_synced": False}
    overview = {"generated_at": "2026-10-04T21:00:00Z", "selected_date": "2026-10-04", "timezone": "America/Los_Angeles",
        "storage_available": True, "booklet_available": True, "automation_paused": True,
        "summary": {"confirmed_today": 0, "confirmed_total": 0, "prepared_today": 1, "ready": 1, "running": 0, "queued": 0,
        "questions": 1, "uncertain": 0, "sheet_synced": 0, "legacy_review": 0}, "daily": [{"date": "2026-10-04", "confirmed": 0, "prepared": 1}],
        "states": {"waiting_review": 1}, "source_states": {}, "applications": [app], "applications_truncated": False,
        "questions": [question], "activity": [], "pipeline": None, "submission_pipeline": None}
    detail = {"job_hash": KEY, "state": "waiting_review", "inventory_complete": True, "resume_role": "sde", "automation_paused": True,
        "documents": [{"kind": "resume", "filename": "synthetic-sde.pdf"}], "reviewer_issues": [], "incident": None,
        "role_fit_notes": ["This posting prefers another year of experience."],
        "fields": [{"ref": "name", "question": "Full Name", "type": "text", "required": True, "status": "answered", "category": "profile_fact",
                    "answer": "Synthetic Candidate", "candidate_wording_required": False, "proposed": True},
                   {"ref": "why", "question": "Why this company? Please, no AI text.", "type": "textarea", "required": False,
                    "status": "blank", "category": "substantive_written", "answer": None, "candidate_wording_required": True}],
        "approval": {"can_approve": True, "revision": "exact-draft-revision", "blank_questions": [{"ref": "why", "question": "Why this company? Please, no AI text.", "required": False, "type": "textarea"}]}}
    if state is not None:
        state.update(overview=overview, detail=detail, question=question, screenshot_requests=[])
    if configure is not None:
        configure(question, detail)
    def respond(route):
        path = route.request.url.removeprefix(base).split("?")[0]
        if route.request.method == "POST":
            actions.append((path, route.request.post_data_json))
            assert route.request.headers["x-jhb-csrf"] == "synthetic-csrf"
            if path.endswith("/answer"):
                overview["questions"] = []; overview["summary"]["questions"] = 0
            elif path.endswith("/approve"):
                detail["approval"]["can_approve"] = False
                detail["approval"]["approval"] = {"state": "approved"}
            if path.endswith("/focus"):
                if state is not None and state.get("focus_unavailable"):
                    route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": "Saved draft tab is unavailable. No new form was opened."}))
                    return
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"state": "focused", "guarded": True}))
                return
            value = {"status": "answered", "state": "approved", "job_hash": KEY,
                "affected_jobs": [KEY], "resumed_jobs": [KEY], "automation_paused": True,
                "applications": [{"job_hash": KEY, "state": "queued", "remaining_required_questions": 0}]}
        elif path == f"/api/v1/applications/{KEY}/screenshot":
            if state is not None:
                state["screenshot_requests"].append(route.request.url)
            route.fulfill(status=200, content_type="image/png", body=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jOYQAAAAASUVORK5CYII="))
            return
        elif path == "/api/v1/session": value = {"csrf_token": "synthetic-csrf"}
        elif path == "/api/v1/overview": value = overview
        elif path == f"/api/v1/applications/{KEY}": value = detail
        else: raise AssertionError("Unexpected dashboard API call")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(value))
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/v1/**", respond)
            page.goto(base+fragment)
            page.get_by_text("Automation paused", exact=True).wait_for()
            yield page, actions, errors
            browser.close()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_dashboard_question_requires_explicit_value_and_keeps_false_boolean():
    with workspace() as (page, actions, errors):
        button = page.get_by_role("button", name="Save answer", exact=True)
        assert button.is_disabled() and actions == []
        page.get_by_label("Answer: May we contact your current employer?").select_option("false")
        button.click()
        page.get_by_text("You’re all caught up", exact=True).wait_for()
        assert actions[0] == (f"/api/v1/questions/{QID}/answer", {"value": False, "decline": False, "revision": "fixture-revision"})
        assert errors == []
        target = ROOT / "artifacts" / "dashboard"; target.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(target / "synthetic-overview.png"), full_page=True)


def test_review_shows_all_fields_and_never_prechecks_optional_blank_or_autoapproves():
    with workspace() as (page, actions, errors):
        page.get_by_role("button", name="Review Synthetic Employer application").click()
        page.get_by_text("Synthetic Candidate", exact=True).wait_for()
        assert page.get_by_text("Left blank", exact=True).is_visible()
        assert page.get_by_text("Proposed wording · Check this grounded draft before approving.", exact=True).is_visible()
        assert page.get_by_text("Role-fit considerations", exact=True).is_visible()
        assert page.get_by_text("This posting prefers another year of experience.", exact=True).is_visible()
        assert page.get_by_text("The employer requests your own wording. The agent must not write this answer.", exact=True).is_visible()
        button = page.get_by_role("button", name="Approve and submit this application", exact=True)
        ack = page.get_by_label("Leave blank: Why this company? Please, no AI text.")
        assert not ack.is_checked() and button.is_disabled() and actions == []
        ack.check(); assert button.is_enabled()
        button.click()
        page.get_by_text("Your approval status: approved. A pending approval is specific to this saved draft.", exact=True).wait_for()
        assert actions == [(f"/api/v1/applications/{KEY}/approve", {"revision": "exact-draft-revision", "acknowledged_blank_refs": ["why"]})]
        assert button.is_disabled() and errors == []


def test_review_deep_link_opens_only_requested_draft_without_approval():
    with workspace(f"#review/{KEY}") as (page, actions, errors):
        page.get_by_text("Synthetic Candidate", exact=True).wait_for()
        assert page.get_by_role("dialog").is_visible() and actions == []
        assert page.get_by_role("button", name="Approve and submit this application").is_disabled()
        page.get_by_role("button", name="Close review").click()
        assert page.get_by_role("dialog").count() == 0 and "#review/" not in page.url
        page.get_by_role("button", name="Review Synthetic Employer application").click()
        assert page.url.endswith(f"#review/{KEY}") and actions == []
        page.keyboard.press("Escape")
        assert page.get_by_role("dialog").count() == 0 and "#review/" not in page.url
        assert errors == []


def test_saved_answer_keeps_paused_queue_feedback_after_question_disappears():
    with workspace() as (page, actions, errors):
        page.get_by_label("Answer: May we contact your current employer?").select_option("false")
        page.get_by_role("button", name="Save answer", exact=True).click()
        page.get_by_text("Answer saved", exact=True).wait_for()
        page.get_by_text("Filling is paused. Queued answers will be used when automation resumes.", exact=False).wait_for()
        assert page.get_by_text("You’re all caught up", exact=True).is_visible()
        assert len(actions) == 1 and errors == []


def test_open_review_refreshes_and_clears_blank_ack_when_draft_changes():
    state = {}
    with workspace(f"#review/{KEY}", state=state) as (page, actions, errors):
        ack = page.get_by_label("Leave blank: Why this company? Please, no AI text.")
        ack.wait_for(); ack.check()
        button = page.get_by_role("button", name="Approve and submit this application", exact=True)
        assert button.is_enabled()
        state["detail"]["approval"]["revision"] = "new-draft-revision"
        state["detail"]["fields"][0]["answer"] = "Updated Synthetic Candidate"
        page.get_by_text("Updated Synthetic Candidate", exact=True).wait_for(timeout=10000)
        assert not ack.is_checked() and button.is_disabled()
        assert actions == [] and errors == []


def test_question_changes_preserve_text_but_require_explicit_version_refresh():
    state = {}
    with workspace(state=state) as (page, actions, errors):
        answer = page.get_by_label("Answer: May we contact your current employer?")
        answer.select_option("false")
        state["question"]["updated_at"] = "changed-question-revision"
        page.get_by_text("This question changed while you were answering.", exact=False).wait_for(timeout=10000)
        button = page.get_by_role("button", name="Save answer", exact=True)
        assert answer.input_value() == "false" and button.is_disabled() and actions == []
        page.get_by_role("button", name="Use updated question", exact=True).click()
        button.click()
        page.get_by_text("Answer saved", exact=True).wait_for()
        assert actions[0][1]["revision"] == "changed-question-revision" and errors == []


def test_exact_review_question_can_be_answered_inline_without_approving():
    state = {}
    with workspace(f"#review/{KEY}", state=state) as (page, actions, errors):
        page.get_by_text("Synthetic Candidate", exact=True).wait_for()
        state["detail"]["state"] = "waiting_input"
        state["detail"]["questions"] = [state["question"]]
        dialog = page.get_by_role("dialog")
        dialog.get_by_text("Your input for this application", exact=True).wait_for(timeout=10000)
        dialog.get_by_label("Answer: May we contact your current employer?").select_option("true")
        dialog.get_by_role("button", name="Save answer", exact=True).click()
        dialog.get_by_text("Answer saved", exact=True).wait_for()
        assert len(actions) == 1 and actions[0][0].endswith("/answer") and errors == []


def test_required_pending_question_disables_approval_despite_stale_approve_hint():
    state = {}
    with workspace(f"#review/{KEY}", state=state) as (page, actions, errors):
        ack = page.get_by_label("Leave blank: Why this company? Please, no AI text.")
        ack.wait_for(); ack.check()
        state["detail"]["questions"] = [state["question"]]
        page.get_by_role("dialog").get_by_text("Your input for this application", exact=True).wait_for(timeout=10000)
        assert page.get_by_role("button", name="Approve and submit this application", exact=True).is_disabled()
        assert actions == [] and errors == []


def test_text_to_checkbox_question_revision_cannot_coerce_old_text_to_false():
    state = {}
    def initial_text(question, detail):
        question["contexts"][0]["type"] = "text"
    with workspace(state=state, configure=initial_text) as (page, actions, errors):
        page.get_by_label("Answer: May we contact your current employer?").fill("I need to check first")
        state["question"]["contexts"][0]["type"] = "checkbox"
        state["question"]["updated_at"] = "changed-widget-revision"
        page.get_by_role("button", name="Use updated question", exact=True).wait_for(timeout=10000)
        page.get_by_role("button", name="Use updated question", exact=True).click()
        button = page.get_by_role("button", name="Save answer", exact=True)
        assert button.is_disabled() and actions == []
        page.get_by_label("Answer: May we contact your current employer?").select_option("false")
        button.click()
        page.get_by_text("Answer saved", exact=True).wait_for()
        assert actions[0][1]["value"] is False and errors == []


def test_open_review_fetches_updated_capture_by_digest_and_uses_detail_timestamp():
    state = {}
    def initial_capture(question, detail):
        detail["screenshot"] = {"available": True, "revision": "a"*64, "captured_at": "2026-10-04T21:05:00Z"}
    with workspace(f"#review/{KEY}", state=state, configure=initial_capture) as (page, actions, errors):
        image = page.get_by_alt_text("Saved review screenshot for Synthetic Employer")
        image.wait_for()
        assert image.get_attribute("src").endswith("revision="+"a"*64)
        assert "2:05" in page.locator(".screenshot-caption").inner_text()
        state["detail"]["screenshot"] = {"available": True, "revision": "b"*64, "captured_at": "2026-10-04T21:10:00Z"}
        state["detail"]["approval"]["revision"] = "new-image-bound-draft"
        page.wait_for_function("document.querySelector('.review-image')?.src.endsWith('revision=' + 'b'.repeat(64))", timeout=10000)
        assert "2:10" in page.locator(".screenshot-caption").inner_text()
        page.wait_for_function("document.querySelector('.review-image')?.complete")
        assert any(url.endswith("revision="+"a"*64) for url in state["screenshot_requests"])
        assert any(url.endswith("revision="+"b"*64) for url in state["screenshot_requests"])
        assert actions == [] and errors == []



def test_owned_help_text_is_plain_and_changed_note_invalidates_unsaved_answer():
    state = {}
    def initial_help(question, detail):
        question["contexts"][0]["description"] = "California applicants must select N/A."
        detail["fields"][1]["description"] = "<script>window.__helpExecuted = true</script> Please use your own words."
    with workspace(state=state, configure=initial_help) as (page, actions, errors):
        page.get_by_text("California applicants must select N/A.", exact=True).wait_for()
        page.get_by_label("Answer: May we contact your current employer?").select_option("false")
        state["question"]["contexts"][0]["description"] = "Only current California residents should select N/A."
        state["question"]["updated_at"] = "changed-help-text-revision"
        page.get_by_text("Only current California residents should select N/A.", exact=True).wait_for(timeout=10000)
        assert page.get_by_role("button", name="Save answer", exact=True).is_disabled()
        assert page.get_by_label("Answer: May we contact your current employer?").input_value() == "false"
        page.get_by_role("button", name="Review Synthetic Employer application").click()
        page.get_by_text("<script>window.__helpExecuted = true</script> Please use your own words.", exact=True).wait_for()
        assert page.evaluate("window.__helpExecuted === undefined") is True
        assert actions == [] and errors == []


def test_saved_draft_focus_is_explicit_csrf_post_without_approval_or_new_tab():
    def focus_enabled(question, detail):
        detail['draft_focus_available'] = True
        detail['packet_revision'] = 'a'*64
    with workspace(f'#review/{KEY}', configure=focus_enabled) as (page, actions, errors):
        page.get_by_role('button', name='Open saved draft').click()
        page.get_by_text('Your existing draft is focused in Chrome. Its submission guard remains enabled.').wait_for()
        assert actions == [(f'/api/v1/applications/{KEY}/focus', {'revision': 'a'*64})]
        assert len(page.context.pages) == 1 and not errors
        assert page.get_by_role('link', name='Open live form').count() == 0


def test_unavailable_saved_draft_reports_handoff_without_fresh_form_fallback():
    state = {'focus_unavailable': True}
    def focus_enabled(question, detail):
        detail['draft_focus_available'] = True
        detail['packet_revision'] = 'a'*64
    with workspace(f'#review/{KEY}', state=state, configure=focus_enabled) as (page, actions, errors):
        page.get_by_role('button', name='Open saved draft').click()
        page.get_by_role('alert').get_by_text('Saved draft tab is unavailable. No new form was opened.').wait_for()
        page.wait_for_timeout(5200)  # Live inventory refresh must not erase action feedback.
        assert page.get_by_role('alert').get_by_text('Saved draft tab is unavailable. No new form was opened.').is_visible()
        assert actions == [(f'/api/v1/applications/{KEY}/focus', {'revision': 'a'*64})]
        assert len(page.context.pages) == 1 and not errors


def test_public_descriptor_refresh_shows_context_and_requires_answer_version_refresh():
    state={}
    with workspace(state=state) as (page,actions,errors):
        page.get_by_label('Answer: May we contact your current employer?').select_option('false')
        state['question']['contexts'][0]['public_metadata_description']='Public guidance: do not select Yes without this condition.'
        state['question']['updated_at']='public-metadata-revision'
        page.get_by_text('Public application guidance',exact=True).wait_for()
        assert page.get_by_text('Public guidance: do not select Yes without this condition.').is_visible()
        assert page.get_by_text('This public descriptor has not established a live field observation or approval.').is_visible()
        assert page.get_by_role('button',name='Save answer',exact=True).is_disabled()
        assert not actions and not errors
