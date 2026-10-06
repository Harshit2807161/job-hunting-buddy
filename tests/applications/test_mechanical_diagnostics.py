"""Only fixed mechanics enums survive the transport→packet→feedback boundary."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from jhb import config
from jhb.applications import attempt_feedback, cli_browser, worker


@pytest.mark.parametrize("message", ["Education add-record control is ambiguous", "synthetic private candidate text"])
def test_cli_mechanical_error_survives_packet_and_private_feedback_without_page_text(monkeypatch, tmp_path, message):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(cli_browser, "ROOT", tmp_path)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:12345")
    monkeypatch.setattr(cli_browser.BrowserUseCLI, "_run", lambda *args: subprocess.CompletedProcess(
        ["browser-use"], 0, cli_browser.MARKER+json.dumps({"error": message})+"\n", ""))
    client = cli_browser.BrowserUseCLI()
    with pytest.raises(cli_browser.BrowserOperationError) as failure:
        client.call("education", count=2)
    result = worker.failure_result(failure.value, client)
    job = {"dedupe_hash": "a"*64, "url": "https://job-boards.greenhouse.io/example/jobs/1234"}
    path = attempt_feedback.record_attempt(job, result, attempt_token="one-owned-attempt")
    feedback = json.loads(path.read_text())
    known = message in cli_browser.MECHANICAL_ERRORS
    assert (client.last_failure.get("mechanical_error") == message) is known
    assert (result["events"][0].get("mechanical_error") == message) is known
    assert (feedback.get("mechanical_error") == message) is known
    assert path.stat().st_mode & 0o777 == 0o600
    if not known:
        assert message not in json.dumps(result) and message not in json.dumps(feedback)


@pytest.mark.parametrize("detail", ["candidate secret", {"malicious": "page text"}, None])
def test_untrusted_adapter_or_packet_diagnostics_cannot_escape_allowlist(monkeypatch, tmp_path, detail):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    adapter = SimpleNamespace(last_failure={"operation": "education", "mechanical_error": detail})
    result = worker.failure_result(ValueError("private exception detail"), adapter)
    assert "mechanical_error" not in result["events"][0]
    # The reducer independently rechecks even a crafted input packet.
    result["events"][0]["mechanical_error"] = detail
    row = attempt_feedback.build({"dedupe_hash": "b"*64, "url": "https://job-boards.greenhouse.io/example/jobs/1234"},
                                 result, attempt_token="synthetic")
    assert "mechanical_error" not in row
    assert "private exception detail" not in json.dumps(row)


@pytest.mark.parametrize("message", [
    "Approved-answer native catalog inspection exceeds its bounded budget",
    "Approved-answer native dropdown catalog is unavailable",
    "Approved-answer native select catalog changed during inspection",
    "Approved-answer native dropdown inspection failed",
    "private candidate dropdown answer",
])
def test_local_catalog_failure_retains_only_static_diagnostic(message):
    result = worker.failure_result(cli_browser.BrowserOperationError(message, retryable=True),
                                  SimpleNamespace(last_failure=None))
    feedback = attempt_feedback.build(
        {"dedupe_hash": "c"*64, "url": "https://job-boards.greenhouse.io/example/jobs/1234"},
        result, attempt_token="local-enrichment")
    known = message in cli_browser.MECHANICAL_ERRORS
    assert (result["events"][0].get("mechanical_error") == message) is known
    assert (feedback.get("mechanical_error") == message) is known
    assert result["error_kind"] == "browser_mechanics" and not result.get("missing")
    if not known:
        assert message not in json.dumps(result) and message not in json.dumps(feedback)
