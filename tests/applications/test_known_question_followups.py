"""Synthetic known facts and a scoped conditional; no candidate or live browser."""
import asyncio
import copy

import pytest

from jhb.applications import boards, booklet, known_answers, question_routing
from jhb.applications.planner import deterministic_plan, key_for_field, validate_plan
from jhb.applications.worker import _discovery_detail_omission, prepare


def job():
    url = "https://job-boards.greenhouse.io/synthetic/jobs/123"
    return {"company": "Synthetic", "url": url, "dedupe_hash": boards.application_hash(url),
            "source": "simplify", "source_url": "https://synthetic.example/discovered"}


def question(label, *, kind="combobox", ref="question_123", choices=("Yes", "No"), required=True):
    return {"label": label, "ref": ref, "type": kind, "required": required,
            "description": "", "description_truncated": False,
            "options": [{"label": choice} for choice in choices]}


def facts():
    data = {"answers": {"preferences.relocation": booklet.answer(True, "Synthetic explicit relocation")},
            "workflow_preferences": {"office_locations": {"value": True, "source": "Synthetic explicit onsite willingness"}}}
    answers = booklet.common_answers(data, job=job())
    answers["role.projects"] = booklet.answer(
        "Synthetic Planner | Python, PostgreSQL\n• Built a scheduling tool used by 20 synthetic accounts.",
        {"provider": "synthetic selected resume", "sha256": "a"*64})
    return answers


ONSITE = "I understand this is an in-person role in Philadelphia, PA.*"
PROJECT = "Do you have a personal project you're proud of that you'd like to share?*"
DISCOVERY = "How did you hear about us?*"
CHOICES = ["Career fair", "Google", "Handshake", "LinkedIn", "Word of mouth", "I'm a customer", "Other"]


@pytest.mark.parametrize("label,choices,expected", [(ONSITE,["Yes","No"],"Yes"),
    (PROJECT,["Yes","No"],"Yes"),(DISCOVERY,CHOICES,"Other")])
def test_verified_fact_routes_and_binds_the_exact_observed_choice(label, choices, expected):
    field, answers = question(label, choices=choices), facts()
    original = copy.deepcopy(answers)
    key = known_answers.enrich(field, job(), answers)
    assert key and answers[key]["value"] == expected
    assert all(answers[k] == v for k,v in original.items())
    assert answers[key]["source"]["records"]
    assert key_for_field(field, answers) == key
    snapshot = {"fields": [field], "buttons": []}
    assert validate_plan(deterministic_plan(snapshot, answers), snapshot, answers)["bindings"] == [{"ref":field["ref"],"answer_key":key}]
    assert question_routing.field_route({**field,"choices":choices}, original, job=job()) == question_routing.KNOWN


@pytest.mark.parametrize("label", [ONSITE, PROJECT, DISCOVERY])
@pytest.mark.parametrize("changed", ["description", "truncated", "duplicate", "unsupported", "own_words"])
def test_changed_context_or_catalog_cannot_become_a_known_answer(label, changed):
    field = question(label, choices=CHOICES if label==DISCOVERY else ["Yes","No"])
    if changed=="description":field["description"]="Also confirm current full-time work authorization."
    elif changed=="truncated":field["description_truncated"]=True
    elif changed=="duplicate":field["options"].append(copy.deepcopy(field["options"][-1 if label==DISCOVERY else 0]))
    elif changed=="unsupported":field["options"]=[{"label":"Not an observed compatible option"}]
    else:field["description"]="Use your own words. Do not use AI."
    values=facts()
    assert known_answers.enrich(field,job(),values) is None
    assert question_routing.field_route(field, values, job=job()) == question_routing.CANDIDATE


@pytest.mark.parametrize("changed", ["office", "relocation", "projects", "blank_projects", "source", "url", "identity", "partial_catalog"])
def test_derivations_require_current_verified_basis_and_exact_source_job(changed):
    values, current = facts(), job()
    field=question(ONSITE if changed in {"office","relocation"} else PROJECT if "projects" in changed else DISCOVERY,
                   choices=CHOICES if changed in {"source","url","identity","partial_catalog"} else ["Yes","No"])
    assert known_answers.enrich(field,current,values)
    if changed=="office":values["standing.office_willingness"]["value"]=False
    elif changed=="relocation":values["preferences.relocation"]["status"]="needs_input"
    elif changed=="projects":values["role.projects"]["status"]="needs_input"
    elif changed=="blank_projects":values["role.projects"]["value"]="N/A"
    elif changed=="source":current["source"]="user_provided"
    elif changed=="url":current["source_url"]="https://different.example/discovered"
    elif changed=="identity":current["url"]=current["url"].replace("123","999")
    else:field["options_truncated"]=True
    assert known_answers.enrich(field,current,values) is None


def test_no_personal_referral_does_not_hide_recorded_discovery_source():
    values=facts()
    values['screening.personal_referral']=booklet.answer(False,'Synthetic no personal referral')
    field=question(DISCOVERY,choices=CHOICES)
    key=known_answers.enrich(field,job(),values)
    assert values[key]['value']=='Other'


@pytest.mark.parametrize('value',[False,True,'Synthetic direct answer'])
def test_existing_verified_answer_to_discovery_question_is_preserved(value):
    # screening.referral is the legacy discovery-question alias, distinct from
    # the factual screening.personal_referral boolean. Do not reinterpret an
    # explicit older answer here; incompatible answers need scoped review.
    values=facts();values['screening.referral']=booklet.answer(value,'Synthetic existing discovery response')
    before=copy.deepcopy(values)
    assert known_answers.enrich(question(DISCOVERY,choices=CHOICES),job(),values) is None
    assert values==before


def conditional():
    parent=question("How did you hear about Synthetic?*",choices=["Simplify","Other"],ref="question_100")
    detail=question("If other, please specify",kind="text",choices=[],ref="question_101",required=False)
    values=facts()
    retained={parent["ref"]:{"ref":parent["ref"],"question":parent["label"],"key":"standing.discovery_source",
            "value":"Simplify","source":values["standing.discovery_source"]["source"]}}
    snapshot={"url":job()["url"],"fields":[parent,detail]}
    return values,retained,snapshot,parent,detail


def test_conditional_omission_requires_retained_adjacent_owned_parent_and_stays_review_blank():
    values,retained,snapshot,parent,detail=conditional()
    proof=_discovery_detail_omission(detail,values,snapshot=snapshot,filled=retained,job=job())
    assert proof["parent_ref"]==parent["ref"] and proof["selected_choice"]=="Simplify"
    from jhb.applications.review_inventory import build
    inventory=build(snapshot["fields"],list(retained.values()),values,key_for_field,complete=True,step_count=1)
    last=inventory["review_inventory"]["fields"][-1]
    assert last["status"]=="blank" and last["required"] is False and last["source"] is None
    assert not any(record.get("status")=="declined" for record in values.values())


@pytest.mark.parametrize("changed",["parent_other","unretained","source_drift","wrong_parent","nonadjacent",
    "duplicate","wrong_job","required","context","unknown_options","wrong_value"])
def test_conditional_never_uses_bare_label_or_assumed_parent(changed):
    values,retained,snapshot,parent,detail=conditional()
    if changed=="parent_other":
        values["standing.discovery_source"]["value"]="Other";retained[parent["ref"]]["value"]="Other"
    elif changed=="unretained":retained.clear()
    elif changed=="source_drift":retained[parent["ref"]]["source"]={"different":"source"}
    elif changed=="wrong_parent":parent["label"]="Are you a US citizen?"
    elif changed=="nonadjacent":snapshot["fields"].insert(1,question("A different question",ref="between"))
    elif changed=="duplicate":snapshot["fields"].append(copy.deepcopy(parent))
    elif changed=="wrong_job":snapshot["url"]=snapshot["url"].replace("123","999")
    elif changed=="required":detail["required"]=True
    elif changed=="context":detail["description"]="Explain why you chose this answer in your own words."
    elif changed=="unknown_options":parent["options"]=[]
    else:retained[parent["ref"]]["value"]="LinkedIn"
    assert _discovery_detail_omission(detail,values,snapshot=snapshot,filled=retained,job=job()) is None


def test_normal_preparation_reconciles_conditional_only_after_native_parent_fill():
    values,_,snapshot,parent,detail=conditional()
    class Form:
        blocked_requests=0
        def __init__(self):self.values={}
        def allowed_url(self,url):return url==job()["url"]
        async def open(self,url):assert url==job()["url"]
        async def ensure_education(self,count):pass
        async def observe(self):return {**snapshot,"buttons":[{"ref":"submit","label":"Submit application"}]}
        async def fill(self,field,value):self.values[field["ref"]]=value
        async def click_next(self,button):raise AssertionError("No submit or navigation")
    form=Form();result,_=asyncio.run(prepare(None,job(),values,deterministic_plan,None,cli_actions=form))
    assert result["state"]=="waiting_review" and form.values=={parent["ref"]:"Simplify"}
    assert not result.get("optional_questions") and detail["ref"] in result["resolved_optional_refs"]
    assert result["review_inventory"]["fields"][-1]["status"]=="blank"
    assert any(event["event"]=="conditional_optional_inapplicable" for event in result["events"])


@pytest.mark.parametrize("label",["Are you legally authorized to work in the United States on a full-time basis?*",
    "Do you identify as transgender? (select one)","Did you attend a fall career fair?*"])
def test_unrelated_facts_are_not_answered_by_these_projections(label):
    field=question(label);values=facts()
    assert known_answers.enrich(field,job(),values) is None
    assert question_routing.field_route(field,values,job=job())==question_routing.CANDIDATE


@pytest.mark.parametrize("mode", ["delegated", "portal"])
@pytest.mark.parametrize("change", [None, "retained", "source_fact"])
def test_native_final_audit_checks_all_three_projected_answers_without_submission(tmp_path, monkeypatch, mode, change):
    from html import escape
    from playwright.sync_api import sync_playwright
    from jhb import config
    from jhb.applications import native_question_context, review_inventory, submission_runtime
    from jhb.applications.cli_runtime import dispatch
    from tests.applications.test_reviewed_derived_catalogs import URL, bind
    monkeypatch.setattr(config,"ROOT",tmp_path)
    current={**job(),"url":URL,"dedupe_hash":boards.application_hash(URL),"role_classes":"sde"}
    values=facts()
    profile={"schema_version":1,"answers":{"preferences.relocation":values["preferences.relocation"]},
             "roles":{"sde":{"role.projects":values["role.projects"]},"ml":{}},"custom_answers":{},
             "workflow_preferences":{"office_locations":{"value":True,"source":"Synthetic explicit onsite willingness"}}}
    rows=[(ONSITE,["Yes","No"],"Yes"),(PROJECT,["Yes","No"],"Yes"),(DISCOVERY,CHOICES,"Other")]
    html='<form id="application">'+''.join(
        f'<label for="question_{i}">{escape(label)}</label><select id="question_{i}" required>'+''.join(
            f'<option value="{escape(choice)}" {"selected" if choice==selected else ""}>{escape(choice)}</option>' for choice in choices)+'</select>'
        for i,(label,choices,selected) in enumerate(rows))+'''<button type="submit">Submit application</button></form>
        <script>window.submissions=0;document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++}</script>'''
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page()
        try:
            page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=html));page.goto(URL)
            session=page.context.new_cdp_session(page)
            helpers={'cdp':lambda method,**params:session.send(method,params),'js':page.evaluate,
                'wait':lambda s:page.wait_for_timeout(s*1000),'click_at_xy':page.mouse.click,
                'current_tab':lambda:{'targetId':'fixture'},'switch_tab':lambda target:None,
                'list_tabs':lambda:[{'targetId':'fixture','url':URL}]}
            dispatch({'operation':'open','url':URL},helpers)
            snapshot=dispatch({'operation':'observe'},helpers)
            answers=booklet.for_role(profile,'sde',job=current)
            native_question_context.enrich_sync(snapshot,current,answers,lambda f:dispatch({'operation':'describe','field':f},helpers))
            retained=[]
            for field in snapshot['fields']:
                key=known_answers.enrich(field,current,answers);assert key
                retained.append({'ref':field['ref'],'question':field['label'],'key':key,
                                 'value':answers[key]['value'],'source':answers[key]['source']})
            packet={'job':current,'selected_role':'sde','filled':retained,
                    **review_inventory.build(snapshot['fields'],retained,answers,key_for_field,complete=True)}
            request,attempt,_=bind(tmp_path,packet,profile,mode)
            if change=='source_fact':
                profile['roles']['sde']['role.projects']['value']='Changed candidate projects'
                booklet.write_private(tmp_path/'private/book.json',profile)
                with pytest.raises(ValueError,match='facts'):
                    submission_runtime._checks(request,helpers,packet,attempt)
            else:
                if change=='retained':page.select_option('#question_2','LinkedIn')
                result=submission_runtime._checks(request,helpers,packet,attempt)
                if change is None:assert result['double_check_count']==3 and not result.get('state')
                else:assert result['state']=='waiting_review' and result['click_started'] is False
            assert page.evaluate('window.submissions')==0
        finally:browser.close()
