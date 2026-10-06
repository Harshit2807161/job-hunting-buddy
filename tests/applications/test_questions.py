import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from jhb.applications import booklet, questions, queue


def make_book(tmp_path):
    path = tmp_path / "private" / "booklet.json"
    booklet.write_private(path, {
        "schema_version": 1, "answers": {
            "identity.email": booklet.answer("sam@example.org", "synthetic resume"),
            "consent.truthfulness": booklet.answer(),
        }, "roles": {"sde": {}, "ml": {}}, "custom_answers": {},
    })
    return path


def job(n=1, board="example"):
    return {"dedupe_hash": f"{n:064x}", "url": f"https://job-boards.greenhouse.io/{board}/jobs/{n}",
            "title": "Synthetic Engineer", "company": "Example employer",
            "source_url": "https://example.org/careers"}


def result(label="Do you agree to this employer's certification?", **extra):
    return {"state": "waiting_input", "missing": [{"question": label, "ref": "new_field",
                                                   "required": True, **extra}]}


def test_same_employer_question_dedupes_across_jobs_preserving_context(tmp_path):
    path = make_book(tmp_path)
    first = questions.collect(job(), result(), path)[0]
    second = questions.collect(job(2), result("  DO YOU AGREE TO THIS EMPLOYER'S CERTIFICATION? *"), path)[0]
    assert first["id"] == second["id"]
    assert set(second["contexts"]) == {job()["dedupe_hash"], job(2)["dedupe_hash"]}
    assert len(questions.pending(path)) == 1
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_question_separates_employers_jurisdictions_and_education_rows(tmp_path):
    path = make_book(tmp_path)
    ids = {
        questions.collect(job(), result(country_context="US"), path)[0]["id"],
        questions.collect(job(2), result(country_context="Canada"), path)[0]["id"],
        questions.collect(job(3, "another"), result(country_context="US"), path)[0]["id"],
        questions.collect(job(), result("School", ref="school--0"), path)[0]["id"],
        questions.collect(job(), result("School", ref="school--1"), path)[0]["id"],
    }
    assert len(ids) == 5


def test_boolean_false_kept_explicit_and_certification_never_global(tmp_path):
    path = make_book(tmp_path)
    q = questions.collect(job(), result(answer_key="consent.truthfulness"), path)[0]
    with pytest.raises(ValueError, match="shared profile"):
        questions.answer(q["id"], True, path, promote=True)
    assert questions.answer(q["id"], False, path) == [job()["dedupe_hash"]]
    book = booklet.load(path)
    response = next(iter(book["custom_answers"].values()))
    assert response["value"] is False
    assert response["status"] == "verified"
    assert response["scope"] == {"region": "global", "board": "example"}
    assert response["source"]["provider"] == "explicit user question response"
    assert book["answers"]["consent.truthfulness"]["status"] == "needs_input"
    assert questions.pending(path) == []


def test_corrected_answers_preserve_history_and_renotify(tmp_path):
    path = make_book(tmp_path)
    q = questions.collect(job(), result(), path)[0]
    questions.mark_notified([q["id"]], path)
    assert questions.pending(path, unnotified=True) == []
    questions.answer(q["id"], False, path)
    custom_key = booklet.load(path)["question_handoffs"][q["id"]]["custom_answer_key"]
    reopened = questions.collect(job(), result(answer_key=custom_key, reason="Stored answer unavailable or incompatible with field"), path)[0]
    assert reopened["id"] == q["id"]
    assert "notified_at" not in reopened
    assert questions.pending(path, unnotified=True)
    questions.answer(q["id"], True, path)
    book = booklet.load(path)
    custom_key = book["question_handoffs"][q["id"]]["custom_answer_key"]
    assert book["answer_history"][custom_key][0]["value"] is False
    assert book["custom_answers"][custom_key]["value"] is True
    assert [e["event"] for e in book["question_handoffs"][q["id"]]["history"]] == ["answered", "reopened", "answered"]


def test_profile_promotion_requires_known_exact_ordinary_label(tmp_path):
    path = make_book(tmp_path)
    q = questions.collect(job(), result("Email", answer_key="identity.email"), path)[0]
    questions.answer(q["id"], "sam.updated@example.org", path, promote=True)
    book = booklet.load(path)
    assert book["answers"]["identity.email"]["value"] == "sam.updated@example.org"
    assert book["answer_history"]["identity.email"][0]["value"] == "sam@example.org"
    assert not book["custom_answers"]
    malicious = questions.collect(job(), result("I consent to background checks", answer_key="identity.email"), path)[0]
    with pytest.raises(ValueError, match="shared profile"):
        questions.answer(malicious["id"], True, path, promote=True)


@pytest.mark.parametrize("label,key", [
    ("Verification code", None), ("Password", None), ("Enter OTP", None),
    ("One-time security code", None), ("Complete CAPTCHA", None),
    ("Access token", None), ("Answer", "credentials.password"),
])
def test_authentication_challenges_and_secrets_never_persist(tmp_path, label, key):
    path = make_book(tmp_path)
    assert questions.collect(job(), result(label, answer_key=key), path) == []
    assert booklet.load(path)["question_handoffs"] == {}


def test_background_verification_is_a_consent_question_not_a_code(tmp_path):
    path = make_book(tmp_path)
    assert questions.collect(job(), result("I authorize employment verification"), path)


def test_blank_and_invalid_answers_cannot_mark_question_answered(tmp_path):
    path = make_book(tmp_path)
    q = questions.collect(job(), result(), path)[0]
    for invalid in [None, "", "   ", {}, [], [""], float("nan")]:
        with pytest.raises(ValueError):
            questions.answer(q["id"], invalid, path)
    assert questions.pending(path)[0]["status"] == "pending"


def test_optional_decline_stays_closed_until_required(tmp_path):
    path = make_book(tmp_path)
    optional = {"unknown_questions": [{"question": "Preferred pronouns", "required": False}]}
    q = questions.collect(job(), optional, path)[0]
    questions.answer(q["id"], None, path, decline=True)
    assert questions.collect(job(), optional, path) == []
    assert questions.pending(path) == []
    reopened = questions.collect(job(), result("Preferred pronouns"), path)[0]
    with pytest.raises(ValueError, match="Only optional"):
        questions.answer(reopened["id"], None, path, decline=True)


def test_role_choices_are_per_job_explicit_and_readable(tmp_path):
    path = make_book(tmp_path)
    label = "Choose the SDE or ML resume variant"
    first = questions.collect(job(), result(label), path)[0]
    second = questions.collect(job(2), result(label), path)[0]
    assert first["id"] != second["id"]
    with pytest.raises(ValueError, match="Choose sde or ml"):
        questions.answer(first["id"], ["ml"], path)
    questions.answer(first["id"], "ml", path)
    assert questions.role_override(job()["dedupe_hash"], path) == "ml"
    assert questions.role_override(job(2)["dedupe_hash"], path) is None
    assert not booklet.load(path)["custom_answers"]


def test_only_fully_answered_waiting_input_jobs_resume(tmp_path):
    path = make_book(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    queue.initialize(conn)
    for index, state in enumerate(["waiting_input", "waiting_review", "submitted", "waiting_captcha"], 1):
        row = job(index)
        conn.execute("INSERT INTO applications(job_hash,job_json,state,updated_at) VALUES(?,?,?,0)",
                     (row["dedupe_hash"], json.dumps(row), state))
    conn.commit()
    first = questions.collect(job(), result("First new question"), path)[0]
    second = questions.collect(job(), result("Second new question"), path)[0]
    for index in range(2, 5):
        questions.collect(job(index), result("First new question"), path)
    affected = questions.answer(first["id"], False, path, conn)
    assert len(affected) == 4
    assert conn.execute("SELECT state FROM applications WHERE job_hash=?", (job()["dedupe_hash"],)).fetchone()[0] == "waiting_input"
    questions.answer(second["id"], "An explicit answer", path, conn)
    states = [r[0] for r in conn.execute("SELECT state FROM applications ORDER BY job_hash")]
    assert states == ["queued", "waiting_review", "submitted", "waiting_captcha"]


def test_repeated_education_response_retains_exact_row_reference(tmp_path):
    path = make_book(tmp_path)
    q = questions.collect(job(), result("School", ref="school--1"), path)[0]
    questions.answer(q["id"], "Example University", path)
    response = next(iter(booklet.load(path)["custom_answers"].values()))
    assert response["field_ref"] == "school--1"


def test_concurrent_collect_preserves_all_job_contexts(tmp_path):
    path = make_book(tmp_path)
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda n: questions.collect(job(n), result(), path), range(1, 13)))
    assert len({row[0]["id"] for row in rows}) == 1
    pending = questions.pending(path)
    assert len(pending) == 1
    assert len(pending[0]["contexts"]) == 12
    assert booklet.load(path)["answers"]["identity.email"]["value"] == "sam@example.org"


def test_invalid_or_unresolved_job_does_not_modify_booklet(tmp_path):
    path = make_book(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="exact resolved"):
        questions.collect({**job(), "url": "https://example.org/jobs/view/123"}, result(), path)
    with pytest.raises(ValueError, match="Invalid job identity"):
        questions.collect({**job(), "dedupe_hash": "../../bad"}, result(), path)
    assert path.read_bytes() == before
