"""An exhausted readiness recovery must not replay the whole open operation."""
import ast
import asyncio
import json
import os
import re
import subprocess

import pytest

from jhb.applications.cli_browser import BrowserOperationError, BrowserUseCLI, MARKER
from jhb.applications.cli_runtime import dispatch


URL = "https://job-boards.greenhouse.io/synthetic-fixture/jobs/1234"
MESSAGE = "Owned application tab remains unresponsive after activation"


def helpers_for(js, selected, activated):
    def unexpected(*args, **kwargs):
        pytest.fail("Unresponsive opening must not change the form or other tabs")

    return {
        "js": js, "cdp": unexpected, "wait": lambda seconds: None,
        "list_tabs": lambda: [{"targetId": "owned-tab", "url": URL},
                              {"targetId": "unrelated", "url": "https://example.test"}],
        "switch_tab": selected.append, "activate_tab": activated.append,
        "current_tab": lambda: {"targetId": "owned-tab", "url": URL},
        "new_tab": unexpected, "close_tab": unexpected, "click_at_xy": unexpected,
    }


@pytest.mark.parametrize("error_type", [TimeoutError, RuntimeError])
def test_exhausted_readiness_recovery_does_not_repeat_open(monkeypatch, tmp_path, error_type):
    monkeypatch.setattr("jhb.applications.cli_browser.ROOT", tmp_path)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:12345")
    calls, reads, selected, activated = [], [], [], []

    def js(expression):
        assert expression == "document.readyState"
        reads.append(expression)
        raise error_type("Runtime.evaluate timed out; expression: document.readyState")

    helpers = helpers_for(js, selected, activated)

    def run(self, script, env, deadline, cancelled):
        assert env.get("JHB_REPAIR_PROCESS_TOKEN") == os.environ.get("JHB_REPAIR_PROCESS_TOKEN")
        match = re.search(r"dispatch_owned\(json.loads\((.+)\),helpers,dispatch,", script)
        request = json.loads(ast.literal_eval(match[1]))
        calls.append(request)
        # Emulate the fixed CLI wrapper's exception boundary without starting
        # any CLI, daemon, live browser or application worker.
        try:
            response = dispatch(request, helpers)
        except ValueError as exc:
            response = {"error": str(exc)}
        except (RuntimeError, TimeoutError):
            return subprocess.CompletedProcess(["browser-use"], 1, "", "synthetic renderer timeout")
        return subprocess.CompletedProcess(["browser-use"], 0, MARKER + json.dumps(response), "")

    monkeypatch.setattr(BrowserUseCLI, "_run", run)
    client = BrowserUseCLI()
    with pytest.raises(BrowserOperationError, match=MESSAGE) as error:
        asyncio.run(client.open(URL))

    assert error.value.retryable is True
    assert [request["operation"] for request in calls] == ["open"]
    assert selected == activated == ["owned-tab"]
    assert len(reads) == 2
    assert client.target_id is None and client.expected_url is None
    assert client.last_failure == {
        "operation": "open", "kind": "browser_mechanics", "mechanical_error": MESSAGE,
    }


@pytest.mark.parametrize("failure_read", [1, 2])
def test_readiness_recovery_preserves_unrelated_runtime_errors(failure_read):
    reads, selected, activated = [], [], []
    error = RuntimeError("Synthetic JavaScript evaluation failure")

    def js(expression):
        assert expression == "document.readyState"
        reads.append(expression)
        if len(reads) == failure_read:
            raise error
        raise RuntimeError("Runtime.evaluate timed out; expression: document.readyState")

    with pytest.raises(RuntimeError) as caught:
        dispatch({"operation": "open", "url": URL}, helpers_for(js, selected, activated))
    assert caught.value is error
    assert len(reads) == failure_read
    assert selected == ["owned-tab"]
    assert activated == (["owned-tab"] if failure_read == 2 else [])
