"""Synthetic question drift must never widen a saved agent-derived answer."""
import asyncio
from copy import deepcopy

import pytest

from jhb.applications import booklet, native_question_context, observed_question, planner, worker
from jhb.applications.cli_runtime import dispatch
from tests.applications.test_greenhouse_owned_descriptions import browser_form

URL = 'https://job-boards.greenhouse.io/synthetic-company/jobs/1234'
KEY = 'custom.profile.synthetic.eligibility'


def field():
    return {'ref': 'question_501', 'label': 'Are you legally eligible to work in the US?',
            'type': 'combobox', 'required': True, 'description': '', 'description_truncated': False,
            'options': [{'label': 'Yes'}, {'label': 'No'}]}


def record(control=None):
    control = control or field()
    return {**booklet.answer('Yes', {'provider': 'agent-derived synthetic verified facts',
                'basis_values': {'eligibility.authorized_us': True},
                'observed_question': deepcopy(control), 'observed_choices': ['Yes', 'No']}),
            'field_ref': control['ref'], 'question': control['label'],
            'proposed': True, 'candidate_confirmation': False}


def answers(control=None):
    return {KEY: record(control), 'eligibility.authorized_us': booklet.answer(True, 'synthetic candidate fact')}


def test_unchanged_binding_passes_without_mutating_facts_or_inventing_confirmation():
    control, values = field(), answers()
    before = deepcopy(values)
    assert planner.key_for_field(control, values) == KEY
    planned = planner.deterministic_plan({'fields': [control], 'buttons': []}, values)
    assert planner.validate_plan(planned, {'fields': [control], 'buttons': []}, values)['bindings'] == [
        {'ref': control['ref'], 'answer_key': KEY}]
    native = observed_question.fill_field(control, KEY, values[KEY])
    assert native[observed_question.FILL_PROOF] == control
    assert observed_question.FILL_PROOF not in control
    assert values == before and values[KEY]['candidate_confirmation'] is False


@pytest.mark.parametrize('change', ['immediate', 'unrestricted', 'trial_terms', 'own_words', 'truncated',
                                  'type', 'required', 'choices', 'ref', 'label', 'country', 'duplicate_choices'])
def test_context_drift_blocks_planning_and_generic_authorization_fallback(change):
    control, values = field(), answers()
    if change == 'immediate': control['description'] = 'You must be eligible to begin employment immediately.'
    elif change == 'unrestricted': control['description'] = 'Only unrestricted employment authorization qualifies.'
    elif change == 'trial_terms': control['description'] = 'Trial work is unpaid and assigns all intellectual property.'
    elif change == 'own_words': control['description'] = 'Answer in your own words without using AI.'
    elif change == 'truncated': control['description_truncated'] = True
    elif change == 'type': control['type'] = 'textarea'
    elif change == 'required': control['required'] = False
    elif change == 'choices': control['options'].append({'label': 'Other'})
    elif change == 'ref': control['ref'] = 'question_502'
    elif change == 'label': control['label'] += ' Immediately?'
    elif change == 'country': control['country_context'] = 'Canada'
    else: control['options'].append({'label': 'Yes'})
    assert planner.key_for_field(control, values) is None
    assert planner.deterministic_plan({'fields': [control], 'buttons': []}, values)['bindings'] == []
    with pytest.raises(ValueError, match='question context changed'):
        observed_question.fill_field(control, KEY, values[KEY])


@pytest.mark.parametrize('missing', observed_question.DESCRIPTOR_KEYS)
def test_partial_or_malformed_proof_is_not_legacy(missing):
    values = answers()
    del values[KEY]['source']['observed_question'][missing]
    assert planner.key_for_field(field(), values) is None


def test_legacy_record_without_observation_keeps_existing_policy():
    values = answers()
    del values[KEY]['source']['observed_question']
    assert planner.key_for_field(field(), values) == KEY
    assert observed_question.fill_field(field(), KEY, values[KEY]) == field()


@pytest.mark.parametrize('change', ['missing', 'changed', 'unverified', 'unsourced', 'invalid_bases'])
def test_guarded_profile_cannot_survive_loss_of_current_base_fact(change):
    values = answers()
    if change == 'missing': del values['eligibility.authorized_us']
    elif change == 'changed': values['eligibility.authorized_us']['value'] = False
    elif change == 'unverified': values['eligibility.authorized_us']['status'] = 'needs_input'
    elif change == 'unsourced': values['eligibility.authorized_us']['source'] = None
    else: values[KEY]['source']['basis_values'] = None
    assert planner.key_for_field(field(), values) is None
    snapshot = {'url': URL, 'fields': [{**field(), 'options': []}]}
    native_question_context.enrich_sync(snapshot, {'url': URL}, values,
        lambda f: pytest.fail('Missing verified basis must not authorize a native catalog probe'))


def test_external_annotations_are_not_fabricated_in_native_descriptor():
    control = field()
    control.update(country_context='united states', max_length=120)
    proof = deepcopy(control)
    native = {key: value for key, value in control.items() if key not in {'country_context', 'max_length'}}
    assert not observed_question.matches(native, proof)
    assert observed_question.matches(native, proof, native_owned_only=True)
    for key, value in [('country_context', 'Canada'), ('max_length', 40)]:
        changed = {**control, key: value}
        assert not observed_question.matches(changed, proof)
    native['description'] = 'You must start immediately.'
    assert not observed_question.matches(native, proof, native_owned_only=True)


def test_complete_narrative_proof_keeps_original_question_and_writing_constraints():
    control = {**field(), 'label': 'Why Synthetic Company?', 'type': 'textarea', 'required': False, 'options': []}
    item = record(control); item['value'] = 'A synthetic grounded paragraph.'
    key = 'custom.narrative.synthetic'
    assert planner.key_for_field(control, {key: item}) == key
    control['description'] = 'Please do not use AI to draft this answer.'
    assert planner.key_for_field(control, {key: item}) is None


def test_closed_catalog_is_native_work_only_when_owned_context_matches():
    control, values = field(), answers()
    control['options'] = []
    assert planner.key_for_field(control, values) is None
    snapshot = {'url': URL, 'fields': [control]}
    calls = []
    native_question_context.enrich_sync(snapshot, {'url': URL}, values,
        lambda f: calls.append(f['ref']) or {'choices': ['Yes', 'No'], 'type': 'combobox', 'truncated': False})
    assert calls == ['question_501'] and planner.key_for_field(control, values) == KEY
    control['options'] = []; control['description'] = 'You must start immediately.'
    native_question_context.enrich_sync(snapshot, {'url': URL}, values,
        lambda f: pytest.fail('Changed context must not trigger a proposal-guided probe'))
    assert planner.key_for_field(control, values) is None


def test_normal_worker_carries_descriptor_without_candidate_approval_or_terminal_action():
    class CLI:
        blocked_requests = 0
        def __init__(self): self.fills = []
        def allowed_url(self, url): return url == URL
        async def open(self, url): assert url == URL
        async def observe(self):
            return {'url': URL, 'fields': [{**field(), 'options': []}],
                    'buttons': [{'ref': 'submit', 'label': 'Submit application'}]}
        async def describe(self, f): return {'choices': ['Yes', 'No'], 'type': 'combobox', 'truncated': False}
        async def fill(self, f, value):
            self.fills.append((f, value)); return {'verified': True}
        async def click_next(self, *args): pytest.fail('Unexpected terminal action')
    cli, values = CLI(), answers()
    result, _ = asyncio.run(worker.prepare(None, {'url': URL}, values, planner.deterministic_plan, None, cli_actions=cli))
    assert result['state'] == 'waiting_review' and len(cli.fills) == 1
    assert cli.fills[0][0][observed_question.FILL_PROOF] == field()
    assert result['filled'][0]['proposed'] is True and not result['filled'][0].get('user_override')


def native_field(helpers):
    control = next(f for f in dispatch({'operation': 'observe'}, helpers)['fields'] if f['ref'] == 'question_101')
    options = dispatch({'operation': 'describe', 'field': control}, helpers)['choices']
    control['options'] = [{'label': label} for label in options]
    return observed_question.fill_field(control, KEY, record(control))


@pytest.mark.parametrize('change', ['immediate', 'unrestricted', 'trial_terms', 'own_words', 'required', 'type'])
def test_native_context_drift_stops_before_any_input(browser_form, change):
    page, helpers = browser_form
    control = native_field(helpers)
    if change == 'required': page.locator('#question_101').evaluate("e=>e.setAttribute('aria-required','false')")
    elif change == 'type': page.locator('#question_101').evaluate("e=>e.removeAttribute('role')")
    else:
        text = {'immediate': 'Immediate employment authorization required.',
                'unrestricted': 'Unrestricted authorization for any employer required.',
                'trial_terms': 'Accept an unpaid project and transfer all IP.',
                'own_words': 'Write this answer yourself; do not use AI.'}[change]
        page.locator('#question_101-description').evaluate('(e,text)=>e.textContent=text', text)
    original = helpers['cdp']; inputs = []
    def cdp(method, **params):
        if method.startswith('Input.') or method == 'DOM.focus': inputs.append(method)
        return original(method, **params)
    with pytest.raises(ValueError, match='question context changed'):
        dispatch({'operation': 'fill', 'field': control, 'value': 'Yes'}, {**helpers, 'cdp': cdp})
    assert inputs == [] and page.evaluate('window.selections') == page.evaluate('window.submissions') == 0


def test_native_unchanged_descriptor_and_choices_select_exact_answer(browser_form):
    page, helpers = browser_form
    control = native_field(helpers)
    assert dispatch({'operation': 'fill', 'field': control, 'value': 'Yes'}, helpers)['verified']
    assert page.locator('.select__single-value').inner_text() == 'Yes'
    assert page.evaluate('window.selections') == 1 and page.evaluate('window.submissions') == 0


@pytest.mark.parametrize('change', ['choices', 'help_on_open'])
def test_native_catalog_or_help_change_after_open_never_selects_an_answer(browser_form, change):
    page, helpers = browser_form
    control = native_field(helpers)
    page.evaluate(r'''(change)=>{window.openMenu=()=>{
        const e=document.querySelector('#question_101');e.setAttribute('aria-controls','options');e.setAttribute('aria-expanded','true');
        document.querySelector('#options').innerHTML='<div role="option" onclick="choose(\'Yes\')">Yes</div><div role="option" onclick="choose(\'No\')">No</div>';
        if(change==='choices')document.querySelector('#options').innerHTML+='<div role="option">Other</div>';
        else document.querySelector('#question_101-description').textContent='Unrestricted authorization required.';
    }}''', change)
    with pytest.raises(ValueError, match='question context changed'):
        dispatch({'operation': 'fill', 'field': control, 'value': 'Yes'}, helpers)
    assert page.locator('.select__single-value').inner_text() == ''
    assert page.locator('#question_101').input_value() == ''
    assert page.evaluate('window.selections') == page.evaluate('window.submissions') == 0
