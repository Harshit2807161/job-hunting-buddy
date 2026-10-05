"""Historical spreadsheet imports use synthetic data and read-only executors."""
import fcntl
import json
import subprocess
from pathlib import Path

import pytest

from jhb import config
from jhb.applications import historical, tracking
from test_tracking import setup, ASHBY, CANONICAL


def row(job, *, url=None, date="15th sep", company=None, title=None, location=None):
    return [company or job["company"], title or job["title"], location or "; ".join(job["locations"]),
            date, "", "", "", job["url"] if url is None else url]


def test_import_deduplicates_history_without_append_receipt_queue_or_guessed_date(setup):
    conn, job, receipt, sheets, _ = setup
    sheets.rows[2] = row(job)
    result = historical.import_sheet(conn, executor=sheets, now=1000)
    assert result["state"] == "imported" and result["rows"] == 1
    found = historical.match(conn, {**job, "url": CANONICAL+"?utm_source=other"})
    assert found["state"] == "previously_applied" and found["disposition"] == "exclude"
    assert found["submission_confirmed"] is False and found["applied_date_raw"] == "15th sep"
    assert "confirmed_at" not in found
    assert sheets.appends == 0 and all(slug in historical.READ_TOOLS for slug, _ in sheets.calls)
    assert conn.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='applications'").fetchone() is None
    assert historical.import_sheet(conn, executor=lambda *a: pytest.fail("cached history must not fetch"), now=1050)["state"] == "cached"
    assert historical.import_sheet(conn, executor=sheets, now=2000)["imported"] == 0
    assert conn.execute("SELECT COUNT(*) FROM sheet_application_history").fetchone()[0] == 1
    assert Path(found["evidence_path"]).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("saved,current", [
    (ASHBY, CANONICAL+"?embed=true&utm_source=feed"),
    ('=HYPERLINK("'+CANONICAL+'"; "Application")', ASHBY),
    ("https://boards.greenhouse.io/example/jobs/123?gh_src=a", "https://job-boards.greenhouse.io/example/jobs/123?gh_src=b"),
    ("https://www.linkedin.com/jobs/view/synthetic-role-123456/", "https://www.linkedin.com/jobs/view/123456/?trackingId=example"),
    ("https://example.wd5.myworkdayjobs.com/en-US/Careers/job/City/Engineer_JR12345", "https://example.wd5.myworkdayjobs.com/Careers/job/City/Engineer_JR12345/apply/applyManually?source=feed"),
])
def test_exact_job_variants_and_hyperlinks_do_not_reenter_browser(setup, saved, current):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job, url=saved)
    historical.import_sheet(conn, executor=sheets)
    assert historical.match(conn, {**job, "url": current})["match_kind"] == "exact_job_identity"


def test_resolved_ats_job_reuses_matching_original_linkedin_history(setup):
    conn, job, _, sheets, _ = setup
    source = "https://www.linkedin.com/jobs/view/123456/"
    sheets.rows[2] = row(job, url=source)
    historical.import_sheet(conn, executor=sheets)
    assert historical.match(conn, {**job, "source_url": source})["disposition"] == "exclude"


@pytest.mark.parametrize("change", [
    {"url": ASHBY.replace("555555555555", "555555555556")},
    {"url": ASHBY.replace("/example/", "/different/")},
])
def test_different_exact_ats_requisitions_do_not_collapse_on_company_title(setup, change):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job)
    historical.import_sheet(conn, executor=sheets)
    assert historical.match(conn, {**job, **change}) is None


def test_legacy_aliases_hold_for_identity_reconciliation_without_confirmation(setup):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job, url="", company="Example.ai", title="Software Engineer - New Grad", location="SF")
    historical.import_sheet(conn, executor=sheets)
    found = historical.match(conn, {**job, "company": "Example", "title": "Software Engineer, New Grad - Example (Remote)",
                                   "locations": '["San Francisco, CA"]'})
    assert found["disposition"] == "hold" and found["match_kind"] == "legacy_company_role_location"
    assert found["applied_date_raw"] == "15th sep" and found["submission_confirmed"] is False
    assert found["state"] == "possible_prior_application"


@pytest.mark.parametrize("change", [
    {"company": "Different Corp"}, {"title": "Senior AI Engineer"}, {"locations": ["London, UK"]},
])
def test_legacy_history_does_not_block_different_company_role_or_explicit_location(setup, change):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job, url="")
    historical.import_sheet(conn, executor=sheets)
    assert historical.match(conn, {**job, **change}) is None


def test_known_ats_history_can_hold_unresolved_linkedin_with_same_legacy_details(setup):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job)
    historical.import_sheet(conn, executor=sheets)
    assert historical.match(conn, {**job, "url": "https://www.linkedin.com/jobs/view/99999/"})["disposition"] == "hold"


@pytest.mark.parametrize("date", ["", "not applied", "TBD", "=TODAY()"])
def test_pending_undated_rows_and_generic_links_are_not_submission_evidence(setup, date):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job, date=date)
    assert historical.import_sheet(conn, executor=sheets)["rows"] == 0
    assert historical.match(conn, job) is None
    assert historical._url_key("https://example.test/careers") is None
    assert historical._url_key("https://example.test/careers/search") is None


def test_unresolved_exact_job_url_only_holds_and_preserves_job_query_identifiers(setup):
    conn, job, _, sheets, _ = setup
    url = "https://example.test/careers/software-engineer?gh_jid=111&utm_source=feed"
    sheets.rows[2] = row(job, url=url)
    historical.import_sheet(conn, executor=sheets)
    found = historical.match(conn, {**job, "url": url.replace("utm_source=feed", "utm_source=other")})
    assert found["match_kind"] == "same_unresolved_url" and found["disposition"] == "hold"
    assert historical._url_key(url) != historical._url_key(url.replace("gh_jid=111", "gh_jid=222"))


@pytest.mark.parametrize("damage", ["deleted", "changed", "symlink"])
def test_damaged_exact_history_still_blocks_reapplication_without_claiming_submission(setup, damage):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job)
    result = historical.import_sheet(conn, executor=sheets)
    path = Path(result["snapshot_path"])
    original = path.read_bytes()
    if damage == "deleted":
        path.unlink()
    elif damage == "changed":
        path.write_text('{}')
    else:
        path.unlink()
        other = path.with_name("other.json");other.write_bytes(original);path.symlink_to(other)
    found = historical.match(conn, job)
    assert found["disposition"] == "hold" and found["state"] == "history_integrity_handoff"
    assert found["submission_confirmed"] is False


def test_read_failure_or_header_change_preserves_prior_history_and_sanitizes_errors(setup):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job)
    historical.import_sheet(conn, executor=sheets, now=1000)
    def broken(*args):
        raise RuntimeError("synthetic account data must not escape")
    assert historical.import_sheet(conn, executor=broken, now=2000) == {"state": "pending", "imported": 0, "reason": "RuntimeError"}
    assert historical.match(conn, job)["disposition"] == "exclude"
    sheets.rows[1] = ["Wrong header"]
    assert historical.import_sheet(conn, executor=sheets, now=2000)["state"] == "pending"
    assert sheets.appends == 0


def test_history_import_respects_shared_tracker_lock(setup):
    conn, _, _, sheets, _ = setup
    path = config.ROOT / "private" / "application-tracker.lock"
    with path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert historical.import_sheet(conn, executor=sheets)["state"] == "busy"
    assert sheets.calls == []


def test_fresh_get_can_repair_missing_snapshot_without_erasing_seen_identity(setup):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job)
    first = historical.import_sheet(conn, executor=sheets, now=1000)
    Path(first["snapshot_path"]).unlink()
    second = historical.import_sheet(conn, executor=sheets, now=1001)
    assert second["state"] == "imported" and second["imported"] == 0
    assert historical.match(conn, job)["disposition"] == "exclude"


def test_cli_timeout_is_a_retryable_read_failure_not_pipeline_exception(setup):
    conn, _, _, _, _ = setup
    def timeout(*args):
        raise subprocess.TimeoutExpired("synthetic", 45)
    assert historical.import_sheet(conn, executor=timeout)["reason"] == "TimeoutExpired"


def test_ci_never_uses_live_composio_authentication(setup, monkeypatch):
    conn, _, _, _, _ = setup
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(tracking, "ComposioSheets", lambda *a: pytest.fail("CI must not authenticate"))
    assert historical.import_sheet(conn)["reason"] == "CI"
