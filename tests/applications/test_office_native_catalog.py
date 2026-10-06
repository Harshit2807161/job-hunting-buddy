"""Closed known office controls are inspected before application planning."""
import asyncio
import copy
import pytest
from jhb.applications import booklet, known_answers, native_question_context, planner, question_routing, worker

URL='https://job-boards.greenhouse.io/synthetic-company/jobs/1234'
HYBRID='Are you able and willing to report to the office location listed in the job description, in a hybrid capacity?*'


def field(label=HYBRID):
    return {'label':label,'ref':'question_101','type':'combobox','required':True,'options':[],
            'description':'','description_truncated':False}


def answers():
    return {k:booklet.answer(True,'synthetic explicit office/relocation willingness') for k in
            ['standing.office_willingness','preferences.relocation']}


@pytest.mark.parametrize('label',[HYBRID,
    'Are you willing to work four days per week in our San Francisco office?',
    'Are you able to work from our US office three days per week?',
    'Are you able to work from our Marina Del Rey, CA office on Mondays and Thursdays (2 days/week)?'])
def test_closed_office_catalog_is_described_before_deterministic_binding(label):
    f=field(label);a=answers();calls=[];snapshot={'url':URL,'fields':[f],'buttons':[]}
    assert planner.key_for_field(f,a) is None
    assert question_routing.field_route(f,a)==question_routing.KNOWN
    assert set(known_answers.catalog_basis(f,a))==set(a)
    async def describe(control):
        calls.append(control['ref']);return {'type':'combobox','choices':['Yes','No']}
    asyncio.run(native_question_context.enrich_async(snapshot,{'url':URL},a,describe))
    assert calls==['question_101']
    known_answers.enrich(f,{},a)
    plan=planner.validate_plan(planner.deterministic_plan(snapshot,a),snapshot,a)
    assert len(plan['bindings'])==1 and a[plan['bindings'][0]['answer_key']]['value']=='Yes'


@pytest.mark.parametrize('change',['missing_office','negative_office','missing_relocation','unverified_relocation','different_question','truncated'])
def test_unknown_office_facts_or_extra_eligibility_never_trigger_probe(change):
    f=field();a=answers()
    if change=='missing_office':del a['standing.office_willingness']
    if change=='negative_office':a['standing.office_willingness']['value']=False
    if change=='missing_relocation':del a['preferences.relocation']
    if change=='unverified_relocation':a['preferences.relocation']['status']='needs_input'
    if change=='different_question':f['label']='Are you able to work from our US office without sponsorship?'
    if change=='truncated':f['description_truncated']=True
    assert not known_answers.needs_catalog(f,a)
    snapshot={'url':URL,'fields':[f]}
    native_question_context.enrich_sync(snapshot,{'url':URL},a,lambda _:pytest.fail('Unknown fact must not select a probe'))
    assert planner.key_for_field(f,a) is None


def test_full_preparation_retains_hybrid_answer_and_stops_for_review():
    class CLI:
        blocked_requests=0
        def __init__(self):self.probes=[];self.fills=[]
        def allowed_url(self,url):return url==URL
        async def open(self,url):assert url==URL
        async def observe(self):return {'url':URL,'fields':[field()],'buttons':[{'ref':'submit','label':'Submit application'}]}
        async def describe(self,f):self.probes.append(f['ref']);return {'type':'combobox','choices':['Yes','No']}
        async def fill(self,f,value):
            assert f['options']==[{'label':'Yes'},{'label':'No'}]
            self.fills.append((f['ref'],value));return {'verified':True}
        async def click_next(self,*args):pytest.fail('No terminal action is permitted')
    cli=CLI();a=answers()
    result,_=asyncio.run(worker.prepare(None,{'url':URL},a,planner.deterministic_plan,None,cli_actions=cli))
    assert result['state']=='waiting_review' and not result.get('missing')
    assert cli.fills==[('question_101','Yes')] and len(cli.probes)==2
    assert result['review_inventory']['complete'] is True
    assert result['review_inventory']['fields'][0]['choices']==['Yes','No']


@pytest.mark.parametrize('label',['How did you hear about this role?','How did you hear about this job opportunity?','How did you hear about Synthetic Company?'])
def test_discovery_probe_requires_explicit_scoped_record_and_exact_question(label):
    a={'standing.discovery_source':{**booklet.answer('Simplify','synthetic recorded source'),
         'company_question':'How did you hear about Synthetic Company?'}}
    f=field(label);snapshot={'url':URL,'fields':[f]};calls=[]
    native_question_context.enrich_sync(snapshot,{'url':URL},a,lambda control:calls.append(control['ref']) or {'type':'combobox','choices':['Simplify','Other']})
    assert calls==['question_101']
    a['standing.discovery_source']['status']='needs_input'
    assert not known_answers.needs_catalog(f,a)
    a['standing.discovery_source']['status']='verified'
    f['label']='Who personally referred you?'
    assert not known_answers.needs_catalog(f,a)


def test_blank_discovery_question_is_not_an_exact_match():
    a={'standing.discovery_source':booklet.answer('Simplify','synthetic recorded source')}
    assert not known_answers.needs_catalog(field(''),a)
