"""Synthetic injected Codex responses: no auth, network, private profile or browser."""
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from jhb import config
from jhb.applications import boards, booklet, grounded_narratives as drafts, worker
from jhb.applications.planner import deterministic_plan


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setenv("CI", "1")
    url = "https://jobs.ashbyhq.com/synthetic/11111111-2222-3333-4444-555555555555"
    text = "Build reliable retrieval APIs for useful search results. Work with customers to measure quality."
    job = {"url": url, "company": "Synthetic Search", "selected_role": "ml", "verified_job_description": {
        "status": "verified", "source_url": url, "job_identity": list(boards.job_identity(url)),
        "retrieved_at": time.time(), "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}}
    resume = tmp_path / "chosen-ml.pdf"; resume.write_bytes(b"%PDF-1.4\nSynthetic chosen ML resume")
    answers = {"documents.resume": booklet.answer(str(resume), "Synthetic selected ML resume"),
        "role.experience": booklet.answer("Synthetic Lab  Jan 2024 – May 2024\nEngineer\n• Improved retrieval quality by 20% through measured evaluation.", "Synthetic ML resume"),
        "role.skills": booklet.answer("Python, retrieval, APIs", "Synthetic ML skills"),
        "other_role.experience": booklet.answer("Built embedded hardware with 99% improvement", "Unselected SDE facts"),
        "identity.email": booklet.answer("excluded@example.invalid", "Synthetic private contact")}
    field = {"ref": "observed-interest", "label": "Tell us why you would like to join our search team.", "type": "textarea", "required": True}
    return field, job, answers


def model(calls, mutate=None):
    def execute(command, **kwargs):
        inputs = json.loads(kwargs["input"].split("\nINPUT:\n", 1)[1])
        calls.append((command, kwargs, inputs))
        company = inputs['job_description']['units'][0]
        candidate_units = inputs['facts']['role.experience']['units']
        candidate = next((unit for unit in candidate_units if 'Improved retrieval' in unit['text']), candidate_units[0])
        recipe = {"field_ref": inputs["field_ref"], "state": "proposed", "framing": "focus", "closing": "contribute",
            "company_quote": company['text'], 'company_unit_id': company['id'], "candidate_key": "role.experience",
            "candidate_quote": candidate['text'], 'candidate_unit_id': candidate['id'], "answer": ""}
        recipe["answer"] = drafts.render(inputs, recipe)
        if mutate: mutate(recipe)
        Path(command[command.index("--output-last-message")+1]).write_text(json.dumps(recipe))
        return subprocess.CompletedProcess(command, 0, "", "")
    return execute


def test_brief_company_focused_proposal_preserves_exact_selected_facts_and_has_review_provenance(context, monkeypatch):
    field, job, answers = context
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-do-not-pass")
    monkeypatch.setenv("SMTP_PASSWORD", "synthetic-do-not-pass")
    calls = []
    result = drafts.draft(field, job, answers, execute=model(calls), preferences={"max_words": 65, "tone": "direct"})
    assert result["state"] == "proposed", result
    record = result["record"]
    assert record["proposed"] and record["source"]["review_status"] == "proposed"
    assert record["field_ref"] == field["ref"] and "Synthetic Search" in record["value"]
    assert "20%" in record["value"] and "99%" not in record["value"]
    assert len(record["value"].split()) <= 65 and record["source"]["selected_role"] == "ml"
    assert record["source"]["resume_sha256"] == hashlib.sha256(Path(answers["documents.resume"]["value"]).read_bytes()).hexdigest()
    assert all(support["quote"] in (job["verified_job_description"]["text"] if support["input_id"] == "job_description" else answers[support["input_id"]]["value"]) for support in record["source"]["support"])
    command, kwargs, inputs = calls[0]
    assert command[:2] == ["codex", "exec"] and command[command.index("--sandbox")+1] == "read-only"
    assert "--ignore-user-config" in command and "features.shell_tool=false" in command and 'web_search="disabled"' in command
    assert "--output-schema" in command and "mcp_servers={}" in command
    assert "OPENAI_API_KEY" not in kwargs["env"] and "SMTP_PASSWORD" not in kwargs["env"]
    assert "excluded@example.invalid" not in kwargs["input"] and "Unselected SDE facts" not in kwargs["input"]
    assert str(Path(answers["documents.resume"]["value"])) not in kwargs["input"]
    files = list((config.ROOT / "private" / "grounded-narratives").glob("*.json"))
    assert len(files) == 1 and files[0].stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("label", [
    "Why did you leave your last job?", "Describe a project: what did you own, what failed and how did you fix it?",
    "What motivates you and how many years of ML experience do you have?", "Why should we sponsor your visa?",
    "Are you legally authorized to work?", "Have you earned your degree?", "Do you agree to arbitration?",
    "Why are you interested in this team? Please, no AI text", "What motivates you? In your own words.",
    "What motivates you? Do not use ChatGPT", "What is your greatest weakness?",
    "Why do you want to leave your current role?", "What motivates you outside work?",
    "Why would you join our team? Don't use AI.",
    "Why would you join our team? Please no AI.", "Why would you join our team? Without AI assistance.",
    "Why would you join our team? Please do not use generative AI.",
    "Why this company? Without using large language models.",
    "What motivates you? Refrain from using artificial intelligence.",
])
def test_factual_unknown_history_and_candidate_only_prompts_never_call_a_model(context, label):
    field, job, answers = context; field["label"] = label
    calls = []
    assert drafts.draft(field, job, answers, execute=model(calls))["state"] == "needs_input"
    assert calls == []


@pytest.mark.parametrize('source,shortened', [('job_description', 'cryptocurrency'),
                                            ('role.experience', 'built production ML systems')])
def test_exact_substring_cannot_strip_negation_from_a_complete_unit(context, source, shortened):
    field, job, answers = context
    text = 'This role does not work on cryptocurrency.'
    job['verified_job_description'].update(text=text, sha256=hashlib.sha256(text.encode()).hexdigest())
    answers['role.experience'] = booklet.answer('I have not built production ML systems.', 'Synthetic verified factual statement')
    original = model([])
    def execute(command, **kwargs):
        result = original(command, **kwargs)
        inputs = json.loads(kwargs['input'].split('\nINPUT:\n', 1)[1])
        output = Path(command[command.index('--output-last-message')+1])
        recipe = json.loads(output.read_text())
        recipe['company_quote' if source == 'job_description' else 'candidate_quote'] = shortened
        recipe['answer'] = drafts.render(inputs, recipe)
        output.write_text(json.dumps(recipe))
        return result
    assert drafts.draft(field, job, answers, execute=execute)['state'] == 'needs_input'


@pytest.mark.parametrize('text', ['If licensed, you may build models. Otherwise, you cannot represent clients.',
                                 'I have not\nbuilt production ML systems.',
                                 'Do not:\n• Build cryptocurrency services.\n• Represent clients.'])
def test_semantic_units_preserve_wrapped_negation_and_leading_conditions(text):
    units = drafts.semantic_units(text, 'synthetic', 35)
    assert len(units) == 1 and units[0]['text'] == text
    assert drafts.semantic_units(text, 'synthetic', 2) == []


def test_complete_achievement_bullet_excludes_header_but_keeps_wrapped_context():
    text = 'Synthetic Lab\nResearch Engineer\n• Improved retrieval by 20%\n  using measured evaluation.\n• Built Python APIs.'
    units = drafts.semantic_units(text, 'role.experience', 35)
    assert [unit['text'] for unit in units] == ['Synthetic Lab\nResearch Engineer',
            'Improved retrieval by 20%\n  using measured evaluation.', 'Built Python APIs.']
    assert all(unit['text'] in text for unit in units)


def test_oversized_indivisible_company_context_hands_off_without_model(context):
    field, job, answers = context
    text = 'Only after receiving required approval may this role ' + 'perform complex engineering work '*10 + '.'
    job['verified_job_description'].update(text=text, sha256=hashlib.sha256(text.encode()).hexdigest())
    calls = []
    assert drafts.draft(field, job, answers, execute=model(calls))['state'] == 'needs_input'
    assert calls == []


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(field_ref="another-control"),
    lambda r: r.update(company_quote="industry-leading quantum hardware"),
    lambda r: r.update(candidate_quote="Improved retrieval quality by 99% through measured evaluation."),
    lambda r: r.update(candidate_key="other_role.experience"),
    lambda r: r.update(company_unit_id='wrong-unit'),
    lambda r: r.update(candidate_unit_id='wrong-unit'),
    lambda r: r.update(answer=r["answer"]+" I led a team of ten engineers."),
    lambda r: r.update(answer=r["answer"]+" Synthetic Search is the global market leader."),
    lambda r: r.update(state="needs_input"),
])
def test_fabricated_support_wrong_ref_and_extra_factual_claims_fail_closed_and_back_off(context, mutation):
    calls = []
    execute = model(calls, mutation)
    assert drafts.draft(*context, execute=execute)["state"] == "needs_input"
    assert drafts.draft(*context, execute=execute)["state"] == "needs_input"
    assert len(calls) == 1


def test_cache_normalizes_prompt_spacing_but_invalidates_changed_jd_resume_and_role_facts(context):
    field, job, answers = context; calls = []
    execute = model(calls)
    first = drafts.draft(field, job, answers, execute=execute)
    field["label"] = "  TELL US WHY YOU WOULD LIKE TO JOIN OUR SEARCH TEAM.  "
    second = drafts.draft(field, job, answers, execute=execute)
    assert first["record"]["value"] == second["record"]["value"] and len(calls) == 1
    description = job["verified_job_description"]
    description["text"] += " Measure API latency."
    description["sha256"] = hashlib.sha256(description["text"].encode()).hexdigest()
    assert drafts.draft(field, job, answers, execute=execute)["state"] == "proposed" and len(calls) == 2
    Path(answers["documents.resume"]["value"]).write_bytes(b"%PDF-1.4\nNew synthetic selected resume")
    assert drafts.draft(field, job, answers, execute=execute)["state"] == "proposed" and len(calls) == 3
    answers["role.skills"]["value"] += ", evaluation"
    assert drafts.draft(field, job, answers, execute=execute)["state"] == "proposed" and len(calls) == 4


@pytest.mark.parametrize("missing", ["resume", "role", "jd", "facts"])
def test_verified_selected_resume_and_exact_official_job_evidence_are_mandatory(context, missing):
    field, job, answers = context; calls = []
    if missing == "resume": answers["documents.resume"]["status"] = "needs_input"
    elif missing == "role": job["selected_role"] = None
    elif missing == "jd": job["verified_job_description"]["sha256"] = "0"*64
    else:
        answers["role.experience"]["status"] = "needs_input"; answers["role.skills"]["status"] = "needs_input"
    assert drafts.draft(field, job, answers, execute=model(calls))["state"] == "needs_input" and calls == []


def test_timeout_and_attempted_tool_are_handoffs_without_repeated_calls(context):
    calls = []
    def timed_out(command, **kwargs):
        calls.append(command); raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    assert drafts.draft(*context, execute=timed_out)["state"] == "needs_input"
    assert drafts.draft(*context, execute=timed_out)["state"] == "needs_input" and len(calls) == 1
    field, job, answers = context; field["ref"] = "new-ref"
    execute = model([])
    def tools(command, **kwargs):
        result = execute(command, **kwargs)
        result.stdout = json.dumps({"type": "item.completed", "item": {"type": "command_execution"}})
        return result
    assert drafts.draft(field, job, answers, execute=tools)["state"] == "needs_input"


def test_ci_default_never_uses_subscription_auth(context, monkeypatch):
    monkeypatch.setattr(drafts, "_run", lambda *a, **k: pytest.fail("Real Codex invoked in CI"))
    assert drafts.draft(*context)["reason_code"] == "ci_disabled"


def test_timeout_terminates_and_reaps_actual_synthetic_process(tmp_path):
    import os
    pidfile = tmp_path / "process.pid"
    script = "import os,time;open("+repr(str(pidfile))+",'w').write(str(os.getpid()));time.sleep(10)"
    with pytest.raises(subprocess.TimeoutExpired):
        drafts._run([sys.executable, "-c", script], input="", text=True, capture_output=True, timeout=.2, env={"PATH": os.environ["PATH"]})
    assert pidfile.is_file()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_short_why_company_variant_is_qualitative_but_unsupported_other_company_is_not(context):
    field, job, answers = context
    field["label"] = "Why Synthetic Search?"
    assert drafts.draft(field, job, answers, execute=model([]))["state"] == "proposed"
    field["label"] = "Why Different Company?"
    assert drafts.draft(field, job, answers, execute=model([]))["state"] == "needs_input"


class Form:
    blocked_requests = 0
    def __init__(self, field): self.fields = [field]; self.values = {}
    def allowed_url(self, url): return True
    async def open(self, url): pass
    async def observe(self): return {"fields": self.fields, "buttons": [{"ref": "submit", "label": "Submit application"}]}
    async def fill(self, field, value): self.values[field["ref"]] = value


@pytest.mark.parametrize("enabled", [False, True])
def test_worker_optional_drafting_is_default_off_and_proposals_remain_review_only(context, monkeypatch, enabled):
    field, job, answers = context; form = Form(field); calls = []
    original = drafts.draft
    monkeypatch.setenv("JHB_GROUNDED_NARRATIVES", "1" if enabled else "0")
    monkeypatch.setattr(drafts, "draft", lambda *a, **k: original(*a, **k, execute=model(calls)))
    result, _ = asyncio.run(worker.prepare(None, job, answers, deterministic_plan, None, cli_actions=form))
    assert result["state"] == ("waiting_review" if enabled else "waiting_input"), result
    assert bool(form.values) is enabled and len(calls) == int(enabled)
    if enabled:
        assert result["review_inventory"]["fields"][0]["proposed"] is True
        assert result["filled"][0]["proposed"] is True
        assert result["review_completeness"]["requires_explicit_approval"] is True


def test_worker_limits_drafting_calls_and_preserves_verified_factual_answers(context, monkeypatch):
    field, job, answers = context; form = Form(field); calls = []
    form.fields = [{**field, "ref": "why-"+str(i)} for i in range(4)] + [
        {"ref": "authorization", "label": "Are you legally authorized to work in the United States?", "type": "text", "required": True}]
    answers["eligibility.authorized_us"] = booklet.answer(True, "Synthetic explicit user fact")
    original = drafts.draft
    monkeypatch.setenv("JHB_GROUNDED_NARRATIVES", "1")
    monkeypatch.setattr(drafts, "draft", lambda *a, **k: original(*a, **k, execute=model(calls)))
    result, _ = asyncio.run(worker.prepare(None, job, answers, deterministic_plan, None, cli_actions=form))
    assert len(calls) == 3 and result["state"] == "waiting_input"
    assert form.values["authorization"] is True
    assert any(row["ref"] == "why-3" for row in result["missing"])
    assert all("authorization" != inputs["field_ref"] for _, _, inputs in calls)
