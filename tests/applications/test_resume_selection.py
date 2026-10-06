"""Synthetic PDF comparisons; injected Codex output, no subscription/network calls."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest

from jhb import config
from jhb.applications import boards, booklet, overnight, resume_selection as selection, role_fit, worker
from tests.applications.test_cover_letter_runner import pdf

URL = 'https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555'
SDE = 'Built React TypeScript customer interfaces and reliable Java backend APIs.'
ML = 'Built PyTorch inference pipelines, retrieval systems and evaluated machine learning models.'


@pytest.fixture
def context(tmp_path, monkeypatch):
    root = config.ROOT
    (tmp_path/'schemas').mkdir()
    shutil.copy(root/'schemas/resume-selection.json', tmp_path/'schemas/resume-selection.json')
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    book = {'schema_version':1, 'answers':{}, 'roles':{}}
    for role, text in [('sde', SDE), ('ml', ML)]:
        path = pdf(tmp_path/(role+'.pdf'), text)
        book['roles'][role] = {'documents.resume':booklet.answer(str(path), 'Synthetic verified resume'),
                               'role.skills':booklet.answer(text, 'Synthetic verified resume')}
    job = {'dedupe_hash':boards.application_hash(URL), 'url':URL, 'title':'Member of Technical Staff',
           'company':'Synthetic', 'role_classes':'swe'}
    change_jd(job, 'Build efficient model inference and LLM serving systems.')
    return job, book


def change_jd(job, text):
    job['verified_job_description'] = {'status':'verified', 'text':text, 'sha256':hashlib.sha256(text.encode()).hexdigest(),
        'source_url':URL, 'job_identity':list(boards.job_identity(URL)), 'retrieved_at':time.time()}


def executor(decision, calls, *, transform=None):
    def run(command, **kwargs):
        assert command[command.index('--sandbox')+1] == 'read-only' and 'features.shell_tool=false' in command
        assert 'OPENAI_API_KEY' not in kwargs['env'] and 'CODEX_API_KEY' not in kwargs['env']
        assert kwargs['timeout'] == selection.TIMEOUT_SECONDS
        supplied = json.loads(kwargs['input'].split('EVIDENCE:\n', 1)[1]); calls.append(supplied)
        assert set(supplied['resumes']) == {'sde','ml'}
        assert SDE in supplied['resumes']['sde']['text'] and ML in supplied['resumes']['ml']['text']
        assert 'A Phase 1 category' in kwargs['input'] and 'not authority' in kwargs['input']
        verdict = {'decision':decision, 'reason':'Chosen evidence directly supports the actual core duties.',
            'jd_duties':[supplied['description']['text']], 'resume_comparisons':{
                'sde':{'evidence':[SDE], 'reason':'Web/API delivery evidence compared with the core duties.'},
                'ml':{'evidence':[ML], 'reason':'Model inference/evaluation evidence compared with the core duties.'}}}
        if transform: transform(verdict)
        Path(command[command.index('--output-last-message')+1]).write_text(json.dumps(verdict))
        return SimpleNamespace(returncode=0)
    return run


@pytest.mark.parametrize('title,description,expected', [
    ('Member of Technical Staff, New Grad', 'Build efficient model inference and LLM serving systems.', 'ml'),
    ('Software Engineer, AI Platform', 'Build React TypeScript frontend interfaces for AI tools.', 'sde'),
    ('Software Engineer', 'Train, evaluate and deploy machine learning inference models.', 'ml'),
])
def test_full_jd_and_both_actual_pdfs_drive_selection_despite_discovery_tag(context, title, description, expected):
    job, book = context; job['title'] = title; change_jd(job, description)
    original = copy.deepcopy(book); calls=[]
    result = selection.select(job, book, execute=executor(expected, calls))
    assert result['state']=='selected' and result['selected_role']==expected
    assert result['selected_resume_sha256']==hashlib.sha256(Path(book['roles'][expected]['documents.resume']['value']).read_bytes()).hexdigest()
    assert result['jd_duties']==[description] and len(calls)==1 and book==original
    packet={'job':job,'selected_role':expected,'resume_selection':result,
            'filled':[{'key':'documents.resume','value':book['roles'][expected]['documents.resume']['value']}]}
    assert selection.packet_role(job,packet,book)==expected
    assert overnight._manifest(job,packet,book)['selected_role']==expected


def test_source_timestamps_do_not_invalidate_comparison_but_either_pdf_or_job_content_does(context):
    job,book=context; calls=[]; run=executor('ml',calls)
    first=selection.select(job,book,execute=run)
    job['verified_job_description']['retrieved_at']=time.time()-2
    assert selection.select(job,book,execute=run)['evidence_hash']==first['evidence_hash'] and len(calls)==1
    pdf(Path(book['roles']['sde']['documents.resume']['value']), SDE+' Added verified backend experience.')
    assert selection.select(job,book,execute=run)['evidence_hash']!=first['evidence_hash'] and len(calls)==2
    change_jd(job,'Build model inference tooling and evaluate latency.')
    selection.select(job,book,execute=run); assert len(calls)==3


@pytest.mark.parametrize('mode',['wrong_jd_quote','invented_ml_fact','missing_sde_comparison','empty_duties'])
def test_unsupported_or_one_sided_reasoning_cannot_choose_a_resume(context,mode):
    job,book=context
    def transform(v):
        if mode=='wrong_jd_quote':v['jd_duties']=['Not in this job description']
        if mode=='invented_ml_fact':v['resume_comparisons']['ml']['evidence']=['Invented CUDA kernel work']
        if mode=='missing_sde_comparison':v['resume_comparisons']['sde']['evidence']=[]
        if mode=='empty_duties':v['jd_duties']=[]
    result=selection.select(job,book,execute=executor('ml',[],transform=transform))
    assert result['state']=='unsupported' and result['selected_role'] is None


def test_candidate_choice_wins_but_agent_prior_guess_does_not(context):
    job,book=context; key=job['dedupe_hash']; calls=[]
    book['job_role_answers']={key:booklet.answer('sde',{'method':'agent_exact_job_resume_selection'})}
    assert selection.select(job,book,execute=executor('ml',calls))['selected_role']=='ml' and len(calls)==1
    book['job_role_answers'][key]={**booklet.answer('ml',{'provider':'explicit user question response'}),'user_override':True}
    result=selection.select(job,book,requested_role='sde',execute=lambda *a,**k:pytest.fail('Explicit choice launched inference'))
    assert result['selected_role']=='ml' and result['method']=='explicit_candidate_choice'


@pytest.mark.parametrize('mode',['missing_pdf','unverified_source','empty_pdf','malformed_pdf','wrong_job','unverified_description'])
def test_incomplete_source_evidence_never_falls_back_to_title_or_tag(context,mode):
    job,book=context
    if mode=='missing_pdf':Path(book['roles']['ml']['documents.resume']['value']).unlink()
    if mode=='unverified_source':book['roles']['ml']['documents.resume']['status']='needs_input'
    if mode=='empty_pdf':pdf(Path(book['roles']['ml']['documents.resume']['value']),'')
    if mode=='malformed_pdf':Path(book['roles']['ml']['documents.resume']['value']).write_bytes(b'%PDF-invalid-stream')
    if mode=='wrong_job':job['dedupe_hash']='0'*64
    if mode=='unverified_description':job['verified_job_description']['text']='Changed without source proof'
    result=selection.select(job,book,execute=lambda *a,**k:pytest.fail('Missing evidence launched inference'))
    assert result['state']=='unsupported' and result['selected_role'] is None


def test_uncertainty_timeout_and_ci_preserve_no_selection(context,monkeypatch):
    job,book=context
    result=selection.select(job,book,execute=executor('needs_review',[]))
    assert result['state']=='waiting_input' and result['selected_role'] is None
    def timeout(*a,**k):raise subprocess.TimeoutExpired('synthetic',120)
    result=selection.select(job,book,execute=timeout)
    assert result['retryable'] is True and result['error_kind']=='TimeoutExpired'
    monkeypatch.setenv('CI','1')
    assert selection.select(job,book)['state']=='unsupported'


def test_resume_change_during_comparison_cannot_become_a_selection(context):
    job,book=context
    def changed(_):pdf(Path(book['roles']['ml']['documents.resume']['value']),ML+' Added material after inference began.')
    result=selection.select(job,book,execute=executor('ml',[],transform=changed))
    assert result['state']=='unsupported' and result['selected_role'] is None


def test_selected_bytes_cannot_change_between_choice_and_native_upload(context):
    from jhb.applications.planner import deterministic_plan
    job,book=context;record=copy.deepcopy(book['roles']['ml']['documents.resume'])
    record['resume_selection_sha256']=hashlib.sha256(Path(record['value']).read_bytes()).hexdigest()
    pdf(Path(record['value']),ML+' Changed before upload.')
    class NativeForm:
        blocked_requests=0
        def allowed_url(self,url):return True
        async def open(self,url):pass
        async def observe(self):return {'fields':[{'ref':'resume','label':'Resume','type':'file','required':True}],
                                       'buttons':[{'ref':'submit','label':'Submit Application'}]}
        async def fill(self,*args):pytest.fail('Changed resume must not reach native upload')
    with pytest.raises(ValueError,match='field repair') as failure:
        asyncio.run(worker.prepare(None,job,{'documents.resume':record},deterministic_plan,None,cli_actions=NativeForm()))
    assert 'Selected resume bytes changed' in str(failure.value.__cause__)


@pytest.mark.parametrize('change',['selected_pdf','other_pdf','description','candidate_choice','cross_job','selection_role'])
def test_submission_rejects_changed_selection_evidence_without_relabeling_packet(context,change):
    job,book=context; result=selection.select(job,book,execute=executor('ml',[]))
    packet={'job':copy.deepcopy(job),'selected_role':'ml','resume_selection':result,'filled':[]}
    original=copy.deepcopy(packet)
    if change=='selected_pdf':pdf(Path(book['roles']['ml']['documents.resume']['value']),ML+' New content.')
    if change=='other_pdf':pdf(Path(book['roles']['sde']['documents.resume']['value']),SDE+' New content.')
    if change=='description':change_jd(job,'Now build frontend application interfaces.')
    if change=='candidate_choice':book['job_role_answers']={job['dedupe_hash']:{**booklet.answer('sde','Explicit candidate selection'),'user_override':True}}
    if change=='cross_job':job['dedupe_hash']='0'*64
    if change=='selection_role':packet['resume_selection']['selected_role']='sde';original=copy.deepcopy(packet)
    with pytest.raises(ValueError):selection.packet_role(job,packet,book)
    assert packet==original


def test_legacy_packet_uses_actual_uploaded_resume_instead_of_misleading_title(context):
    job,book=context
    packet={'job':job,'filled':[{'key':'documents.resume','value':book['roles']['ml']['documents.resume']['value']}]}
    assert selection.packet_role(job,packet,book)=='ml'


def test_resume_selection_cannot_bypass_robotics_role_fit(context,monkeypatch,tmp_path):
    job,book=context; job['title']='Robotics Software Engineer - New Grad'
    change_jd(job,'Implement robot motion planning and embedded control systems.')
    monkeypatch.delenv('JHB_ROLE_FIT_REVIEW',raising=False)
    original=selection.select
    monkeypatch.setattr(selection,'select',lambda job,book,**kwargs:original(job,book,execute=executor('sde',[])))
    result,path=asyncio.run(worker.run_job(job,book,artifacts=tmp_path/'packets'))
    assert result['state']=='skipped' and result['role_fit']['unsupported_core_requirements']==['robotics']
    assert result['filled']==[] and result['resume_selection']['selected_role']=='sde'


def test_fresh_objective_citizenship_exclusion_precedes_selector(context,monkeypatch,tmp_path):
    job,book=context;change_jd(job,'US citizenship is required for this role.')
    monkeypatch.setattr(selection,'select',lambda *a,**k:pytest.fail('Excluded job reached selector'))
    result,path=asyncio.run(worker.run_job(job,book,artifacts=tmp_path/'packets'))
    assert result['state']=='skipped' and result['filled']==[]
