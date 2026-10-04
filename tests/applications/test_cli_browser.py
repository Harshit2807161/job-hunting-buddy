import json
import asyncio
import os
import subprocess
import sys
import time

import pytest

from jhb.applications.cli_browser import BrowserUseCLI, MARKER
from jhb.applications.cli_runtime import option_matches


def test_salary_categories_represent_approved_amount_only_for_salary_fields():
    bands = ["$75,000 - $100,000", "$100,000 - $125,000", "$125,000 - $150,000", "$250,000 - +"]
    label = "What are your salary expectations?*"
    assert [band for band in bands if option_matches(band, 100000, field_label=label)] == [bands[1]]
    assert option_matches(bands[2], 131000, field_label=label)
    assert option_matches(bands[3], 300000, field_label=label)
    assert not option_matches(bands[1], True, field_label=label)
    assert not option_matches(bands[1], 100000, field_label="Investment holdings")
    for unsupported in ["CAD $100,000 - $125,000", "$100,000 - $125,000 per hour", "$125,000 - $100,000", "$100,000 or less"]:
        assert not option_matches(unsupported, 110000, field_label=label)


def test_read_transport_reconnects_once_but_does_not_repeat_mutation(monkeypatch):
    client = BrowserUseCLI()
    calls = []
    async def once(operation, **payload):
        calls.append(operation)
        if len(calls) == 1:
            raise RuntimeError("Browser Use CLI failed; run browser-use --doctor")
        return {"verified": True}
    monkeypatch.setattr(client, "_invoke_once", once)
    assert asyncio.run(client.invoke("observe")) == {"verified": True}
    assert calls == ["observe", "observe"]
    calls.clear()
    with pytest.raises(RuntimeError):
        asyncio.run(client.invoke("fill", value="synthetic"))
    assert calls == ["fill"]


def test_cli_uses_stdin_default_daemon_and_preserves_target(monkeypatch, tmp_path):
    monkeypatch.setattr("jhb.applications.cli_browser.ROOT", tmp_path)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:12345")
    monkeypatch.setenv("BU_NAME", "unwanted-job-controller")
    calls = []

    def run(self, script, env, deadline, cancelled):
        calls.append(([self.executable], {"input": script, "env": env}))
        return subprocess.CompletedProcess([self.executable], 0, MARKER+json.dumps({"verified": True})+"\n", "")

    monkeypatch.setattr(BrowserUseCLI, "_run", run)
    client = BrowserUseCLI()
    client.target_id = "synthetic-tab"
    assert client.call("fill", field={"ref": "email", "type": "text"}, value="sam@example.test")["verified"]
    command, options = calls[0]
    assert command == ["browser-use"]
    assert "sam@example.test" in options["input"]
    assert "synthetic-tab" in options["input"]
    assert "BU_NAME" not in options["env"]
    assert options["env"]["BH_TELEMETRY"] == "0"


def test_cli_errors_do_not_expose_page_content(monkeypatch, tmp_path):
    monkeypatch.setattr("jhb.applications.cli_browser.ROOT", tmp_path)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:12345")
    monkeypatch.setattr(BrowserUseCLI, "_run", lambda *a, **kw: subprocess.CompletedProcess(["browser-use"], 1, "private page content", "private secret"))
    with pytest.raises(RuntimeError, match="Browser Use CLI failed") as error:
        BrowserUseCLI().call("observe")
    assert "private" not in str(error.value)


def test_dropdown_matching_does_not_guess_sensitive_answers():
    assert option_matches("Yes", True)
    assert not option_matches("No", True)
    assert not option_matches("I decline to answer", False)
    assert not option_matches("I am not a protected veteran", False)
    assert option_matches("I am not a protected veteran", False, field_id="veteran_status")
    assert option_matches("California", "CA")
    assert not option_matches("New York", "CA")
    assert not option_matches("San Diego County, California, United States", "San Diego, CA, USA", field_id="candidate-location")
    assert option_matches("San Diego, California, United States", "San Diego, CA", field_id="candidate-location")
    assert option_matches("Example University - North Campus", "Example University North Campus", field_id="school--0")
    assert not option_matches("Example University South Campus", "Example University North Campus", field_id="school--0")


def test_cli_timeout_reaps_process_before_releasing_lane(monkeypatch, tmp_path):
    monkeypatch.setattr("jhb.applications.cli_browser.ROOT", tmp_path)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:12345")
    executable = tmp_path / "sleep-cli"
    executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    executable.chmod(0o700)
    # The timeout includes interpreter startup. Capture the real process in
    # the parent so a slow startup cannot race a child-written PID marker.
    spawned, real_popen = [], subprocess.Popen
    def spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process
    monkeypatch.setattr("jhb.applications.cli_browser.subprocess.Popen", spawn)
    client = BrowserUseCLI(executable=str(executable), timeout=1)
    with pytest.raises(TimeoutError, match="timed out"):
        client.call("observe")
    assert len(spawned) == 1 and spawned[0].returncode is not None
    pid = spawned[0].pid
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert client.last_failure["kind"] == "timeout"
    assert client.last_failure["operation"] == "observe"


def test_async_cancellation_waits_for_child_cleanup(monkeypatch, tmp_path):
    monkeypatch.setattr("jhb.applications.cli_browser.ROOT", tmp_path)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:12345")
    executable = tmp_path / "sleep-cli"
    executable.write_text(f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\nPath({str(tmp_path / 'pid')!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n")
    executable.chmod(0o700)
    client = BrowserUseCLI(executable=str(executable), timeout=5)
    async def exercise():
        task = asyncio.create_task(client.invoke("observe"))
        deadline = time.monotonic()+3
        while not (tmp_path / "pid").exists():
            assert time.monotonic()<deadline
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(int((tmp_path / "pid").read_text()), 0)
    asyncio.run(exercise())
    assert client.last_failure["kind"] == "cancelled"


def test_lane_wait_is_bounded_and_never_starts_second_process(monkeypatch, tmp_path):
    import fcntl
    monkeypatch.setattr("jhb.applications.cli_browser.ROOT", tmp_path)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:12345")
    lane = tmp_path / "private" / "browser-lane.lock"
    lane.parent.mkdir()
    lane.touch()
    with lane.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        monkeypatch.setattr(BrowserUseCLI, "_run", lambda *a: pytest.fail("Started without lane"))
        with pytest.raises(TimeoutError, match="lane timed out"):
            BrowserUseCLI(timeout=0.1).call("observe")


def test_upload_cache_is_per_client_target_path_and_contents(monkeypatch, tmp_path):
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"synthetic ML resume")
    calls = []
    async def invoke(operation, **payload):
        calls.append(payload)
        return {"verified": True, "upload_receipt": "receipt"}
    client = BrowserUseCLI()
    client.target_id = "job-one"
    monkeypatch.setattr(client, "invoke", invoke)
    field = {"ref": "uploaded:Resume/CV", "type": "file", "label": "Resume/CV"}
    async def exercise():
        await client.fill(field, str(path))
        await client.fill(field, str(path))
        path.write_bytes(b"synthetic SDE resume same filename")
        await client.fill(field, str(path))
        client.target_id = "job-two"
        await client.fill(field, str(path))
    asyncio.run(exercise())
    assert [call["upload_receipt"] for call in calls] == [None, "receipt", None, None]
