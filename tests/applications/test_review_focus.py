"""Explicit existing-tab focus, with no navigation or candidate mutations."""
import json
import subprocess
import pytest

from jhb.applications.cli_runtime import dispatch
from jhb.applications.cli_browser import BrowserUseCLI, MARKER

URL = 'https://job-boards.greenhouse.io/synthetic/jobs/123'
ASHBY = 'https://jobs.ashbyhq.com/synthetic/11111111-2222-3333-4444-555555555555/application'


def helpers(url=URL, kind='local', guard=True):
    state = {'url': url, 'target': 'draft', 'calls': [], 'tabs': [{'targetId': 'draft', 'url': url}]}
    def forbidden(*a, **k):
        raise AssertionError('No navigation, fields, tab creation or closing permitted')
    def js(expr):
        assert expr in {'location.href', 'window.__jhbGuard === true'}
        return state['url'] if expr == 'location.href' else guard
    result = {'cdp': forbidden, 'js': js, 'wait': forbidden,
        'daemon_browser_kind': lambda: kind, 'list_tabs': lambda: state['tabs'],
        'current_tab': lambda: {'targetId': state['target'], 'url': state['url']},
        'switch_tab': lambda target: state['calls'].append(('switch', target)),
        'activate_tab': lambda target: state['calls'].append(('activate', target)),
        'new_tab': forbidden, 'goto_url': forbidden, 'close_tab': forbidden}
    return state, result


@pytest.mark.parametrize('url', [URL, ASHBY, 'https://apply.workable.com/synthetic/j/ABC123/apply/',
    'https://jobs.lever.co/synthetic/11111111-2222-3333-4444-555555555555/apply',
    'https://example.wd5.myworkdayjobs.com/en-US/Careers/job/San-Diego/Engineer_JR123456/apply'])
def test_focus_only_exact_saved_guarded_target(url):
    state, api = helpers(url)
    assert dispatch({'operation': 'review_focus', 'url': url, 'target_id': 'draft'}, api) == {
        'state': 'focused', 'focused': True, 'guarded': True}
    assert state['calls'] == [('switch', 'draft'), ('activate', 'draft')]


@pytest.mark.parametrize('fault', ['cloud', 'unknown', 'missing', 'wrong_job', 'duplicate', 'unguarded', 'changed'])
def test_focus_fails_closed_without_new_or_wrong_tab(fault):
    state, api = helpers(kind='cloud' if fault == 'cloud' else None if fault == 'unknown' else 'cdp', guard=fault != 'unguarded')
    if fault == 'missing': state['tabs'] = []
    if fault == 'wrong_job': state['tabs'][0]['url'] = URL.replace('/123', '/999')
    if fault == 'duplicate': state['tabs'] *= 2
    if fault == 'changed':
        def switch(target): state['url'] = URL.replace('/123', '/999')
        api['switch_tab'] = switch
    with pytest.raises(ValueError): dispatch({'operation': 'review_focus', 'url': URL, 'target_id': 'draft'}, api)
    assert not any(action == 'activate' for action, _ in state['calls'])


def test_cli_focus_requires_existing_default_daemon_and_rechecks_under_lane(monkeypatch, tmp_path):
    monkeypatch.setattr('jhb.applications.cli_browser.ROOT', tmp_path)
    monkeypatch.setenv('BU_CDP_URL', 'http://127.0.0.1:12345')
    monkeypatch.setenv('BU_NAME', 'wrong-daemon')
    calls = []
    def run(self, script, env, deadline, cancelled):
        assert calls == ['revalidated']
        assert env['BH_REQUIRE_EXISTING_DAEMON'] == '1' and 'BU_NAME' not in env
        assert 'review_focus' in script and 'draft' in script
        return subprocess.CompletedProcess(['browser-use'], 0, MARKER+json.dumps({'focused': True})+'\n', '')
    monkeypatch.setattr(BrowserUseCLI, '_run', run)
    client = BrowserUseCLI(); client.target_id = 'draft'
    assert client.call('review_focus', url=URL, _before_run=lambda: calls.append('revalidated'))['focused']
    monkeypatch.setattr(BrowserUseCLI, '_run', lambda *a: pytest.fail('CLI must not run after stale evidence'))
    def stale(): raise ValueError('Draft changed')
    with pytest.raises(ValueError, match='Draft changed'): client.call('review_focus', url=URL, _before_run=stale)


@pytest.mark.parametrize('endpoint', ['https://remote.example.test:443', 'http://user:password@127.0.0.1:12345',
    'http://127.0.0.1:12345/?token=private', 'ws://remote.example.test:12345/devtools/browser/abc', ''])
def test_focus_rejects_remote_or_credential_bearing_endpoint_before_cli(monkeypatch, tmp_path, endpoint):
    monkeypatch.setattr('jhb.applications.cli_browser.ROOT', tmp_path)
    monkeypatch.delenv('BU_CDP_WS', raising=False)
    monkeypatch.setenv('BU_CDP_URL', endpoint)
    monkeypatch.setattr(BrowserUseCLI, '_run', lambda *a: pytest.fail('Unsafe endpoint must never invoke CLI'))
    with pytest.raises(ValueError): BrowserUseCLI().call('review_focus', url=URL, target_id='draft')
