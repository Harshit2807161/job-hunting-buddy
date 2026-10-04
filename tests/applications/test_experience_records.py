from jhb.applications import booklet, narratives


def book(text):
    return {'answers':{}, 'roles':{'sde':{'role.experience':booklet.answer(text,'synthetic SDE resume')},'ml':{}}}


def test_repeaters_keep_month_precision_original_role_and_separate_bullets():
    b=book('Example Lab      May 2024 – Oct 2024\nResearch Intern      Example City\n'
           '• Built a routing algorithm, reducing depth by 10%.\n• Added search pruning,\n'
           ' reducing runtime by 20%.\n• Presented peer-reviewed research.\n')
    r=booklet.experience_records(b,'sde')[0]
    assert r['start_date']=='2024-05' and r['end_date']=='2024-10'
    assert r['summary'].splitlines()==['• Built a routing algorithm, reducing depth by 10%.',
        '• Added search pruning, reducing runtime by 20%.','• Presented peer-reviewed research.']
    values=booklet.for_role(b,'sde')
    assert values['experience.0.current']['value'] is False
    assert values['experience.0.start_date']['source']['date_precision']=='month'
    assert booklet.experience_records(b,'ml')==[]


def test_unverified_and_malformed_resume_sections_do_not_create_work_history():
    b=book('Example       Jun 2026 – Jan 2026\nEngineer       City\n• Built something.\n')
    assert booklet.experience_records(b,'sde')==[]
    b=book('Example       Jun 2026 – Present\nEngineer       City\n• Built something.\n')
    r=booklet.experience_records(b,'sde')[0]
    assert r['current'] is True and r['end_date']==''
    b['roles']['sde']['role.experience']['status']='needs_input'
    assert booklet.experience_records(b,'sde')==[]


def test_technical_project_baseline_improvement_does_not_answer_failure_history():
    text=('Example Product Co      Feb 2025 – Aug 2025\nEngineer      Remote\n'
          '• Implemented a hybrid filtering recommender system, replacing vanilla popularity-based sorting and increasing\n'
          ' CTR by 22%, with improvements validated through A/B testing and Azure Monitor.\n')
    values=booklet.for_role(book(text),'sde')
    field={'label':'Describe one technical project you built. What did you own, what failed, and how did you test the fix? (150 words max.)',
           'ref':'project','type':'textarea','required':True}
    assert narratives.proposal(field,{},values) is None
    values['role.experience']['value']=text.replace('Azure Monitor','an undocumented method')
    assert narratives.proposal(field,{},values) is None
