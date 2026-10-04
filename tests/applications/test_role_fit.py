import hashlib
import json
from pathlib import Path
import shutil
import time
from types import SimpleNamespace

import pytest

from jhb import config
from jhb.applications import boards, role_fit

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


def test_explicit_exact_job_exclusion_precedes_fit(inputs):
    job, book, role = inputs
    book["job_exclusions"] = {job["dedupe_hash"]: {"status": "verified", "source": "Explicit synthetic user refusal"}}
    assert role_fit.assess(job, book, role)["state"] == "skipped"


def executor(verdict, calls):
    def run(command, **kwargs):
        calls.append(kwargs["input"])
        assert "OPENAI_API_KEY" not in kwargs["env"] and "CODEX_API_KEY" not in kwargs["env"]
        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(verdict))
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
