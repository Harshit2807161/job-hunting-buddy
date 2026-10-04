"""Selected-role preparation routing with synthetic CLI observations only."""
import asyncio
import json

import pytest

from jhb.applications import booklet, manual_ats
from jhb.applications.planner import CodexPlanner
from jhb.applications.worker import run_job

URL='https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555'

@pytest.mark.parametrize('unknown',[False,True])
def test_worker_routes_exact_ashby_job_role_and_unknown_handoff(monkeypatch,tmp_path,unknown):
    from jhb import eligibility
    from jhb.applications import role_fit
    monkeypatch.setattr(eligibility,'assess_job',lambda job:{'state':'eligible','reason':'Synthetic verified JD','policy':'synthetic'})
    monkeypatch.setattr(role_fit,'assess',lambda job,book,role:{'state':'eligible','reason':'Synthetic role-fit dependency'})
    instances=[]
    class FixtureCLI:
        blocked_requests=0
        target_id=None
        def __init__(self,url,*,board):
            assert url==URL and board=='ashby'
            self.fills={};instances.append(self)
        def allowed_url(self,url):return url==URL
        async def open(self,url):assert url==URL
        async def observe(self):
            fields=[{'ref':'name','label':'Full Name','type':'text','required':True},
                    {'ref':'resume','label':'Resume','type':'file','required':True}]
            if unknown:fields.append({'ref':'new','label':'Are you licensed in Example State?','type':'text','required':True})
            return {'url':URL+'/application','fields':fields,'buttons':[{'ref':'submit','label':'Submit application'}]}
        async def fill(self,field,value):self.fills[field['ref']]=value
        async def describe(self,field):return {'choices':[]}
        async def click_next(self,button):raise AssertionError('Terminal action must not execute')
    monkeypatch.setattr(manual_ats,'ManualATSCLI',FixtureCLI)
    sde=tmp_path/'sde.pdf';sde.write_bytes(b'%PDF-synthetic')
    ml=tmp_path/'ml.pdf';ml.write_bytes(b'%PDF-synthetic-other')
    book={'answers':{'identity.full_name':booklet.answer('Sam Example','synthetic verified identity')},
          'roles':{'sde':{'documents.resume':booklet.answer(str(sde),'selected SDE source')},
                   'ml':{'documents.resume':booklet.answer(str(ml),'selected ML source')}}}
    job={'dedupe_hash':'a'*64,'url':URL,'title':'Software Engineer','company':'Example'}
    result,path=asyncio.run(run_job(job,book,role='sde',artifacts=tmp_path/'packets'))
    assert result['state']==('waiting_input' if unknown else 'waiting_review')
    assert instances[0].fills=={'name':'Sam Example','resume':str(sde)}
    assert result['board']=='ashby' and result['planner_skill']=='skills/prepare-ashby/SKILL.md'
    assert result['selected_role']=='sde'
    assert json.loads((path.parent/'packet.json').read_text())['selected_role']=='sde'
    if unknown:assert result['missing'][0]['question']=='Are you licensed in Example State?'
    audit=json.loads((path.parent/'planner-audit.json').read_text())
    assert audit[0]['board']=='ashby' and audit[0]['skill']=='skills/prepare-ashby/SKILL.md'
    assert 'Sam Example' not in json.dumps(audit)


def test_registered_skill_is_required_before_planning(tmp_path):
    with pytest.raises(ValueError,match='registered board planning skill'):
        CodexPlanner(tmp_path,board='invented')
