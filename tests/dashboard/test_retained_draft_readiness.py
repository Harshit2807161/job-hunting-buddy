"""Readiness uses saved reconciliation evidence, never live GET browser work."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jhb import store
from jhb.applications import approvals, boards, booklet
from test_dashboard import portal, add_job, focusable


def ledger(root, job, packet, *, state="departed", changed=None):
    target = packet["capture"]["target_id"]
    timestamp = datetime.fromisoformat(packet["capture"]["captured_at"].replace("Z", "+00:00")).timestamp()
    row = {"state": state, "requested_url": job["url"], "job_identity": list(boards.job_identity(job["url"])),
           "creation_proof": "official_new_tab_returned_new_target", state+"_at": timestamp+30, **(changed or {})}
    booklet.write_private(root / "private" / "browser-tab-ledger.json", {"schema_version": 1, "tabs": {target: row}})


@pytest.mark.parametrize("state", ["closed", "departed"])
def test_known_closed_exact_target_is_attention_not_ready_and_preserves_evidence(portal, monkeypatch, state):
    root, conn, book, client, headers = portal
    job, folder, packet = focusable(portal)
    approvals.initialize(conn)
    ledger(root, job, packet, state=state)
    before_db, before_packet = list(conn.iterdump()), (folder / "packet.json").read_bytes()
    from jhb.applications.cli_browser import BrowserUseCLI
    async def forbidden(*a, **kw):
        pytest.fail("Dashboard GET must not access Chrome")
    monkeypatch.setattr(BrowserUseCLI, "invoke", forbidden)
    view = client.get("/api/v1/overview").json()
    application = view["applications"][0]
    assert view["summary"]["ready"] == 0
    assert view["summary"]["legacy_review"] == 0
    assert application["state"] == "waiting_review" and application["display_state"] == "needs_review"
    assert application["inventory_verified"] is True and application["inventory_ready"] is False
    assert application["draft_target_state"] == "unavailable"
    detail = client.get(f"/api/v1/applications/{job['dedupe_hash']}").json()
    assert detail["screenshot"]["available"] is True
    assert detail["display_state"] == "needs_review" and detail["draft_focus_available"] is False
    assert detail["approval"]["can_approve"] is False and "tab is closed" in detail["approval"]["reason"]
    response = client.post(f"/api/v1/applications/{job['dedupe_hash']}/focus", headers=headers,
                           json={"revision": detail["packet_revision"]})
    assert response.status_code == 409
    assert list(conn.iterdump()) == before_db
    assert (folder / "packet.json").read_bytes() == before_packet


@pytest.mark.parametrize("changed", [
    {"job_identity": ["greenhouse", "global", "different", "999"]},
    {"requested_url": "https://job-boards.greenhouse.io/other/jobs/999"},
    {"departed_at": 1},
    {"creation_proof": "unproven"},
])
def test_unrelated_or_older_closed_target_evidence_never_marks_current_draft_closed(portal, changed):
    root, conn, book, client, headers = portal
    job, folder, packet = focusable(portal)
    ledger(root, job, packet, changed=changed)
    view = client.get("/api/v1/overview").json()
    assert view["applications"][0]["draft_target_state"] == "unknown"
    assert view["summary"]["ready"] == 1


def test_retained_target_remains_ready_but_no_live_presence_is_claimed(portal):
    root, conn, book, client, headers = portal
    job, folder, packet = focusable(portal)
    ledger(root, job, packet, state="active")
    view = client.get("/api/v1/overview").json()
    assert view["summary"]["ready"] == 1
    assert view["applications"][0]["draft_target_state"] == "retained"


def test_phase_one_opening_status_matches_closed_draft_readiness(portal):
    root, conn, book, client, headers = portal
    job, folder, packet = focusable(portal)
    ledger(root, job, packet)
    conn.executescript(store.SCHEMA)
    conn.execute("INSERT INTO jobs(dedupe_hash,company,title,url,source,first_seen) VALUES(?,?,?,?,?,?)",
                 (job["dedupe_hash"], job["company"], job["title"], job["url"], "synthetic", 1))
    conn.commit()
    opening = client.get("/api/v1/openings").json()["items"][0]
    assert opening["application_state"] == "waiting_review"
    assert opening["application_display_state"] == "needs_review"


def test_prepared_day_counts_one_complete_verified_capture_not_legacy_marker_or_attempts(portal):
    root, conn, book, client, headers = portal
    job, folder, packet = focusable(portal)
    add_job(conn, root, n=2, complete=False)
    add_job(conn, root, n=3, complete=True)  # Legacy PNG header alone is not fresh capture evidence.
    conn.execute("UPDATE applications SET attempts=9 WHERE job_hash=?", (job["dedupe_hash"],))
    conn.commit()
    date = datetime.fromtimestamp(packet["created_at"], ZoneInfo("America/Los_Angeles")).date().isoformat()
    view = client.get(f"/api/v1/overview?day={date}").json()
    assert view["summary"]["prepared_today"] == 1
    # Successfully submitted or later closed drafts retain their preparation history.
    ledger(root, job, packet)
    conn.execute("UPDATE applications SET state='submitted' WHERE job_hash=?", (job["dedupe_hash"],)); conn.commit()
    assert client.get(f"/api/v1/overview?day={date}").json()["summary"]["prepared_today"] == 1


def test_incomplete_or_damaged_verified_capture_does_not_count_prepared(portal):
    root, conn, book, client, headers = portal
    job, folder, packet = focusable(portal)
    packet["review_inventory"]["fields"][1]["required"] = True
    booklet.write_private(folder / "packet.json", packet)
    date = datetime.fromtimestamp(packet["created_at"], ZoneInfo("America/Los_Angeles")).date().isoformat()
    assert client.get(f"/api/v1/overview?day={date}").json()["summary"]["prepared_today"] == 0
