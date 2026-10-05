"""Exact observed California instruction; synthetic facts and isolated controls."""
import hashlib
import os

import pytest

from jhb import config
from jhb.applications import booklet, known_answers
from jhb.applications.manual_runtime import application_scope, dispatch
from jhb.applications.planner import key_for_field
from jhb.applications.submission_runtime import _checks

URL = 'https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application'
NOTE = 'Note: If you are based in California, please mark N/A.'
LABEL = known_answers._RESTRICTION


def field(note=NOTE, *, choices=('Yes', 'No', 'Not Sure', 'N/A')):
    return {'ref': 'ashby:restriction', 'label': LABEL, 'type': 'radio', 'required': True,
            'description': note, 'description_truncated': False,
            'options': [{'label': choice, 'value': choice} for choice in choices]}


def facts():
    return {key: booklet.answer(value, 'synthetic verified contact or explicit screening answer') for key, value in {
        'identity.state': 'California', 'identity.country': 'United States', 'screening.non_compete': False}.items()}


def test_owned_california_note_overrides_general_no_without_changing_original_fact():
    answers = facts()
    control = field()
    # A previously saved general No must not bypass the site's explicit rule.
    answers['custom.existing'] = {**booklet.answer(False, 'synthetic previous user reply'),
                                  'question': LABEL, 'field_ref': control['ref']}
    key = known_answers.enrich(control, {}, answers)
    assert key_for_field(control, answers) == key and answers[key]['value'] == 'N/A'
    assert answers['screening.non_compete']['value'] is False
    assert answers[key]['source']['owned_instruction'] == NOTE
    assert answers[key]['source']['records']['identity.state']['value'] == 'California'


@pytest.mark.parametrize('change', ['unknown_note', 'truncated', 'unverified_state', 'unknown_state',
                                  'missing_country', 'foreign_country', 'missing_na', 'duplicate_na'])
def test_unproven_condition_does_not_silently_fallback_to_general_no(change):
    answers, control = facts(), field()
    if change == 'unknown_note':control['description'] = 'If you have a special agreement, select a different answer.'
    elif change == 'truncated':control['description_truncated'] = True
    elif change == 'unverified_state':answers['identity.state']['status'] = 'needs_input'
    elif change == 'unknown_state':answers['identity.state']['value'] = 'Calif.'
    elif change == 'missing_country':answers.pop('identity.country')
    elif change == 'foreign_country':answers['identity.country']['value'] = 'Canada'
    elif change == 'missing_na':control['options'].pop()
    else:control['options'].append({'label': 'N/A', 'value': 'duplicate'})
    assert known_answers.enrich(control, {}, answers) is None
    assert key_for_field(control, answers) is None


def test_known_noncalifornia_state_code_reuses_explicit_restriction_answer():
    answers = facts();answers['identity.state']['value'] = 'NY'
    control = field()
    key = known_answers.enrich(control, {}, answers)
    assert key_for_field(control, answers) == key and answers[key]['value'] is False


def test_changed_owned_instruction_invalidates_old_derived_choice():
    answers, control = facts(), field()
    key = known_answers.enrich(control, {}, answers)
    changed = {**control, 'description': 'Note: If you are based in New York, please mark N/A.'}
    assert key_for_field(changed, answers) is None
    assert key in answers  # Evidence remains, but cannot bind the changed field.


def test_unrelated_question_does_not_inherit_california_rule():
    answers = facts()
    control = {**field(), 'label': 'Are you legally authorized to work in the United States?'}
    assert known_answers.enrich(control, {}, answers) is None


HTML = '''<!doctype html><html><form class=ashby-application-form-container>
<fieldset data-field-path=restriction><legend class=ashby-application-form-question-title>LABEL</legend>
<div class=ashby-application-form-question-description><p><em>Note</em>: If you are based in California, please mark N/A.</p></div>
<div class=ashby-application-form-question-description style="display:none">Hidden foreign instruction</div>
<label><input id=yes name=restriction type=radio value=yes required>Yes</label>
<label><input id=no name=restriction type=radio value=no checked>No</label>
<label><input id=not-sure name=restriction type=radio value=unknown>Not Sure</label>
<label><input id=na name=restriction type=radio value=na>N/A</label>
<div data-field-path=other><label class=ashby-application-form-question-title for=other>Other optional note</label>
 <div class=ashby-application-form-question-description>Foreign nested instruction</div><input id=other></div>
</fieldset><button type=submit>Submit application</button></form><script>
window.submissions=0;document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
</script></html>'''.replace('LABEL', LABEL)


@pytest.mark.parametrize('change', [None, 'instruction', 'selection', 'missing_packet_note', 'truncated_packet_note'])
def test_native_owned_note_choice_and_fresh_audit_preserve_context_without_submission(change):
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(config.ROOT / '.local-browsers'))
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch();page = browser.new_page()
        page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=HTML))
        page.goto(URL);session = page.context.new_cdp_session(page)
        helpers = {'cdp': lambda method, **params: session.send(method, params), 'js': page.evaluate,
            'wait': lambda seconds: page.wait_for_timeout(seconds*1000), 'click_at_xy': lambda x, y: page.mouse.click(x, y),
            'list_tabs': lambda: [{'targetId': 'fixture', 'url': URL}], 'current_tab': lambda: {'targetId': 'fixture'},
            'switch_tab': lambda target: None}
        def call(operation, **payload):return dispatch({'operation': operation, 'scope': application_scope(URL), **payload}, helpers)
        try:
            call('open', url=URL);snapshot = call('observe')
            control = next(f for f in snapshot['fields'] if f['ref'] == 'ashby:restriction')
            assert control['label'] == LABEL and control['description'] == NOTE
            assert control['description_truncated'] is False
            assert 'Foreign nested' not in control['description'] and 'Hidden' not in control['description']
            answers = facts();key = known_answers.enrich(control, {}, answers)
            assert call('fill', field=control, value=answers[key]['value'])['verified']
            assert page.locator('#na').is_checked() and not page.locator('#no').is_checked()
            filled = [{'ref': control['ref'], 'question': LABEL, 'key': key,
                       'value': answers[key]['value'], 'source': answers[key]['source']}]
            # The portal propagation patch consumes the same owned metadata;
            # this fixture supplies it explicitly to test the fresh audit.
            inventory = [{'ref': f['ref'], 'question': f['label'], 'type': f['type'], 'required': f['required'],
                          'description': f.get('description', ''), 'description_truncated': f.get('description_truncated', False)}
                         for f in snapshot['fields']]
            packet = {'job': {'url': URL}, 'filled': filled, 'review_inventory': {'fields': inventory}}
            if change == 'missing_packet_note':
                inventory[0].pop('description');inventory[0].pop('description_truncated')
            elif change == 'truncated_packet_note':inventory[0]['description_truncated'] = True
            if change == 'instruction':page.evaluate("document.querySelector('[data-field-path=restriction] > .ashby-application-form-question-description').textContent='Select No instead.'")
            elif change == 'selection':page.locator('#no').check()
            result = _checks({'target_id': 'fixture', 'documents': {}}, helpers, packet,
                {'application_url': URL, 'authorization_scope': 'one exact application explicitly approved in the local review portal'})
            if change is None:
                assert result['retained'][0]['state']['selected'] == ['N/A']
                assert result['fields'][0]['description'] == NOTE
            else:
                assert result['state'] == 'waiting_review' and result['click_started'] is False
            assert page.evaluate('window.submissions') == 0 and page.evaluate('window.__jhbGuard') is True
        finally:browser.close()


def test_preparation_cannot_become_ready_after_owned_instruction_changes():
    import asyncio
    from jhb.applications.planner import deterministic_plan
    from jhb.applications.worker import prepare
    class SyntheticCLI:
        blocked_requests = 0
        calls = 0
        filled = []
        async def open(self, url):pass
        def allowed_url(self, url):return url == URL
        async def observe(self):
            self.calls += 1
            control = field() if self.calls == 1 else field('Select No instead.')
            return {'url': URL, 'fields': [control], 'buttons': [{'ref': 'submit', 'label': 'Submit application'}]}
        async def fill(self, field, value):self.filled.append(value)
        async def click_next(self, button):raise AssertionError('No terminal click')
    cli = SyntheticCLI()
    result, _ = asyncio.run(prepare(None, {'url': URL}, facts(), deterministic_plan, None, cli_actions=cli))
    assert result['state'] == 'waiting_input' and cli.filled == ['N/A']
    assert result['review_inventory']['complete'] is False
    assert any(event['event'] == 'fields_revealed' for event in result['events'])


@pytest.mark.parametrize('proof_style', ['singular', 'list'])
def test_unfamiliar_note_accepts_only_fresh_explicit_candidate_context_response(proof_style):
    answers = facts()
    control = field('For a different contractual situation, select the applicable choice.')
    proof = {'owned_description_sha256': hashlib.sha256(control['description'].encode()).hexdigest(),
             'owned_description_truncated': False}
    source = {'provider': 'explicit user question response', 'question_id': 'q_synthetic',
              **(proof if proof_style == 'singular' else {'owned_description_proofs': [proof]})}
    answers['custom.context'] = {**booklet.answer(False, source), 'question': LABEL, 'field_ref': control['ref']}
    assert key_for_field(control, answers) == 'custom.context'
    assert key_for_field({**control, 'description': 'Changed instruction'}, answers) is None
    assert key_for_field({**control, 'description_truncated': True}, answers) is None
    answers['custom.context']['field_ref'] = 'different control'
    assert key_for_field(control, answers) is None


@pytest.mark.parametrize('change', ['wrong_digest', 'generated_provider', 'missing_question_id', 'missing_truncation'])
def test_context_response_requires_exact_complete_instruction_and_candidate_provenance(change):
    answers, control = facts(), field('Select the applicable contractual answer.')
    source = {'provider': 'explicit user question response', 'question_id': 'q_synthetic',
              'owned_description_sha256': hashlib.sha256(control['description'].encode()).hexdigest(),
              'owned_description_truncated': False}
    if change == 'wrong_digest':source['owned_description_sha256'] = '0'*64
    elif change == 'generated_provider':source['provider'] = 'generated'
    elif change == 'missing_question_id':source.pop('question_id')
    else:source.pop('owned_description_truncated')
    answers['custom.context'] = {**booklet.answer(False, source), 'question': LABEL, 'field_ref': control['ref']}
    assert key_for_field(control, answers) is None


def test_explicit_no_cannot_override_california_instruction_even_when_na_unavailable():
    answers, control = facts(), field(choices=('Yes', 'No'))
    source = {'provider': 'explicit user question response', 'question_id': 'q_synthetic',
              'owned_description_sha256': hashlib.sha256(NOTE.encode()).hexdigest(),
              'owned_description_truncated': False}
    answers['custom.context'] = {**booklet.answer(False, source), 'question': LABEL, 'field_ref': control['ref']}
    assert key_for_field(control, answers) is None
