"""Read-only Apply href routing creates no popup and preserves unclaimed tabs."""
import asyncio
import hashlib
import json
import time

import pytest

from jhb import config
from jhb.applications import boards, booklet, linkedin, linkedin_runtime
from jhb.applications.tab_lifecycle import OwnedTabs, TabCapacityReached, dispatch_owned

SOURCE = 'https://www.linkedin.com/jobs/view/1234567890/'
DEST = 'https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555'
MODULE = 'jhb.applications.linkedin_runtime'


def description():
    text = 'Software Engineer: build software and work with distributed systems.'
    return {'status': 'verified', 'text': text, 'source_url': DEST, 'job_identity': list(boards.job_identity(DEST)),
            'retrieved_at': time.time(), 'sha256': hashlib.sha256(text.encode()).hexdigest()}


def resolution():
    return {'state': 'not_greenhouse', 'board_type': 'ashby', 'source_url': DEST, 'final_url': DEST,
            'application_url': DEST, 'verified_job_description': description(),
            'evidence': [{'kind': 'mcp_navigation', 'url': DEST}]}


class FixtureBrowser:
    def __init__(self, browser, apply_html):
        self.browser, self.pages, self.current = browser, {}, 'user'
        self.new_calls, self.closed, self.sessions = [], [], {}
        self.apply_html = apply_html
        for target, url in [('user', 'https://mail.google.com/'), ('unclaimed', DEST)]:
            self.add(target, url)

    def add(self, target, url):
        page = self.browser.new_page()
        page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=self.apply_html))
        page.goto(url)
        self.pages[target] = page
        self.sessions[target] = page.context.new_cdp_session(page)
        return page

    def new(self, url):
        self.new_calls.append(url)
        target = 'created-'+str(len(self.new_calls))
        self.add(target, url)
        self.current = target
        return target

    def close(self, target):
        self.closed.append(target)
        self.pages.pop(target).close()
        if self.current == target:
            self.current = 'user'

    def helpers(self):
        return {'list_tabs': lambda: [{'targetId': t, 'url': p.url} for t, p in self.pages.items()],
                'current_tab': lambda: {'targetId': self.current, 'url': self.pages[self.current].url},
                'switch_tab': lambda target: setattr(self, 'current', target), 'new_tab': self.new, 'close_tab': self.close,
                'wait_for_load': lambda: self.pages[self.current].wait_for_load_state(),
                'wait': lambda seconds: None, 'js': lambda expression: self.pages[self.current].evaluate(expression),
                'cdp': lambda method, **params: self.sessions[self.current].send(method, params),
                'click_at_xy': lambda x, y: self.pages[self.current].mouse.click(x, y)}


@pytest.fixture
def fixture_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.delenv('JHB_MAX_OWNED_TABS', raising=False)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        html = f'<h1>Software Engineer</h1><a id="apply" href="{DEST}" target="_blank">Apply</a><script>window.clicks=0;document.addEventListener("click",()=>window.clicks++)</script>'
        fixture = FixtureBrowser(browser, html)
        owner = OwnedTabs(fixture.helpers(), tmp_path)
        owner.unclaimed['unclaimed'] = {'state': 'active', 'url': DEST, 'reason': 'popup_ownership_not_proven'}
        owner.save()
        yield fixture, tmp_path
        browser.close()


def resolve(fixture):
    return dispatch_owned({'operation': 'resolve_link', 'approved_url': SOURCE}, fixture.helpers(), linkedin_runtime.dispatch,
                          dispatcher_name=MODULE)


def proof_path(root, result):
    path = root/'private'/'source-checks'/'proof.json'
    booklet.write_private(path, {'provider': 'readonly_linkedin_apply_href_and_isolated_mcp', 'recorded_at': time.time(),
        'native_apply_clicked': False, 'source_url': result['source_url'], 'source_target_id': result['source_target_id'],
        'observed_apply_url': result['application_url'], 'resolved': resolution()})
    return path


def cleanup(fixture, path):
    return dispatch_owned({'operation': 'cleanup_source_verified', 'approved_url': SOURCE, 'evidence_path': str(path),
                          'evidence_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}, fixture.helpers(), linkedin_runtime.dispatch,
                          dispatcher_name=MODULE)


def test_real_ax_link_is_read_without_click_popup_or_unclaimed_guard_bypass(fixture_browser):
    fixture, root = fixture_browser
    result = resolve(fixture)
    assert result['state'] == 'observed_link' and result['application_url'] == DEST
    assert result['native_apply_clicked'] is False
    assert fixture.pages[result['source_target_id']].evaluate('window.clicks') == 0
    assert fixture.new_calls == [SOURCE] and len(fixture.pages) == 3
    owner = OwnedTabs(fixture.helpers(), root)
    assert owner.tabs[result['source_target_id']]['purpose'] == 'source_readonly'
    assert owner.unclaimed['unclaimed']['state'] == 'active'
    with pytest.raises(TabCapacityReached):
        owner.before_apply_click()  # Native source clicks still fail closed.


def test_fresh_jd_proof_closes_only_exact_proven_source_and_preserves_popup(fixture_browser):
    fixture, root = fixture_browser
    result = resolve(fixture)
    fixture.current = 'user'
    assert cleanup(fixture, proof_path(root, result))['closed_targets'] == [result['source_target_id']]
    assert fixture.closed == [result['source_target_id']]
    assert set(fixture.pages) == {'user', 'unclaimed'} and fixture.current == 'user'


@pytest.mark.parametrize('problem', ['user_source', 'changed_source', 'changed_job', 'stale_jd', 'closed_job', 'no_mcp', 'bad_hash', 'source_mismatch'])
def test_incomplete_destination_proof_never_closes_source_or_unknown_tab(fixture_browser, problem):
    fixture, root = fixture_browser
    if problem == 'user_source':
        fixture.add('existing-source', SOURCE)
    result = resolve(fixture)
    path = proof_path(root, result)
    proof = json.loads(path.read_bytes())
    if problem == 'changed_source': fixture.pages[result['source_target_id']].goto('https://example.test/')
    elif problem == 'changed_job': proof['resolved']['application_url'] = DEST.replace('11111111', '22222222')
    elif problem == 'stale_jd': proof['resolved']['verified_job_description']['retrieved_at'] -= 90000
    elif problem == 'closed_job': proof['resolved']['closed'] = True
    elif problem == 'no_mcp': proof['resolved']['evidence'] = []
    elif problem == 'bad_hash': proof['resolved']['verified_job_description']['sha256'] = 'f'*64
    elif problem == 'source_mismatch': proof['source_url'] = SOURCE.replace('1234567890', '9999999999')
    booklet.write_private(path, proof)
    assert cleanup(fixture, path)['closed_targets'] == []
    assert fixture.closed == [] and 'unclaimed' in fixture.pages


def test_unknown_popup_counts_toward_capacity_and_exact_source_reuse_adds_none(fixture_browser, monkeypatch):
    fixture, root = fixture_browser
    monkeypatch.setenv('JHB_MAX_OWNED_TABS', '1')
    with pytest.raises(TabCapacityReached): resolve(fixture)
    assert fixture.new_calls == []
    fixture.add('existing-source', SOURCE)
    assert resolve(fixture)['state'] == 'observed_link'
    assert fixture.new_calls == []
    assert OwnedTabs(fixture.helpers(), root).tabs == {}


@pytest.mark.parametrize('apply', ['<button>Apply</button>', '<a href="https://127.0.0.1/admin">Apply</a>', '<a href="http://jobs.example.test/job">Apply</a>'])
def test_read_only_path_never_clicks_button_or_unsafe_href(fixture_browser, apply):
    fixture, _ = fixture_browser
    fixture.apply_html = apply+'<script>window.clicks=0;document.addEventListener("click",()=>window.clicks++)</script>'
    result = resolve(fixture)
    assert result['state'] == 'ambiguous'
    assert result['native_apply_required'] is ('<button' in apply)
    assert fixture.pages[fixture.current].evaluate('window.clicks') == 0


class Client:
    def __init__(self, result): self.result, self.calls = result, []
    async def invoke(self, operation, **values):
        self.calls.append((operation, values))
        return self.result if operation == 'resolve_link' else {'closed_targets': ['source']}


def test_cli_href_flows_to_exact_isolated_verifier_before_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    cli = Client({'state': 'observed_link', 'application_url': DEST, 'source_target_id': 'source', 'source_url': SOURCE, 'evidence': []})
    observed = []
    async def check(job, **kwargs):
        observed.append(job['url'])
        return resolution()
    from jhb.applications import greenhouse_source
    monkeypatch.setattr(greenhouse_source, 'resolve_job', check)
    result = asyncio.run(linkedin.resolve_source({'url': SOURCE}, client=cli))
    assert observed == [DEST]
    assert [c[0] for c in cli.calls] == ['resolve_link', 'cleanup_source_verified']
    assert result['resolution_transport'] == 'authenticated_browser_use_cli_readonly_href'
    path = result['readonly_source_evidence']
    from pathlib import Path
    assert Path(path).stat().st_mode & 0o777 == 0o600


def test_unverified_or_closed_isolated_destination_preserves_source(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    cli = Client({'state': 'observed_link', 'application_url': DEST, 'source_target_id': 'source', 'source_url': SOURCE, 'evidence': []})
    async def check(job, **kwargs): return {'state': 'blocked', 'reason': 'Site verification required'}
    from jhb.applications import greenhouse_source
    monkeypatch.setattr(greenhouse_source, 'resolve_job', check)
    result = asyncio.run(linkedin.resolve_source({'url': SOURCE}, client=cli))
    assert result['state'] == 'blocked'
    assert [c[0] for c in cli.calls] == ['resolve_link']


@pytest.mark.parametrize("path", ["/safety/go/", "/redir/redirect", "/jobs/redirect"])
def test_observed_first_party_apply_redirects_decode_one_public_href(fixture_browser, path):
    from urllib.parse import quote
    fixture, _ = fixture_browser
    href = "https://www.linkedin.com"+path+"?url="+quote(DEST, safe="")
    fixture.apply_html = f'<a href="{href}">Apply</a><script>window.clicks=0</script>'
    result = resolve(fixture)
    assert result["state"] == "observed_link" and result["application_url"] == DEST
    assert len(fixture.new_calls) == 1 and len(fixture.pages) == 3
