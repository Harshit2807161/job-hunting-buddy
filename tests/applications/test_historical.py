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


def test_submission_refresh_detects_manual_row_added_during_cache_window(setup):
    conn, job, _, sheets, _ = setup
    historical.import_sheet(conn, executor=sheets)
    assert historical.match(conn, job) is None
    sheets.rows[2] = row(job)
    assert historical.import_sheet(conn, executor=sheets)["state"] == "cached"
    result = historical.refresh_before_submit(job, connection=conn, executor=sheets)
    assert result["state"] == "blocked"
    assert result["match"]["disposition"] == "exclude"
    assert sheets.appends == 0


def test_submission_refresh_preserves_new_unapplied_exact_job(setup):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job, url=ASHBY.replace("555555555555", "555555555556"))
    assert historical.refresh_before_submit(job, connection=conn, executor=sheets) == {"state": "clear"}
    assert sheets.appends == 0


def test_submission_history_refresh_failure_does_not_allow_click(setup):
    conn, job, _, _, _ = setup
    def unavailable(*args):
        raise RuntimeError("Synthetic unavailable sheet")
    assert historical.refresh_before_submit(job, connection=conn, executor=unavailable)["state"] == "pending"


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


def test_legacy_remote_is_a_work_arrangement_not_a_conflicting_location(setup):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job, url="", company="Example", title="Software Engineer New Grad, ML Platform ", location="Remote")
    historical.import_sheet(conn, executor=sheets)
    found = historical.match(conn, {**job, "company": "Example", "title": "Software Engineer New Grad, ML Platform - Example (Remote)",
                                   "locations": ["Remote - Multiple Locations", "United States", "Canada"]})
    assert found["disposition"] == "hold" and found["match_kind"] == "legacy_company_role_location"
    assert found["submission_confirmed"] is False


@pytest.mark.parametrize('saved,current', [
    ('US', '["United States"]'), ('USA', ['United States']),
    ('U.S.A.', ['United States of America']), ('United States', ['New York, NY']),
    ('US', ['San Francisco, CA']), ('US', ['Remote - United States']),
    ('US', ['Remote']), ('Remote', ['Canada']), ('Multiple', ['Seattle, WA']),
    ('NYC', ['New York, NY']), ('SF', ['San Francisco, California, USA']),
    ('New York, NY; San Francisco, CA', ['San Francisco, CA']),
    ('New York, NY | San Francisco, CA', ['New York, NY']),
    ('US / Canada', ['Toronto, Canada']), ('US, Canada', ['Canada']),
    ('["London, UK", "New York, NY"]', ['New York, NY']),
    ('US', ['London, UK', 'Seattle, WA']),
    ('California', ['San Diego, CA']), ('CA', ['California, US']),
    ('London, UK', ['United Kingdom']),
    ('Unlisted City', ['US']),  # A country is not established for this city.
])
def test_legacy_broad_alias_and_overlapping_locations_preserve_cautious_hold(setup, saved, current):
    conn, job, _, sheets, _ = setup
    sheets.rows[73] = row(job, url='', date='21st sep', location=saved,
                          title='Software Engineer - New Grad (Helios)')
    historical.import_sheet(conn, executor=sheets)
    found = historical.match(conn, {**job, 'title': 'Software Engineer New Grad - Helios', 'locations': current})
    assert found['disposition'] == 'hold' and found['state'] == 'possible_prior_application'
    assert found['match_kind'] == 'legacy_company_role_location' and found['row_number'] == 73
    assert found['applied_date_raw'] == '21st sep' and found['location_raw'] == saved
    assert found['submission_confirmed'] is False and sheets.appends == 0


@pytest.mark.parametrize('saved,current', [
    ('US', ['UK']), ('United States', ['Canada']), ('US', ['London, UK']),
    ('US', ['London, UK', 'Berlin, Germany']),
    ('San Francisco, CA', ['New York, NY']),
    ('Portland, OR', ['Portland, ME']), ('California', ['New York, NY']),
    ('San Francisco, CA; Austin, TX', ['London, UK', 'New York, NY']),
    ('Germany', ['France']), ('London, UK', ['London, Canada']),
])
def test_only_disjoint_explicit_geographies_suppress_legacy_hold(setup, saved, current):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job, url='', location=saved)
    historical.import_sheet(conn, executor=sheets)
    assert historical.match(conn, {**job, 'locations': current}) is None


@pytest.mark.parametrize('saved,current', [
    ('AI Engineer I', 'AI Engineer 1'),
    ('Software Engineer I', 'Software Engineer 1'),
    ('Software Engineer II, Backend', 'Backend Software Engineer 2'),
    ('New Grad Software Engineer, Platform', 'Software Engineer Platform - New Grad'),
])
def test_legacy_role_level_and_word_order_variations_hold_without_exact_confirmation(setup, saved, current):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job, url='', title=saved)
    historical.import_sheet(conn, executor=sheets)
    found = historical.match(conn, {**job, 'title': current})
    assert found['disposition'] == 'hold' and found['submission_confirmed'] is False


@pytest.mark.parametrize('saved,current', [
    ('Software Engineer I', 'Software Engineer II'),
    ('Software Engineer 1', 'Senior Software Engineer 1'),
    ('Software Engineer New Grad 2026', 'Software Engineer New Grad 2027'),
    ('Software Engineer, Backend', 'Software Engineer, Embedded'),
    ('Software Engineer, ML Platform', 'Software Engineer, Data Platform'),
])
def test_title_normalization_preserves_conflicting_levels_cohorts_and_specialties(setup, saved, current):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job, url='', title=saved)
    historical.import_sheet(conn, executor=sheets)
    assert historical.match(conn, {**job, 'title': current}) is None


def test_new_aliases_never_merge_two_explicitly_different_ats_job_ids(setup):
    conn, job, _, sheets, _ = setup
    sheets.rows[2] = row(job, title='AI Engineer I', location='US')
    historical.import_sheet(conn, executor=sheets)
    assert historical.match(conn, {**job, 'title': 'AI Engineer 1', 'locations': ['United States'],
        'url': ASHBY.replace('555555555555', '555555555556')}) is None


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
