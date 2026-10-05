"""A selected stretch role needs a new, exact and informed candidate approval."""
import hashlib
import json
import time

import pytest

from test_portal_approvals import draft
from jhb import eligibility
from jhb.applications import approvals, boards, booklet, overnight, role_fit, stretch_role


@pytest.fixture
def stretch(draft, monkeypatch):
    conn, key, path, bookpath, resume = draft
    job = json.loads(path.read_text())["job"]
    book = booklet.load(bookpath)
    book["roles"]["sde"]["role.experience"] = booklet.answer("Synthetic Python systems research", "Synthetic resume")
    book["candidate_selected_jobs"] = {key: {"status": "verified", "role": "user", "action": "apply",
        "job_hash": key, "url": job["url"], "content": "Apply to this: " + job["url"], "source": "Synthetic exact user request"}}
    booklet.write_private(bookpath, book)
    text = "Research commercial software systems. Experience with specialized model training is desired."
    description = {"text": text, "source_url": job["url"], "status": "verified", "retrieved_at": time.time(),
        "job_identity": list(boards.job_identity(job["url"])), "sha256": hashlib.sha256(text.encode()).hexdigest()}
    booklet.write_private(path.with_name("eligibility.json"), {"state": "eligible", "policy": eligibility.POLICY_ID,
        "description": description})
    fit = {"state": "skipped", "verdict": "not_fit", "review_status": "complete", "mode": "independent_codex",
        "source": role_fit.POLICY, "selected_role": "sde", "reason": "Specialized model training is not demonstrated.",
        "matched_requirements": ["Python systems research"], "unsupported_core_requirements": ["Model training"],
        "review_notes": ["Adjacent systems research is documented."], "retryable": False,
        "evidence_hash": role_fit.evidence_hash({**job, "verified_job_description": description}, book, "sde")}
    booklet.write_private(path.with_name("role-fit.json"), fit)
    monkeypatch.setenv("JHB_ROLE_FIT_REVIEW", "1")
    return draft


def authorize(stretch):
    conn, key, path, bookpath, _ = stretch
    view = approvals.review(conn, key, bookpath)
    result = approvals.approve(conn, key, view["revision"], ["motivation"], bookpath,
                               acknowledge_role_fit_warning=True)
    authpath = conn.execute("SELECT authorization_path FROM application_approvals WHERE approval_id=?", (result["approval_id"],)).fetchone()[0]
    return overnight.load_authorization(authpath)


def test_exact_selection_and_fresh_explicit_acknowledgment_preserve_negative_verdict(stretch):
    conn, key, path, bookpath, _ = stretch
    before = {p: p.read_bytes() for p in path.parent.iterdir()}
    view = approvals.review(conn, key, bookpath)
    assert view["can_approve"] and view["requires_role_fit_acknowledgment"] is True
    assert view["role_fit_warning"]["reason"] == "Specialized model training is not demonstrated."
    assert view["role_fit_warning"]["gaps"] == ["Model training", "Adjacent systems research is documented."]
    with pytest.raises(ValueError, match="acknowledge the role-fit warning"):
        approvals.approve(conn, key, view["revision"], ["motivation"], bookpath)
    assert conn.execute("SELECT COUNT(*) FROM application_approvals").fetchone()[0] == 0
    auth = authorize(stretch)
    assert auth["binding"]["candidate_selected_stretch"]["fit_verdict"] == "not_fit"
    assert auth["acknowledge_role_fit_warning"] is True
    row = conn.execute("SELECT * FROM applications").fetchone()
    assert overnight._candidate(conn, row, auth, booklet.load(bookpath))[0]["dedupe_hash"] == key
    assert approvals.validate_binding(auth, path)
    assert all(p.read_bytes() == value for p, value in before.items())
    assert auth["require_independent_review"] and auth["require_browser_double_check"]


@pytest.mark.parametrize("damage", ["absent", "broad", "wrong_job", "wrong_url", "agent", "not_apply", "no_source", "unverified"])
def test_selection_never_infers_global_or_another_jobs_direction(stretch, damage):
    conn, key, path, bookpath, _ = stretch
    book = booklet.load(bookpath); selected = book["candidate_selected_jobs"][key]
    if damage == "absent": book.pop("candidate_selected_jobs")
    elif damage == "broad": selected["content"] = "Apply to all jobs through the night."
    elif damage == "wrong_job": selected["job_hash"] = "f"*64
    elif damage == "wrong_url": selected["url"] = selected["url"].replace("555555555555", "555555555556")
    elif damage == "agent": selected["role"] = "assistant"
    elif damage == "not_apply": selected["action"] = "research"
    elif damage == "no_source": selected["source"] = ""
    else: selected["status"] = "needs_input"
    booklet.write_private(bookpath, book)
    view = approvals.review(conn, key, bookpath)
    assert view["requires_role_fit_acknowledgment"] is False and view["role_fit_warning"] is None
    with pytest.raises(ValueError, match="role-fit warning changed"):
        authorize(stretch)
    assert conn.execute("SELECT COUNT(*) FROM application_approvals").fetchone()[0] == 0


@pytest.mark.parametrize("change", [
    {"state": "unsupported"}, {"verdict": "needs_review"}, {"review_status": "technical_failure"},
    {"mode": "interactive_candidate_selected_job"}, {"source": "old-policy"}, {"selected_role": "ml"},
    {"evidence_hash": "a"*64}, {"reason": ""}, {"matched_requirements": "invalid"},
    {"review_notes": [False]}, {"retryable": True}, {"error_kind": "role_review_timeout"},
])
def test_missing_invalid_or_unfinished_model_review_never_qualifies(stretch, change):
    conn, key, path, bookpath, _ = stretch
    fit = json.loads(path.with_name("role-fit.json").read_text()); fit.update(change)
    booklet.write_private(path.with_name("role-fit.json"), fit)
    assert approvals.review(conn, key, bookpath)["requires_role_fit_acknowledgment"] is False
    with pytest.raises(ValueError): authorize(stretch)


@pytest.mark.parametrize("change", ["selection", "fit", "jd", "facts", "resume", "selection_removed", "fit_removed"])
def test_current_selection_fit_job_resume_and_facts_are_bound_after_approval(stretch, change):
    conn, key, path, bookpath, resume = stretch
    auth = authorize(stretch)
    if change in {"selection", "selection_removed", "facts"}:
        book = booklet.load(bookpath)
        if change == "selection": book["candidate_selected_jobs"][key]["source"] = "Changed transcript source"
        elif change == "selection_removed": book.pop("candidate_selected_jobs")
        else: book["roles"]["sde"]["role.experience"]["value"] = "Changed factual research experience"
        booklet.write_private(bookpath, book)
    elif change == "resume": resume.write_bytes(b"%PDF-1.4\nDifferent resume")
    elif change == "fit_removed": path.with_name("role-fit.json").unlink()
    else:
        target = path.with_name("role-fit.json" if change == "fit" else "eligibility.json")
        data = json.loads(target.read_text())
        if change == "fit": data["review_notes"].append("Another gap was discovered.")
        else:
            data["description"]["text"] += " More scope."
            data["description"]["sha256"] = hashlib.sha256(data["description"]["text"].encode()).hexdigest()
        booklet.write_private(target, data)
    with pytest.raises(ValueError): approvals.validate_binding(auth, path)


@pytest.mark.parametrize("restriction", ["US citizenship is required.", "Active TS/SCI clearance required.",
                                       "We cannot provide visa sponsorship."])
def test_candidate_choice_cannot_bypass_objective_eligibility(stretch, restriction):
    conn, key, path, bookpath, _ = stretch
    data = json.loads(path.with_name("eligibility.json").read_text())
    data["description"]["text"] = restriction
    data["description"]["sha256"] = hashlib.sha256(restriction.encode()).hexdigest()
    booklet.write_private(path.with_name("eligibility.json"), data)
    assert approvals.review(conn, key, bookpath)["requires_role_fit_acknowledgment"] is False
    with pytest.raises(ValueError): authorize(stretch)


def test_explicit_exclusion_or_duplicate_history_cannot_use_stretch_approval(stretch, monkeypatch):
    from jhb.applications import historical
    conn, key, path, bookpath, _ = stretch
    auth = authorize(stretch)
    row = conn.execute("SELECT * FROM applications").fetchone()
    monkeypatch.setattr(historical, "match", lambda *a: {"disposition": "hold"})
    rejected = []
    assert overnight._candidate(conn, row, auth, booklet.load(bookpath), rejections=rejected) is None
    assert rejected == ["application_history_blocked"]
    monkeypatch.setattr(historical, "match", lambda *a: None)
    book = booklet.load(bookpath)
    book["job_exclusions"] = {key: {"status": "verified", "source": "Candidate declined exact role"}}
    rejected = []
    assert overnight._candidate(conn, row, auth, book, rejections=rejected) is None
    assert rejected == ["candidate_excluded_job"]


def test_selection_never_grants_autonomous_or_old_approval_authority(stretch):
    conn, key, path, bookpath, _ = stretch
    auth = authorize(stretch)
    current = auth["binding"]["candidate_selected_stretch"]
    assert not stretch_role.allowed({**auth, "scope": overnight.MULTI_SCOPE}, current)
    assert not stretch_role.allowed({**auth, "acknowledge_role_fit_warning": False}, current)
    assert not stretch_role.allowed({**auth, "job_hash": "f"*64}, current)
    row = conn.execute("SELECT * FROM applications").fetchone()
    rejected = []
    assert overnight._candidate(conn, row, {**auth, "scope": overnight.MULTI_SCOPE},
                                booklet.load(bookpath), rejections=rejected) is None
    assert rejected == ["role_fit_not_eligible"]
    old = {**auth}; old.pop("acknowledge_role_fit_warning")
    with pytest.raises(ValueError, match="acknowledge"): approvals.validate_binding(old, path)
    conn.execute("UPDATE application_approvals SET state='needs_review'"); conn.commit()
    newer = authorize(stretch)
    assert newer["approval_id"] != auth["approval_id"]
    states = [row[0] for row in conn.execute("SELECT state FROM application_approvals ORDER BY rowid")]
    assert states == ["needs_review", "approved"]


def test_saving_exact_selection_cannot_upgrade_an_earlier_uninformed_approval(stretch):
    conn, key, path, bookpath, _ = stretch
    book = booklet.load(bookpath); selection = book.pop("candidate_selected_jobs")
    booklet.write_private(bookpath, book)
    view = approvals.review(conn, key, bookpath)
    approvals.approve(conn, key, view["revision"], ["motivation"], bookpath)
    authpath = conn.execute("SELECT authorization_path FROM application_approvals").fetchone()[0]
    old = overnight.load_authorization(authpath)
    assert "candidate_selected_stretch" not in old["binding"]
    book["candidate_selected_jobs"] = selection; booklet.write_private(bookpath, book)
    with pytest.raises(ValueError): approvals.validate_binding(old, path)
    assert conn.execute("SELECT COUNT(*) FROM application_approvals").fetchone()[0] == 1
    assert "acknowledge_role_fit_warning" not in json.loads(open(authpath).read())
