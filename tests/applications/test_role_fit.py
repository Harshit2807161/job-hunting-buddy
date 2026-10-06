import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest

from jhb import config
from jhb.applications import boards, role_fit
from jhb.applications.booklet import answer

URL = "https://jobs.ashbyhq.com/synthetic/11111111-2222-3333-4444-555555555555"


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    (tmp_path / "schemas").mkdir()
    shutil.copy(config.ROOT / "schemas/role-fit.json", tmp_path / "schemas/role-fit.json")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("JHB_ROLE_FIT_REVIEW", raising=False)
    description = "Early career engineer building Python backend APIs and web integrations."
    job = {"dedupe_hash": boards.application_hash(URL), "url": URL, "title": "Software Engineer", "company": "Synthetic",
           "verified_job_description": {"text": description, "status": "verified", "source_url": URL,
            "retrieved_at": time.time(), "sha256": hashlib.sha256(description.encode()).hexdigest(),
            "job_identity": list(boards.job_identity(URL))}}
    book = {"roles": {"sde": {"role.skills": {"value": "Python, C++, SQL, REST APIs, React",
                       "status": "verified", "source": "Synthetic resume"}}}, "education_records": []}
    return job, book, "sde"


@pytest.mark.parametrize("title", ["Robotics Software Engineer - New Grad", "Embedded Software Engineer", "FPGA Engineer"])
def test_general_coding_skills_do_not_prove_absent_core_specialty(inputs, title):
    job, book, role = inputs
    assert role_fit.assess({**job, "title": title}, book, role)["state"] == "skipped"


def test_robotics_company_backend_role_is_not_rejected_by_company_name(inputs):
    job, book, role = inputs
    assert role_fit.assess({**job, "company": "Synthetic Robotics"}, book, role)["state"] == "eligible"


def test_work_preferences_reuse_verified_willingness_without_identity_or_disclosures(inputs):
    job, book, role = inputs
    book['answers'] = {'preferences.relocation': answer(True, 'Synthetic explicit relocation preference'),
                       'preferences.remote': answer(None), 'identity.email': answer('sam@example.test'),
                       'disclosure.gender': answer('Synthetic private disclosure')}
    book['workflow_preferences'] = {'office_locations': 'Willing to work onsite or hybrid',
                                    'source': 'Synthetic explicit office preference'}
    data = role_fit.evidence(job, book, role)
    assert set(data['work_preferences']) == {'preferences.relocation', 'office_locations'}
    assert data['work_preferences']['preferences.relocation']['value'] is True
    assert 'sam@example.test' not in json.dumps(data)
    before = role_fit.evidence_hash(job, book, role)
    book['answers']['preferences.relocation']['value'] = False
    assert role_fit.evidence_hash(job, book, role) != before


def test_explicit_exact_job_exclusion_precedes_fit(inputs):
    job, book, role = inputs
    book["job_exclusions"] = {job["dedupe_hash"]: {"status": "verified", "source": "Explicit synthetic user refusal"}}
    assert role_fit.assess(job, book, role)["state"] == "skipped"


def executor(verdict, calls):
    def run(command, **kwargs):
        calls.append(kwargs["input"])
        assert "OPENAI_API_KEY" not in kwargs["env"] and "CODEX_API_KEY" not in kwargs["env"]
        schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
        assert set(schema["required"]) == set(schema["properties"])
        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps({"review_notes": [], **verdict}))
        return SimpleNamespace(returncode=0)
    return run


def test_independent_fit_cached_only_for_same_candidate_and_job_evidence(inputs, monkeypatch):
    monkeypatch.setenv("JHB_ROLE_FIT_REVIEW", "1")
    job, book, role = inputs
    calls = []
    run = executor({"verdict": "fit", "reason": "Python APIs match", "matched_requirements": ["Resume documents Python and REST APIs"],
                    "unsupported_core_requirements": []}, calls)
    result = role_fit.assess(job, book, role, execute=run)
    assert result["state"] == "eligible"
    role_fit.assess(job, book, role, execute=run)
    assert len(calls) == 1
    book["roles"][role]["role.skills"]["value"] += ", Redis"
    assert role_fit.assess(job, book, role, execute=run)["evidence_hash"] != result["evidence_hash"]
    assert len(calls) == 2
    assert "identity.email" not in calls[0]


@pytest.mark.parametrize("verdict", [
    {"verdict": "fit", "reason": "Yes", "matched_requirements": [], "unsupported_core_requirements": []},
    {"verdict": "fit", "reason": "Yes", "matched_requirements": ["Python"], "unsupported_core_requirements": ["Required hardware design"]},
    {"verdict": "fit"},
])
def test_incomplete_or_contradictory_reviewer_cannot_approve(inputs, monkeypatch, verdict):
    monkeypatch.setenv("JHB_ROLE_FIT_REVIEW", "1")
    assert role_fit.assess(*inputs, execute=executor(verdict, []))["state"] == "unsupported"


def test_ci_and_missing_verified_description_never_launch_live_reviewer(inputs, monkeypatch):
    monkeypatch.setenv("JHB_ROLE_FIT_REVIEW", "1"); monkeypatch.setenv("CI", "true")
    assert role_fit.assess(*inputs)["state"] == "unsupported"
    inputs[0]["verified_job_description"]["text"] = "Modified unverified description"
    assert role_fit.assess(*inputs)["state"] == "unsupported"


def update_description(job, text):
    job['verified_job_description'].update(text=text, sha256=hashlib.sha256(text.encode()).hexdigest())


def test_minor_early_career_experience_gap_survives_as_honest_review_note(inputs, monkeypatch):
    monkeypatch.setenv('JHB_ROLE_FIT_REVIEW', '1')
    job, book, role = inputs
    update_description(job, 'Junior data engineer: build Python and SQL data pipelines. Requires two years of relevant experience.')
    book['roles'][role]['role.experience'] = answer(
        'Synthetic Analytics  Jan 2025 – Apr 2026\nEngineer\n• Built Python/SQL validation pipelines.', 'Synthetic resume')
    original = json.dumps(book, sort_keys=True)
    calls = []
    result = role_fit.assess(job, book, role, execute=executor({
        'verdict':'fit', 'reason':'Strong transferable early-career pipeline skills support preparation for personal review.',
        'matched_requirements':['Verified Python and SQL pipeline work'], 'unsupported_core_requirements':[],
        'review_notes':['The role lists two years; documented relevant work is about sixteen months, not two years.']}, calls))
    assert result['state'] == 'eligible'
    assert result['unsupported_core_requirements'] == []
    assert result['review_notes'] == ['The role lists two years; documented relevant work is about sixteen months, not two years.']
    assert json.dumps(book, sort_keys=True) == original
    supplied = json.loads(calls[0].split('EVIDENCE:\n', 1)[1])
    assert supplied['candidate']['role.experience']['value'] == book['roles'][role]['role.experience']['value']
    assert 'do not round it up' in calls[0].lower()
    assert 'personal portal review' in calls[0]


def test_expected_degree_and_verified_availability_are_evidence_not_an_earned_degree(inputs, monkeypatch):
    monkeypatch.setenv('JHB_ROLE_FIT_REVIEW', '1')
    job, book, role = inputs
    book['answers'] = {'preferences.start_date':answer('January 2027', 'Synthetic approved availability'),
                      'eligibility.authorized_us':answer(False, 'Synthetic factual answer')}
    book['education_records'] = [{'status':'verified', 'source':'Synthetic resume', 'degree':'Master of Science',
        'major':'Computer Science', 'end_date':'2026-12-14', 'expected':True}]
    before = json.dumps(book, sort_keys=True)
    calls = []
    result = role_fit.assess(job, book, role, execute=executor({
        'verdict':'fit', 'reason':'The expected degree precedes the verified proposed start.',
        'matched_requirements':['Relevant verified engineering skills'], 'unsupported_core_requirements':[],
        'review_notes':['MS expected December 2026; not currently earned. Proposed availability is January 2027.']}, calls))
    supplied = json.loads(calls[0].split('EVIDENCE:\n', 1)[1])
    assert result['state'] == 'eligible'
    assert supplied['earliest_availability'] == {'value':'January 2027', 'calendar_value':'2027-01',
        'precision':'month', 'source':'Synthetic approved availability', 'status':'verified'}
    assert supplied['education'][0]['expected'] is True
    assert 'eligibility.authorized_us' not in supplied['candidate']
    assert json.dumps(book, sort_keys=True) == before
    assert 'immediately or at application time' in calls[0]


@pytest.mark.parametrize('value,expected,precision', [
    ('Jan 2027', '2027-01', 'month'), ('2027-01', '2027-01', 'month'),
    ('2027-01-14', '2027-01-14', 'day'), ('2027-02-30', None, None), ('ASAP', None, None),
])
def test_availability_preserves_original_calendar_precision_and_rejects_guessing(inputs, value, expected, precision):
    job, book, role = inputs
    book['answers'] = {'preferences.start_date':answer(value, 'Synthetic approved availability')}
    result = role_fit.evidence(job, book, role)['earliest_availability']
    if expected is None:
        assert result is None
    else:
        assert result['calendar_value'] == expected and result['precision'] == precision and result['value'] == value
    book['answers']['preferences.start_date']['status'] = 'needs_input'
    assert role_fit.evidence(job, book, role)['earliest_availability'] is None
    book['answers']['preferences.start_date'] = answer(value, '')
    assert role_fit.evidence(job, book, role)['earliest_availability'] is None


@pytest.mark.parametrize('title,description,gap', [
    ('Senior Data Scientist', 'Own department-wide architecture, mentor teams; eight years of production experience required.',
     'Actual senior architecture/leadership and eight years of professional depth unsupported'),
    ('Software Engineer', 'Design embedded real-time robotics motion controllers and sensor firmware.',
     'Core embedded robotics motion-control specialization unsupported'),
])
def test_real_seniority_and_core_specialization_are_still_hard_mismatches(inputs, monkeypatch, title, description, gap):
    monkeypatch.setenv('JHB_ROLE_FIT_REVIEW', '1')
    job, book, role = inputs
    job['title'] = title
    update_description(job, description)
    result = role_fit.assess(job, book, role, execute=executor({
        'verdict':'not_fit', 'reason':gap, 'matched_requirements':['Python syntax only'],
        'unsupported_core_requirements':[gap], 'review_notes':[]}, []))
    assert result['state'] == 'skipped' and result['unsupported_core_requirements'] == [gap]


def test_policy_bump_and_availability_change_invalidate_semantic_cache(inputs, monkeypatch):
    monkeypatch.setenv('JHB_ROLE_FIT_REVIEW', '1')
    job, book, role = inputs
    current_policy = role_fit.POLICY
    calls = []
    run = executor({'verdict':'fit', 'reason':'Relevant APIs', 'matched_requirements':['Verified REST APIs'],
                    'unsupported_core_requirements':[]}, calls)
    monkeypatch.setattr(role_fit, 'POLICY', 'resume-core-role-fit-v1')
    old = role_fit.assess(job, book, role, execute=run)
    monkeypatch.setattr(role_fit, 'POLICY', current_policy)
    new = role_fit.assess(job, book, role, execute=run)
    assert old['evidence_hash'] != new['evidence_hash'] and len(calls) == 2
    assert new['review_notes'] == []  # No qualification gap is represented by an empty array.
    book['answers'] = {'preferences.start_date':answer('January 2027','Synthetic approved availability')}
    changed = role_fit.assess(job, book, role, execute=run)
    assert changed['evidence_hash'] != new['evidence_hash'] and len(calls) == 3
    assert role_fit.assess(job, book, role, execute=run)['evidence_hash'] == changed['evidence_hash']
    assert len(calls) == 3


def test_timeout_retains_only_sanitized_partial_events_and_never_accepts_a_late_output(inputs, monkeypatch):
    monkeypatch.setenv("JHB_ROLE_FIT_REVIEW", "1")
    calls = []
    def timed_out(command, **kwargs):
        calls.append(kwargs["timeout"])
        assert kwargs["timeout"] == 120  # No blind deadline extension.
        # Even an output written before process timeout is not a completed run.
        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps({
            "verdict": "fit", "reason": "Synthetic premature verdict", "matched_requirements": ["Python"],
            "unsupported_core_requirements": [], "review_notes": []}))
        events = (b'{"type":"thread.started","thread_id":"synthetic-private-id"}\n'
                  b'{"type":"turn.started"}\n'
                  b'{"type":"error","message":"synthetic-secret-credential"}\n')
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=events, stderr=b"synthetic-private-error")
    result = role_fit.assess(*inputs, execute=timed_out)
    assert result["state"] == "unsupported" and result["review_status"] == "technical_failure"
    assert result["error_kind"] == "role_review_timeout" and result["retryable"] is True
    assert "verdict" not in result
    evidence = result["execution"]
    assert evidence["event_counts"] == {"thread.started": 1, "turn.started": 1, "error": 1}
    assert evidence["last_lifecycle_phase"] == "turn.started"
    assert evidence["stdout_bytes"] > 0 and evidence["stderr_bytes"] > 0
    assert evidence["exception_class"] == "TimeoutExpired" and evidence["timeout_seconds"] == 120
    assert evidence["returncode"] is None and evidence["elapsed_seconds"] >= 0
    assert not evidence["events_truncated"]
    assert "synthetic-private" not in json.dumps(result) and "synthetic-secret" not in json.dumps(result)
    cache = config.ROOT / "private" / "role-fit-reviews" / inputs[0]["dedupe_hash"] / (result["evidence_hash"] + ".json")
    assert json.loads(cache.read_text())["execution"] == evidence
    assert cache.stat().st_mode & 0o777 == 0o600
    # A technical cache entry does not become a semantic decision or suppress a
    # separately authorized retry. These are injected subprocesses, not live calls.
    role_fit.assess(*inputs, execute=timed_out)
    assert calls == [120, 120]


def test_semantic_needs_review_is_distinct_from_retryable_execution_failure(inputs, monkeypatch):
    monkeypatch.setenv("JHB_ROLE_FIT_REVIEW", "1")
    result = role_fit.assess(*inputs, execute=executor({
        "verdict": "needs_review", "reason": "Core scope is unclear", "matched_requirements": ["Python"],
        "unsupported_core_requirements": [], "review_notes": []}, []))
    assert result["state"] == "unsupported" and result["review_status"] == "needs_review"
    assert result["retryable"] is False and "error_kind" not in result
    assert result["execution"]["returncode"] == 0


@pytest.mark.parametrize("mode,kind,retryable", [
    ("exit", "role_review_process_exit", False),
    ("missing", "role_review_output_missing", True),
    ("malformed", "role_review_output_invalid", False),
    ("schema", "role_review_output_invalid", False),
    ("executable", "role_review_execution", False),
])
def test_execution_and_output_failures_have_safe_structured_diagnostics(inputs, monkeypatch, mode, kind, retryable):
    monkeypatch.setenv("JHB_ROLE_FIT_REVIEW", "1")
    def execute(command, **kwargs):
        output = Path(command[command.index("--output-last-message") + 1])
        if mode == "executable":
            raise FileNotFoundError("synthetic-private-local-path")
        if mode == "malformed":
            output.write_text("synthetic-private-broken-output")
        if mode == "schema":
            output.write_text('{"verdict":"fit"}')
        return SimpleNamespace(returncode=7 if mode == "exit" else 0,
            stdout='{"type":"turn.failed","error":{"message":"synthetic-private-message"}}\n',
            stderr="synthetic-secret-credential")
    result = role_fit.assess(*inputs, execute=execute)
    assert result["state"] == "unsupported" and result["review_status"] == "technical_failure"
    assert result["error_kind"] == kind and result["retryable"] is retryable
    assert "synthetic-private" not in json.dumps(result) and "synthetic-secret" not in json.dumps(result)


def test_event_summary_uses_actual_last_phase_and_bounds_untrusted_json_lines():
    stream = ('{"type":"turn.completed"}\n'
              '{"type":"turn.failed","error":"synthetic-secret"}\n'
              '{"type":"synthetic-private-command"}\ninvalid-synthetic-private-json\n')
    evidence = role_fit._execution_evidence(stream, "synthetic-private-stderr")
    assert evidence["last_lifecycle_phase"] == "turn.failed"
    assert evidence["event_counts"] == {"turn.completed": 1, "turn.failed": 1, "other": 1}
    assert evidence["malformed_event_count"] == 1
    assert "synthetic-private" not in json.dumps(evidence) and "synthetic-secret" not in json.dumps(evidence)
    large = role_fit._execution_evidence('{"type":"error"}\n' * 2100)
    assert large["event_counts"] == {"error": 2000} and large["events_truncated"] is True
