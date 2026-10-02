import json
import subprocess

import pytest

from jhb.applications.cli_browser import BrowserUseCLI, MARKER
from jhb.applications.cli_runtime import option_matches


def test_cli_uses_stdin_default_daemon_and_preserves_target(monkeypatch, tmp_path):
    monkeypatch.setattr("jhb.applications.cli_browser.ROOT", tmp_path)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:12345")
    monkeypatch.setenv("BU_NAME", "unwanted-job-controller")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, MARKER+json.dumps({"verified": True})+"\n", "")

    monkeypatch.setattr(subprocess, "run", run)
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
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, "private page content", "private secret"))
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
