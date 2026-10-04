"""Fresh capture/portal proofs use synthetic candidates and isolated fixture pages."""
import asyncio
import base64
import hashlib
import json
import os

import pytest

from jhb.applications import approvals, booklet, capture, pipeline, worker
from tests.dashboard.test_dashboard import portal, reviewable

IMAGE = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC")


class CLI:
    target_id = "synthetic-owned-tab"

    def __init__(self, action="success"):
        self.action = action
        self.path = None

    async def screenshot(self, path):
        self.path = path
        assert path.name.startswith(".capture-") and path.name.endswith(".png")
        assert not path.exists()
        if self.action == "timeout":
            raise TimeoutError("Synthetic screenshot timeout")
        if self.action == "absent":
            return
        path.write_bytes(IMAGE if self.action != "invalid" else capture.PNG+b"invalid")
        if self.action == "old":
            os.utime(path, ns=(1, 1))


def prepare(portal, cli):
    job, folder, packet = reviewable(portal)
    for field in packet["review_inventory"]["fields"]:
        field.setdefault("category", "profile_fact")
        field.setdefault("candidate_wording_required", False)
    result = {k: v for k, v in packet.items() if k not in {"job", "created_at", "submitted"}}
    result.update(reason="Synthetic complete draft", events=[])
    asyncio.run(worker.write_packet(None, folder, job, result, cli_actions=cli))
    return job, folder, result, json.loads((folder/"packet.json").read_text())


@pytest.mark.parametrize("action", ["timeout", "absent", "invalid", "old"])
def test_old_screenshot_cannot_make_failed_new_capture_review_ready(portal, action):
    root, conn, book, client, _ = portal
    job, folder, result, packet = prepare(portal, CLI(action))
    assert result["state"] == packet["state"] == "failed"
    assert packet["retryable"] and packet["error_kind"] == "browser_capture"
    assert packet["filled"] and packet["review_inventory"]["complete"] and not packet["missing"]
    assert packet["capture"]["verified"] is False and pipeline._recoverable(packet)
    assert (folder/"browser.png").read_bytes() == capture.PNG+b"synthetic image"
    assert not list(folder.glob(".capture-*.png"))
    assert '<img src="browser.png"' not in (folder/"review.html").read_text()
    assert approvals.review(conn, job["dedupe_hash"], book)["can_approve"] is False
    assert client.get(f"/api/v1/applications/{job['dedupe_hash']}/screenshot").status_code == 404
    row = client.get("/api/v1/overview").json()["applications"][0]
    assert row["has_screenshot"] is False and row["inventory_ready"] is False


def test_fresh_capture_is_digest_bound_and_approvable(portal):
    _, conn, book, client, _ = portal
    cli = CLI()
    job, folder, result, packet = prepare(portal, cli)
    assert packet["state"] == result["state"] == "waiting_review"
    assert capture.valid(packet, IMAGE)
    assert packet["capture"]["method"] == "browser_use_cli"
    assert packet["capture"]["target_id"] == cli.target_id
    assert packet["capture"]["sha256"] == hashlib.sha256(IMAGE).hexdigest()
    assert (folder/"browser.png").read_bytes() == IMAGE and not cli.path.exists()
    assert (folder/"browser.png").stat().st_mode & 0o777 == 0o600
    assert approvals.review(conn, job["dedupe_hash"], book)["can_approve"]
    assert client.get(f"/api/v1/applications/{job['dedupe_hash']}/screenshot").content == IMAGE


@pytest.mark.parametrize("change", ["image", "manifest", "target_job", "packet_time"])
def test_fresh_capture_tampering_blocks_portal_approval_and_display(portal, change):
    _, conn, book, client, _ = portal
    job, folder, _, packet = prepare(portal, CLI())
    if change == "image":
        (folder/"browser.png").write_bytes(IMAGE+b"tampered")
    else:
        if change == "manifest": packet["capture"]["sha256"] = "0"*64
        elif change == "target_job": packet["capture"]["job_hash"] = "0"*64
        else: packet["capture"]["packet_created_at"] -= 1
        booklet.write_private(folder/"packet.json", packet)
    view = approvals.review(conn, job["dedupe_hash"], book)
    assert view["can_approve"] is False and "capture" in view["reason"]
    assert client.get(f"/api/v1/applications/{job['dedupe_hash']}/screenshot").status_code == 404


def test_capture_rejects_symlink_without_touching_referenced_file(tmp_path):
    victim = tmp_path/"keep.png"; victim.write_bytes(IMAGE)
    class SymlinkCLI(CLI):
        async def screenshot(self, path):
            path.symlink_to(victim)
    item = asyncio.run(capture.fresh(tmp_path, {"dedupe_hash": "a"*64}, cli_actions=SymlinkCLI()))
    assert item["verified"] is False and victim.read_bytes() == IMAGE
    assert not list(tmp_path.glob(".capture-*.png"))


def test_fixture_browser_writes_valid_fresh_capture(tmp_path):
    async def run():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content("<h1>Synthetic review</h1><input value='Synthetic Candidate'>")
            result = {"state": "waiting_review", "reason": "Synthetic complete form", "events": [], "filled": []}
            job = {"dedupe_hash": "a"*64, "url": "synthetic", "title": "Synthetic Engineer", "company": "Synthetic"}
            await worker.write_packet(page, tmp_path, job, result)
            await browser.close()
            packet = json.loads((tmp_path/"packet.json").read_text())
            assert packet["state"] == "waiting_review" and packet["capture"]["method"] == "fixture_browser"
            assert capture.valid(packet, (tmp_path/"browser.png").read_bytes())
    asyncio.run(run())


@pytest.mark.parametrize("content", [capture.PNG, IMAGE[:-1], IMAGE+b"trailing", b"not a png", None])
def test_png_requires_complete_checksummed_chunks(content):
    assert capture.png(content) is False
