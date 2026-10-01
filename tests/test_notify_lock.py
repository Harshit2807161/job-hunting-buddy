"""Duplicate-email prevention.

Two mechanisms caused (or could cause) the same openings to arrive twice:
  1. the SMTP retry resending after a delivery that failed only during teardown
  2. a manual poll cycle racing a scheduled one, both reading the same pending set
"""

import sys, pathlib, smtplib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest
from jhb import notify, lock, config


JOBS = [{"title": "Software Engineer", "company": "Acme", "url": "https://x.test/1",
         "dedupe_hash": "aaa", "locations": "[]", "role_classes": "swe",
         "date_posted": 0, "source": "simplify"}]


class _FakeSMTP:
    """Records sends; optionally raises at a chosen point."""
    sends = 0

    def __init__(self, *a, fail_on_exit=False, fail_on_send=False, **k):
        self.fail_on_exit = fail_on_exit
        self.fail_on_send = fail_on_send

    def __enter__(self): return self
    def __exit__(self, *a):
        if self.fail_on_exit:
            raise smtplib.SMTPServerDisconnected("connection closed during QUIT")
        return False

    def login(self, *a): pass

    def send_message(self, msg):
        if self.fail_on_send:
            raise smtplib.SMTPServerDisconnected("dropped before accept")
        type(self).sends += 1


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setattr(config, "SMTP_USER", "u@example.com")
    monkeypatch.setattr(config, "SMTP_PASS", "x" * 16)
    _FakeSMTP.sends = 0


def test_teardown_failure_after_delivery_does_not_resend(monkeypatch):
    """The exact regression: mail accepted, QUIT throws, retry sent a 2nd copy."""
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL",
                        lambda *a, **k: _FakeSMTP(fail_on_exit=True))
    assert notify.send(JOBS) is True
    assert _FakeSMTP.sends == 1, f"resent {_FakeSMTP.sends} times after teardown error"


def test_failure_before_delivery_does_retry(monkeypatch):
    """A genuine pre-delivery failure must still retry."""
    monkeypatch.setattr(notify.time, "sleep", lambda *_: None)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL",
                        lambda *a, **k: _FakeSMTP(fail_on_send=True))
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        notify.send(JOBS)
    assert _FakeSMTP.sends == 0


def test_auth_error_is_not_retried(monkeypatch):
    class _Auth(_FakeSMTP):
        def login(self, *a):
            raise smtplib.SMTPAuthenticationError(535, b"bad app password")
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", lambda *a, **k: _Auth())
    with pytest.raises(smtplib.SMTPAuthenticationError):
        notify.send(JOBS)


def test_message_id_is_stable_for_the_same_batch(monkeypatch):
    seen = []
    class _Cap(_FakeSMTP):
        def send_message(self, msg):
            seen.append(msg["Message-ID"]); _FakeSMTP.sends += 1
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", lambda *a, **k: _Cap())
    notify.send(JOBS); notify.send(JOBS)
    assert seen[0] == seen[1], "same jobs must yield one Message-ID so Gmail collapses them"

    other = [dict(JOBS[0], dedupe_hash="bbb")]
    notify.send(other)
    assert seen[2] != seen[0], "different jobs must yield a different Message-ID"


# --- single-instance lock ---------------------------------------------------

@pytest.fixture
def isolated_lock(tmp_path, monkeypatch):
    """Point the lock at a temp file: the real scheduled task fires every 15
    minutes and would otherwise hold the production lock mid-test."""
    monkeypatch.setattr(lock, "LOCK_PATH", tmp_path / "poll.lock")
    return tmp_path



def test_lock_is_exclusive(isolated_lock):
    with lock.single_instance() as first:
        assert first is True
        with lock.single_instance() as second:
            assert second is False, "a second concurrent cycle must not run"
    # the non-owner must not have deleted the owner's lock on its way out
    assert not lock.LOCK_PATH.exists(), "owner should have cleaned up exactly once"


def test_non_owner_does_not_delete_owner_lock(isolated_lock):
    """Regression: the original finally: block unlinked the lock even when the
    caller never acquired it, so a losing process freed the winner's lock."""
    with lock.single_instance() as first:
        assert first is True
        with lock.single_instance() as second:
            assert second is False
        assert lock.LOCK_PATH.exists(), "non-owner deleted the owner's lock file"


def test_lock_is_released_after_use(isolated_lock):
    with lock.single_instance() as a:
        assert a is True
    with lock.single_instance() as b:
        assert b is True, "lock must be released when the cycle finishes"


def test_lock_released_even_on_exception(isolated_lock):
    with pytest.raises(ValueError):
        with lock.single_instance() as held:
            assert held is True
            raise ValueError("cycle blew up")
    with lock.single_instance() as after:
        assert after is True, "a crashed cycle must not leave the lock held"
