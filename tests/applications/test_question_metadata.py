"""Synthetic public descriptors: no live HTTP, account or candidate browser."""
import io
import json
import sqlite3
import pytest
from jhb.applications import boards, booklet, questions, queue, question_metadata as metadata


def seed(tmp_path, count=3):
    conn = sqlite3.connect(':memory:'); queue.initialize(conn)
    book = tmp_path/'private'/'book.json'
    booklet.write_private(book, {'schema_version':1,'answers':{},'custom_answers':{},'roles':{'sde':{},'ml':{}}})
    jobs = []
    for i in range(count):
        url = f'https://job-boards.greenhouse.io/example/jobs/{100+i}'
        job = {'url':url,'dedupe_hash':boards.application_hash(url),'company':'Synthetic Employer','title':f'Role {i}'}
        conn.execute('INSERT INTO applications(job_hash,job_json,state,updated_at) VALUES(?,?,?,?)',
            (job['dedupe_hash'],json.dumps(job),'waiting_input',1))
        q = questions.collect(job, {'missing':[{'question':'Are you authorized?*','ref':f'question_{700+i}',
            'type':'combobox','required':True,'country_context':'United States'}]}, book)[0]
        jobs.append(job)
    conn.commit()
    return conn, book, jobs, q


def api(job_id, ref, description='Read this official condition.', choices=('Yes','No'), **overrides):
    question = {'label':'Are you authorized?','description':f'<p>{description}</p>','required':True,
        'fields':[{'name':ref,'type':'multi_value_single_select','values':[{'label':c,'value':str(i)} for i,c in enumerate(choices)]}]}
    question.update(overrides)
    return {'id':job_id,'questions':[question]}


def opener_for(values, calls=None):
    def opener(request, timeout):
        assert timeout==10 and request.get_method()=='GET' and request.data is None
        assert dict(request.header_items())=={'Accept':'application/json'}
        if calls is not None: calls.append(request.full_url)
        job_id=request.full_url.split('/jobs/')[1].split('?')[0]
        return io.BytesIO(json.dumps(values[int(job_id)]).encode())
    return opener


def test_each_merged_context_uses_its_own_ref_and_keeps_answers_states_and_native_notes(tmp_path):
    conn, book, jobs, q = seed(tmp_path)
    initial=booklet.load(book)
    initial['answers']['identity.country']=booklet.answer('Synthetic verified country','Synthetic source')
    initial['custom_answers']['custom.old']=booklet.answer(False,'Synthetic historical response')
    initial['question_handoffs'][q['id']]['contexts'][jobs[0]['dedupe_hash']]['description']='Native help must remain unchanged'
    booklet.write_private(book,initial)
    calls=[]
    values={100+i:api(100+i,f'question_{700+i}',f'Official condition {i}') for i in range(3)}
    assert metadata.enrich(conn,book,opener=opener_for(values,calls))=={'jobs_checked':3,'contexts_enriched':3}
    current=booklet.load(book); record=current['question_handoffs'][q['id']]
    assert record['id']==q['id'] and record['question']==q['question'] and record['status']=='pending'
    assert record['updated_at']!=q['updated_at']
    for i,job in enumerate(jobs):
        context=record['contexts'][job['dedupe_hash']]
        assert context['public_question_metadata']['field_ref']==f'question_{700+i}'
        assert context['public_question_metadata']['description']==f'Official condition {i}'
        assert context['choices']==['Yes','No']
    assert record['contexts'][jobs[0]['dedupe_hash']]['description']=='Native help must remain unchanged'
    assert current['answers']==initial['answers'] and current['custom_answers']==initial['custom_answers']
    assert list(conn.execute('SELECT state FROM applications'))==[('waiting_input',)]*3
    assert len(calls)==3 and all(x.endswith('?questions=true') for x in calls)
    assert book.stat().st_mode & 0o777==0o600
    before=book.read_bytes()
    assert metadata.enrich(conn,book,opener=opener_for(values))['contexts_enriched']==0
    assert book.read_bytes()==before


def test_changed_public_help_and_choices_invalidate_view_and_saved_answer_has_separate_proof(tmp_path):
    conn, book, jobs, q=seed(tmp_path,1)
    values={100:api(100,'question_700')}
    metadata.enrich(conn,book,opener=opener_for(values))
    old=booklet.load(book)['question_handoffs'][q['id']]['updated_at']
    values[100]=api(100,'question_700','Revised U.S. condition.',('Eligible','Ineligible'))
    metadata.enrich(conn,book,opener=opener_for(values))
    record=booklet.load(book)['question_handoffs'][q['id']]
    assert record['contexts'][jobs[0]['dedupe_hash']]['choices']==['Eligible','Ineligible']
    with pytest.raises(questions.QuestionChanged): questions.answer(q['id'],'Eligible',book,expected_revision=old)
    questions.answer(q['id'],'Eligible',book,expected_revision=record['updated_at'])
    saved=booklet.load(book); source=next(iter(saved['custom_answers'].values()))['source']
    proof=source['public_question_metadata_proofs'][0]
    assert proof['field_ref']=='question_700' and proof['country_context']=='united states'
    assert proof['source']=='official_public_question_metadata'
    assert 'owned_description_sha256' not in source
    # Answered records are no longer metadata maintenance targets.
    before=book.read_bytes()
    assert metadata.enrich(conn,book,opener=lambda *a,**k: pytest.fail('Answered metadata must not fetch'))['jobs_checked']==0
    assert book.read_bytes()==before


@pytest.mark.parametrize('fault',['ref','label','type','required','duplicate','oversize','redirect','wrong_id','bad_values'])
def test_mismatched_or_incomplete_metadata_never_changes_pending_records(tmp_path,fault):
    conn,book,jobs,q=seed(tmp_path,1)
    data=api(100,'question_700')
    item=data['questions'][0]
    if fault=='ref': item['fields'][0]['name']='question_999'
    if fault=='label': item['label']='Are you a citizen?'
    if fault=='type': item['fields'][0]['type']='input_text'
    if fault=='required': item['required']=False
    if fault=='duplicate': data['questions']*=2
    if fault=='oversize': item['description']='x'*4097
    if fault=='wrong_id': data['id']=999
    if fault=='bad_values': item['fields'][0]['values']=[{'label':'Yes'},{'label':'Yes'}]
    def opener(request,**kwargs):
        response=io.BytesIO(json.dumps(data).encode())
        if fault=='redirect': response.geturl=lambda:'https://attacker.example/jobs/100'
        return response
    before=book.read_bytes()
    assert metadata.enrich(conn,book,opener=opener)['contexts_enriched']==0
    assert book.read_bytes()==before


def test_candidate_response_during_fetch_is_never_overwritten(tmp_path):
    conn,book,jobs,q=seed(tmp_path,1)
    def opener(*args,**kwargs):
        questions.answer(q['id'],False,book)
        return io.BytesIO(json.dumps(api(100,'question_700')).encode())
    assert metadata.enrich(conn,book,opener=opener)['contexts_enriched']==0
    current=booklet.load(book)
    assert current['question_handoffs'][q['id']]['status']=='answered'
    assert next(iter(current['custom_answers'].values()))['value'] is False


def test_terminal_contexts_skip_and_native_choices_are_not_replaced(tmp_path):
    conn,book,jobs,q=seed(tmp_path,3)
    conn.execute("UPDATE applications SET state='submitted' WHERE job_hash=?",(jobs[0]['dedupe_hash'],))
    conn.execute("UPDATE applications SET state='submission_uncertain' WHERE job_hash=?",(jobs[1]['dedupe_hash'],))
    current=booklet.load(book)
    current['question_handoffs'][q['id']]['contexts'][jobs[2]['dedupe_hash']]['choices']=['Native Yes','Native No']
    booklet.write_private(book,current)
    calls=[]
    metadata.enrich(conn,book,opener=opener_for({102:api(102,'question_702')},calls))
    assert len(calls)==1 and '/102?' in calls[0]
    assert booklet.load(book)['question_handoffs'][q['id']]['contexts'][jobs[2]['dedupe_hash']]['choices']==['Native Yes','Native No']


def test_fixed_public_get_handles_failure_body_limit_and_bounds_job_count(tmp_path):
    conn,book,jobs,q=seed(tmp_path,3)
    calls=[]
    assert metadata.enrich(conn,book,limit=1,opener=opener_for({100:api(100,'question_700')},calls))['jobs_checked']==1
    with pytest.raises(ValueError): metadata.enrich(conn,book,limit=11)
    assert metadata.fetch(jobs[0]['url'],opener=lambda *a,**k: io.BytesIO(b'x'*(metadata.MAX_BYTES+1)))=={}
    assert metadata.fetch('https://attacker.example/example/jobs/100',opener=lambda *a,**k: pytest.fail('Untrusted URL'))=={}
    def fail(*a,**k): raise OSError('Synthetic network unavailable')
    assert metadata.fetch(jobs[0]['url'],opener=fail)=={}


def test_maintenance_cli_never_loads_candidate_data_or_fetches_in_ci(monkeypatch,capsys):
    monkeypatch.setenv('CI','true')
    monkeypatch.setattr(metadata,'enrich',lambda *a,**k: pytest.fail('CI must not open the ledger or network'))
    metadata.main([])
    assert json.loads(capsys.readouterr().out)=={'state':'disabled','reason_code':'ci_disabled'}


def guided_answer(tmp_path):
    from jhb.applications import planner
    conn,book,jobs,q=seed(tmp_path,1)
    metadata.enrich(conn,book,opener=opener_for({100:api(100,'question_700','Exact instruction.')}))
    current=booklet.load(book)['question_handoffs'][q['id']]
    questions.answer(q['id'],'Yes',book,expected_revision=current['updated_at'])
    saved=booklet.load(book)
    key,record=next(iter(saved['custom_answers'].items()))
    field={'ref':'question_700','label':'Are you authorized?*','type':'combobox','required':True,
        'country_context':'united states','description':'Exact instruction.','options':[{'label':'Yes'},{'label':'No'}]}
    return planner,key,record,field


def test_public_response_reuses_only_actual_exact_fresh_native_context(tmp_path):
    planner,key,record,field=guided_answer(tmp_path)
    assert metadata.public_response_allowed(field,record) is True
    assert planner.key_for_field(field,{key:record})==key


@pytest.mark.parametrize('fault',['ref','country','description','truncated','type','required','choices','closed_catalog','unknown_source','empty_proofs'])
def test_changed_native_context_blocks_public_response_and_generic_fallback(tmp_path,fault):
    planner,key,record,field=guided_answer(tmp_path)
    if fault=='ref': field['ref']='question_999'
    elif fault=='country': field['country_context']='Canada'
    elif fault=='description': field['description']='Different conditional instruction.'
    elif fault=='truncated': field['description_truncated']=True
    elif fault=='type': field['type']='text'
    elif fault=='required': field['required']=False
    elif fault=='choices': field['options']=[{'label':'Yes'},{'label':'Different No'}]
    elif fault=='closed_catalog': field['options']=[]
    elif fault=='unknown_source': record['source']['provider']='unverified source'
    elif fault=='empty_proofs': record['source']['public_question_metadata_proofs']=[]
    fallback={'custom.legacy':{**booklet.answer('Yes','Synthetic historical reply'),'question':record['question']}}
    assert metadata.public_response_allowed(field,record) is False
    assert planner.key_for_field(field,{**fallback,key:record}) is None


def test_closed_catalog_can_resume_after_bounded_native_inspection_without_public_catalog_injection(tmp_path):
    planner,key,record,field=guided_answer(tmp_path)
    field['options']=[]
    assert planner.key_for_field(field,{key:record}) is None
    field['choices']=['Yes','No']  # Trusted runtime's actual described choices.
    assert planner.key_for_field(field,{key:record})==key


def test_whitespace_only_rendering_changes_match_but_words_negation_and_punctuation_do_not(tmp_path):
    planner,key,record,field=guided_answer(tmp_path)
    field['description']='  Exact\u00a0instruction.\n\n'
    assert metadata.public_response_allowed(field,record)
    for changed in ['Not Exact instruction.','Exact instruction!','Exact']:
        field['description']=changed
        assert not metadata.public_response_allowed(field,record)


def test_distinct_indexed_or_country_scoped_answer_does_not_veto_another_same_heading(tmp_path):
    planner,key,record,field=guided_answer(tmp_path)
    record['field_ref']='question_999'
    other={**booklet.answer('No','Synthetic explicit source'),'question':record['question'],'field_ref':'question_700'}
    assert planner.key_for_field(field,{key:record,'custom.other':other})=='custom.other'
    record['country_context']='canada'
    assert planner.key_for_field(field,{key:record,'custom.other':other})=='custom.other'


def test_matching_public_context_selects_real_catalog_probe_without_trusting_absent_catalog(tmp_path):
    planner,key,record,field=guided_answer(tmp_path)
    field['options']=[]
    assert metadata.public_response_context_matches(field,record)
    assert not metadata.public_response_allowed(field,record)
    assert not metadata.public_response_context_matches(field,booklet.answer('Yes','Synthetic old source'))


def test_native_changed_catalog_reopens_answered_guided_record_for_real_candidate_correction(tmp_path):
    conn,book,jobs,q=seed(tmp_path,1)
    metadata.enrich(conn,book,opener=opener_for({100:api(100,'question_700')}))
    questions.answer(q['id'],'Yes',book)
    result={'missing':[{'question':q['question'],'ref':'question_700','required':True,'type':'combobox',
        'choices':['Yes','Different No'],'country_context':'United States','description':'Read this official condition.'}]}
    pending=questions.collect(jobs[0],result,book)
    assert len(pending)==1 and pending[0]['id']==q['id'] and pending[0]['status']=='pending'
    assert next(iter(booklet.load(book)['custom_answers'].values()))['status']=='needs_input'


def test_public_guidance_cannot_bypass_candidate_wording_guard_with_changed_native_help(tmp_path):
    planner,key,record,field=guided_answer(tmp_path)
    field['label']='Why this company? Please, no AI text.'
    record['question']=field['label']
    field['description']='Changed instruction.'
    assert planner.key_for_field(field,{key:record}) is None


def test_explicit_matching_public_response_can_resolve_unfamiliar_conditional_note(tmp_path):
    from jhb.applications import known_answers
    planner,key,record,field=guided_answer(tmp_path)
    field['label']=known_answers._RESTRICTION
    record['question']=field['label']
    assert known_answers.has_conditional_instruction(field)
    assert planner.key_for_field(field,{key:record})==key
    field['description']='Changed unfamiliar note.'
    assert planner.key_for_field(field,{key:record}) is None
