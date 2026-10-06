import asyncio
import copy
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from jhb.applications import boards, booklet, cover_letter_runner as letters, worker, planner, pipeline

URL = 'https://job-boards.greenhouse.io/synthetic/jobs/123'
FIELD = {'ref':'cover_letter','label':'Cover Letter','type':'file','required':True,'options':[]}
TEMPLATE = r'''\documentclass{article}
\begin{document}
Dear OriginalCo Recruiting Team,

I am writing to express interest in the Software Engineer position at OriginalCo. I believe OriginalCo's work in cloud analytics is relevant to my interests.

Protected experience: 10.55\%, 50\%, 30\%, 3,000 students, 1,400 rooms, 15 minutes.

Protected skills: Python and distributed systems.

I am particularly interested in contributing to OriginalCo's cloud platform. I am available starting in January 2027.
\vspace{1em}
Synthetic Candidate
\end{document}
'''


def pdf(path, text='Synthetic resume: Python and cloud software', pages=1):
    writer=PdfWriter()
    for _ in range(pages):
        page=writer.add_blank_page(612,792)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream=DecodedStreamObject();stream.set_data(('BT /F1 12 Tf 20 750 Td ('+text.replace('(','').replace(')','')+') Tj ET').encode())
        page[NameObject('/Contents')]=writer._add_object(stream)
    with path.open('wb') as output:writer.write(output)
    return path


@pytest.fixture
def setup_letter(tmp_path):
    folder=tmp_path/'source'/'sde-roles';folder.mkdir(parents=True)
    resume=pdf(folder/'SyntheticResume.pdf')
    template=folder/'cover-letter-ref.tex';template.write_text(TEMPLATE)
    skill=folder.parent/'SKILL.md';skill.write_text('Keep the template intact except company, role and why-them zones. Preserve January 2027. Deliver only company PDF beside selected resume.')
    records={'documents.resume':booklet.answer(str(resume),'Synthetic original resume'),
        'documents.cover_template':booklet.answer(str(template),'Synthetic immutable reference'),
        'documents.cover_letter':booklet.answer()}
    book={'schema_version':1,'roles':{'sde':records,'ml':copy.deepcopy(records)},'answers':{},'sources':[
        {'role':'sde','path':str(resume),'sha256':hashlib.sha256(resume.read_bytes()).hexdigest()}],
        'cover_letter_skill':str(skill),'custom_answers':{}}
    text='SyntheticCo builds cloud analytics software for enterprise teams. The Software Engineer role develops Python distributed systems.'
    job={'dedupe_hash':boards.application_hash(URL),'url':URL,'company':'SyntheticCo','title':'Software Engineer','role_classes':'swe',
        'verified_job_description':{'text':text,'status':'verified','retrieved_at':time.time(),
            'source_url':'https://boards-api.greenhouse.io/v1/boards/synthetic/jobs/123',
            'sha256':hashlib.sha256(text.encode()).hexdigest()}}
    directory=tmp_path/'private'/'applications'/job['dedupe_hash'];directory.mkdir(parents=True)
    book_path=tmp_path/'private'/'booklet.json';booklet.write_private(book_path,book)
    calls=[]
    def execute(command,**kwargs):
        calls.append((command,kwargs))
        output=Path(command[command.index('--output-last-message')+1])
        data=({key:True for key in letters.CHECKS} if '--image' in command else {
            'why_opening':"I believe SyntheticCo's cloud analytics work offers a concrete setting for reliable software.",
            'why_closing':"I am particularly interested in contributing to SyntheticCo's cloud analytics platform."})
        output.write_text(json.dumps(data))
        return SimpleNamespace(returncode=0,stdout='')
    def compiler(template,replacements,output):
        assert 'Protected experience' in letters.tailor_text(template.read_text(),replacements)
        pdf(output,'Synthetic compiled one-page letter')
        return output
    def renderer(path,directory):
        target=directory/'preview.png';Image.new('RGB',(600,800),'white').save(target)
        return target
    runner=letters.CoverLetterRunner(job,book,'sde',directory,book_path=book_path,execute=execute,compiler=compiler,renderer=renderer)
    return runner,book,job,calls,template,resume,book_path


def test_generation_uses_skill_independent_image_review_delivery_and_immutable_upload_snapshot(setup_letter):
    runner,book,job,calls,template,resume,path=setup_letter
    original=template.read_bytes();resume_original=resume.read_bytes()
    result=runner.generate(FIELD)
    assert result['state']=='verified' and len(calls)==2
    record=result['record'];source=record['source'];snapshot=Path(record['value'])
    assert snapshot.parent.parent.name=='cover-letter' and snapshot.name=='SyntheticCo.pdf'
    assert Path(source['delivered_path']).parent==resume.parent
    assert snapshot.read_bytes()==Path(source['delivered_path']).read_bytes()
    assert template.read_bytes()==original and resume.read_bytes()==resume_original
    assert source['selected_role']=='sde' and source['resume_sha256']==hashlib.sha256(resume_original).hexdigest()
    assert source['visual_review']=={key:True for key in letters.CHECKS}
    assert booklet.load(path)['job_document_answers'][job['dedupe_hash']]['documents.cover_letter']==record
    assert record['proposed'] is True
    for command,kwargs in calls:
        assert '--sandbox' in command and 'read-only' in command and 'mcp_servers={}' in command and 'features.shell_tool=false' in command
        assert 'OPENAI_API_KEY' not in kwargs['env'] and kwargs['timeout']==90
    assert '--image' in calls[1][0] and book['cover_letter_skill'] not in str(calls[0][0])
    cached=runner.generate(FIELD)
    assert cached['record']['value']==record['value'] and len(calls)==2


def test_distinct_jobs_keep_immutable_snapshots_and_back_up_different_company_pdf(setup_letter):
    runner,book,job,calls,template,resume,path=setup_letter
    previous=pdf(resume.parent/'SyntheticCo.pdf','Synthetic previous company letter').read_bytes()
    first=runner.generate(FIELD);first_bytes=Path(first['record']['value']).read_bytes()
    backups=list(Path(first['record']['value']).parent.glob('previous-company-*.pdf'))
    assert len(backups)==1 and backups[0].read_bytes()==previous
    second_url=URL[:-3]+'124'
    second_job={**job,'dedupe_hash':boards.application_hash(second_url),'url':second_url,'title':'Backend Engineer',
                'verified_job_description':{**job['verified_job_description'],'source_url':job['verified_job_description']['source_url'][:-3]+'124'}}
    second_dir=runner.directory.parent/second_job['dedupe_hash'];second_dir.mkdir()
    other=letters.CoverLetterRunner(second_job,book,'sde',second_dir,execute=runner.execute,
        compiler=lambda t,r,p:pdf(p,'Synthetic different role letter'),renderer=runner.renderer)
    second=other.generate(FIELD)
    assert second['state']=='verified' and second['record']['value']!=first['record']['value']
    assert Path(first['record']['value']).read_bytes()==first_bytes


@pytest.mark.parametrize('fault',['stale_jd','wrong_jd_identity','resume_hash','wrong_role','missing_skill','changed_template','overflow','visual_failure','tool_attempt'])
def test_unverified_documents_are_agent_tasks_never_candidate_text_prompts(setup_letter,fault):
    runner,book,job,calls,template,resume,path=setup_letter
    if fault=='stale_jd':job['verified_job_description']['retrieved_at']=time.time()-90000
    elif fault=='wrong_jd_identity':job['verified_job_description']['source_url']='https://boards-api.greenhouse.io/v1/boards/other/jobs/123'
    elif fault=='resume_hash':book['sources'][0]['sha256']='f'*64
    elif fault=='wrong_role':runner.role='ml'
    elif fault=='missing_skill':book['cover_letter_skill']=str(template.parent/'missing.md')
    elif fault=='changed_template':
        def compile_changed(t,r,p):pdf(p);t.write_text(t.read_text()+'changed')
        runner.compiler=compile_changed
    elif fault=='overflow':runner.compiler=lambda t,r,p:pdf(p,pages=2)
    else:
        original=runner.execute
        def reject(command,**kwargs):
            result=original(command,**kwargs)
            if fault=='visual_failure' and '--image' in command:
                Path(command[command.index('--output-last-message')+1]).write_text(json.dumps({key:False for key in letters.CHECKS}))
            if fault=='tool_attempt':result.stdout=json.dumps({'item':{'type':'command_execution'}})
            return result
        runner.execute=reject
    result=runner.generate(FIELD)
    assert result['state']=='agent_task' and result.get('record') is None
    assert 'job_document_answers' not in booklet.load(path)
    assert not (resume.parent/'SyntheticCo.pdf').exists()


def test_no_ai_guidance_and_nonletter_uploads_never_invoke_generator(setup_letter):
    runner,book,job,calls,*_=setup_letter
    assert runner.generate({**FIELD,'description':'Please write this cover letter in your own words without AI.'})['state']=='candidate_input'
    assert runner.generate({**FIELD,'label':'Portfolio'})['state']=='not_applicable'
    assert runner.generate({**FIELD,'description_truncated':True})['state']=='agent_task'
    assert calls==[]


def test_ci_has_no_subscription_engine_access(setup_letter,monkeypatch):
    runner,*_=setup_letter;runner.execute=None;monkeypatch.setenv('CI','true')
    assert runner.generate(FIELD)=={'state':'agent_task','reason_code':'document_engine_ci_disabled'}


def test_standalone_command_in_ci_never_reads_candidate_data(monkeypatch,capsys):
    monkeypatch.setenv('CI','true')
    letters.main(['--job-file','/synthetic/not-accessed.json','--role','sde'])
    assert json.loads(capsys.readouterr().out)['reason_code']=='ci_disabled'


def test_tampered_verified_snapshot_is_held_without_overwriting_or_new_registration(setup_letter):
    runner,book,job,calls,template,resume,book_path=setup_letter
    first=runner.generate(FIELD);path=Path(first['record']['value'])
    path.write_bytes(b'not the approved PDF')
    second=runner.generate(FIELD)
    assert second['state']=='agent_task' and len(calls)==2
    assert path.read_bytes()==b'not the approved PDF'
    assert booklet.load(book_path)['job_document_answers'][job['dedupe_hash']]['documents.cover_letter']==first['record']


def test_existing_verified_or_explicitly_declined_document_never_starts_worker_generation(setup_letter):
    runner,book,job,*_=setup_letter
    verified=runner.generate(FIELD)['record']
    class NoGeneration:
        def generate(self,field):pytest.fail('Unexpected generation for a known or declined document')
    class CLI:
        blocked_requests=0
        def allowed_url(self,u):return True
        async def open(self,u):pass
        async def observe(self):return {'url':URL,'fields':[{**FIELD,'required':False}],
            'buttons':[{'ref':'submit','label':'Submit application'}]}
        async def fill(self,field,value):assert value==verified['value']
        async def click_next(self,*args):pytest.fail('Unexpected terminal action')
    for answer in [verified, {**booklet.answer(None,{'provider':'explicit user question response','question_id':'q_synthetic'},'declined'),
                              'question':'Cover Letter','field_ref':FIELD['ref']}]:
        answers={'documents.cover_letter':booklet.answer(),'custom.cover':answer} if answer['status']=='declined' else {'documents.cover_letter':answer}
        result,_=asyncio.run(worker.prepare(None,job,answers,planner.deterministic_plan,None,cli_actions=CLI(),document_runner=NoGeneration()))
        assert result['state']=='waiting_review' and result['agent_tasks']==[]


def test_transport_outage_is_bounded_retryable_document_task(setup_letter):
    runner,*_=setup_letter
    def failed(*a,**k):raise subprocess.TimeoutExpired('synthetic codex',90)
    runner.execute=failed
    result=runner.generate(FIELD)
    assert result['state']=='agent_task' and result['retryable'] is True


def test_worker_generates_only_observed_letter_then_uploads_verified_pdf_without_submit(setup_letter):
    runner,book,job,*_=setup_letter
    class CLI:
        blocked_requests=0
        def __init__(self):self.fills=[]
        def allowed_url(self,u):return u==URL
        async def open(self,u):pass
        async def observe(self):return {'url':URL,'fields':[FIELD],'buttons':[{'ref':'submit','label':'Submit application'}]}
        async def fill(self,field,value):self.fills.append((field['ref'],value))
        async def click_next(self,*args):pytest.fail('Unexpected submission')
    cli=CLI();answers=booklet.for_role(book,'sde')
    result,_=asyncio.run(worker.prepare(None,job,answers,planner.deterministic_plan,None,cli_actions=cli,document_runner=runner))
    assert result['state']=='waiting_review' and result['review_inventory']['complete'] is True
    assert len(cli.fills)==1 and Path(cli.fills[0][1]).name=='SyntheticCo.pdf'
    assert not result.get('missing')
    assert result['agent_tasks']==[] and result['generated_documents']['documents.cover_letter']['status']=='verified'


def test_worker_document_outage_preserves_other_fills_and_never_asks_candidate_for_path():
    class Runner:
        def generate(self,field):return {'state':'agent_task','reason_code':'cover_letter_generation_unverified','retryable':True}
    class CLI:
        blocked_requests=0
        def allowed_url(self,u):return True
        async def open(self,u):pass
        async def observe(self):return {'url':URL,'fields':[{'ref':'first','label':'First Name','type':'text','required':True,'options':[]},FIELD],
            'buttons':[{'ref':'submit','label':'Submit application'}]}
        async def fill(self,field,value):assert field['ref']=='first'
        async def click_next(self,*args):pytest.fail('Unexpected continuation/submission')
    result,_=asyncio.run(worker.prepare(None,{'url':URL},{'identity.first_name':booklet.answer('Synthetic','Synthetic original')},
        planner.deterministic_plan,None,cli_actions=CLI(),document_runner=Runner()))
    assert result['state']=='failed' and result['missing']==[] and len(result['filled'])==1
    assert result['agent_tasks'][0]['task_kind']=='document_generation'
    assert result['agent_tasks'][0]['answer_key']=='documents.cover_letter'
    assert pipeline._recoverable(result) is True


def test_combined_portfolio_or_cover_label_only_maps_file_documents():
    answers={'documents.cover_letter':booklet.answer('/synthetic/Company.pdf','Synthetic verified PDF')}
    assert planner.key_for_field({**FIELD,'label':'Portfolio or Cover Letter'},answers)=='documents.cover_letter'
    assert planner.key_for_field({**FIELD,'label':'Portfolio or Cover Letter','type':'textarea'},answers) is None


@pytest.mark.skipif(not shutil.which('xelatex') or not shutil.which('pdftoppm'),reason='Local PDF tools unavailable')
def test_real_compiler_and_png_renderer_keep_one_page_and_reference_unchanged(setup_letter):
    runner,book,job,calls,template,resume,path=setup_letter
    runner.compiler=letters.compile_letter;runner.renderer=letters.render_preview
    original=template.read_bytes();result=runner.generate(FIELD)
    assert result['state']=='verified' and len(PdfReader(result['record']['value']).pages)==1
    assert template.read_bytes()==original
    text=PdfReader(result['record']['value']).pages[0].extract_text()
    assert 'January 2027' in text and 'SyntheticCo' in text and 'Protected skills' in text
    assert Path(result['preview']).is_file()


def explicit_availability(value='2026-12-14'):
    return booklet.answer(value, {'provider':'explicit user response', 'answered_at':'2026-10-04T23:40:52+00:00',
        'question':'What is your exact start date?', 'scope':'updated candidate availability',
        'reply':'December 14; update my availability', 'year_context':2026})


def with_availability(setup_letter):
    runner,book,job,calls,template,resume,path=setup_letter
    book['answers']['preferences.start_date']=explicit_availability()
    booklet.write_private(path,book)
    def compiler(template,replacements,output,*,availability=None):
        text=letters.tailor_text(template.read_text(),replacements,availability=availability)
        assert 'Protected experience' in text and 'starting on December 14, 2026' in text
        return pdf(output,'Synthetic letter; starting on '+availability['display_date'])
    runner.compiler=compiler
    return runner,book,job,calls,template,resume,path


@pytest.mark.parametrize('source',[
    'resume says December 2026', {'provider':'assistant','answered_at':'2026-10-04T23:40:52+00:00','scope':'availability'},
    {'provider':'explicit user response','answered_at':'2026-10-04T23:40:52+00:00','scope':'expected graduation'},
    {'provider':'explicit user response','scope':'availability'},
    {'provider':'explicit user response','answered_at':'2026-10-04T23:40:52','scope':'availability'},
])
def test_only_timestamped_explicit_availability_authorizes_template_exception(source):
    answers={'preferences.start_date':booklet.answer('2026-12-14',source),
             'education.expected_graduation_date':explicit_availability()}
    assert letters.availability_from_answers(answers) is None
    record=explicit_availability();record['status']='needs_input'
    assert letters.availability_from_answers({'preferences.start_date':record}) is None


def test_trusted_override_is_separate_from_model_replacements_and_protected_template_content():
    override=letters.availability_from_answers({'preferences.start_date':explicit_availability()})
    result=letters.tailor_text(TEMPLATE,{},availability=override)
    assert result==TEMPLATE.replace('starting in January 2027','starting on December 14, 2026')
    with pytest.raises(ValueError):letters.tailor_text(TEMPLATE,{'availability':'December 14, 2026'})
    changed=copy.deepcopy(override);changed['display_date']='December 1, 2026'
    with pytest.raises(ValueError):letters.tailor_text(TEMPLATE,{},availability=changed)
    with pytest.raises(ValueError):letters.tailor_text(TEMPLATE.replace('starting in January 2027','starting whenever'),{},availability=override)


def test_override_manifest_pdf_review_and_registration_share_exact_answer_proof(setup_letter):
    runner,book,job,calls,template,resume,path=with_availability(setup_letter)
    reference=template.read_bytes();result=runner.generate(FIELD)
    assert result['state']=='verified'
    record=result['record'];source=record['source'];proof=letters.availability_from_answers(book['answers'])
    assert source['availability_override']==proof and source['availability_sha256']
    assert source['availability_override']['record']['source']==explicit_availability()['source']
    assert letters.document_available(record,answers=book['answers'])
    assert 'December 14, 2026' in PdfReader(record['value']).pages[0].extract_text()
    assert 'January 2027' not in PdfReader(record['value']).pages[0].extract_text()
    assert 'availability_override' in calls[1][1]['input']
    assert 'supersedes the source skill date' in calls[1][1]['input']
    assert source['visual_review']['availability_matches_verified_answer'] is True
    assert template.read_bytes()==reference
    assert booklet.load(path)['job_document_answers'][job['dedupe_hash']]['documents.cover_letter']==record


def test_new_user_availability_invalidates_old_letter_without_mutating_old_snapshot(setup_letter):
    runner,book,job,calls,template,resume,path=setup_letter
    old=runner.generate(FIELD)['record'];old_bytes=Path(old['value']).read_bytes()
    runner,book,*_=with_availability(setup_letter)
    assert not letters.document_available(old,answers=book['answers'])
    new=runner.generate(FIELD)['record']
    assert new['value']!=old['value'] and Path(old['value']).read_bytes()==old_bytes
    assert letters.document_available(new,answers=book['answers'])
    revised=copy.deepcopy(book['answers']);revised['preferences.start_date']['source']['answered_at']='2026-10-05T00:00:00+00:00'
    assert not letters.document_available(new,answers=revised)
    assert len(calls)==4


def test_renderer_or_compiler_cannot_silently_preserve_the_stale_pdf_date(setup_letter):
    runner,book,job,calls,template,resume,path=with_availability(setup_letter)
    runner.compiler=lambda t,r,p,**kwargs:pdf(p,'Synthetic letter starting in January 2027')
    result=runner.generate(FIELD)
    assert result['state']=='agent_task' and result['reason_code']=='cover_letter_compile_unverified'
    assert len(calls)==1 and not (resume.parent/'SyntheticCo.pdf').exists()
    assert 'job_document_answers' not in booklet.load(path)


def test_changed_answer_during_generation_prevents_delivery_and_registration(setup_letter):
    runner,book,job,calls,template,resume,path=with_availability(setup_letter)
    original_renderer=runner.renderer
    def renderer(pdf_path,directory):
        preview=original_renderer(pdf_path,directory)
        latest=booklet.load(path);latest['answers']['preferences.start_date']=explicit_availability('2027-01-15')
        booklet.write_private(path,latest);return preview
    runner.renderer=renderer
    result=runner.generate(FIELD)
    assert result['state']=='agent_task' and result['reason_code']=='cover_letter_delivery_unverified'
    assert not (resume.parent/'SyntheticCo.pdf').exists() and 'job_document_answers' not in booklet.load(path)


def test_worker_regenerates_stale_generated_document_before_upload(setup_letter):
    runner,book,job,calls,template,resume,path=setup_letter
    old=runner.generate(FIELD)['record']
    runner,book,*_=with_availability(setup_letter)
    class CLI:
        blocked_requests=0
        def __init__(self):self.uploads=[]
        def allowed_url(self,url):return url==URL
        async def open(self,url):assert url==URL
        async def observe(self):return {'url':URL,'fields':[FIELD],'buttons':[{'ref':'submit','label':'Submit application'}]}
        async def fill(self,field,value):self.uploads.append(value);assert value!=old['value']
        async def click_next(self,*args):pytest.fail('No terminal action')
    cli=CLI();answers=booklet.for_role(book,'sde');answers['documents.cover_letter']=old
    result,_=asyncio.run(worker.prepare(None,job,answers,planner.deterministic_plan,None,cli_actions=cli,document_runner=runner))
    assert result['state']=='waiting_review' and len(cli.uploads)==1
    assert 'December 14, 2026' in PdfReader(cli.uploads[0]).pages[0].extract_text()


@pytest.mark.skipif(not shutil.which('xelatex') or not shutil.which('pdftoppm'),reason='Local PDF tools unavailable')
def test_real_compiler_availability_override_changes_only_copy_and_keeps_one_page(setup_letter):
    runner,book,job,calls,template,resume,path=with_availability(setup_letter)
    runner.compiler=letters.compile_letter;runner.renderer=letters.render_preview
    original=template.read_bytes();result=runner.generate(FIELD)
    assert result['state']=='verified' and len(PdfReader(result['record']['value']).pages)==1
    text=PdfReader(result['record']['value']).pages[0].extract_text()
    assert 'December 14, 2026' in text and 'January 2027' not in text
    assert template.read_bytes()==original


@pytest.mark.parametrize('text', [
    'Synthetic letter starting on December 14, 2026.',
    'SyntheticletterstartingonDecember14,2026.',
    'Synthetic letter starting on December\n14, 2026.',
    'Synthetic letter startingonDecember14,\u00a02026.',
])
def test_pdf_date_validation_tolerates_only_extraction_whitespace(text):
    proof=letters.availability_from_answers({'preferences.start_date':explicit_availability()})
    assert letters._compiled_availability_matches(text,proof)


@pytest.mark.parametrize('text', [
    'SyntheticletterstartingonDecember15,2026.',
    'SyntheticletterstartingonDecember14,2027.',
    'SyntheticletterstartingonDecember14,20260.',
    'SyntheticletterstartinginJanuary2027.',
    'SyntheticletterstartingonDecember14,2026. AlsoavailableJanuary2027.',
    'December14,2026 is graduation; starting later.',
    'startingonDecember14,2026 and startingonDecember14,2026.',
])
def test_pdf_date_validation_rejects_changed_conflicting_or_nonavailability_dates(text):
    proof=letters.availability_from_answers({'preferences.start_date':explicit_availability()})
    assert not letters._compiled_availability_matches(text,proof)


def test_real_extraction_shape_proceeds_to_independent_review_and_registration(setup_letter):
    runner,book,job,calls,template,resume,path=with_availability(setup_letter)
    runner.compiler=lambda t,r,p,**kwargs:pdf(p,'SyntheticletterstartingonDecember14,2026.')
    result=runner.generate(FIELD)
    assert result['state']=='verified' and len(calls)==2
    assert result['record']['source']['visual_review']['availability_matches_verified_answer'] is True
