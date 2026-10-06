"""Synthetic Sheets/receipts only; never use live authentication or APIs."""
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from jhb import config
from jhb.applications import booklet, queue, tracking

ASHBY = "https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application?ref=fixture"
CANONICAL = ASHBY.split("/application")[0]


class Sheets:
    def __init__(self):
        self.rows = {1: list(tracking.HEADERS)}
        self.count, self.columns, self.calls, self.appends = 1000, 26, [], 0
        self.timeout = None
        self.fail_readback = False
        self.misaligned = False

    def __call__(self, slug, payload):
        self.calls.append((slug, payload))
        if slug == "GOOGLESHEETS_GET_SPREADSHEET_INFO":
            if self.fail_readback and self.appends:
                raise RuntimeError("Synthetic failure; message must not be persisted")
            return {"spreadsheetId": "synthetic-sheet", "properties": {"timeZone": "America/Los_Angeles"},
                    "sheets": [{"properties": {"title": "Sheet1", "gridProperties": {"rowCount": self.count, "columnCount": self.columns}}}]}
        if slug == "GOOGLESHEETS_BATCH_GET":
            assert payload["valueRenderOption"] == "FORMULA" and payload["majorDimension"] == "ROWS"
            requested = payload["ranges"][0]
            _, _, start, _, end = tracking._parse_range(requested)
            last = max((i for i in self.rows if start <= i <= end), default=start-1)
            return {"valueRanges": [{"range": requested, "values": [self.rows.get(i, []) for i in range(start, last+1)]}]}
        assert slug == "GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND"
        assert payload["valueInputOption"] == "RAW" and payload["insertDataOption"] == "INSERT_ROWS"
        assert payload["range"] == "'Sheet1'!A:H"
        assert payload["values"][0][4:7] == ["", "", ""]
        self.appends += 1
        if self.timeout == "before":
            raise subprocess.TimeoutExpired("synthetic-cli", 1)
        number = max(self.rows)+1
        self.rows[number] = ([""] if self.misaligned else []) + payload["values"][0]
        if self.timeout == "after":
            raise subprocess.TimeoutExpired("synthetic-cli", 1)
        return {"updates": {"updatedRange": f"Sheet1!{'B' if self.misaligned else 'A'}{number}:{'I' if self.misaligned else 'H'}{number}"}}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # Runtime schema/skill paths are static repository resources. Initialize
    # them before redirecting only mutable private artifacts to this fixture.
    from jhb.applications import worker  # noqa: F401
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("JHB_TRACKER_CONFIG", raising=False)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    tracking.initialize(conn)
    job = {"company": "Example Corp", "title": "Junior AI Engineer", "locations": ["Example City, CA"], "url": ASHBY}
    receipt = tmp_path / "private" / "receipt.json"
    confirmation = "Your application was successfully submitted."
    booklet.write_private(receipt, {"state": "submitted", "confirmed_at": "2026-10-04T01:00:00+00:00", "url": ASHBY,
                                   "confirmation": confirmation, "body": confirmation+" Synthetic applicant data not copied to ledger",
                                   "target_id": "synthetic-owned-tab", "source": "Live Ashby success page after user-reviewed one-off submission"})
    settings = tmp_path / "private" / tracking.CONFIG_NAME
    booklet.write_private(settings, {"enabled": True, "spreadsheet_id": "synthetic-sheet", "sheet_name": "Sheet1",
                                     "headers": tracking.HEADERS, "timezone": "America/Los_Angeles", "date_style": "ordinal_day_short_month"})
    yield conn, job, receipt, Sheets(), settings
    conn.close()


def test_confirmation_immediately_syncs_once_and_never_invents_status(setup):
    conn, job, receipt, sheets, settings = setup
    first = tracking.record_confirmed(conn, job, receipt, executor=sheets)
    second = tracking.record_confirmed(conn, {**job, "url": CANONICAL}, receipt, executor=sheets)
    assert first["tracking"]["state"] == "complete" and first["tracking"]["synced"] == 1
    assert second["submission_key"] == first["submission_key"]
    assert conn.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 1
    assert sheets.appends == 1
    assert sheets.rows[2] == ["Example Corp", "Junior AI Engineer", "Example City, CA", "3rd oct", "", "", "", CANONICAL]
    proof = conn.execute("SELECT proof_json FROM confirmed_submissions").fetchone()[0]
    assert "Synthetic applicant data" not in proof
    assert receipt.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("change", [
    {"state": "waiting_login"}, {"state": "waiting_review"}, {"body": "Please submit your application"},
    {"confirmation": "The submit button was clicked", "body": "The submit button was clicked"},
    {"source": "Automated worker says submitted"}, {"confirmed_at": "2026-10-04"},
    {"url": ASHBY.replace("555555555555", "555555555556")}, {"target_id": ""},
])
def test_marker_attempt_login_and_wrong_job_never_enter_tracker(setup, change):
    conn, job, receipt, sheets, settings = setup
    booklet.write_private(receipt, {**json.loads(receipt.read_text()), **change})
    with pytest.raises(ValueError):
        tracking.record_confirmed(conn, job, receipt, executor=sheets)
    assert conn.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 0
    assert sheets.calls == []


def test_gh_receipt_without_hash_is_terminal_even_when_discovered_later(setup):
    conn, job, receipt, sheets, settings = setup
    url = "https://job-boards.greenhouse.io/example/jobs/123?gh_src=fixture"
    job = {**job, "url": url}
    booklet.write_private(receipt, {**json.loads(receipt.read_text()), "url": url, "source": "Live Greenhouse success page after user-reviewed one-off submission"})
    tracking.record_confirmed(conn, job, receipt, executor=sheets)
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "submitted"
    assert queue.enqueue(conn, [{**job, "dedupe_hash": "later-source"}]) == 0
    assert queue.claim(conn) is None


def test_gh_confirmation_preserves_existing_phase_one_job_context(setup):
    conn, job, receipt, sheets, settings = setup
    url = "https://job-boards.greenhouse.io/example/jobs/123"
    discovered = {**job, "url": url, "source_url": "https://example.test/careers/123",
                  "source_evidence": "private/source-checks/example.json", "role_classes": ["swe"], "dedupe_hash": "synthetic-source"}
    queue.initialize(conn)
    assert queue.enqueue(conn, [discovered]) == 1
    original = conn.execute("SELECT job_json FROM applications").fetchone()[0]
    booklet.write_private(receipt, {**json.loads(receipt.read_text()), "url": url,
                                   "source": "Live Greenhouse success page after user-reviewed one-off submission"})
    tracking.record_confirmed(conn, {**job, "url": url}, receipt, executor=sheets)
    row = conn.execute("SELECT job_json,state,lease_until FROM applications").fetchone()
    assert row["job_json"] == original and row["state"] == "submitted" and row["lease_until"] is None


@pytest.mark.parametrize("delivery_fails", [False, True])
def test_cli_confirmation_persists_before_delivery_and_sync_command_retries(setup, monkeypatch, capsys, delivery_fails):
    from jhb import store
    from jhb.applications import cli
    _, job, receipt, sheets, settings = setup
    # CLI bootstrapping sets this environment variable; isolate it so synthetic
    # private ROOT does not redirect Chromium for later, unrelated fixture tests.
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).resolve().parents[2] / ".local-browsers"))
    database = config.ROOT / "cli.sqlite3"
    connect = store.connect
    monkeypatch.setattr(store, "connect", lambda: connect(database))
    job_file = config.ROOT / "private" / "confirmed-job.json"
    booklet.write_private(job_file, job)
    fail = delivery_fails

    def execute(slug, payload):
        # A distinct SQLite connection proves the receipt transaction committed
        # before any attempt to reach an authenticated Sheets connector.
        with sqlite3.connect(database) as observer:
            assert observer.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 1
        if fail:
            raise RuntimeError("Synthetic connector unavailable")
        return sheets(slug, payload)

    monkeypatch.setattr(tracking, "ComposioSheets", lambda account=None: execute)
    status = cli.main(["confirm-submission", "--job-file", str(job_file), "--receipt", str(receipt)])
    outcome = json.loads(capsys.readouterr().out)
    assert status == int(delivery_fails) and outcome["state"] == "submitted"
    assert outcome["tracking"]["state"] == ("pending" if delivery_fails else "complete")
    fail = False
    assert cli.main(["sync-tracker"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "complete"
    assert sheets.appends == 1 and sheets.rows[2][7] == CANONICAL


def test_pipeline_final_stage_drains_confirmed_proof_only(setup, monkeypatch):
    from jhb.applications import pipeline
    conn, job, receipt, sheets, settings = setup
    saved_settings = json.loads(settings.read_text())
    settings.unlink()
    assert tracking.record_confirmed(conn, job, receipt)["tracking"]["state"] == "disabled"
    booklet.write_private(settings, saved_settings)
    book_path = config.ROOT / "private" / "book.json"
    booklet.write_private(book_path, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}, "custom_answers": {}})
    queue.enqueue(conn, [{**job, "url": "https://job-boards.greenhouse.io/example/jobs/456", "dedupe_hash": "unproven"},
                         {**job, "url": "https://job-boards.greenhouse.io/example/jobs/789", "dedupe_hash": "review-only"}])
    unproven = queue.claim(conn)
    queue.finish(conn, unproven["job_hash"], "submitted")
    prepared = []

    async def runner(candidate, book, **kwargs):
        # Historical duplicate reads precede preparation; appends still follow
        # confirmed submission evidence at the pipeline's final tracking stage.
        assert sheets.appends == 0
        assert all(slug in {"GOOGLESHEETS_GET_SPREADSHEET_INFO", "GOOGLESHEETS_BATCH_GET"} for slug, _ in sheets.calls)
        prepared.append(candidate["url"])
        return {"state": "waiting_review", "events": [], "filled": []}, config.ROOT / "private" / "review.html"

    monkeypatch.setattr(tracking, "ComposioSheets", lambda account=None: sheets)
    result = pipeline.run_cycle(conn, book_path, resolver=lambda *_: None, runner=runner)
    assert len(prepared) == 1 and result["states"] == {"waiting_review": 1}
    assert result["submission_tracking"] == {"state": "complete", "synced": 1, "uncertain": 0, "failed": 0}
    assert sheets.appends == 1 and sheets.rows[2][7] == CANONICAL
    assert conn.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 1
    assert pipeline.run_cycle(conn, book_path, resolver=lambda *_: None, runner=runner)["submission_tracking"]["synced"] == 0
    assert sheets.appends == 1  # Submitted queue markers/review drafts never become events.


@pytest.mark.parametrize("link", ["", "Linkedin", 'https://www.linkedin.com/jobs/view/123/'])
def test_legacy_same_company_role_and_date_dedupes_without_link(setup, link):
    conn, job, receipt, sheets, settings = setup
    sheets.rows[2] = ["EXAMPLE CORP", " Junior AI Engineer ", "", "3rd OCT", "", "", "", link]
    original = json.dumps(sheets.rows)
    tracking.record_confirmed(conn, job, receipt, executor=sheets)
    assert sheets.appends == 0 and json.dumps(sheets.rows) == original
    assert conn.execute("SELECT match_basis FROM submission_sheet_delivery").fetchone()[0] == "legacy_company_role_date"


def test_legacy_other_role_date_and_real_other_ats_identity_do_not_collapse(setup):
    conn, job, receipt, sheets, settings = setup
    sheets.rows[2] = ["Example Corp", "Other Engineer", "", "3rd oct"]
    sheets.rows[3] = ["Example Corp", "Junior AI Engineer", "", "2nd oct"]
    sheets.rows[4] = ["Example Corp", "Junior AI Engineer", "", "3rd oct", "", "", "", CANONICAL.replace("555555555555", "555555555556")]
    tracking.record_confirmed(conn, job, receipt, executor=sheets)
    assert sheets.appends == 1 and sheets.rows[5][7] == CANONICAL


def test_dedupe_reads_full_current_grid_in_chunks_and_understands_hyperlink(setup):
    conn, job, receipt, sheets, settings = setup
    sheets.count = 24000
    sheets.rows[23501] = ["Other display company", "Other display title", "", "old date", "", "", "", f'=HYPERLINK("{CANONICAL}","Application")']
    tracking.record_confirmed(conn, job, receipt, executor=sheets)
    ranges = [payload["ranges"][0] for slug, payload in sheets.calls if slug == "GOOGLESHEETS_BATCH_GET"]
    assert ranges == ["'Sheet1'!A1:Z10000", "'Sheet1'!A10001:Z20000", "'Sheet1'!A20001:Z24000"]
    assert sheets.appends == 0


@pytest.mark.parametrize("failure", ["before", "after"])
def test_ambiguous_timeout_never_blindly_appends_twice(setup, failure):
    conn, job, receipt, sheets, settings = setup
    sheets.timeout = failure
    result = tracking.record_confirmed(conn, job, receipt, executor=sheets)
    assert result["state"] == "submitted" and result["tracking"]["state"] == "pending"
    assert conn.execute("SELECT state FROM submission_sheet_delivery").fetchone()[0] == "uncertain"
    sheets.timeout = None
    retry = tracking.sync_pending(conn, executor=sheets)
    assert sheets.appends == 1
    assert retry["synced"] == int(failure == "after")
    assert retry["uncertain"] == int(failure == "before")


def test_crash_after_write_intent_is_read_only_until_remote_row_is_found(setup):
    conn, job, receipt, sheets, settings = setup
    sheets.timeout = "before"
    tracking.record_confirmed(conn, job, receipt, executor=sheets)
    conn.execute("UPDATE submission_sheet_delivery SET state='writing'"); conn.commit()
    sheets.timeout = None
    tracking.sync_pending(conn, executor=sheets)
    assert sheets.appends == 1
    sheets.rows[2] = [job["company"], job["title"], "", "3rd oct", "", "", "", CANONICAL]
    assert tracking.sync_pending(conn, executor=sheets)["synced"] == 1
    assert sheets.appends == 1


def test_unverified_or_misaligned_append_stays_uncertain_without_rewrite(setup):
    conn, job, receipt, sheets, settings = setup
    sheets.misaligned = True
    result = tracking.record_confirmed(conn, job, receipt, executor=sheets)
    assert result["tracking"]["uncertain"] == 1
    tracking.sync_pending(conn, executor=sheets)
    assert sheets.appends == 1
    assert conn.execute("SELECT state FROM submission_sheet_delivery").fetchone()[0] == "uncertain"


def test_failed_readback_reconciles_committed_row_after_restart(setup):
    conn, job, receipt, sheets, settings = setup
    sheets.fail_readback = True
    assert tracking.record_confirmed(conn, job, receipt, executor=sheets)["tracking"]["uncertain"] == 1
    sheets.fail_readback = False
    assert tracking.sync_pending(conn, executor=sheets)["synced"] == 1
    assert sheets.appends == 1


def test_header_change_blocks_write_and_prewrite_failure_is_retryable(setup):
    conn, job, receipt, sheets, settings = setup
    sheets.rows[1] = ["Unexpected header"]
    result = tracking.record_confirmed(conn, job, receipt, executor=sheets)
    assert result["tracking"]["failed"] == 1 and sheets.appends == 0
    assert conn.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 1
    sheets.rows[1] = list(tracking.HEADERS)
    assert tracking.sync_pending(conn, executor=sheets)["synced"] == 1
    assert sheets.appends == 1


def test_unconfigured_tracker_persists_proof_without_using_auth(setup):
    conn, job, receipt, sheets, settings = setup
    settings.unlink()
    result = tracking.record_confirmed(conn, job, receipt, executor=sheets)
    assert result["tracking"]["state"] == "disabled" and sheets.calls == []
    assert conn.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 1


def test_explicit_user_statement_is_separate_proof_and_requests_are_not_confirmation(setup):
    conn, job, receipt, sheets, settings = setup
    evidence = config.ROOT / "private" / "user-message.json"
    booklet.write_private(evidence, {"role": "user", "content": "I submitted the application."})
    booklet.write_private(receipt, {"state": "submitted", "confirmed_at": "2026-10-04T01:00:00+00:00", "url": ASHBY,
        "source": "explicit_user_confirmation", "confirmation": "I submitted the application.",
        "body": "I submitted the application.", "user_evidence_path": str(evidence)})
    tracking.record_confirmed(conn, job, receipt, executor=sheets)
    assert json.loads(conn.execute("SELECT proof_json FROM confirmed_submissions").fetchone()[0])["kind"] == "explicit_user_confirmation"
    booklet.write_private(evidence, {"role": "user", "content": "Click Submit for me"})
    bad = json.loads(receipt.read_text());bad["confirmation"] = bad["body"] = "Click Submit for me"
    booklet.write_private(receipt, bad)
    with pytest.raises(ValueError, match="affirmative"):
        tracking.record_confirmed(conn, job, receipt, executor=sheets)


@pytest.mark.parametrize("statement,accepted", [("done i submitted it", True), ("Done, I submitted it.", True),
    ("done", False), ("done i submitted it?", False), ("done i have not submitted it", False),
    ("done i submitted it but it failed", False), ("done, please submit it", False)])
def test_user_completion_prelude_still_requires_factual_submission(setup, statement, accepted):
    conn, job, receipt, sheets, settings = setup
    evidence = config.ROOT / "private" / "user-message.json"
    booklet.write_private(evidence, {"role": "user", "content": statement})
    booklet.write_private(receipt, {"state": "submitted", "confirmed_at": "2026-10-04T01:00:00+00:00", "url": ASHBY,
        "source": "explicit_user_confirmation", "confirmation": statement, "body": statement,
        "user_evidence_path": str(evidence)})
    if accepted:
        result = tracking.record_confirmed(conn, job, receipt, executor=sheets)
        assert result["state"] == "submitted" and sheets.appends == 1
    else:
        with pytest.raises(ValueError, match="affirmative"):
            tracking.record_confirmed(conn, job, receipt, executor=sheets)
        assert conn.execute("SELECT COUNT(*) FROM confirmed_submissions").fetchone()[0] == 0
        assert not sheets.calls


@pytest.mark.parametrize("flat", [True, False])
def test_published_composio_single_flat_and_strict_wrapped_responses(monkeypatch, flat):
    slug = "GOOGLESHEETS_BATCH_GET"
    data = {"valueRanges": []}
    result = {"successful": True, "data": data, "error": None}
    response = result if flat else {"successful": True, "results": [{**result, "slug": slug}]}
    def run(command, **kwargs):
        assert command == ["composio", "execute", slug, "-d", "-"]
        assert json.loads(kwargs["input"])["spreadsheet_id"] == "synthetic-private-id"
        assert "synthetic-private-id" not in command
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")
    monkeypatch.setattr(subprocess, "run", run)
    assert tracking.ComposioSheets()(slug, {"spreadsheet_id": "synthetic-private-id"}) == data


@pytest.mark.parametrize("response", [{"successful": False, "data": {}}, {"successful": True, "data": {}, "error": "error"},
                                       {"successful": True, "unexpected": {"data": {}}}])
def test_composio_failure_or_unknown_shape_never_masquerades_as_success(monkeypatch, response):
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, json.dumps(response), ""))
    with pytest.raises(RuntimeError):
        tracking.ComposioSheets()("GOOGLESHEETS_BATCH_GET", {})


@pytest.mark.parametrize("day,suffix", [(1,"st"),(2,"nd"),(3,"rd"),(11,"th"),(12,"th"),(13,"th"),(21,"st"),(22,"nd"),(23,"rd"),(31,"st")])
def test_sheet_date_matches_ordinal_day_style(day, suffix):
    assert tracking._date(f"2026-10-{day:02}T12:00:00+00:00", "America/Los_Angeles") == f"{day}{suffix} oct"


def test_employer_we_received_wording_records_once_and_syncs_without_reclick(setup):
    conn, job, receipt, sheets, settings = setup
    url = 'https://job-boards.greenhouse.io/example/jobs/7410'
    body = 'Thank you for your interest. We wanted to let you know we received your application.'
    booklet.write_private(receipt, {**json.loads(receipt.read_text()), 'url': url,
        'confirmation': 'we received your application.', 'body': body,
        'source': 'Live Greenhouse success page after authorized submission'})
    first = tracking.record_confirmed(conn, {**job, 'url': url}, receipt, executor=sheets)
    second = tracking.record_confirmed(conn, {**job, 'url': url}, receipt, executor=sheets)
    assert first['state'] == 'submitted' and first['tracking']['synced'] == 1
    assert second['tracking']['synced'] == 0 and sheets.appends == 1
    assert conn.execute('SELECT state FROM applications').fetchone()[0] == 'submitted'


def archived_greenhouse(setup):
    conn, job, receipt, sheets, settings = setup
    url = 'https://job-boards.greenhouse.io/example/jobs/123'
    job = {**job, 'url': url}
    archive = receipt.with_name('original-browser-confirmation.json')
    original = {'state': 'submitted', 'submitted': True,
                'authorization': 'Candidate explicitly requested submission of this exact job',
                'confirmed_at': '2026-10-02T07:28:13+00:00',
                'confirmation_url': url+'/confirmation?gh_src=fixture',
                'confirmation_text': 'Thank you for applying! Your application has been received.',
                'source': 'Browser Use CLI, actual Greenhouse confirmation page'}
    booklet.write_private(archive, original)
    booklet.write_private(receipt, {'state': 'submitted', 'url': url,
                'confirmed_at': original['confirmed_at'],
                'source': 'archived_browser_confirmation',
                'confirmation': original['confirmation_text'], 'body': original['confirmation_text'],
                'archived_evidence_path': str(archive)})
    return conn, job, receipt, sheets, settings, archive


def test_archived_actual_browser_receipt_preserves_original_date_and_provenance(setup):
    conn, job, receipt, sheets, _, archive = archived_greenhouse(setup)
    result = tracking.record_confirmed(conn, job, receipt, executor=sheets)
    assert result['tracking']['synced'] == 1
    saved = conn.execute('SELECT * FROM confirmed_submissions').fetchone()
    proof = json.loads(saved['proof_json'])
    assert proof['kind'] == 'archived_browser_confirmation'
    assert proof['archived_evidence_path'] == str(archive)
    assert 'target_id' not in json.loads(receipt.read_text())
    assert saved['confirmed_at'] == '2026-10-02T07:28:13+00:00'
    assert sheets.rows[2][3] == '2nd oct'
    assert tracking.confirmed_application(conn, job['url'])['verified'] is True
    tracking.record_confirmed(conn, job, receipt, executor=sheets)
    assert sheets.appends == 1


@pytest.mark.parametrize('change', [
    {'state': 'waiting_review'}, {'submitted': False}, {'submitted': 1},
    {'source': 'Agent assumes success'}, {'authorization': ''},
    {'confirmation_url': 'https://job-boards.greenhouse.io/example/jobs/124/confirmation'},
    {'confirmation_url': 'https://job-boards.greenhouse.io/example/jobs/123'},
    {'confirmation_text': 'Clicked the Submit button'},
    {'confirmed_at': '2026-10-03T07:28:13+00:00'},
    {'confirmed_at': '2026-10-02'},
])
def test_archived_attempts_wrong_jobs_and_changed_evidence_never_sync(setup, change):
    conn, job, receipt, sheets, _, archive = archived_greenhouse(setup)
    booklet.write_private(archive, {**json.loads(archive.read_text()), **change})
    with pytest.raises(ValueError):
        tracking.record_confirmed(conn, job, receipt, executor=sheets)
    assert sheets.calls == []
    assert conn.execute('SELECT COUNT(*) FROM confirmed_submissions').fetchone()[0] == 0


def test_archived_evidence_is_immutable_for_pending_sync_and_replay_protection(setup):
    conn, job, receipt, sheets, settings, archive = archived_greenhouse(setup)
    config_value = json.loads(settings.read_text())
    booklet.write_private(settings, {**config_value, 'enabled': False})
    tracking.record_confirmed(conn, job, receipt, executor=sheets)
    # Even an added harmless property changes the original evidence bytes.
    booklet.write_private(archive, {**json.loads(archive.read_text()), 'changed_after_recording': True})
    booklet.write_private(settings, config_value)
    summary = tracking.sync_pending(conn, executor=sheets)
    assert summary['failed'] == 1 and sheets.calls == []
    assert tracking.confirmed_application(conn, job['url'])['verified'] is False
    assert queue.enqueue(conn, [{**job, 'dedupe_hash': 'later-source'}]) == 0
