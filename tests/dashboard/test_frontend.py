"""Headless dashboard fixtures only, never live job-site browser validation."""
from contextlib import contextmanager
from datetime import datetime, timezone
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
def workspace(fragment="", state=None, configure=None, browser_now=None):
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
    detail = {"job_hash": KEY, "state": "waiting_review", "inventory_complete": True, "resume_role": "sde", "automation_paused": True, "submission_supported": True,
        "documents": [{"kind": "resume", "filename": "synthetic-sde.pdf"}], "reviewer_issues": [], "incident": None,
        "role_fit_notes": ["This posting prefers another year of experience."],
        "fields": [{"ref": "name", "question": "Full Name", "type": "text", "required": True, "status": "answered", "category": "profile_fact",
                    "answer": "Synthetic Candidate", "candidate_wording_required": False, "proposed": True},
                   {"ref": "why", "question": "Why this company? Please, no AI text.", "type": "textarea", "required": False,
                    "status": "blank", "category": "substantive_written", "answer": None, "candidate_wording_required": True}],
        "approval": {"can_approve": True, "revision": "exact-draft-revision", "blank_questions": [{"ref": "why", "question": "Why this company? Please, no AI text.", "required": False, "type": "textarea"}]}}
    policy = {"mode": "review", "requested_mode": "review", "revision": "synthetic-policy-revision", "enabled_until": None,
              "available_submission_boards": ["ashby", "greenhouse"], "authorized_boards": [], "gate_reasons": [], "max_duration_hours": 8}
    openings = {"items": [], "total": 0, "next_cursor": None}
    if state is not None:
        state.update(policy=policy, openings=openings)
        state.update(overview=overview, detail=detail, question=question, screenshot_requests=[])
    if configure is not None:
        configure(question, detail)
    def respond(route):
        path = route.request.url.removeprefix(base).split("?")[0]
        if route.request.method == "POST":
            actions.append((path, route.request.post_data_json))
            assert route.request.headers["x-jhb-csrf"] == "synthetic-csrf"
            if path == "/api/v1/workflow-policy":
                policy.update(mode=route.request.post_data_json["mode"], requested_mode=route.request.post_data_json["mode"],
                              revision="changed-policy-revision", enabled_until="2031-01-01T12:00:00Z")
                route.fulfill(status=200, content_type="application/json", body=json.dumps(policy))
                return
            if path.endswith("/answer"):
                overview["questions"] = []; overview["summary"]["questions"] = 0
            elif path.endswith("/approve"):
                if state and state.get("approve_error"):
                    route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail":state["approve_error"]}))
                    return
                detail["approval"]["can_approve"] = False
                detail["approval"]["approval"] = {"state": "approved"}
                if state and state.get("approve_result"):
                    response = state["approve_result"]
                    detail.update(state=response.get("application_state", "waiting_review"), display_state=response["state"])
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(response))
                    return
            if path.endswith("/discard"):
                detail.update(state="skipped", display_state="discarded")
                app.update(state="skipped", display_state="discarded", inventory_ready=False)
                overview["summary"]["ready"] = 0
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"state":"discarded", "reason":"Application discarded. Its worker is stopped. Its application tab is closed. History is preserved.", "tab_close":{"state":"closed"}, "worker_stop":{"state":"stopped"}}))
                return
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
        elif path == "/api/v1/workflow-policy": value = policy
        elif path == "/api/v1/openings": value = openings
        elif path == "/api/v1/overview": value = overview
        elif path == f"/api/v1/applications/{KEY}": value = detail
        else: raise AssertionError("Unexpected dashboard API call")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(value))
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            if browser_now is not None:
                page.clock.set_fixed_time(browser_now)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/v1/**", respond)
            page.goto(base+fragment)
            page.get_by_text("Automation paused", exact=True).wait_for()
            yield page, actions, errors
            browser.close()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_static_export_uses_browser_pacific_date_without_build_day_hydration_error():
    with workspace(browser_now=datetime(2031, 1, 2, 2, tzinfo=timezone.utc)) as (page, actions, errors):
        assert page.get_by_label('Dashboard date').input_value() == '2031-01-01'
        assert page.get_by_text('On Jan 1 · before submission', exact=True).is_visible()
        assert not actions and not errors


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
        button = page.get_by_role("button", name="Submit current browser form", exact=True)
        ack = page.get_by_label("Leave blank: Why this company? Please, no AI text.")
        assert not ack.is_checked() and button.is_enabled() and actions == []
        ack.check(); assert button.is_enabled()
        button.click()
        page.get_by_text("Submission status: Submission queued. Your current browser answers are preserved.", exact=True).wait_for()
        assert actions == [(f"/api/v1/applications/{KEY}/approve", {"revision": "exact-draft-revision", "acknowledged_blank_refs": ["why"], "acknowledge_role_fit_warning": False})]
        assert button.is_disabled() and errors == []


def test_review_deep_link_opens_only_requested_draft_without_approval():
    with workspace(f"#review/{KEY}") as (page, actions, errors):
        page.get_by_text("Synthetic Candidate", exact=True).wait_for()
        assert page.get_by_role("dialog").is_visible() and actions == []
        assert page.get_by_role("button", name="Submit current browser form").is_enabled()
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
        button = page.get_by_role("button", name="Submit current browser form", exact=True)
        assert button.is_enabled()
        state["detail"]["approval"]["revision"] = "new-draft-revision"
        state["detail"]["fields"][0]["answer"] = "Updated Synthetic Candidate"
        page.get_by_text("Updated Synthetic Candidate", exact=True).wait_for(timeout=10000)
        assert not ack.is_checked() and button.is_enabled()
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


def test_current_form_action_uses_server_capability_instead_of_stale_saved_required_blank():
    state = {}
    with workspace(f"#review/{KEY}", state=state) as (page, actions, errors):
        ack = page.get_by_label("Leave blank: Why this company? Please, no AI text.")
        ack.wait_for(); ack.check()
        state["detail"]["questions"] = [state["question"]]
        page.get_by_role("dialog").get_by_text("Your input for this application", exact=True).wait_for(timeout=10000)
        assert page.get_by_role("button", name="Submit current browser form", exact=True).is_enabled()
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
        assert page.get_by_text('Published by the employer. The agent will verify these details in your application before using your answer.').is_visible()
        assert page.get_by_role('button',name='Save answer',exact=True).is_disabled()
        assert not actions and not errors


def test_review_agent_work_is_separate_from_candidate_questions_and_blocks_approval():
    def known_tasks(question, detail):
        detail['questions'] = []
        detail['agent_tasks'] = [
            {'question': 'Gender', 'ref': 'gender', 'task_kind': 'known_answer_fill', 'required': False},
            {'question': 'Cover Letter', 'ref': 'cover_letter', 'task_kind': 'document_generation', 'required': False}]
        detail['approval']['can_approve'] = False
        detail['approval']['reason'] = 'Agent work must be verified before approval'
    with workspace(f'#review/{KEY}', configure=known_tasks) as (page, actions, errors):
        page.get_by_text('Agent work remaining', exact=True).wait_for()
        assert page.get_by_text('Gender: fill the saved booklet answer', exact=True).is_visible()
        assert page.get_by_text('Cover Letter: prepare the application document', exact=True).is_visible()
        assert page.get_by_role('button', name='Submit current browser form').is_disabled()
        assert page.get_by_label('Answer: Gender').count() == 0
        assert not actions and not errors


def test_related_submission_warning_shows_distinct_locations_without_auto_action():
    def related(question, detail):
        detail['location'] = 'New York, NY'
        detail['related_submissions'] = [{'job_hash': '2'*64, 'url': 'https://jobs.ashbyhq.com/synthetic/11111111-1111-1111-1111-111111111111',
            'company': 'Synthetic Employer', 'title': 'Software Engineer', 'location': 'San Francisco, CA', 'confirmed_date': '2026-10-03'}]
    with workspace(f'#review/{KEY}', configure=related) as (page, actions, errors):
        page.get_by_text('A similar role was already submitted', exact=True).wait_for()
        assert page.get_by_text('New York, NY', exact=True).is_visible()
        assert page.get_by_text('San Francisco, CA · Recorded submission 2026-10-03', exact=True).is_visible()
        assert page.get_by_role('link', name='View confirmed application').get_attribute('href') == '/#review/'+'2'*64
        assert page.get_by_text('These are different posting IDs.', exact=False).is_visible()
        assert not actions and not errors


@pytest.mark.parametrize('capability', [False, None])
def test_unsupported_or_missing_submission_capability_keeps_review_without_approve_action(capability):
    def unsupported(question, detail):
        if capability is None:
            detail.pop('submission_supported')
        else:
            detail['submission_supported'] = capability
        # Even a stale affirmative approval hint cannot enable an unknown board.
        detail['approval']['can_approve'] = True
        detail['approval']['reason'] = "This board's final submission adapter still needs validation"
        detail['draft_focus_available'] = True
        detail['packet_revision'] = '9'*64
        detail['screenshot'] = {'available': True, 'revision': '8'*64, 'captured_at': 1791144000}
    with workspace(f'#review/{KEY}', configure=unsupported) as (page, actions, errors):
        page.get_by_text('Synthetic Candidate', exact=True).wait_for()
        expected = ('Prepared for review. Automatic submission is not available for this board yet.' if capability is False
                    else 'Submission capability is unavailable. Refresh this review before approving.')
        assert page.get_by_text(expected, exact=True).is_visible()
        assert page.get_by_text("This board's final submission adapter still needs validation", exact=True).is_visible()
        assert page.get_by_role('button', name='Submit current browser form').count() == 0
        assert page.get_by_role('button', name='Open saved draft').is_enabled()
        assert page.get_by_alt_text('Saved review screenshot for Synthetic Employer').is_visible()
        assert not actions and not errors


def test_unsupported_submission_board_preserves_explicit_revoke_action():
    def approved_unsupported(question, detail):
        detail['submission_supported'] = False
        detail['approval']['approval'] = {'state': 'approved'}
    with workspace(f'#review/{KEY}', configure=approved_unsupported) as (page, actions, errors):
        page.get_by_text('Synthetic Candidate', exact=True).wait_for()
        assert page.get_by_role('button', name='Submit current browser form').count() == 0
        page.get_by_role('button', name='Revoke approval', exact=True).click()
        page.get_by_text('Approval revoked. This draft cannot be submitted.', exact=True).wait_for()
        assert actions == [(f'/api/v1/applications/{KEY}/revoke', {})] and not errors


def test_workflow_switch_only_mutates_after_explicit_click_and_revokes():
    with workspace() as (page, actions, errors):
        toggle = page.get_by_role("switch", name="Full autonomy")
        toggle.wait_for()
        assert not toggle.is_checked() and not actions
        toggle.click()
        page.get_by_text("Independent review, then submission", exact=True).wait_for()
        assert toggle.is_checked()
        assert actions == [("/api/v1/workflow-policy", {"mode": "autonomous", "revision": "synthetic-policy-revision"})]
        toggle.click()
        page.get_by_text("You approve each application", exact=True).wait_for()
        assert not toggle.is_checked()
        assert actions[-1] == ("/api/v1/workflow-policy", {"mode": "review", "revision": "changed-policy-revision"})
        assert not errors


def test_phase1_sources_never_render_as_submitted_without_application_state():
    state = {}
    with workspace(state=state) as (page, actions, errors):
        state["openings"].update(items=[{"id": "2"*64, "company": "Discovered Fixture", "title": "ML Engineer", "location": "Remote",
            "source": "fixture", "url": "https://careers.example.test/jobs/1", "first_seen": 100, "date": "2026-10-05",
            "classification_state": "resolved", "board": "ashby", "application_id": None, "application_state": None, "filter_reasons": []}], total=1)
        page.get_by_role("link", name="Discovered Fixture").wait_for(timeout=10000)
        row = page.locator("#openings tbody tr")
        assert "Board identified" in row.locator("td").nth(2).inner_text()
        assert row.get_by_text("Not started", exact=True).is_visible()
        assert not row.get_by_text("Submitted", exact=True).count()
        assert not actions and not errors


def test_history_hold_appears_as_previous_application_check_not_ready_or_missing_answer():
    state = {}
    with workspace(state=state) as (page, actions, errors):
        state["overview"]["applications"][0].update(state="history_hold", inventory_ready=False)
        state["overview"]["questions"] = []
        state["overview"]["summary"].update(ready=0, questions=0)
        state["openings"].update(items=[{"id": "2"*64, "company": "Historical Fixture", "title": "Software Engineer", "location": "Remote",
            "source": "fixture", "url": None, "first_seen": 100, "date": "2026-10-05",
            "classification_state": "history_hold", "board": "ashby", "application_id": KEY,
            "application_state": "history_hold", "filter_reasons": []}], total=1)
        app_row = page.locator("#applications tbody tr")
        app_row.get_by_text("Check previous application", exact=True).wait_for(timeout=10000)
        opening_row = page.locator("#openings tbody tr")
        opening_row.get_by_role("link", name="Check previous application", exact=True).wait_for(timeout=10000)
        assert "Check previous application" in opening_row.locator("td").nth(2).inner_text()
        assert not page.get_by_text("history_hold", exact=True).count()
        assert not app_row.get_by_text("Needs your answer", exact=True).count()
        assert not page.locator(".question-card").count()
        page.get_by_role("button", name="Ready", exact=True).click()
        assert not page.locator("#applications tbody tr").count()
        page.get_by_role("button", name="Needs attention", exact=True).click()
        assert app_row.get_by_text("Check previous application", exact=True).is_visible()
        assert not actions and not errors


def test_autonomy_review_does_not_offer_misleading_per_job_approval_or_revoke():
    with workspace() as (page, actions, errors):
        page.get_by_role('switch', name='Full autonomy').click()
        page.get_by_role('button', name='Review Synthetic Employer application').click()
        page.get_by_text('Full autonomy is enabled', exact=True).wait_for()
        page.get_by_text('Why this company? Please, no AI text.', exact=True).wait_for()
        assert not page.get_by_role('button', name='Submit current browser form', exact=True).count()
        assert not page.get_by_role('button', name='Revoke approval', exact=True).count()
        assert not page.get_by_role('checkbox', name='Leave blank: Why this company? Please, no AI text.', exact=True).count()
        assert page.get_by_text('Why this company? Please, no AI text.', exact=True).is_visible()
        assert len(actions) == 1 and actions[0][0] == '/api/v1/workflow-policy'
        assert not errors


def test_readable_mobile_layout_preserves_status_and_review_without_page_overflow():
    def configure(question, detail):
        question['contexts'][0]['type'] = 'text'
        detail['fields'][0]['question'] = 'Describe your experience building reliable distributed systems and explain the project outcome.'
        detail['fields'][0]['answer'] = 'Synthetic candidate answer for a long review paragraph. ' * 10
    with workspace(configure=configure) as (page, actions, errors):
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.locator('.agent-panel').is_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        answer = page.get_by_label('Answer: May we contact your current employer?')
        assert float(answer.evaluate('e => getComputedStyle(e).fontSize').removesuffix('px')) >= 16
        answer.fill('Synthetic mobile response that remains readable without zooming.')
        assert page.get_by_role('button', name='Save answer', exact=True).is_enabled()
        page.get_by_role('button', name='Review Synthetic Employer application').click()
        page.get_by_text('Describe your experience building reliable distributed systems and explain the project outcome.', exact=True).wait_for()
        assert page.locator('.review-modal').evaluate('e => e.scrollWidth <= e.clientWidth')
        assert float(page.locator('.review-field p').first.evaluate('e => getComputedStyle(e).fontSize').removesuffix('px')) >= 16
        assert not actions and not errors


@pytest.mark.parametrize('result,expected', [
    ({'state':'submitted','application_state':'submitted'}, 'Application submitted. Confirmation is recorded; spreadsheet status will update below.'),
    ({'state':'needs_review','reason':'A required browser field is empty. Your edits were preserved.'}, 'A required browser field is empty. Your edits were preserved.'),
    ({'state':'submitting'}, 'Submitting the current browser form. Your manual edits are preserved.')])
def test_current_form_submit_reports_actual_result_without_claiming_saved_draft_approval(result, expected):
    state = {'approve_result': result}
    with workspace(f'#review/{KEY}', state=state) as (page, actions, errors):
        button = page.get_by_role('button', name='Submit current browser form', exact=True)
        button.wait_for()
        assert button.is_enabled() and not actions
        page.get_by_text('Uses the answers and attachments currently in your Chrome tab. Your manual edits are preserved.', exact=True).wait_for()
        button.click()
        page.get_by_role('status').get_by_text(expected, exact=True).wait_for()
        assert actions == [(f'/api/v1/applications/{KEY}/approve', {'revision':'exact-draft-revision','acknowledged_blank_refs':[], 'acknowledge_role_fit_warning':False})]
        expected_status = {'submitted':'Submitted','submitting':'Submitting','needs_review':'Needs review'}[result['state']]
        assert page.locator('.review-facts .status').inner_text() == expected_status
        assert not errors


def test_review_status_polls_durable_progress_instead_of_stale_overview_snapshot():
    state = {}
    with workspace(f'#review/{KEY}', state=state) as (page, actions, errors):
        page.get_by_text('Synthetic Candidate', exact=True).wait_for()
        state['detail'].update(display_state='submitting', approval_state='submitting')
        page.locator('.review-facts .status').get_by_text('Submitting', exact=True).wait_for(timeout=10000)
        assert state['overview']['applications'][0]['state'] == 'waiting_review'
        assert page.get_by_role('button',name='Submit current browser form',exact=True).is_disabled()
        state['detail'].update(state='submitted', display_state='submitted', approval_state='submitted')
        page.locator('.review-facts .status').get_by_text('Submitted',exact=True).wait_for(timeout=10000)
        assert not page.get_by_role('button',name='Submit current browser form',exact=True).count()
        assert not actions and not errors


def test_discard_is_an_explicit_scoped_csrf_action_and_keeps_review_history():
    with workspace(f'#review/{KEY}') as (page, actions, errors):
        button=page.get_by_role('button',name='Discard application',exact=True)
        button.wait_for(); assert not actions
        button.click()
        page.get_by_role('status').get_by_text('Application discarded. Its worker is stopped. Its application tab is closed. History is preserved.',exact=True).wait_for()
        assert actions == [(f'/api/v1/applications/{KEY}/discard',{})]
        assert page.locator('.review-facts .status').inner_text() == 'Discarded'
        assert page.get_by_text('Synthetic Candidate',exact=True).is_visible()
        assert not page.get_by_role('button',name='Discard application',exact=True).count()
        assert not page.get_by_role('button',name='Submit current browser form',exact=True).count()
        assert not errors


@pytest.mark.parametrize('stage',['submitted','submitting','submission_uncertain','discarded'])
def test_terminal_or_active_submission_never_offers_unsafe_discard(stage):
    def configure(question,detail):
        detail['display_state']=stage
        if stage in {'submitted','submission_uncertain'}:detail['state']=stage
    with workspace(f'#review/{KEY}',configure=configure) as (page,actions,errors):
        page.get_by_text('Synthetic Candidate',exact=True).wait_for()
        assert not page.get_by_role('button',name='Discard application',exact=True).count()
        assert not actions and not errors


def test_open_review_receives_updated_receipt_and_sheet_status_without_reopening():
    state={}
    with workspace(f'#review/{KEY}',state=state) as (page,actions,errors):
        page.get_by_text('Synthetic Candidate',exact=True).wait_for()
        state['overview']['applications'][0].update(state='submitted',display_state='submitted',confirmed_at='2026-10-04T21:10:00Z',confirmed_date='2026-10-04',sheet_synced=True)
        state['detail'].update(state='submitted',display_state='submitted')
        page.locator('.receipt-banner').get_by_text('Sheets synced',exact=False).wait_for(timeout=10000)
        assert page.locator('.review-facts .status').inner_text() == 'Submitted'
        assert not actions and not errors


def test_failed_current_form_submission_explanation_survives_automatic_detail_refresh():
    message = 'Current browser form has a required unanswered question. Your edits are preserved.'
    state={'approve_error':message}
    with workspace(f'#review/{KEY}',state=state) as (page,actions,errors):
        page.get_by_role('button',name='Submit current browser form',exact=True).click()
        page.get_by_role('alert').get_by_text(message,exact=True).wait_for()
        page.wait_for_timeout(5300)
        assert page.get_by_role('alert').get_by_text(message,exact=True).is_visible()
        assert len(actions)==1 and actions[0][0].endswith('/approve')
        assert not errors


@pytest.mark.parametrize('queue_state,display_state,candidate_count,agent_count,label', [
    ('waiting_input', 'needs_form_repair', 0, 1, 'Needs form repair'),
    ('waiting_input', 'needs_verification', 0, 0, 'Needs verification'),
    ('waiting_input', 'waiting_input', 0, 1, 'Needs form repair'),
    ('queued', 'agent_queued', 0, 1, 'Agent queued'),
    ('running', 'agent_working', 0, 1, 'Agent working'),
    ('waiting_input', 'waiting_input', 1, 1, 'Needs your answer'),
    ('waiting_input', 'discarded', 0, 0, 'Discarded'),
])
def test_agent_work_status_matches_overview_and_detail_without_requesting_known_answers(
        queue_state, display_state, candidate_count, agent_count, label):
    state = {}
    def configure(question, detail):
        # Optional genuine input still belongs to the candidate even when no
        # required question remains. Known fields must not produce input cards.
        question['required'] = False
        question['contexts'][0]['required'] = False
        current_questions = [question] if candidate_count else []
        counts = dict(state=queue_state, display_state=display_state,
                      pending_candidate_questions=candidate_count,
                      pending_required_questions=0, pending_agent_tasks=agent_count)
        state['overview']['applications'][0].update(**counts, inventory_ready=False)
        state['overview']['questions'] = current_questions
        state['overview']['summary'].update(ready=0, questions=candidate_count)
        detail.update(**counts, questions=current_questions,
            agent_tasks=[{'question': 'Gender', 'ref': 'gender', 'task_kind': 'known_answer_fill',
                          'required': False}] if agent_count else [])
        detail['approval'].update(can_approve=False, reason='Current work must be verified')
    with workspace(state=state, configure=configure) as (page, actions, errors):
        row = page.locator('#applications tbody tr')
        row.get_by_text(label, exact=True).wait_for()
        assert page.locator('.question-card').count() == candidate_count
        assert not page.get_by_label('Answer: Gender').count()
        if not candidate_count:
            assert not row.get_by_text('Needs your answer', exact=True).count()
        page.get_by_role('button', name='Review Synthetic Employer application').click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_text(label, exact=True).wait_for()
        submit = dialog.get_by_role('button', name='Submit current browser form')
        assert submit.count() == 0 or submit.is_disabled()
        if not candidate_count:
            assert not dialog.get_by_text('Needs your answer', exact=True).count()
        assert not actions and not errors
