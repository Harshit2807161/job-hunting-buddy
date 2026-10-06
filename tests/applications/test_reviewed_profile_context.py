"""Terminal profile prerequisites come from the bound booklet, not source claims."""
from copy import deepcopy

import pytest

from jhb import config
from jhb.applications import boards, booklet, planner, questions, review_inventory, submission_runtime
from jhb.applications.cli_runtime import dispatch
from tests.applications.test_observed_question_guard import KEY, URL, answers, field, browser_form
from tests.applications.test_reviewed_derived_catalogs import bind


def evidence(root, mode='delegated', *, change=None, control=None):
    job = {'url': URL, 'dedupe_hash': boards.application_hash(URL), 'company': 'Synthetic', 'title': 'Engineer'}
    values = answers(control)
    saved = {**values[KEY], 'scope': questions._scope(job), 'job_hash': job['dedupe_hash']}
    # A snapshot alone must never replace a missing current fact.
    saved['source']['records'] = {'eligibility.authorized_us': deepcopy(values['eligibility.authorized_us'])}
    profile = {'schema_version': 1, 'answers': {'eligibility.authorized_us': values['eligibility.authorized_us']},
               'roles': {'sde': {}, 'ml': {}}, 'custom_answers': {KEY: saved}}
    row = {'key': KEY, 'question': saved['question'], 'ref': saved['field_ref'], 'value': saved['value'],
           'source': deepcopy(saved['source']), 'proposed': True}
    if change == 'missing': del profile['answers']['eligibility.authorized_us']
    elif change == 'changed': profile['answers']['eligibility.authorized_us']['value'] = False
    elif change == 'unverified': profile['answers']['eligibility.authorized_us']['status'] = 'needs_input'
    elif change == 'unsourced': profile['answers']['eligibility.authorized_us']['source'] = None
    elif change == 'unscoped': saved['job_hash'] = 'f'*64
    elif change == 'forged_source': row['source']['observed_question']['description'] = 'Forged context'
    packet = {'job': job, 'selected_role': 'sde', 'filled': [row]}
    if control:
        packet.update(review_inventory.build([control], [row], {**values, KEY: saved}, planner.key_for_field, complete=True))
    request, attempt, binding = bind(root, packet, profile, mode)
    approved = {KEY: {**booklet.answer(row['value'], row['source']), 'question': row['question'], 'field_ref': row['ref']}}
    return request, packet, attempt, approved


@pytest.mark.parametrize('mode', ['delegated', 'portal'])
def test_exact_review_bound_profile_restores_current_bases_for_final_binding(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    request, packet, attempt, approved = evidence(tmp_path, mode)
    before = deepcopy(approved)
    assert planner.key_for_field(field(), approved) is None
    probes = submission_runtime._reviewed_probe_answers(request, packet, attempt, approved)
    assert probes['eligibility.authorized_us'] == answers()['eligibility.authorized_us']
    assert planner.key_for_field(field(), probes) == KEY
    assert approved == before and probes[KEY]['candidate_confirmation'] is False


@pytest.mark.parametrize('change', ['missing', 'changed', 'unverified', 'unsourced', 'unscoped', 'forged_source'])
def test_fresh_binding_cannot_validate_bad_bases_or_an_unscoped_saved_projection(tmp_path, monkeypatch, change):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    request, packet, attempt, approved = evidence(tmp_path, change=change)
    with pytest.raises(ValueError, match='reviewed base facts|no value or source'):
        submission_runtime._reviewed_probe_answers(request, packet, attempt, approved)


def test_no_review_binding_cannot_acquire_source_snapshot_as_new_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    request, packet, attempt, approved = evidence(tmp_path)
    del attempt['review_binding']
    probes = submission_runtime._reviewed_probe_answers(request, packet, attempt, approved)
    assert probes == approved and 'eligibility.authorized_us' not in probes
    assert planner.key_for_field(field(), probes) is None


@pytest.mark.parametrize('mode', ['delegated', 'portal'])
@pytest.mark.parametrize('change', [None, 'choices', 'help'])
def test_native_terminal_audit_uses_bound_profile_and_never_selects_or_submits(browser_form, tmp_path, monkeypatch, mode, change):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    page, helpers = browser_form
    page.locator('#question_102').evaluate("e=>e.closest('.field-wrapper').remove()")
    page.locator('.select__single-value').evaluate("e=>e.textContent='Yes'")
    control = dispatch({'operation': 'observe'}, helpers)['fields'][0]
    control['options'] = [{'label': label} for label in dispatch({'operation': 'describe', 'field': control}, helpers)['choices']]
    request, packet, attempt, _ = evidence(tmp_path, mode, control=control)
    if change == 'choices':
        page.evaluate("const original=window.openMenu;window.openMenu=()=>{original();setTimeout(()=>document.querySelector('#options').insertAdjacentHTML('beforeend','<div role=option>Other</div>'),451)}")
    elif change == 'help':
        page.locator('#question_101-description').evaluate("e=>e.textContent='Unrestricted authorization required.'")
    result = submission_runtime._checks(request, helpers, packet, attempt)
    if change is None:
        assert result['double_check_count'] == 1 and not result.get('state')
    else:
        assert result['state'] == 'waiting_review' and result['click_started'] is False
    assert page.locator('.select__single-value').inner_text() == 'Yes'
    assert page.evaluate('window.selections') == page.evaluate('window.submissions') == 0
