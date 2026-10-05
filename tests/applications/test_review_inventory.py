"""Every question, including optional prose, must survive portal review."""
import asyncio
import json

import pytest

from jhb.applications import booklet,narratives
from jhb.applications.planner import deterministic_plan,key_for_field
from jhb.applications.worker import prepare,write_packet
from jhb.applications.review_inventory import candidate_wording_requested

WHY='Why Example? Please, no AI text'
OPINION='What is your opinion on the right use of AI tools in both coding and code review?'

@pytest.mark.parametrize('label', ['Why this company? Please do not use generative AI.',
                                 'Tell us why, without using large language models.',
                                 'Write your answer without the use of AI.',
                                 'Answer without use of generative AI.',
                                 'Write this without help from a language model.',
                                 'Refrain from using artificial intelligence in this answer.',
                                 'Do not rely on LLMs for your answer.',
                                 'Don’t use ChatGPT for your response.',
                                 'No LLMs please.'])
def test_explicit_ai_prohibition_preserves_candidate_only_requirement(label):
    assert candidate_wording_requested(label)

def test_ordinary_opinion_on_ai_tools_does_not_prohibit_candidate_or_grounded_wording():
    assert candidate_wording_requested(OPINION) is False

class Form:
    blocked_requests=0
    def __init__(self,steps):self.steps=steps;self.index=0;self.fills={}
    def allowed_url(self,url):return url=='synthetic'
    async def open(self,url):pass
    async def observe(self):return self.steps[self.index]
    async def fill(self,field,value):self.fills[field['ref']]=value
    async def click_next(self,button):
        assert button['label']=='Next'
        self.index+=1

def field(ref,label,type='text',required=False):
    return {'ref':ref,'label':label,'type':type,'required':required}

def snapshot(fields,next=False):
    return {'fields':fields,'buttons':[{'ref':'next' if next else 'submit','label':'Next' if next else 'Submit application'}]}

def run(form,answers):
    return asyncio.run(prepare(None,{'url':'synthetic'},answers,deterministic_plan,None,cli_actions=form))[0]


def test_pangram_style_blank_optional_prose_and_disclosures_remain_explicit(monkeypatch,tmp_path):
    monkeypatch.setattr(narratives,'proposal',lambda *args:None)
    fields=[field('name','Full Name',required=True),field('opinion',OPINION,'textarea'),
            field('why',WHY,'textarea'),field('more','Anything else you’d like to tell us? Please, no AI text.','textarea'),
            field('gender','Gender','select'),field('marketing','Receive promotional emails','checkbox')]
    answers={'identity.full_name':booklet.answer('Sam Example','Synthetic identity'),
             'disclosure.gender':booklet.answer(source='Synthetic explicit decline',status='declined')}
    form=Form([snapshot(fields)])
    result=run(form,answers)
    rows=result['review_inventory']['fields']
    assert result['review_inventory']['complete'] is True
    assert [row['question'] for row in rows]==[f['label'] for f in fields]
    assert [row['status'] for row in rows]==['answered','blank','blank','blank','declined','blank']
    assert [row['category'] for row in rows][-2:]==['voluntary_disclosure','communications']
    assert result['review_completeness']['blank_substantive_count']==3
    assert result['review_completeness']['candidate_wording_required_count']==2
    assert result['review_completeness']['requires_explicit_acknowledgment'] is True
    assert [q['question'] for q in result['optional_questions']][:3]==[OPINION,WHY,fields[3]['label']]
    assert set(form.fills)=={'name'}
    path=asyncio.run(write_packet(None,tmp_path,{'title':'Engineer','company':'Example','url':'synthetic'},result))
    assert WHY in path.read_text() and 'Blank optional questions require explicit portal acknowledgment' in path.read_text()
    saved=json.loads((tmp_path/'packet.json').read_text())
    assert saved['review_inventory']==result['review_inventory']


@pytest.mark.parametrize('candidate',[False,True])
def test_no_ai_prompt_accepts_only_explicit_candidate_response(monkeypatch,candidate):
    def no_generation(*args):raise AssertionError('Candidate-only prompt must never enter narrative generation')
    monkeypatch.setattr(narratives,'proposal',no_generation)
    source={'provider':'explicit user question response','question_id':'synthetic-question'} if candidate else {'method':'grounded selected resume facts'}
    key='custom.own-wording' if candidate else 'custom.grounded.why'
    record={**booklet.answer('My own written reason.',source),'question':WHY,'field_ref':'why'}
    f=field('why',WHY,'textarea')
    assert key_for_field(f,{key:record})==(key if candidate else None)
    form=Form([snapshot([f])]);result=run(form,{key:record})
    row=result['review_inventory']['fields'][0]
    assert row['status']==('answered' if candidate else 'blank')
    assert ('why' in form.fills) is candidate


def test_candidate_explicit_leave_blank_is_preserved_as_declined(monkeypatch):
    monkeypatch.setattr(narratives,'proposal',lambda *args:None)
    record={**booklet.answer(source={'provider':'explicit user question response','question_id':'synthetic-question'},status='declined'),
            'question':WHY,'field_ref':'why'}
    result=run(Form([snapshot([field('why',WHY,'textarea')])]),{'custom.declined':record})
    assert result['review_inventory']['fields'][0]['status']=='declined'
    assert result['review_completeness']['requires_explicit_acknowledgment']


def test_wizard_inventory_preserves_questions_from_prior_steps(monkeypatch):
    monkeypatch.setattr(narratives,'proposal',lambda *args:None)
    form=Form([snapshot([field('name','Full Name',required=True)],next=True),snapshot([field('why',WHY,'textarea')])])
    result=run(form,{'identity.full_name':booklet.answer('Sam Example','Synthetic profile')})
    inventory=result['review_inventory']
    assert inventory['complete'] and inventory['step_count']==2
    assert [(r['ref'],r['status']) for r in inventory['fields']]==[('name','answered'),('why','blank')]


def test_required_unknown_and_old_packets_cannot_claim_complete_inventory(tmp_path,monkeypatch):
    monkeypatch.setattr(narratives,'proposal',lambda *args:None)
    result=run(Form([snapshot([field('new','Employer-specific certificate',required=True)])]),{})
    assert result['state']=='waiting_input' and not result['review_inventory']['complete']
    assert result['review_inventory']['fields'][0]['status']=='blank'
    legacy={'state':'waiting_review','reason':'Legacy packet','filled':[],'events':[]}
    asyncio.run(write_packet(None,tmp_path,{'company':'Example','title':'Engineer','url':'synthetic'},legacy))
    assert json.loads((tmp_path/'packet.json').read_text())['review_inventory']['complete'] is False



def test_owned_help_no_ai_instruction_is_preserved_and_cannot_verify_model_wording():
    from jhb.applications import booklet
    from jhb.applications.planner import key_for_field
    from jhb.applications.review_inventory import build
    field = {"ref": "why", "label": "Why this employer?", "type": "textarea", "required": False,
             "description": "Please do not use generative AI to write this response.", "description_truncated": False}
    approved = {**booklet.answer("Synthetic generated company-focused wording", "Synthetic proposed template"),
                "question": field["label"], "field_ref": "why"}
    answers = {"custom.why": approved}
    inventory = build([field], [{"ref": "why", "question": field["label"], "key": "custom.why",
                               "value": approved["value"], "source": approved["source"]}], answers, key_for_field)
    item = inventory["review_inventory"]["fields"][0]
    assert item["question"] == field["label"] and item["description"] == field["description"]
    assert item["candidate_wording_required"] is True and item["status"] == "blank"



def test_standalone_narrative_entrypoints_respect_owned_no_ai_help():
    from jhb.applications import grounded_narratives, narratives
    field = {"ref": "why", "label": "Why this company?", "type": "textarea", "required": False,
             "description": "Please do not use generative AI to write this response."}
    assert grounded_narratives.intent(field, "Example") is None
    assert narratives.proposal(field, {"company": "Example"}, {}) is None
    clipped = {**field, "description": "Incomplete employer guidance", "description_truncated": True}
    assert grounded_narratives.intent(clipped, "Example") is None
    assert narratives.proposal(clipped, {"company": "Example"}, {}) is None
