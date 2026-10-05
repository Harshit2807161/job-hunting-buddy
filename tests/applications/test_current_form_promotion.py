"""A discarded draft cannot be replaced by a staged current-form capture."""
import asyncio
import base64
import json

import pytest

from jhb import config
from jhb.applications import application_discard, boards, booklet, live_review, worker

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC")


@pytest.fixture
def capture(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    url = "https://job-boards.greenhouse.io/synthetic/jobs/1901"
    job = {"dedupe_hash": boards.application_hash(url), "url": url, "title": "Engineer", "company": "Synthetic"}
    directory = tmp_path / "private" / "applications" / job["dedupe_hash"]
    packet = {"job": job, "state": "waiting_review", "capture": {"target_id": "synthetic-target"}}
    booklet.write_private(directory / "packet.json", packet)
    (directory / "browser.png").write_bytes(b"old screenshot preserved")
    observation = {"target_id": "synthetic-target", "url": url, "controls": [], "buttons": []}
    result = {"state": "waiting_review", "reason": "Current form captured", "events": [], "filled": [],
              "review_inventory": {"complete": True, "fields": []}}
    monkeypatch.setattr(live_review, "project", lambda *_: result)
    class ReadClient:
        target_id = "synthetic-target"
        async def invoke(self, operation):
            assert operation == "review_current"
            return observation
        async def screenshot(self, path):
            path.write_bytes(PNG)
    return tmp_path, job, directory, packet, ReadClient()


def test_discard_after_staging_keeps_original_packet_and_screenshot(capture, monkeypatch):
    root, job, directory, packet, client = capture
    original = worker.write_packet
    async def staged_then_discarded(*args, **kwargs):
        result = await original(*args, **kwargs)
        booklet.write_private(root / "private" / "application-discards" / (job["dedupe_hash"]+".json"),
                              {"job_hash": job["dedupe_hash"], "state": "discarded"})
        return result
    monkeypatch.setattr(worker, "write_packet", staged_then_discarded)
    with pytest.raises(application_discard.ApplicationDiscarded):
        asyncio.run(live_review.capture_current(directory / "packet.json", client=client))
    assert json.loads((directory / "packet.json").read_text()) == packet
    assert (directory / "browser.png").read_bytes() == b"old screenshot preserved"
    assert not list(directory.glob("before-current-form-approval-*"))


def test_promotion_holds_same_exact_job_lock_as_discard(capture, monkeypatch):
    root, job, directory, packet, client = capture
    original = live_review.shutil.copy2
    locks = []
    def copy_while_checking_lock(source, target):
        with pytest.raises(application_discard.ApplicationActionBusy):
            with application_discard.action_lock(root, job["dedupe_hash"], blocking=False):
                raise AssertionError("Discard could race current-form promotion")
        locks.append(True)
        return original(source, target)
    monkeypatch.setattr(live_review.shutil, "copy2", copy_while_checking_lock)
    path, result = asyncio.run(live_review.capture_current(directory / "packet.json", client=client))
    assert locks and result["state"] == "waiting_review"
    assert path == directory / "review.html"
    with application_discard.action_lock(root, job["dedupe_hash"], blocking=False):
        pass  # Released after promotion, never left blocking unrelated work.
    archived = next(directory.glob("before-current-form-approval-*"))
    assert json.loads((archived / "packet.json").read_text()) == packet
    assert (archived / "browser.png").read_bytes() == b"old screenshot preserved"


def test_new_optional_blank_publishes_preview_without_approval(capture, monkeypatch):
    root, job, directory, packet, client = capture
    result = {"state": "waiting_review", "reason": "Current form captured", "events": [], "filled": [],
              "review_inventory": {"complete": True, "fields": [{"ref": "new-blank", "question": "Optional website",
                "type": "url", "required": False, "status": "blank", "category": "application_question", "candidate_wording_required": False}]}}
    monkeypatch.setattr(live_review, "project", lambda *_: result)
    with pytest.raises(ValueError, match="acknowledge each optional blank"):
        asyncio.run(live_review.capture_current(directory / "packet.json", client=client))
    current = json.loads((directory / "packet.json").read_text())
    assert current["review_inventory"]["fields"][0]["ref"] == "new-blank"
    assert current["review_inventory"]["fields"][0]["status"] == "blank"
    assert (directory / "browser.png").read_bytes() == PNG
    assert (directory / "candidate-current-form.json").is_file()
    assert not (root / "private" / "application-approvals").exists()
    assert not (root / "private" / "authorized-submissions").exists()
