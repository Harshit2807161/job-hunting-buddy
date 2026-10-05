import asyncio

from jhb.applications import booklet
from jhb.applications.planner import deterministic_plan
from jhb.applications.worker import prepare


def test_transport_and_widget_failures_are_retries_not_candidate_questions():
    from jhb.applications.worker import failure_result
    from jhb.applications.cli_browser import BrowserOperationError
    class BrokenWidget:
        last_failure = {"operation": "fill", "kind": "browser_mechanics"}
    result = failure_result(BrowserOperationError("Dropdown did not retain the selected answer", retryable=True), BrokenWidget())
    assert result["retryable"] and result["error_kind"] == "browser_mechanics"
    assert not result.get("missing") and result["events"][0]["operation"] == "fill"
    rejected = failure_result(ValueError("synthetic sensitive private value"))
    assert not rejected["retryable"] and "private" not in str(rejected)
    transport = failure_result(RuntimeError("Browser Use CLI failed; run browser-use --doctor"))
    assert transport["retryable"] and transport["error_kind"] == "browser_transport"


def test_known_answer_widget_failure_cannot_become_unknown_fact():
    import pytest
    from jhb.applications.cli_browser import BrowserOperationError
    class BrokenForm:
        def allowed_url(self, url): return True
        async def open(self, url): pass
        async def observe(self):
            return {"fields": [{"ref":"country","label":"Country","type":"combobox","required":True}], "buttons": []}
        async def fill(self, field, value):
            raise BrowserOperationError("Dropdown did not retain the selected answer", retryable=True)
    with pytest.raises(BrowserOperationError):
        asyncio.run(prepare(None,{"url":"synthetic"},{"identity.country":booklet.answer("Example Country","synthetic")},
                            deterministic_plan,None,cli_actions=BrokenForm()))


def test_legacy_value_error_for_known_optional_control_never_reports_ready():
    import pytest
    from jhb.applications.cli_browser import BrowserOperationError
    class BrokenForm:
        blocked_requests = 0
        def allowed_url(self, url): return True
        async def open(self, url): pass
        async def observe(self):
            return {'fields': [{'ref': 'gender', 'label': 'Gender', 'type': 'select',
                                'required': False, 'options': [{'label': 'Male'}, {'label': 'Female'}]}],
                    'buttons': [{'ref': 'submit', 'label': 'Submit application'}]}
        async def fill(self, field, value): raise ValueError('Legacy retained value failure')
        async def describe(self, field): return {'choices': ['Male', 'Female']}
    with pytest.raises(BrowserOperationError) as failure:
        asyncio.run(prepare(None, {'url': 'synthetic'},
            {'disclosure.gender': booklet.answer('Male', 'synthetic profile')},
            deterministic_plan, None, cli_actions=BrokenForm()))
    assert failure.value.retryable


def test_incompatible_new_choice_stays_candidate_input():
    class ChangedForm:
        blocked_requests = 0
        def allowed_url(self, url): return True
        async def open(self, url): pass
        async def observe(self):
            return {'fields': [{'ref': 'country', 'label': 'Country', 'type': 'combobox', 'required': True}],
                    'buttons': []}
        async def fill(self, field, value): raise ValueError('Stored answer is absent from dropdown options')
        async def describe(self, field): return {'choices': ['Canada', 'France']}
    result, _ = asyncio.run(prepare(None, {'url': 'synthetic'},
        {'identity.country': booklet.answer('United States', 'synthetic profile')},
        deterministic_plan, None, cli_actions=ChangedForm()))
    assert result['state'] == 'waiting_input'
    assert result['missing'][0]['choices'] == ['Canada', 'France']


def test_portal_answer_cannot_leak_to_a_hidden_sibling_application():
    from jhb.applications.worker import _scoped_custom_answers
    scope = {'board': 'example', 'ats': 'greenhouse'}
    book = {'custom_answers': {'custom.question': {**booklet.answer('Yes', 'synthetic user'),
             'scope': scope, 'job_hashes': ['1' * 64]}}}
    assert _scoped_custom_answers(book, {'dedupe_hash': '1' * 64}, scope)
    assert not _scoped_custom_answers(book, {'dedupe_hash': '2' * 64}, scope)


def test_accomplishment_prompts_use_selected_verified_experience_without_input():
    from jhb.applications.narratives import ACCOMPLISHMENTS_PROMPT
    labels=[ACCOMPLISHMENTS_PROMPT,"Second example:","Third example:"]
    class Form:
        blocked_requests=0
        filled={}
        def allowed_url(self,url):return True
        async def open(self,url):pass
        async def observe(self):
            return {"fields":[{"ref":str(i),"label":label,"type":"textarea","required":True} for i,label in enumerate(labels)],
                    "buttons":[{"ref":"submit","label":"Submit application"}]}
        async def fill(self,field,value):self.filled[field['ref']]=value
    experience="Company A Jan 2024 – May 2024\nEngineer\n• Improved runtime by 10%.\nCompany B Jun 2024 – Aug 2024\nEngineer\n• Improved precision by 20%.\nCompany C Sep 2024 – Dec 2024\nResearcher\n• Reduced circuit depth by 15%."
    form=Form()
    result,_=asyncio.run(prepare(None,{"url":"synthetic"},{"role.experience":booklet.answer(experience,"synthetic selected resume")},
                                 deterministic_plan,None,cli_actions=form))
    assert result['state']=='waiting_review' and not result.get('missing')
    assert list(form.filled.values())==["• Improved runtime by 10%.","• Improved precision by 20%.","• Reduced circuit depth by 15%."]


def test_screenshot_timeout_preserves_packet_but_blocks_completed_review(tmp_path):
    from jhb.applications.worker import write_packet
    import json
    class ScreenshotFailure:
        target_id='synthetic-tab'
        async def screenshot(self,path):raise TimeoutError('synthetic timeout')
    result={'state':'waiting_review','reason':'Required retained answers verified','events':[],'filled':[]}
    packet=asyncio.run(write_packet(None,tmp_path,{'company':'Example','title':'Engineer','url':'https://example.test'},result,
                                    cli_actions=ScreenshotFailure()))
    assert packet.is_file()
    saved=json.loads((tmp_path/'packet.json').read_text())
    assert saved['state']=='failed' and saved['error_kind']=='browser_capture' and saved['retryable']
    assert saved['capture']['verified'] is False and not saved.get('missing')


def test_screenshot_handoff_without_file_preserves_packet(tmp_path):
    from jhb.applications.worker import write_packet
    class RedirectedScreenshot:
        target_id='synthetic-tab'
        async def screenshot(self,path):
            return {'handoff':'unsupported','reason':'Job redirected outside supported scope'}
    result={'state':'unsupported','reason':'Job redirected','events':[],'filled':[]}
    packet=asyncio.run(write_packet(None,tmp_path,{'company':'Example','title':'Engineer','url':'https://example.test'},result,
                                    cli_actions=RedirectedScreenshot()))
    assert packet.is_file() and 'Browser screenshot unavailable' in packet.read_text()


def test_required_question_revealed_after_filling_blocks_final_review():
    class ConditionalForm:
        blocked_requests = 0
        revealed = False
        def allowed_url(self, url): return True
        async def open(self, url): pass
        async def fill(self, field, value): self.revealed = True
        async def observe(self):
            fields = [{"ref": "first", "label": "First Name", "type": "text", "required": True}]
            if self.revealed:
                fields.append({"ref": "new", "label": "Explain this employer-specific certification", "type": "text", "required": True})
            return {"fields": fields, "buttons": [{"ref": "submit", "label": "Submit application"}]}
    result, _ = asyncio.run(prepare(None, {"url": "synthetic"},
                         {"identity.first_name": booklet.answer("Sam", "synthetic user")},
                         deterministic_plan, None, cli_actions=ConditionalForm()))
    assert result["state"] == "waiting_input"
    assert result["missing"][0]["question"] == "Explain this employer-specific certification"
    assert any(e["event"] == "fields_revealed" for e in result["events"])


def test_review_packet_preserves_both_education_rows():
    class EducationForm:
        blocked_requests = 0
        def allowed_url(self, url): return True
        async def open(self, url): pass
        async def ensure_education(self, count): assert count == 2
        async def fill(self, field, value): pass
        async def observe(self):
            return {"fields": [{"ref": f"school--{i}", "label": "School", "type": "combobox", "required": False} for i in [0, 1]],
                    "buttons": [{"ref": "submit", "label": "Submit application"}]}
    answers = {"education.0.school": booklet.answer("Example Graduate School", "synthetic resume"),
               "education.1.school": booklet.answer("Example College", "synthetic resume")}
    result, _ = asyncio.run(prepare(None, {"url": "synthetic"}, answers,
                                  deterministic_plan, None, cli_actions=EducationForm()))
    assert result["state"] == "waiting_review"
    assert {r["value"] for r in result["filled"]} == {"Example Graduate School", "Example College"}



def test_owned_no_ai_help_prevents_drafting_and_legacy_generated_fill(monkeypatch):
    from jhb.applications import narratives
    class Form:
        blocked_requests = 0
        calls = []
        def allowed_url(self, url): return True
        async def open(self, url): pass
        async def observe(self):
            return {"fields": [{"ref": "why", "label": "Why this employer?", "type": "textarea", "required": False,
                                "description": "Please do not use generative AI to write this response.",
                                "description_truncated": False}], "buttons": [{"ref": "submit", "label": "Submit application"}]}
        async def fill(self, field, value): self.calls.append(value)
    def forbidden_draft(*args): raise AssertionError("Employer forbids generated wording")
    monkeypatch.setattr(narratives, "proposal", forbidden_draft)
    form = Form()
    approved = {**booklet.answer("Legacy generated wording", "Synthetic template"),
                "question": "Why this employer?", "field_ref": "why"}
    result, _ = asyncio.run(prepare(None, {"url": "synthetic"}, {"custom.why": approved},
                                   deterministic_plan, None, cli_actions=form))
    assert form.calls == []
    assert result["optional_questions"][0]["description"].startswith("Please do not use")
    assert result["review_inventory"]["fields"][0]["candidate_wording_required"] is True
