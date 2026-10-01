"""Ledger idempotency -- the UNIQUE constraint is the re-poll safety guard."""

import json
import sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jhb.store import connect, Job, upsert_jobs, pending_notification, mark_notified, is_first_run


def _db():
    return connect(pathlib.Path(tempfile.mkdtemp()) / "t.sqlite3")


def _job(title="Software Engineer", company="Acme", sid="abc"):
    return Job(source="simplify", source_id=sid, company=company, title=title,
               url="https://example.com/1", locations=["Austin, TX"], role_classes=["swe"])


def test_first_run_then_not():
    c = _db()
    assert is_first_run(c)
    upsert_jobs(c, [_job()])
    assert not is_first_run(c)


def test_repoll_is_idempotent():
    c = _db()
    assert len(upsert_jobs(c, [_job()])) == 1
    assert len(upsert_jobs(c, [_job()])) == 0, "re-polling the same job must yield no new rows"


def test_seed_mode_suppresses_notification():
    c = _db()
    upsert_jobs(c, [_job()], mark_notified=True)
    assert pending_notification(c)[0] == []


def test_new_job_becomes_pending_then_clears():
    c = _db()
    upsert_jobs(c, [_job()], mark_notified=True)
    new = upsert_jobs(c, [_job(title="ML Engineer", sid="xyz")])
    assert len(new) == 1
    pend, _ = pending_notification(c)
    assert len(pend) == 1 and pend[0]["title"] == "ML Engineer"
    mark_notified(c, [p["dedupe_hash"] for p in pend])
    assert pending_notification(c)[0] == []


def test_distinct_companies_same_title_are_distinct():
    c = _db()
    upsert_jobs(c, [_job(company="Acme", sid="1")])
    new = upsert_jobs(c, [_job(company="Globex", sid="2")])
    assert len(new) == 1


# --- cross-source / multi-location collapsing -------------------------------

def _j(title, company, url, source="simplify", sid="x"):
    return Job(source=source, source_id=sid, company=company, title=title, url=url,
               locations=["Austin, TX"], role_classes=["swe"], date_posted=1000)


def _jl(title, company, url, locs, source="simplify", sid="x"):
    return Job(source=source, source_id=sid, company=company, title=title, url=url,
               locations=locs, role_classes=["swe"], date_posted=1000)


def test_same_role_many_locations_collapses_to_one_card():
    """RTX 'Software Engineer 1' appeared 13 times, one row per city, and read
    as the same opening arriving over and over."""
    c = _db()
    cities = ["Fort Wayne, IN", "Cedar Rapids, IA", "Tucson, AZ", "Aurora, CO"]
    upsert_jobs(c, [_jl("Software Engineer 1", "RTX", f"https://rtx.com/{i}", [city],
                        sid=str(i)) for i, city in enumerate(cities)])
    pend, suppressed = pending_notification(c)
    assert len(pend) == 1, "one role must produce one card regardless of location count"
    assert len(suppressed) == len(cities) - 1
    assert pend[0]["dupe_count"] == len(cities)
    merged = json.loads(pend[0]["locations"])
    assert set(merged) == set(cities), "every location must survive on the one card"


def test_cross_source_duplicates_are_KEPT():
    """Deliberate: the Simplify row carries the employer ATS link and the JobSpy
    row the LinkedIn one. Both are wanted, so grouping is per (role, source)."""
    c = _db()
    upsert_jobs(c, [
        _j("Applied AI Engineer", "CHAOS Industries",
           "https://job-boards.greenhouse.io/chaosindustries/jobs/5201", "simplify", "gh-1"),
        _j("Applied AI Engineer", "CHAOS Industries",
           "https://www.linkedin.com/jobs/view/4465798919", "jobspy:linkedin", "li-44657"),
    ])
    pend, suppressed = pending_notification(c)
    assert len(pend) == 2, "cross-source duplicates are intentionally kept"
    assert suppressed == []


def test_same_role_same_source_already_emailed_is_suppressed():
    c = _db()
    upsert_jobs(c, [_j("Software Engineer 1", "RTX", "https://rtx.com/a", "simplify", "1")])
    pend, _ = pending_notification(c)
    mark_notified(c, [p["dedupe_hash"] for p in pend])
    upsert_jobs(c, [_j("Software Engineer 1", "RTX", "https://rtx.com/b", "simplify", "2")])
    pend2, suppressed = pending_notification(c)
    assert pend2 == [], "a new location for an already-emailed role must not re-email"
    assert len(suppressed) == 1


def test_company_suffix_differences_collapse_within_a_source():
    c = _db()
    upsert_jobs(c, [
        _j("Software Engineer", "Acme Inc.", "https://acme.com/1", "simplify", "a"),
        _j("Software Engineer", "Acme Technologies", "https://acme.com/2", "simplify", "b"),
    ])
    pend, _ = pending_notification(c)
    assert len(pend) == 1


def test_genuinely_different_roles_are_kept():
    c = _db()
    upsert_jobs(c, [
        _j("Software Engineer", "Acme", "https://a.com/1", "simplify", "1"),
        _j("Machine Learning Engineer", "Acme", "https://a.com/2", "simplify", "2"),
        _j("Software Engineer", "Globex", "https://g.com/1", "simplify", "3"),
    ])
    pend, suppressed = pending_notification(c)
    assert len(pend) == 3 and suppressed == []
