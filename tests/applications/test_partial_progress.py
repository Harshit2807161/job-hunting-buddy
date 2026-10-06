"""Synthetic current-run fill checkpoints; no live browser or model calls."""
import asyncio
import base64
import json

import pytest

from jhb import config
from jhb.applications import boards, booklet, worker
from jhb.applications.cli_browser import BrowserOperationError
from jhb.applications.planner import deterministic_plan

IMAGE = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC')


def job(number=1):
    url = f'https://job-boards.greenhouse.io/example/jobs/{number}'
    return {'url': url, 'dedupe_hash': boards.application_hash(url), 'company': 'Example',
            'title': 'Software Engineer', 'role_classes': 'swe'}


def answers():
    return {'identity.full_name': booklet.answer('Synthetic Candidate', 'Synthetic verified resume'),
            'custom.date': {**booklet.answer('2027-02-15', 'Synthetic approved calendar date'),
                            'question': 'Approved starting day', 'field_ref': 'calendar'}}


class Form:
    target_id = 'synthetic-owned-tab'
    blocked_requests = 0
    def __init__(self, failure='calendar'):
        self.failure, self.retained, self.observations, self.stage = failure, {}, 0, 0
        self.name_attempts = 0
    def allowed_url(self, url): return True
    async def open(self, url): pass
    async def observe(self):
        self.observations += 1
        if self.failure == 'observe' and self.observations == 2:
            raise TimeoutError('Synthetic secret-bearing diagnostic MUST NOT APPEAR')
        fields = [{'ref': 'name', 'label': 'Full Name', 'type': 'text', 'required': True}]
        if self.failure != 'revisit':
            fields.append({'ref': 'calendar', 'label': 'Approved starting day', 'type': 'text', 'required': True})
        next_button = self.failure == 'continue' or self.failure == 'revisit' and self.stage == 0
        return {'fields': fields, 'buttons': [{'ref': 'continue' if next_button else 'submit',
                                              'label': 'Continue' if next_button else 'Submit application'}]}
    async def fill(self, field, value):
        if field['ref'] == 'name': self.name_attempts += 1
        if ((self.failure == 'calendar' and field['ref'] == 'calendar') or
                (self.failure == 'revisit' and field['ref'] == 'name' and self.name_attempts == 2)):
            self.retained.pop(field['ref'], None)
            raise BrowserOperationError('Calendar input did not retain the approved day', retryable=True)
        self.retained[field['ref']] = value  # this fake returns only after retention verification
    async def click_next(self, button):
        if self.failure == 'continue':
            raise BrowserOperationError('Synthetic secret-bearing diagnostic MUST NOT APPEAR', retryable=True)
        self.stage += 1
    async def screenshot(self, path): path.write_bytes(IMAGE)


def fail(form, current_job=None):
    current_job = current_job or job()
    try:
        asyncio.run(worker.prepare(None, current_job, answers(), deterministic_plan, None, cli_actions=form))
    except Exception as exc:
        return worker.failure_result(exc, form, job=current_job)
    raise AssertionError('Expected a synthetic mechanical failure')


def test_calendar_error_keeps_only_verified_current_fills_and_incomplete_inventory():
    form = Form()
    result = fail(form)
    assert result['state'] == 'failed' and result['retryable'] is True
    assert result['error_kind'] == 'browser_mechanics'
    assert [(r['ref'], r['value']) for r in result['filled']] == [('name', 'Synthetic Candidate')]
    assert result['failure_context'] == {'operation': 'fill', 'field_ref': 'calendar', 'field_type': 'text'}
    assert result['review_inventory']['complete'] is False
    assert {f['ref']: f['status'] for f in result['review_inventory']['fields']} == {'name': 'answered', 'calendar': 'blank'}
    assert result['review_completeness']['requires_explicit_approval'] is True
    assert not result.get('missing')  # known widget mechanics do not become candidate questions


@pytest.mark.parametrize('failure,operation', [('observe', 'observe'), ('continue', 'continue')])
def test_observe_or_continue_failure_keeps_verified_fills_without_stale_failing_ref(failure, operation):
    result = fail(Form(failure))
    assert {r['ref'] for r in result['filled']} == {'name', 'calendar'}
    assert result['failure_context'] == {'operation': operation}
    assert result['review_inventory']['complete'] is False
    assert 'MUST NOT APPEAR' not in json.dumps(result)


def test_failed_refill_removes_old_checkpoint_for_that_control():
    result = fail(Form('revisit'))
    assert result['filled'] == []
    assert result['review_inventory']['complete'] is False
    assert result['review_inventory']['fields'][0]['status'] == 'blank'


def test_checkpoint_cannot_cross_job_or_survive_a_fresh_open_failure():
    form = Form()
    fail(form)
    rejected = worker.failure_result(TimeoutError('private diagnostic'), form, job=job(2))
    assert rejected['filled'] == [] and 'failure_context' not in rejected
    async def failed_open(url): raise TimeoutError('private diagnostic')
    form.open = failed_open
    result = fail(form)
    assert result['filled'] == [] and 'failure_context' not in result


def test_run_job_persists_actual_partial_fills_and_never_imports_prior_packet(tmp_path, monkeypatch):
    from jhb import eligibility
    from jhb.applications import cli_browser, role_fit, resume_selection
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setattr(eligibility, 'assess_job', lambda *a: {'state': 'eligible', 'description': 'Synthetic official description'})
    monkeypatch.setattr(role_fit, 'assess', lambda *a: {'state': 'eligible', 'reason': 'Synthetic verified fit'})
    monkeypatch.setattr(resume_selection, 'select', lambda *args, **kwargs: {
        'state': 'selected', 'selected_role': 'sde', 'reason': 'Synthetic upstream document comparison',
        'selected_resume_sha256': '0' * 64})
    form = Form()
    monkeypatch.setattr(cli_browser, 'BrowserUseCLI', lambda: form)
    current_job = job()
    folder = tmp_path / 'private' / 'applications' / current_job['dedupe_hash']
    booklet.write_private(folder / 'packet.json', {'filled': [{'ref': 'old', 'value': 'Stale unverified prior-run value'}]})
    book = {'answers': {'identity.full_name': answers()['identity.full_name']}, 'roles': {'sde': {}, 'ml': {}},
            'custom_answers': {'custom.date': {**answers()['custom.date'], 'scope': {'region': 'global', 'board': 'example'}}}}
    result, path = asyncio.run(worker.run_job(current_job, book, planner_name='deterministic'))
    saved = json.loads(path.with_name('packet.json').read_bytes())
    assert saved['state'] == 'failed' and saved['submitted'] is False
    assert saved['capture']['verified'] is True
    assert saved['review_inventory']['complete'] is False
    assert [r['ref'] for r in saved['filled']] == ['name']
    assert saved['failure_context']['field_ref'] == 'calendar'
    assert 'Stale unverified prior-run value' not in json.dumps(saved)
    assert result['error_kind'] == 'browser_mechanics'
