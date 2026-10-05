"""Synthetic current-study projections and native school catalog mechanics."""
import asyncio
from copy import deepcopy
from datetime import date

import pytest

from jhb.applications import ashby_education as education, booklet
from jhb.applications.manual_ats import ManualATSCLI
from jhb.applications.manual_runtime import application_scope, dispatch

URL = "https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application"
NOTE = "For most recent or in progress degree."
TODAY = date(2026, 10, 5)


def answers():
    record = {"school": "Example University", "degree": "Master of Science", "major": "Computer Science",
              "start_date": "2025-09", "end_date": "2026-12", "expected": True,
              "status": "verified", "source": "synthetic resume"}
    return {"standing.current_education_school": booklet.answer(record["school"], {
                "method": "verified_expected_education_record", "expected": True,
                "original_source": record["source"], "original_record": record}),
            "education.expected_graduation_date": booklet.answer("2026-12-14", "synthetic explicit exact day")}


def field(column):
    values = {
        "school": ("ashby:_systemfield_education_history:control:0", "School Name", "combobox"),
        "degree": ("ashby:degree", "Degree", "radio"),
        "major": ("major", "Discipline/Field of Study", "text"),
        "graduation": ("ashby:graduation:control:0", "Graduation Date or Anticipated Graduation Date", "text"),
    }
    ref, label, kind = values[column]
    return {"ref": ref, "label": label, "type": kind, "description": NOTE,
            "description_truncated": False, "required": True, "calendar_format": "MM/DD/YYYY",
            "options": [{"label": v, "value": v} for v in
                        (["Example University", "Other"] if column == "school" else ["Bachelor's Degree", "Master's Degree"])]}


@pytest.mark.parametrize("column,expected", [
    ("school", {"query": "Example University", "choice": "Example University"}),
    ("degree", "Master's Degree"), ("major", "Computer Science"), ("graduation", "2026-12-14"),
])
def test_one_current_record_supplies_all_four_current_or_progress_fields(column, expected):
    catalog = answers()
    before = deepcopy(catalog)
    value, source = education.derive(field(column), catalog, as_of=TODAY)
    assert value == expected
    assert source["expected"] is True
    assert source["records"]["standing.current_education_school"]["source"]["original_record"]["expected"] is True
    assert catalog == before


@pytest.mark.parametrize("column", ["school", "degree", "major", "graduation"])
@pytest.mark.parametrize("description,truncated", [
    ("For completed degree only.", False), (NOTE, True), ("", False),
])
def test_changed_or_clipped_education_context_never_claims_current_degree_is_completed(column, description, truncated):
    f = {**field(column), "description": description, "description_truncated": truncated}
    assert education.derive(f, answers(), as_of=TODAY) is None
    if column == "school":
        assert not education.school_control(f)


@pytest.mark.parametrize("choices", [[], ["Other"], ["Example University", "Example University"], ["Example State University"]])
def test_school_requires_one_actual_original_institution_choice(choices):
    f = {**field("school"), "options": [{"label": v, "value": v} for v in choices]}
    assert education.derive(f, answers(), as_of=TODAY) is None


@pytest.mark.parametrize("change", ["unverified", "earned", "expired", "different_school", "missing_source", "malformed"])
def test_school_basis_rejects_stale_or_unverified_current_study(change):
    a = answers()
    item = a["standing.current_education_school"]
    record = item["source"]["original_record"]
    if change == "unverified": item["status"] = "needs_input"
    if change == "earned": record["expected"] = False
    if change == "expired": record["end_date"] = "2025-12"
    if change == "different_school": record["school"] = "Different University"
    if change == "missing_source": record["source"] = None
    if change == "malformed": item["source"]["original_record"] = []
    assert education.school_basis(a, as_of=TODAY) is None


@pytest.mark.parametrize("day", [None, "2026-11-14", "2026-12-32", "December 14"])
def test_graduation_requires_explicit_real_day_in_the_same_current_degree_month(day):
    a = answers()
    a["education.expected_graduation_date"]["value"] = day
    assert education.derive(field("graduation"), a, as_of=TODAY) is None
    assert education.derive({**field("graduation"), "calendar_format": None}, answers(), as_of=TODAY) is None


def test_current_education_is_bound_through_production_planner_and_audited_after_catalog_closes():
    from jhb.applications.known_answers import enrich
    from jhb.applications.planner import key_for_field, deterministic_plan, validate_plan
    a = answers()
    fields = [field(column) for column in ("school", "degree", "major", "graduation")]
    for f in fields:
        assert enrich(f, {}, a, as_of=TODAY)
    plan = validate_plan(deterministic_plan({"fields": fields, "buttons": []}, a),
                         {"fields": fields, "buttons": []}, a)
    assert len(plan["bindings"]) == 4
    closed = {**fields[0], "options": []}
    assert key_for_field(closed, a) == key_for_field(fields[0], a)
    changed = {**fields[0], "description": "For an already completed degree."}
    assert key_for_field(changed, a) is None
    assert enrich(changed, {}, a, as_of=TODAY) is None


def test_controller_queries_only_the_verified_current_school(monkeypatch):
    import jhb.applications.ashby_education
    original = education.school_basis
    monkeypatch.setattr(jhb.applications.ashby_education, "school_basis", lambda a, **kwargs: original(a, as_of=TODAY))
    class CLI(ManualATSCLI):
        def __init__(self): super().__init__(URL); self.calls = []
        async def invoke(self, operation, **payload):
            self.calls.append((operation, payload))
            if operation == "observe": return {"fields": [field("school")], "buttons": []}
            return {"choices": ["Example University", "Other"], "truncated": False}
    cli = CLI()
    async def inspect():
        await cli.ensure_profile(answers())
        return await cli.observe()
    snapshot = asyncio.run(inspect())
    assert [p["query"] for op, p in cli.calls if op == "describe"] == ["Example University"]
    assert education.derive(snapshot["fields"][0], answers(), as_of=TODAY)[0]["choice"] == "Example University"


@pytest.mark.parametrize("available", [True, False])
def test_native_school_catalog_restores_blank_and_requires_committed_owned_option(available):
    from playwright.sync_api import sync_playwright
    html = '''<form class=ashby-application-form-container><div data-field-path=_systemfield_education_history>
<label class=ashby-application-form-question-title>School Name</label>
<div class=ashby-application-form-question-description>For most recent or in progress degree.</div>
<input role=combobox aria-expanded=false oninput="menu(this)"><div id=options role=listbox></div>
</div><button type=submit>Submit</button></form>
<div role=listbox><div role=option>Example University</div></div>
<script>window.commits=0;window.submissions=0;
document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};
function menu(e){e.setAttribute('aria-controls','options');e.setAttribute('aria-expanded','true');
if(AVAILABLE && e.value)document.querySelector('#options').innerHTML='<div role="option" onclick="choose()">Example University</div><div role="option">Other</div>';else document.querySelector('#options').innerHTML=''}
function choose(){window.commits++;const e=document.querySelector('input');e.value='Example University';e.setAttribute('aria-expanded','false');document.querySelector('#options').innerHTML=''}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){document.querySelector('input').setAttribute('aria-expanded','false');document.querySelector('#options').innerHTML=''}});
</script>'''.replace('AVAILABLE', 'true' if available else 'false')
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        page.goto(URL)
        session = page.context.new_cdp_session(page)
        helpers = {"cdp": lambda method, **params: session.send(method, params), "js": page.evaluate,
                   "wait": lambda seconds: page.wait_for_timeout(seconds * 1000), "click_at_xy": page.mouse.click,
                   "list_tabs": lambda: [{"targetId": "fixture", "url": URL}],
                   "current_tab": lambda: {"targetId": "fixture"}, "switch_tab": lambda target: None}
        def call(op, **payload):
            return dispatch({"operation": op, "scope": application_scope(URL), **payload}, helpers)
        try:
            call("open", url=URL)
            observed = call("observe")["fields"][0]
            catalog = call("describe", field=observed, query="Example University")
            assert page.locator("input").input_value() == ""
            assert page.evaluate("window.commits") == 0
            observed["options"] = [{"label": v, "value": v} for v in catalog["choices"]]
            projected = education.derive(observed, answers(), as_of=TODAY)
            if available:
                assert call("fill", field=observed, value=projected[0])["verified"]
                assert page.locator("input").input_value() == "Example University"
                assert call("describe", field=observed, query="Example University")["source"] == "retained_owned_native_catalog"
                assert page.evaluate("window.commits") == 1
                page.locator(".ashby-application-form-question-description").evaluate("e=>e.textContent='Completed degrees only.'")
                with pytest.raises(ValueError, match="outside its approved scope"):
                    call("describe", field=observed, query="Example University")
                with pytest.raises(ValueError, match="Observed manual field has changed"):
                    call("fill", field=observed, value=projected[0])
                assert page.evaluate("window.commits") == 1
            else:
                assert projected is None  # A foreign listbox cannot supply a choice.
            assert page.evaluate("window.submissions") == 0
            assert page.evaluate("window.__jhbGuard") is True
        finally:
            browser.close()
