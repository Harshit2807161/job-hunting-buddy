import hashlib
import json
import sqlite3

import pytest

from jhb.applications import boards, queue

UUID = "19eb22cd-9540-49ed-840b-6422714413b5"
ASHBY = f"https://jobs.ashbyhq.com/example/{UUID}"


@pytest.mark.parametrize("url,identity", [
    ("https://boards.greenhouse.io/example/jobs/123?gh_src=x", ("greenhouse", "global", "example", "123")),
    ("https://boards.eu.greenhouse.io/embed/job_app?for=example&token=123", ("greenhouse", "eu", "example", "123")),
    (ASHBY+"/application?embed=true", ("ashby", "example", UUID)),
    ("https://apply.workable.com/example/j/FC4151F98F/apply/", ("workable", "example", "FC4151F98F")),
    (f"https://jobs.lever.co/example/{UUID}/apply", ("lever", "global", "example", UUID)),
    (f"https://jobs.eu.lever.co/example/{UUID}", ("lever", "eu", "example", UUID)),
    ("https://jobs.smartrecruiters.com/Example/744000000001234-engineer", ("smartrecruiters", "example", "744000000001234")),
    ("https://careers-example.icims.com/jobs/123/engineer/job", ("icims", "careers-example.icims.com", "123")),
    ("https://example.wd5.myworkdayjobs.com/en-US/Careers/job/San-Diego/Engineer_JR123456/apply", ("workday", "example", "careers", "JR123456")),
    ("https://jobs.myworkdaysite.com/recruiting/example/Careers/job/San-Diego/Engineer_JR123456", ("workday", "example", "careers", "JR123456")),
    ("https://www.linkedin.com/jobs/view/software-engineer-1234567890/", ("linkedin", "1234567890")),
])
def test_exact_official_job_identity(url, identity):
    assert boards.job_identity(url) == identity
    assert boards.identity(url) == identity
    assert boards.job_identity(boards.canonical_url(url)) == identity


@pytest.mark.parametrize("url", [
    None, True, 123, b"https://jobs.ashbyhq.com/example/job", ASHBY.replace("ashby", "ash\nby"),
    ASHBY.replace("https", "http"), ASHBY.replace(".com/", ".com.attacker.test/"),
    ASHBY.replace("https://", "https://user:pass@"), ASHBY.replace(".com/", ".com:444/"),
    ASHBY+"/../login", ASHBY+"/%2Flogin", ASHBY+"\\login", ASHBY+"/extra",
    "https://jobs.ashbyhq.com/example", "https://apply.workable.com/example/j/not-a-job",
    "https://example.wd5.myworkdayjobs.com/en-US/Careers/login", "https://icims.com/jobs/123/engineer/job",
    "https://www.linkedin.com/jobs/search/", "https://jobs.lever.co/example/login",
])
def test_board_branding_unsafe_urls_and_non_job_pages_are_not_exact_jobs(url):
    assert boards.job_identity(url) is None
    assert boards.application_hash(url) is None
    assert boards.canonical_url(url) is None


def test_identity_dedupe_preserves_greenhouse_baseline_and_namespaces_new_boards():
    gh = "https://job-boards.greenhouse.io/example/jobs/123"
    assert boards.application_hash(gh) == hashlib.sha256(b"global|example|123").hexdigest()
    assert boards.application_hash(ASHBY) == boards.application_hash(ASHBY+"/application?utm_source=x")
    assert boards.application_hash(ASHBY) != boards.application_hash(f"https://jobs.lever.co/example/{UUID}")
    assert boards.route_board("https://www.linkedin.com/jobs/view/123", "linkedin_easy_apply") == "linkedin_easy_apply"
    assert boards.route_board(ASHBY, "linkedin_easy_apply") == "ashby"


def test_queue_only_enables_reviewed_adapters_and_deduplicates_protected_states(monkeypatch):
    monkeypatch.setitem(boards.ADAPTERS, "lever", {**boards.ADAPTERS["lever"], "prep_enabled": False})
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    def job(url, source):
        return {"url": url, "dedupe_hash": source, "title": "Engineer", "company": "Synthetic"}
    try:
        assert queue.enqueue(db, [job(ASHBY, "first"), job(ASHBY+"/application?ref=x", "second")]) == 1
        row = db.execute("SELECT * FROM applications").fetchone()
        value = json.loads(row["job_json"])
        assert value["board_type"] == "ashby" and value["adapter_skill"] == "skills/prepare-ashby/SKILL.md"
        assert value["source_job_hash"] == "first"
        queue.claim(db); queue.finish(db, row["job_hash"], "submission_uncertain")
        assert queue.enqueue(db, [job(ASHBY, "third")]) == 0
        assert db.execute("SELECT state FROM applications").fetchone()[0] == "submission_uncertain"
        assert queue.enqueue(db, [job(f"https://jobs.lever.co/example/{UUID}", "lever")]) == 0
    finally:
        db.close()
