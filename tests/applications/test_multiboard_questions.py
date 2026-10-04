import pytest

from jhb.applications import booklet, questions


def test_scope_keeps_employer_answers_separate_across_boards_and_tenants(tmp_path):
    path=tmp_path/'private/book.json'
    booklet.write_private(path, {'schema_version':1,'answers':{},'roles':{'sde':{},'ml':{}},'custom_answers':{}})
    urls=['https://jobs.ashbyhq.com/example/11111111-1111-1111-1111-111111111111/application',
          'https://apply.workable.com/example/j/ABCDEF1234/apply/',
          'https://jobs.lever.co/example/11111111-1111-1111-1111-111111111111/apply',
          'https://example.wd5.myworkdayjobs.com/tenant-one/job/City/Engineer_JR123',
          'https://example.wd5.myworkdayjobs.com/tenant-two/job/City/Engineer_JR123']
    records=[]
    for i,url in enumerate(urls):
        job={'dedupe_hash':f'{i+1:064x}','url':url,'company':'Example','title':'Engineer'}
        records.append(questions.collect(job, {'missing':[{'question':'Do you accept this employer agreement?','ref':'agreement','required':True}]},path)[0])
    assert len({r['id'] for r in records})==len(urls)
    assert all(r['scope']['ats'] for r in records)
    questions.answer(records[0]['id'],True,path)
    assert len(questions.pending(path))==4


def test_other_ashby_jobs_share_same_employer_question_and_reuse_explicit_answer(tmp_path):
    path=tmp_path/'private/book.json'
    booklet.write_private(path, {'schema_version':1,'answers':{},'roles':{'sde':{},'ml':{}},'custom_answers':{}})
    jobs=[{'dedupe_hash':f'{i:064x}','url':f'https://jobs.ashbyhq.com/example/{i:08x}-1111-1111-1111-111111111111/application','company':'Example','title':'Engineer'} for i in (1,2)]
    result={'missing':[{'question':'Are you related to an employee?','ref':'related','required':True}]}
    first=questions.collect(jobs[0],result,path)[0]
    second=questions.collect(jobs[1],result,path)[0]
    assert first['id']==second['id']
    assert questions.answer(first['id'],False,path)==[j['dedupe_hash'] for j in jobs]
    saved=next(iter(booklet.load(path)['custom_answers'].values()))
    assert saved['value'] is False and saved['scope']=={'ats':'ashby','region':'global','board':'example'}


def test_board_index_cannot_collect_candidate_answers(tmp_path):
    with pytest.raises(ValueError,match='exact resolved'):
        questions._scope({'url':'https://jobs.ashbyhq.com/example'})
