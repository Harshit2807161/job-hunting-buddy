import json
import sqlite3
from datetime import datetime, timezone

import pytest

from jhb import config
from jhb.applications import booklet, hourly_reports, overnight, tracking


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setenv('JHB_HOURLY_PROGRESS_EMAIL', '1')
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    tracking.initialize(conn)
    conn.execute('CREATE TABLE applications (state TEXT)')
    auth = {'authorization_id': 'synthetic', 'authorized_at': datetime.fromtimestamp(10000, timezone.utc).isoformat(),
            'expires_at': datetime.fromtimestamp(40000, timezone.utc).isoformat()}
    return conn, auth


def receipt(conn, key, when):
    conn.execute('INSERT INTO confirmed_submissions VALUES(?,?,?,?,?,?,?)',
        (key, 'ashby', 'https://example.org/'+key, json.dumps({'company':'Example','title':'Engineer','url':'https://example.org/'+key}),
         datetime.fromtimestamp(when, timezone.utc).isoformat(), '{}', when))
    conn.commit()


def test_zero_confirmations_still_email_once_per_hour(setup):
    conn, auth = setup
    mail = []
    def send(jobs, **kwargs): mail.append((jobs, kwargs)); return True
    assert hourly_reports.report(conn, auth, now=13599, sender=send)['state'] == 'not_due'
    assert hourly_reports.report(conn, auth, now=13600, sender=send)['state'] == 'delivered'
    hourly_reports.report(conn, auth, now=14000, sender=send)
    assert len(mail) == 1 and mail[0][0] == []
    assert '0 submitted' in mail[0][1]['subject_override']
    hourly_reports.report(conn, auth, now=17200, sender=send)
    assert len(mail) == 2
    assert mail[0][1]['subject_override'] != mail[1][1]['subject_override']


def test_only_positive_receipts_count_and_synced_sheet_rows_reported(setup):
    conn, auth = setup
    receipt(conn, 'old', 9999); receipt(conn, 'new', 12000)
    conn.executemany('INSERT INTO applications VALUES(?)', [('waiting_review',), ('submission_uncertain',), ('submitted',)])
    conn.execute("INSERT INTO submission_sheet_delivery VALUES('new','sink','synced',1,'Sheet!A1:H1',NULL,NULL,12001)")
    conn.commit()
    mail=[]
    def send(jobs, **kwargs): mail.append((jobs,kwargs));return True
    r=hourly_reports.report(conn,auth,now=13600,sender=send)
    assert r['confirmed'] == 1 and len(mail[0][0]) == 1
    assert 'Verified spreadsheet entries for this period: 1' in mail[0][1]['details']
    receipt(conn,'next',15000)
    r=hourly_reports.report(conn,auth,now=17200,sender=send)
    assert r['confirmed'] == 1 and '2 total this run' in mail[1][1]['subject_override']


def test_delivery_failure_backoff_does_not_spam_each_cycle(setup):
    conn,auth=setup;calls=[]
    def fail(jobs,**kwargs):calls.append(jobs);return False
    assert hourly_reports.report(conn,auth,now=13600,sender=fail)['state']=='pending'
    hourly_reports.report(conn,auth,now=13601,sender=fail)
    assert len(calls)==1
    hourly_reports.report(conn,auth,now=13900,sender=fail)
    assert len(calls)==2


def test_expired_authority_sends_nothing(setup):
    conn,auth=setup
    def no_mail(*args,**kwargs):raise AssertionError('expired run sent email')
    assert hourly_reports.report(conn,auth,now=40000,sender=no_mail)['state']=='authorization_ended'


def window(auth):
    return {"role": "user", "status": "verified", "enabled": True, "scope": hourly_reports.REPORT_SCOPE,
            "authorized_at": auth["authorized_at"], "expires_at": auth["expires_at"], "submission_authority": False,
            "frequency_seconds": 3600, "content": "Keep emailing me every hour during the next five hours",
            "source": "Synthetic explicit hourly email and finite run consent"}


def test_reporting_window_sends_while_submission_authority_is_disabled(setup, monkeypatch):
    conn, auth = setup
    monkeypatch.setenv("JHB_OVERNIGHT_SUBMISSIONS_ENABLED", "0")
    monkeypatch.setenv("JHB_PORTAL_SUBMISSIONS_ENABLED", "0")
    monkeypatch.setenv("JHB_REQUIRE_PORTAL_APPROVAL", "1")
    monkeypatch.setattr(overnight, "load_authorization", lambda *a, **kw: pytest.fail("Progress reporting must not consume submit permission"))
    booklet.write_private(config.ROOT / "private" / hourly_reports.REPORT_WINDOW, window(auth))
    mail = []
    def send(jobs, **kwargs): mail.append(kwargs); return True
    assert hourly_reports.report(conn, now=13600, sender=send)["state"] == "delivered"
    assert hourly_reports.report(conn, now=14000, sender=send)["state"] == "delivered"
    assert len(mail) == 1 and "independent checks remain required" in mail[0]["details"]


@pytest.mark.parametrize("change", [
    {"role": "assistant"}, {"status": "needs_input"}, {"enabled": False}, {"source": ""},
    {"scope": "submit all applications"}, {"submission_authority": True}, {"frequency_seconds": 60},
    {"content": "Send updates whenever a job fails"}, {"content": "Do not send hourly email updates"},
    {"expires_at": "2026-10-05T00:00:00"},
    {"expires_at": datetime.fromtimestamp(10000+86401, timezone.utc).isoformat()},
])
def test_invalid_reporting_consent_cannot_enable_hourly_email(setup, change):
    conn, auth = setup
    booklet.write_private(config.ROOT / "private" / hourly_reports.REPORT_WINDOW, {**window(auth), **change})
    def forbidden(*args, **kwargs): pytest.fail("Invalid report consent must not send")
    assert hourly_reports.report(conn, now=13600, sender=forbidden)["state"] == "authorization_ended"


def test_reporting_consent_expiry_and_revocation_send_nothing(setup):
    conn, auth = setup
    path = config.ROOT / "private" / hourly_reports.REPORT_WINDOW
    booklet.write_private(path, window(auth))
    assert hourly_reports.load_report_window(now=40000) is None
    booklet.write_private(path, {**window(auth), "enabled": False})
    assert hourly_reports.load_report_window(now=13600) is None
