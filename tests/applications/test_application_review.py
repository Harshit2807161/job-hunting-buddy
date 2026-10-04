from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jhb import config
from jhb.applications import application_review as review, booklet


@pytest.fixture
def context(tmp_path, monkeypatch):
    schema=json.loads((config.ROOT/'schemas/application-review.json').read_text())
    monkeypatch.setattr(config,'ROOT',tmp_path)
    (tmp_path/'schemas').mkdir()
    (tmp_path/'schemas/application-review.json').write_text(json.dumps(schema))
    bookpath=tmp_path/'private/answer-booklet.json'
    booklet.write_private(bookpath, {'schema_version':1,'answers':{'identity.email':booklet.answer('sam@example.org','synthetic resume')},
        'roles':{'sde':{},'ml':{}},'custom_answers':{
            'custom.other':{'scope':{'ats':'ashby','region':'global','board':'other'},'value':'PRIVATE_OTHER_EMPLOYER'},
            'custom.here':{'scope':{'ats':'ashby','region':'global','board':'example'},'value':'CURRENT_EMPLOYER'}},
        'education_records':[{'school':'Example University','degree':"Master's",'major':'CS','end_date':'2027-12-14','expected':True,'status':'verified','source':'synthetic resume'}]})
    job={'dedupe_hash':'a'*64,'url':'https://jobs.ashbyhq.com/example/11111111-1111-1111-1111-111111111111/application','title':'Engineer','company':'Example'}
    auth={'authorization_id':'synthetic','expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}
    return job,{'selected_role':'sde','documents':{},'filled':[]},{'fields':[],'retained':[]},auth,bookpath


def fake_result(verdict):
    def execute(command,**kwargs):
        assert command[command.index('--sandbox')+1]=='read-only'
        assert 'features.shell_tool=false' in command
        assert 'OPENAI_API_KEY' not in kwargs['env'] and 'CODEX_API_KEY' not in kwargs['env']
        assert 'PRIVATE_OTHER_EMPLOYER' not in kwargs['input']
        assert 'CURRENT_EMPLOYER' in kwargs['input']
        assert '2027-12-14' in kwargs['input'] and '"expected": true' in kwargs['input']
        Path(command[command.index('--output-last-message')+1]).write_text(json.dumps(verdict))
        return SimpleNamespace(returncode=0)
    return execute


def test_independent_verdict_is_bound_to_exact_snapshot_with_private_evidence(context, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-key');monkeypatch.setenv('CODEX_API_KEY','synthetic-key')
    job,manifest,checks,auth,path=context
    r=review.review_application(job,manifest,checks,auth,book_path=path,
        execute=fake_result({'verdict':'approved','issues':[],'summary':'Consistent verified evidence'}))
    assert r['verdict']=='approved' and r['snapshot_sha256']==review.snapshot_digest(checks)
    assert r['job_hash']==job['dedupe_hash'] and r['authorization_id']==auth['authorization_id']
    assert review.snapshot_digest({**checks,'retained':[{'value':'changed'}]})!=r['snapshot_sha256']
    for p in (config.ROOT/'private/application-reviews'/job['dedupe_hash']).glob('*.json'):
        assert p.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('verdict',[
    {'verdict':'approved','issues':[{'field_ref':'degree','reason':'Future degree marked completed'}],'summary':'Contradiction'},
    {'verdict':'reject','issues':[],'summary':'Factual contradiction'},
])
def test_no_approval_when_reviewer_finds_contradiction(context,verdict):
    job,manifest,checks,auth,path=context
    assert review.review_application(job,manifest,checks,auth,book_path=path,execute=fake_result(verdict))['verdict']=='reject'


def test_bad_schema_and_cli_failures_never_approve(context):
    job,manifest,checks,auth,path=context
    r=review.review_application(job,manifest,checks,auth,book_path=path,execute=fake_result({'verdict':'approved'}))
    assert r['verdict']=='handoff'
    def unavailable(*args,**kwargs):raise FileNotFoundError('synthetic')
    assert review.review_application(job,manifest,checks,auth,book_path=path,execute=unavailable)['verdict']=='handoff'


def test_ci_cannot_launch_subscription_reviewer(context,monkeypatch):
    monkeypatch.setenv('CI','true')
    assert review.review_application(*context[:4])['verdict']=='handoff'
