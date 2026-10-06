"""Only fixed mechanics enums survive the transport→packet→feedback boundary."""
import asyncio
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


@pytest.mark.parametrize("catalog, expected", [
    ({"choices": []}, "Verified answer needs a complete native catalog"),
    ({"choices": ["United States"], "truncated": True}, "Verified answer needs a complete native catalog"),
    (None, "Verified answer needs a native field inspection"),
    ({"choices": ["United States"]}, "Verified answer needs a field repair"),
])
def test_wrapped_dropdown_failure_retains_specific_safe_diagnostic(monkeypatch, tmp_path, catalog, expected):
    from jhb.applications import booklet
    from jhb.applications.planner import deterministic_plan

    monkeypatch.setattr(config, "ROOT", tmp_path)
    private_text = "Synthetic private answer must not appear in diagnostics"

    class Form:
        last_failure = {"operation": "fill", "kind": "invalid_operation"}
        fill_count = 0

        def allowed_url(self, url): return True
        async def open(self, url): pass
        async def observe(self):
            return {"fields": [{"ref": "country", "label": "Country", "type": "combobox", "required": True}],
                    "buttons": []}
        async def fill(self, field, value):
            self.fill_count += 1
            raise cli_browser.BrowserOperationError("Stored answer is absent from dropdown options")
        async def describe(self, field):
            if catalog is None:
                raise ValueError(private_text)
            return catalog

    form = Form()
    with pytest.raises(cli_browser.BrowserOperationError) as failure:
        asyncio.run(worker.prepare(None, {"url": "synthetic"},
            {"identity.country": booklet.answer("United States", "synthetic profile")},
            deterministic_plan, None, cli_actions=form))
    result = worker.failure_result(failure.value, form)
    path = attempt_feedback.record_attempt(
        {"dedupe_hash": "d" * 64, "url": "https://job-boards.greenhouse.io/example/jobs/1234"},
        result, attempt_token="wrapped-dropdown")
    feedback = json.loads(path.read_text())
    assert result["events"][0].get("mechanical_error") == expected
    assert feedback.get("mechanical_error") == expected
    assert result["state"] == "failed" and result["error_kind"] == "browser_mechanics" and result["retryable"]
    assert not result.get("missing") and not result.get("submitted")
    assert form.fill_count == 1
    assert private_text not in json.dumps(result) and private_text not in json.dumps(feedback)


def test_wrapped_diagnostics_bound_cyclic_exception_causes():
    failure = cli_browser.BrowserOperationError("Verified answer needs a field repair", retryable=True)
    failure.__cause__ = failure
    result = worker.failure_result(failure)
    assert result["events"][0].get("mechanical_error") == str(failure)


@pytest.mark.parametrize("message", [
    "Verified answer needs a complete native catalog",
    "Verified answer needs a native field inspection",
    "Verified answer needs a field repair",
])
def test_retaining_preparation_diagnostics_does_not_expand_cli_retries(monkeypatch, tmp_path, message):
    monkeypatch.setattr(cli_browser, "ROOT", tmp_path)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:12345")
    monkeypatch.setattr(cli_browser.BrowserUseCLI, "_run", lambda *args: subprocess.CompletedProcess(
        ["browser-use"], 0, cli_browser.MARKER + json.dumps({"error": message}) + "\n", ""))
    client = cli_browser.BrowserUseCLI()
    with pytest.raises(cli_browser.BrowserOperationError) as failure:
        client.call("fill", field={"ref": "synthetic", "type": "combobox"}, value="Synthetic")
    assert failure.value.retryable is False
    assert client.last_failure == {"operation": "fill", "kind": "invalid_operation"}
