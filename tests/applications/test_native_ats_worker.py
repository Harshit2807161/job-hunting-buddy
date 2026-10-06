"""Native adapter manifests preserve indexed, selected-role verified records."""
import asyncio
from jhb.applications.booklet import answer
from jhb.applications.manual_ats import ManualATSCLI


def test_workable_profile_manifest_carries_two_degrees_and_selected_resume_experience(monkeypatch):
    client=ManualATSCLI('https://apply.workable.com/example/j/ABC1234567/',board='workable')
    calls=[]
    async def invoke(operation,**payload):
        calls.append((operation,payload))
        return {'fields':[{'ref':'firstname','label':'First name','type':'text','required':True}],'buttons':[]} if operation=='observe' else {'verified':True,'filled':[]}
    monkeypatch.setattr(client,'invoke',invoke)
    records={}
    for i,(school,degree,start,end) in enumerate([('Example Graduate School','Master of Science','2025-09','2027-12'),('Example College','Bachelor of Science','2021-08','2025-05')]):
        for key,value in {'school':school,'degree':degree,'major':'Computer Science','start_date':start,'end_date':end}.items():
            records[f'education.{i}.{key}']=answer(value,'synthetic original education')
    for key,value in {'company':'Example Employer','title':'Engineer','summary':'• Improved runtime by 10%.','start_date':'2024-06','end_date':'2024-09','current':False}.items():
        records['experience.0.'+key]=answer(value,'synthetic selected SDE resume')
    records['experience.1.company']=answer('Unverified Employer',status='needs_input')
    assert asyncio.run(client.ensure_profile(records))['verified']
    packets=[payload for operation,payload in calls if operation=='records']
    assert len(packets)==3
    education=[row for packet in packets for row in packet['education']]
    experience=[row for packet in packets for row in packet['experience']]
    assert [r['school'] for r in education]==['Example Graduate School','Example College']
    assert education[0]['end_date']=='2027-12'
    assert len(experience)==1 and experience[0]['current'] is False
    assert experience[0]['source']=='synthetic selected SDE resume'


def test_workable_authentication_handoff_precedes_any_editor_mutation(monkeypatch):
    client=ManualATSCLI('https://apply.workable.com/example/j/ABC1234567/',board='workable')
    calls=[]
    async def invoke(operation,**payload):
        calls.append(operation);return {'handoff':'waiting_login','reason':'Google SSO handoff'}
    monkeypatch.setattr(client,'invoke',invoke)
    result=asyncio.run(client.ensure_profile({'education.0.school':answer('Example School','synthetic verified education')}))
    assert result['handoff']=='waiting_login' and calls==['observe']
