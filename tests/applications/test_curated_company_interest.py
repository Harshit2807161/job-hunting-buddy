"""Synthetic two-model narrative pipeline; no account, network or candidate browser."""
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import time

import pytest

from jhb import config
from jhb.applications import boards, booklet, grounded_narratives as drafts, worker
from jhb.applications.planner import deterministic_plan
from tests.applications.test_grounded_narratives import Form


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setenv("CI", "1")
    url = "https://jobs.ashbyhq.com/synthetic/11111111-2222-3333-4444-555555555555"
    text = ("Our mission is to make search quality measurable for customers. "
            "This role builds retrieval APIs and evaluation tools, working with customers to understand where results fall short. "
            "The aim is a feedback loop that helps engineers improve search usefulness rather than only measure latency.")
    job = {"url": url, "company": "Synthetic Search", "selected_role": "ml", "verified_job_description": {
        "status": "verified", "source_url": url, "job_identity": list(boards.job_identity(url)), "retrieved_at": time.time(),
        "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}}
    resume = tmp_path / "ml.pdf"; resume.write_bytes(b"%PDF-1.4\nSynthetic ML variant")
    answers = {"documents.resume": booklet.answer(str(resume), "synthetic selected resume"),
        "role.experience": booklet.answer("Built retrieval evaluation tools to measure the relevance of search results.", "synthetic resume"),
        "role.skills": booklet.answer("Python and evaluation", "synthetic resume"),
        "identity.email": booklet.answer("never-in-prompts@example.invalid", "synthetic private contact"),
        "other_role.projects": booklet.answer("Built robots with 99% improvement", "unselected resume")}
    field = {"ref": "company_interest", "label": "Why are you interested in working at Synthetic Search?", "type": "textarea", "required": True}
    return field, job, answers


ANSWER = ("Search is useful when engineers can see which results miss the mark and improve them. "
          "I’d like to help build the retrieval APIs and evaluation tools that make that feedback practical for customers. "
          "My work on retrieval evaluation gave me a concrete connection to that problem, and this role would let me apply it to useful search tools.")


def models(calls, mutate=None, verdict="approved", review_mutate=None):
    def execute(command, **kwargs):
        inputs = json.loads(kwargs["input"].split("\nINPUT:\n", 1)[1])
        calls.append(("draft", command, kwargs, inputs))
        company = inputs["job_description"]["units"][0]; candidate = inputs["facts"]["role.experience"]["units"][0]
        parsed = {"field_ref": inputs["field_ref"], "state": "proposed", "answer": ANSWER, "reason_code": "none", "support": [
            {"input_id": "job_description", "unit_id": company["id"]},
            {"input_id": "role.experience", "unit_id": candidate["id"]}]}
        if mutate: mutate(parsed)
        Path(command[command.index("--output-last-message")+1]).write_text(json.dumps(parsed))
        return subprocess.CompletedProcess(command, 0, "", "")
    def review(command, **kwargs):
        inputs = json.loads(kwargs["input"].split("\nINPUT:\n", 1)[1]); calls.append(("review", command, kwargs, inputs))
        parsed = {"field_ref": inputs["evidence"]["field_ref"], "answer_sha256": inputs["answer_sha256"],
            "verdict": verdict, "factual_claims_supported": verdict == "approved", "all_claims_checked": True,
            "style_pass": verdict == "approved", "issues": [] if verdict == "approved" else ["Unverified experience or weak rationale"]}
        if review_mutate: review_mutate(parsed)
        Path(command[command.index("--output-last-message")+1]).write_text(json.dumps(parsed))
        return subprocess.CompletedProcess(command, 0, "", "")
    return execute, review


def invoke(context, calls, **kwargs):
    execute, review = models(calls, **kwargs)
    return drafts.curated_company_interest(*context, execute=execute, review_execute=review,
        preferences={"tone": "direct", "max_words": 100, "company_interest_reference": "Synthetic style example: focus on the useful work, then a reason to care."})


def test_paraphrased_specific_company_answer_has_separate_exact_factual_style_review(context, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-never-pass"); monkeypatch.setenv("SMTP_PASSWORD", "synthetic-never-pass")
    calls = []; result = invoke(context, calls)
    assert result["state"] == "proposed", result
    record = result["record"]
    assert record["value"] == ANSWER and '“' not in record["value"] and "mission stands out" not in record["value"]
    assert len(record["value"].split()) <= 100 and record["proposed"] is True
    assert [call[0] for call in calls] == ["draft", "review"]
    source = record["source"]
    assert source["method"] == "codex_curated_company_interest" and source["review_status"] == "proposed"
    assert source["independent_review"]["verdict"] == "approved"
    assert source["independent_review"]["answer_sha256"] == hashlib.sha256(ANSWER.encode()).hexdigest()
    assert source["resume_sha256"] == hashlib.sha256(Path(context[2]["documents.resume"]["value"]).read_bytes()).hexdigest()
    assert len(source["support"][0]["quote"].split()) > 25  # complete long factual context is available
    for kind, command, kwargs, inputs in calls:
        assert command[:2] == ["codex", "exec"] and command[command.index("--sandbox")+1] == "read-only"
        assert "features.shell_tool=false" in command and "mcp_servers={}" in command and 'web_search="disabled"' in command
        assert "OPENAI_API_KEY" not in kwargs["env"] and "SMTP_PASSWORD" not in kwargs["env"]
        assert "never-in-prompts@example.invalid" not in kwargs["input"] and "99%" not in kwargs["input"]
        assert str(Path(context[2]["documents.resume"]["value"])) not in kwargs["input"]
    assert all(p.stat().st_mode & 0o077 == 0 for p in (config.ROOT/"private/grounded-narratives").glob("*.json"))


@pytest.mark.parametrize("mutation", [
    lambda p:p.update(answer=ANSWER+" I improved customer retention by 85%."),
    lambda p:p["support"][0].update(quote="Build autonomous quantum tools", unit_id="invented"),
    lambda p:p["support"][0].update(quote="engineers improve search usefulness"),
    lambda p:p.update(field_ref="another_job_field"),
    lambda p:p.update(answer="Your mission stands out to me: “Make search useful.”"),
])
def test_invented_metrics_sources_partial_negation_units_or_wrong_question_never_reach_reviewer(context, mutation):
    calls=[]; result=invoke(context,calls,mutate=mutation)
    assert result["state"] == "agent_task" and [call[0] for call in calls] == ["draft"]
    assert invoke(context,calls)["state"] == "agent_task" and len(calls) == 1


def test_independent_review_rejects_unsupported_experience_despite_valid_citations(context):
    calls=[]
    result=invoke(context,calls,mutate=lambda p:p.update(answer=ANSWER+" I led the company’s engineering department."),verdict="reject")
    assert result["state"] == "agent_task" and [c[0] for c in calls] == ["draft","review"]
    assert "record" not in result


@pytest.mark.parametrize("mutation", [lambda r:r.update(answer_sha256="0"*64), lambda r:r.update(field_ref="another_control"),
    lambda r:r.update(factual_claims_supported=False), lambda r:r.update(all_claims_checked=False), lambda r:r.update(style_pass=False)])
def test_review_is_exact_answer_bound_and_must_check_every_claim(context, mutation):
    calls=[]; assert invoke(context,calls,review_mutate=mutation)["state"] == "agent_task"


@pytest.mark.parametrize("label", ["Why are you interested in this company? No AI text.", "Why join? Tell us about a failure.",
    "Why should we sponsor your visa?", "Why join in your own words?", "How many years of experience do you have?"])
def test_no_ai_or_new_factual_history_is_never_drafted(context,label):
    field,job,answers=context;field["label"]=label;calls=[]
    assert invoke(context,calls)["state"] == "needs_input" and not calls


def test_no_ai_owned_help_blocks_even_short_company_heading(context):
    context[0]["description"]="Do not use generative AI. Write in your own words.";calls=[]
    assert invoke(context,calls)["state"] == "needs_input" and not calls


def test_company_interest_reference_and_resume_bytes_invalidate_both_calls_cache(context):
    calls=[]; execute,review=models(calls)
    prefs={"company_interest_reference":"A concise synthetic style example"}
    first=drafts.curated_company_interest(*context,execute=execute,review_execute=review,preferences=prefs)
    second=drafts.curated_company_interest(*context,execute=execute,review_execute=review,preferences=prefs)
    assert first==second and len(calls)==2
    prefs["company_interest_reference"]+=" Focus on the problem."
    assert drafts.curated_company_interest(*context,execute=execute,review_execute=review,preferences=prefs)["state"]=="proposed" and len(calls)==4
    Path(context[2]["documents.resume"]["value"]).write_bytes(b"%PDF-1.4\nDifferent selected verified variant")
    assert drafts.curated_company_interest(*context,execute=execute,review_execute=review,preferences=prefs)["state"]=="proposed" and len(calls)==6


def test_transports_fail_as_agent_tasks_with_backoff_not_candidate_questions(context):
    calls=[]
    def timeout(command,**kwargs):calls.append(command);raise subprocess.TimeoutExpired(command,kwargs["timeout"])
    _,review=models([])
    for _ in range(2):
        assert drafts.curated_company_interest(*context,execute=timeout,review_execute=review)["state"]=="agent_task"
    assert len(calls)==1


def test_ci_requires_both_injected_processes_and_never_uses_subscription(context,monkeypatch):
    monkeypatch.setattr(drafts,"_run",lambda *a,**k:pytest.fail("Real Codex in CI"))
    execute,_=models([])
    assert drafts.curated_company_interest(*context,execute=execute)["reason_code"]=="ci_disabled"


def test_observed_word_and_character_limits_are_enforced(context):
    field,job,answers=context;field["description"]="Maximum 20 words.";calls=[]
    assert invoke(context,calls)["state"]=="agent_task"
    field["description"]="";field["max_length"]=30;calls=[]
    assert invoke(context,calls)["state"]=="agent_task"


def test_worker_curated_route_precedes_mission_template_but_explicit_user_answer_wins(context,monkeypatch):
    field,job,answers=context;form=Form(field);calls=[]
    original=drafts.curated_company_interest
    execute,review=models(calls)
    monkeypatch.setenv("JHB_CURATED_COMPANY_INTEREST","1");monkeypatch.setenv("JHB_GROUNDED_NARRATIVES","0")
    monkeypatch.setattr(drafts,"curated_company_interest",lambda *a,**k:original(*a,**k,execute=execute,review_execute=review))
    result,_=asyncio.run(worker.prepare(None,job,answers,deterministic_plan,None,cli_actions=form))
    assert result["state"]=="waiting_review" and form.values[field["ref"]]==ANSWER
    assert result["filled"][0]["proposed"] is True and result["review_inventory"]["fields"][0]["proposed"] is True
    assert len(calls)==2
    explicit={**answers,"custom.explicit":{**booklet.answer("My exact personal paragraph", {"provider":"explicit user question response","question_id":"q_"+"a"*24}),
        "question":field["label"],"user_override":True}}
    form=Form(field);calls.clear()
    result,_=asyncio.run(worker.prepare(None,job,explicit,deterministic_plan,None,cli_actions=form))
    assert result["state"]=="waiting_review" and form.values[field["ref"]]=="My exact personal paragraph" and not calls


def test_worker_transport_failure_is_retryable_agent_work_without_template_fallback(context,monkeypatch):
    field,job,answers=context;form=Form(field)
    monkeypatch.setenv("JHB_CURATED_COMPANY_INTEREST","1")
    monkeypatch.setattr(drafts,"curated_company_interest",lambda *a,**k:{"state":"agent_task","reason_code":"review_transport"})
    result,_=asyncio.run(worker.prepare(None,job,answers,deterministic_plan,None,cli_actions=form))
    assert result["state"]=="failed" and result["retryable"] and result["error_kind"]=="narrative_generation"
    assert not form.values and result["missing"]==[] and result["agent_tasks"][0]["task_kind"]=="narrative_generation"
    assert result["review_inventory"]["complete"] is False


def test_independent_review_outage_or_tool_attempt_is_agent_work_not_a_new_question(context):
    calls=[];execute,review=models(calls)
    def outage(command,**kwargs):raise subprocess.TimeoutExpired(command,kwargs['timeout'])
    assert drafts.curated_company_interest(*context,execute=execute,review_execute=outage)['state']=='agent_task'
    context[0]['ref']='new-review-control'
    def tool(command,**kwargs):
        result=review(command,**kwargs)
        result.stdout=json.dumps({'type':'item.completed','item':{'type':'command_execution'}})
        return result
    assert drafts.curated_company_interest(*context,execute=execute,review_execute=tool)['state']=='agent_task'


def test_negated_complete_units_cannot_be_shortened_even_with_same_unit_id(context):
    field,job,answers=context
    text='This company does not build autonomous weapons. It builds search tools.'
    job['verified_job_description'].update(text=text,sha256=hashlib.sha256(text.encode()).hexdigest())
    calls=[]
    assert invoke(context,calls,mutate=lambda p:p['support'][0].update(quote='build autonomous weapons'))['state']=='agent_task'
    assert [c[0] for c in calls]==['draft']


def test_reviewer_detects_negation_reversal_despite_exact_unmodified_support(context):
    field,job,answers=context
    text='This company does not build autonomous weapons. It builds search tools.'
    job['verified_job_description'].update(text=text,sha256=hashlib.sha256(text.encode()).hexdigest())
    calls=[]
    assert invoke(context,calls,mutate=lambda p:p.update(answer='I want to build autonomous weapons at this company.'),verdict='reject')['state']=='agent_task'
    assert [c[0] for c in calls]==['draft','review']


def test_true_missing_autobiographical_evidence_remains_candidate_handoff(context):
    calls=[]
    def missing(parsed):parsed.update(state='needs_input',reason_code='unknown_autobiographical',answer='',support=[])
    assert invoke(context,calls,mutate=missing)['state']=='needs_input'
    assert [c[0] for c in calls]==['draft']


def test_changed_fact_jd_and_cache_review_invalidate_safe_reuse(context):
    calls=[];execute,review=models(calls)
    assert drafts.curated_company_interest(*context,execute=execute,review_execute=review)['state']=='proposed'
    context[2]['role.skills']['value']+=' and APIs'
    assert drafts.curated_company_interest(*context,execute=execute,review_execute=review)['state']=='proposed' and len(calls)==4
    description=context[1]['verified_job_description'];description['text']+=' Work on useful evaluation loops.'
    description['sha256']=hashlib.sha256(description['text'].encode()).hexdigest()
    assert drafts.curated_company_interest(*context,execute=execute,review_execute=review)['state']=='proposed' and len(calls)==6
    path=next(p for p in (config.ROOT/'private/grounded-narratives').glob('*.json') if json.loads(p.read_text()).get('recipe'))
    data=json.loads(path.read_text());data['review']['verdict']='reject';path.write_text(json.dumps(data))
    # Exercise the exact corrupted cache, without treating an earlier recipe as approved.
    cache_context= drafts._inputs(*context,None,curated=True)
    key=drafts._digest({'version':drafts.VERSION,'mode':drafts.CURATED_CACHE_MODE,**cache_context,'question':booklet.normalize(cache_context['question'])})
    current=config.ROOT/'private/grounded-narratives'/f'{key}.json'
    data=json.loads(current.read_text());data['review']['verdict']='reject';current.write_text(json.dumps(data))
    assert drafts.curated_company_interest(*context,execute=execute,review_execute=review)['state']=='agent_task'


def test_factual_request_in_owned_help_is_not_disguised_by_a_company_heading(context):
    context[0]['description']='Please state your citizenship and work authorization.';calls=[]
    assert invoke(context,calls)['state']=='needs_input' and not calls
    context[0]['description']='We build software for legal professionals.'
    assert invoke(context,calls)['state']=='proposed' and len(calls)==2


def test_missing_already_verified_resume_is_agent_work_not_a_new_profile_question(context):
    Path(context[2]['documents.resume']['value']).unlink();calls=[]
    assert invoke(context,calls)['state']=='agent_task' and not calls


def test_owned_writing_help_reaches_both_models_and_changes_cache(context):
    field,job,answers=context;calls=[]
    field['description']='Connect one relevant project to our evaluation tools.'
    first=invoke(context,calls)
    assert first['state']=='proposed'
    assert calls[0][3]['observed_question']['description']==field['description']
    assert calls[1][3]['evidence']['observed_question']['description']==field['description']
    assert first['record']['source']['observed_question']['required'] is True
    assert invoke(context,calls)['state']=='proposed' and len(calls)==2
    field['description']='Focus entirely on the employer; omit your background.'
    # The independent reviewer rejects the unchanged sample's background clause.
    second=invoke(context,calls,verdict='reject')
    assert second['state']=='agent_task' and len(calls)==4
    assert calls[2][3]['observed_question']['description']==field['description']
    assert calls[3][3]['evidence']['observed_question']['description']==field['description']


@pytest.mark.parametrize('failure',['oversized_jd','missing_jd','truncated_help'])
def test_unavailable_company_sources_are_agent_tasks_without_model_or_candidate_question(context,failure):
    field,job,answers=context;calls=[]
    if failure=='oversized_jd':
        text='Search quality. '*1601
        job['verified_job_description'].update(text=text,sha256=hashlib.sha256(text.encode()).hexdigest())
    elif failure=='missing_jd':
        job.pop('verified_job_description')
    else:
        field.update(description='An incomplete writing requirement...',description_truncated=True)
    result=invoke(context,calls)
    assert result=={'state':'agent_task','reason_code':'narrative_source_unavailable'} and not calls


def test_drafter_source_insufficiency_is_agent_retry_with_durable_backoff(context):
    calls=[]
    def missing(parsed):parsed.update(state='needs_input',reason_code='insufficient_evidence',answer='',support=[])
    assert invoke(context,calls,mutate=missing)=={'state':'agent_task','reason_code':'narrative_source_unavailable'}
    assert invoke(context,calls)['state']=='agent_task' and len(calls)==1


def test_explicit_optional_decline_wins_over_reused_proposal_catalog(context,monkeypatch):
    field,job,answers=context;field['required']=False;calls=[]
    generated=invoke(context,calls)['record']
    answers['custom.grounded.'+field['ref']]=generated
    answers['custom.explicit']={**booklet.answer('',{'provider':'explicit user question response','question_id':'q_'+'b'*24}),
        'status':'declined','question':field['label'],'field_ref':field['ref'],'user_override':True}
    form=Form(field)
    monkeypatch.setenv('JHB_CURATED_COMPANY_INTEREST','1')
    def forbidden(*a,**k):raise AssertionError('Explicit decline must not draft')
    monkeypatch.setattr(drafts,'curated_company_interest',forbidden)
    result,_=asyncio.run(worker.prepare(None,job,answers,deterministic_plan,None,cli_actions=form))
    assert result['state']=='waiting_review' and not form.values
    assert result['review_inventory']['fields'][0]['status']=='declined'


def test_same_run_changed_help_cannot_fill_a_previously_reviewed_proposal(context,monkeypatch):
    field,job,answers=context;calls=[]
    answers['custom.grounded.'+field['ref']]=invoke(context,calls)['record']
    field['description']='Focus entirely on the employer; omit your background.'
    execute,review=models(calls,verdict='reject')
    original=drafts.curated_company_interest
    monkeypatch.setenv('JHB_CURATED_COMPANY_INTEREST','1')
    monkeypatch.setattr(drafts,'curated_company_interest',lambda *a,**k:original(*a,**k,execute=execute,review_execute=review))
    form=Form(field)
    result,_=asyncio.run(worker.prepare(None,job,answers,deterministic_plan,None,cli_actions=form))
    assert result['state']=='failed' and result['error_kind']=='narrative_generation'
    assert len(calls)==4 and not form.values and result['missing']==[]


def test_flattened_long_official_jd_uses_whole_context_and_separate_review(context):
    field,job,answers=context;calls=[]
    description=job['verified_job_description']
    text=(description['text']+' '+('The team builds search tools, evaluates quality, and supports customer feedback. '*170)).strip()
    assert len(text.split())>1000 and 3000<len(text)<24000
    description.update(text=text,sha256=hashlib.sha256(text.encode()).hexdigest())
    result=invoke(context,calls)
    assert result['state']=='proposed' and [c[0] for c in calls]==['draft','review']
    assert calls[0][3]['job_description']['units']==[{'id':'job_description:'+hashlib.sha256(text.encode()).hexdigest()[:16],'text':text}]
    assert result['record']['source']['support'][0]['quote']==text
    assert calls[1][3]['evidence']['job_description']['text']==text
    assert invoke(context,calls)['state']=='proposed' and len(calls)==2


def test_whole_flattened_jd_support_cannot_trim_negation_or_conditions(context):
    field,job,answers=context;calls=[]
    text='This role does not involve weapons; the team builds search tools. '+('Work on retrieval evaluation. '*100)
    job['verified_job_description'].update(text=text,sha256=hashlib.sha256(text.encode()).hexdigest())
    result=invoke(context,calls,mutate=lambda p:p['support'][0].update(quote='the team builds search tools.'))
    assert result['state']=='agent_task' and [c[0] for c in calls]==['draft']


def test_drafter_sends_only_short_evidence_ids_but_review_and_cache_keep_full_context(context):
    field,job,answers=context;calls=[];wire_sizes=[]
    text=('This team does not build weapons. '+job['verified_job_description']['text']+' ')*25
    job['verified_job_description'].update(text=text,sha256=hashlib.sha256(text.encode()).hexdigest())
    execute,review=models(calls)
    def capture_wire(command,**kwargs):
        result=execute(command,**kwargs)
        raw=Path(command[command.index('--output-last-message')+1]).read_text()
        parsed=json.loads(raw)
        assert all(set(item)=={'input_id','unit_id'} for item in parsed['support'])
        wire_sizes.append(len(raw))
        return result
    result=drafts.curated_company_interest(*context,execute=capture_wire,review_execute=review)
    assert result['state']=='proposed' and wire_sizes[0]<1000<len(text)
    assert calls[1][3]['draft']['support'][0]['quote']==text.strip()
    assert result['record']['source']['support'][0]['quote']==text.strip()
    assert drafts.curated_company_interest(*context,execute=capture_wire,review_execute=review)['state']=='proposed'
    assert len(calls)==2
    path=next((config.ROOT/'private/grounded-narratives').glob('*.json'))
    data=json.loads(path.read_text())
    data['recipe']['support'][0]['quote']='This team builds weapons.'
    path.write_text(json.dumps(data))
    assert drafts.curated_company_interest(*context,execute=capture_wire,review_execute=review)['state']=='agent_task'
    assert len(calls)==2  # Tampering cannot become a shortened positive cache.


@pytest.mark.parametrize('change',[lambda p:p['support'][0].update(unit_id='unknown-approved-unit'),
                                lambda p:p['support'][0].update(input_id='role.projects')])
def test_unknown_or_wrong_source_unit_ids_fail_before_independent_review(context,change):
    calls=[]
    assert invoke(context,calls,mutate=change)['state']=='agent_task'
    assert [c[0] for c in calls]==['draft']
