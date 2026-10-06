"""Supervisor validation has no inherited application authority or live sessions."""
import json
import os
from pathlib import Path
import sys

import pytest

from jhb import config
from jhb.applications import monitor, owned_processes
from test_monitor import setup


RUNTIME = {
    'BH_HOME': '/synthetic/live-browser-home', 'BH_REQUIRE_EXISTING_DAEMON': '1',
    'BU_CDP_WS': 'ws://127.0.0.1:9222/devtools/browser/synthetic', 'BU_CDP_URL': 'http://127.0.0.1:9222',
    'JHB_APPLICATIONS_ENABLED': '1', 'JHB_PORTAL_SUBMISSIONS_ENABLED': '1',
    'JHB_OVERNIGHT_SUBMISSIONS_ENABLED': '1', 'JHB_REQUIRE_PORTAL_APPROVAL': '1',
    'JHB_OVERNIGHT_MONITOR_ENABLED': '1', 'JHB_BROWSER_RECONNECT_ENABLED': '1',
    'JHB_APPLICATION_EMAIL': '1', 'JHB_SMTP_USER': 'synthetic@example.invalid',
    'JHB_SMTP_PASS': 'synthetic-never-send', 'JHB_TRACKER_CONFIG': '/synthetic/tracker.json',
    'JHB_AUTHORIZATION_PATH': '/synthetic/authority.json', 'COMPOSIO_API_KEY': 'synthetic-no-cloud',
    'OPENAI_API_KEY': 'synthetic-no-api', 'CODEX_API_KEY': 'synthetic-no-api',
    'ANTHROPIC_API_KEY': 'synthetic-no-api', 'GOOGLE_APPLICATION_CREDENTIALS': '/synthetic/credentials.json',
    'CODEX_HOME': '/synthetic/subscription', 'PYTHONPATH': '/synthetic/external-plugins',
    'PYTEST_ADDOPTS': '--synthetic-live-plugin', 'PYTEST_PLUGINS': 'synthetic_live_plugin',
    owned_processes.TOKEN_ENV: 'synthetic-parent-token',
}


def runtime(monkeypatch):
    for key, value in RUNTIME.items():
        monkeypatch.setenv(key, value)


def test_validation_allowlist_drops_runtime_credentials_and_keeps_tooling(monkeypatch):
    runtime(monkeypatch)
    keep = {'PATH': '/synthetic/bin', 'HOME': '/synthetic/home', 'USER': 'synthetic',
            'TMPDIR': '/synthetic/tmp', 'LANG': 'en_US.UTF-8', 'LC_ALL': 'C',
            'TZ': 'America/Los_Angeles', 'PLAYWRIGHT_BROWSERS_PATH': '/synthetic/chromium',
            'VIRTUAL_ENV': '/synthetic/venv', 'SSL_CERT_FILE': '/synthetic/cert.pem',
            'JHB_MCP_FIXTURE_NO_SANDBOX': '1'}
    for key, value in keep.items(): monkeypatch.setenv(key, value)
    before = dict(os.environ)
    env = monitor.child_environment(validation=True)
    assert not set(RUNTIME).intersection(env)
    assert all(env[key] == value for key, value in keep.items())
    assert env['CI'] == env['JHB_VALIDATION_ISOLATED'] == '1'
    assert dict(os.environ) == before


def test_repair_environment_still_has_its_existing_session_but_no_api_keys(monkeypatch):
    runtime(monkeypatch)
    env = monitor.child_environment()
    assert env['BH_HOME'] == RUNTIME['BH_HOME']
    assert env['BU_CDP_WS'] == RUNTIME['BU_CDP_WS']
    assert env['CODEX_HOME'] == RUNTIME['CODEX_HOME']
    assert env['JHB_OVERNIGHT_MONITOR_ENABLED'] == '1'
    assert 'OPENAI_API_KEY' not in env and 'CODEX_API_KEY' not in env


def test_validation_marker_blocks_default_dotenv_but_allows_explicit_synthetic_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setenv('JHB_VALIDATION_ISOLATED', '1')
    monkeypatch.delenv('SYNTHETIC_LIVE_SETTING', raising=False)
    monkeypatch.delenv('SYNTHETIC_EXPLICIT_SETTING', raising=False)
    (tmp_path/'.env').write_text('SYNTHETIC_LIVE_SETTING=must-not-load\n')
    fixture = tmp_path/'fixture.env'; fixture.write_text('SYNTHETIC_EXPLICIT_SETTING=fixture-value\n')
    config.load_dotenv()
    assert 'SYNTHETIC_LIVE_SETTING' not in os.environ
    config.load_dotenv(fixture)
    assert os.environ['SYNTHETIC_EXPLICIT_SETTING'] == 'fixture-value'
    assert 'SYNTHETIC_LIVE_SETTING' not in os.environ
    # Keep environment cleanup scoped even though the loader writes directly.
    monkeypatch.delenv('SYNTHETIC_EXPLICIT_SETTING')


def test_default_dotenv_behavior_is_unchanged_outside_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.delenv('JHB_VALIDATION_ISOLATED', raising=False)
    monkeypatch.delenv('SYNTHETIC_ORDINARY_SETTING', raising=False)
    (tmp_path/'.env').write_text('SYNTHETIC_ORDINARY_SETTING=ordinary-value\n')
    config.load_dotenv()
    assert os.environ['SYNTHETIC_ORDINARY_SETTING'] == 'ordinary-value'
    monkeypatch.delenv('SYNTHETIC_ORDINARY_SETTING')


def test_validate_marks_every_child_isolated_without_changing_parent_authority(setup):
    auth = monitor.authorization()
    calls = []
    def run(command, prefix, authority, timeout, **kwargs):
        assert authority['authorization_id'] == auth['authorization_id']
        assert kwargs['validation'] is True
        calls.append(command)
        return {'state': 'complete'}
    assert monitor.validate(auth, run)['state'] == 'complete'
    assert len(calls) == 3 and sum('-m' in command and 'pytest' in command for command in calls) == 1
    assert monitor.authorization()['authorization_id'] == auth['authorization_id']


def test_actual_validation_child_cannot_rehydrate_runtime_and_gets_fresh_owner_token(setup, monkeypatch):
    monitor.directory().mkdir()
    authority = monitor.authorization()
    runtime(monkeypatch)
    # The synthetic legacy fixture has a different gate policy than the live
    # flags deliberately injected here. Keep the parent's known authority fixed
    # while testing only what reaches the validation subprocess.
    monkeypatch.setattr(monitor, 'authorization', lambda *args: authority)
    root = setup[0]
    (root/'.env').write_text('JHB_SMTP_PASS=synthetic-env-secret\nJHB_PORTAL_SUBMISSIONS_ENABLED=1\n')
    (root/'fixture.env').write_text('SYNTHETIC_FIXTURE_SETTING=fixture-value\n')
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', '/synthetic/fixture-browser')
    project = str(Path(monitor.__file__).resolve().parents[2])
    script = f'''import json,os,sys
from pathlib import Path
sys.path.insert(0,{project!r})
from jhb import config
config.ROOT=Path.cwd()
config.load_dotenv()
assert not any(k in os.environ for k in {list(set(RUNTIME)-{owned_processes.TOKEN_ENV})!r})
assert os.environ['CI']==os.environ['JHB_VALIDATION_ISOLATED']=='1'
assert len(os.environ[{owned_processes.TOKEN_ENV!r}])==64
assert os.environ[{owned_processes.TOKEN_ENV!r}]!='synthetic-parent-token'
assert os.environ['PLAYWRIGHT_BROWSERS_PATH']=='/synthetic/fixture-browser'
config.load_dotenv(Path('fixture.env'))
assert os.environ['SYNTHETIC_FIXTURE_SETTING']=='fixture-value'
print(json.dumps({{'isolated':True,'fresh_token':True,'explicit_fixture':True}}))
'''
    result = monitor.bounded([sys.executable, '-c', script], monitor.directory()/'isolated-env',
                             authority, 5, validation=True)
    assert result['state'] == 'complete', result
    assert json.loads((monitor.directory()/'isolated-env.jsonl').read_text()) == {
        'isolated': True, 'fresh_token': True, 'explicit_fixture': True}
    assert os.environ['JHB_SMTP_PASS'] == RUNTIME['JHB_SMTP_PASS']


@pytest.mark.parametrize('value', ['0', 'true', 'arbitrary'])
def test_only_explicit_fixture_sandbox_optout_survives(value, monkeypatch):
    monkeypatch.setenv('JHB_MCP_FIXTURE_NO_SANDBOX', value)
    assert 'JHB_MCP_FIXTURE_NO_SANDBOX' not in monitor.child_environment(validation=True)
