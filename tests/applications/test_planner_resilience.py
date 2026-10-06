import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from jhb.applications import booklet
from jhb.applications.planner import CodexPlanner, deterministic_plan, key_for_field, validate_plan


def observation():
    return {"fields": [
        {"ref": "email", "label": "Email", "type": "text", "required": True},
        {"ref": "novel", "label": "Do you hold an employer-specific license?", "type": "combobox", "required": True},
    ], "buttons": [{"ref": "submit", "label": "Submit application"}]}


def answers():
    return {"identity.email": booklet.answer("synthetic@example.test", "synthetic verified profile"),
            "eligibility.authorized_us": booklet.answer(True, "explicit synthetic answer")}


def test_default_planner_does_not_start_codex_and_keeps_unknown_question_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("Known bindings must not wait for a model"))
    planner = CodexPlanner(tmp_path)
    actual = planner(observation(), answers())
    assert actual == validate_plan(deterministic_plan(observation(), answers()), observation(), answers())
    assert actual['bindings'] == [{"ref": "email", "answer_key": "identity.email"}]
    assert actual['next_ref'] is None
    assert planner.last_outcome['planner_outcome'] == 'deterministic'
    audit = json.loads((tmp_path / 'planner-audit.json').read_text())
    assert len(audit) == 1 and audit[0]['error_kind'] is None
    assert 'synthetic@example.test' not in (tmp_path / 'planner-audit.json').read_text()
    assert (tmp_path / 'planner-audit.json').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('failure,expected', [
    ('missing_cli', 'cli_unavailable'), ('timeout', 'timeout'), ('nonzero', 'cli_nonzero'),
    ('missing_output', 'missing_output'), ('invalid_json', 'invalid_json'),
    ('invalid_schema', 'invalid_plan'), ('unsupported_binding', 'invalid_plan'),
    ('terminal_click', 'invalid_plan'),
])
def test_optional_codex_audit_failure_falls_back_to_only_valid_pairs(tmp_path, monkeypatch, failure, expected):
    def run(command, **kwargs):
        if failure == 'missing_cli':
            raise FileNotFoundError('secret-bearing exception must not be retained')
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(command, 1, output='private raw output')
        if failure == 'nonzero':
            return SimpleNamespace(returncode=1, stdout='private raw output', stderr='sensitive stderr')
        path = Path(command[command.index('--output-last-message') + 1])
        if failure == 'invalid_json':
            path.write_text('not JSON: private raw output')
        elif failure == 'invalid_schema':
            path.write_text('{}')
        elif failure == 'unsupported_binding':
            path.write_text(json.dumps({'bindings': [{'ref': 'novel', 'answer_key': 'eligibility.authorized_us'}],
                                        'next_ref': None, 'reason': 'unsupported guess'}))
        elif failure == 'terminal_click':
            path.write_text(json.dumps({'bindings': [], 'next_ref': 'submit', 'reason': 'terminal click'}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, 'run', run)
    planner = CodexPlanner(tmp_path, audit_mode=True)
    plan = planner(observation(), answers())
    assert plan == deterministic_plan(observation(), answers())
    assert planner.last_outcome['planner_outcome'] == 'deterministic_fallback'
    assert planner.last_outcome['error_kind'] == expected
    audit = (tmp_path / 'planner-audit.json').read_text()
    assert all(x not in audit for x in ['private raw output', 'sensitive stderr', 'synthetic@example.test'])


def test_optional_codex_audit_never_reuses_previous_output(tmp_path, monkeypatch):
    outputs = []
    def run(command, **kwargs):
        path = Path(command[command.index('--output-last-message') + 1]);outputs.append(path)
        if len(outputs) == 1:
            path.write_text(json.dumps(deterministic_plan(observation(), answers())))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, 'run', run)
    planner = CodexPlanner(tmp_path, audit_mode=True)
    planner(observation(), answers())
    assert planner.last_outcome['planner_outcome'] == 'codex'
    planner(observation(), answers())
    assert outputs[0] != outputs[1]
    assert planner.last_outcome['error_kind'] == 'missing_output'
    assert len(json.loads((tmp_path / 'planner-audit.json').read_text())) == 2


@pytest.mark.parametrize('label,key', [
    ('Are you authorized to work in the United States?*', 'eligibility.authorized_us'),
    ('Will you, at any point, require employer sponsorship to work in the United States?*', 'eligibility.sponsorship'),
    ('Will you require sponsorship from Synthetic Corp for employment now or in the future (e.g, H1B visa)?*', 'eligibility.sponsorship'),
    ('I am willing and able to work entirely on-site.*', 'standing.office_willingness'),
    ('When is your earliest available start date?', 'preferences.start_date'),
    ('Ideal start date in office', 'preferences.start_date'),
])
def test_approved_standing_answers_bind_observed_question_variants(label, key):
    values = {key: booklet.answer('January 2027' if key == 'preferences.start_date' else True, 'explicit synthetic standing answer')}
    field = {'ref': 'q', 'label': label, 'type': 'combobox', 'required': True}
    assert key_for_field(field, values) == key


@pytest.mark.parametrize('label', [
    'Are you currently authorized to work in the United States?*',
    'If offered employment, would you be legally eligible to begin employment immediately?',
    'Do you require sponsorship now?', 'Are you a U.S. Person for export controls?',
    'I will need relocation to work on-site.',
    'Have you ever been employed by Synthetic or any company it acquired?',
])
def test_related_but_distinct_screening_facts_are_not_reinterpreted(label):
    values = {'eligibility.authorized_us': booklet.answer(True, 'synthetic'),
              'eligibility.sponsorship': booklet.answer(True, 'synthetic'),
              'preferences.relocation': booklet.answer(True, 'synthetic'),
              'standing.office_willingness': booklet.answer(True, 'synthetic'),
              'standing.previous_employment': booklet.answer(False, 'synthetic')}
    assert key_for_field({'ref': 'q', 'label': label, 'type': 'combobox', 'required': True}, values) is None
