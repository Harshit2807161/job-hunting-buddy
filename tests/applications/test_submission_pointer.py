"""Synthetic native pointer faults around the durable terminal-click boundary."""
import asyncio
import json

import pytest

from jhb.applications.authorized_submission import AuthorizedSubmissionCLI, submit_reviewed
from tests.applications.test_authorized_submission import evidence, synthetic_runtime

TRANSPORT_ERROR = "Browser Use CLI failed; run browser-use --doctor"


def terminal_pointer(helpers, params):
    """Fault the terminal pointer, independently of owned dropdown inspection."""
    return helpers["js"]("(([x,y])=>Boolean(document.elementFromPoint(x,y)?.closest('#application button[type=submit]')))(" +
                         json.dumps([params.get("x", 0), params.get("y", 0)]) + ")")


@pytest.mark.parametrize("failure", ["priming_release", "priming_move", "press", "release_before", "release_after"])
def test_pointer_timeout_boundary_keeps_prepress_retryable_and_postpress_uncertain(tmp_path, monkeypatch, failure):
    job, packet, packet_path, manifest, authority, attempt = evidence(tmp_path, monkeypatch)
    with synthetic_runtime() as (_, invoke, inspect, helpers, target, other_guard, _):
        original_cdp = helpers["cdp"]
        helpers["jhb_cdp_timeout"] = 15
        armed = {"value": False, "failed": False}
        events = []
        def cdp(method, **params):
            budget = params.pop("_response_timeout", None)
            if armed["value"] and method == "Input.dispatchMouseEvent" and terminal_pointer(helpers, params):
                clicked = bool(json.loads(attempt.read_text()).get("runtime_click_started"))
                guard = helpers["js"]("window.__jhbGuard===true")
                kind = params["type"]
                events.append({"kind": kind, "budget": budget, "clicked": clicked, "guard": guard})
                fault = ((failure == "priming_release" and kind == "mouseReleased" and not clicked)
                         or (failure == "priming_move" and kind == "mouseMoved")
                         or (failure == "press" and kind == "mousePressed")
                         or (failure in {"release_before", "release_after"} and kind == "mouseReleased" and clicked))
                if fault and not armed["failed"]:
                    armed["failed"] = True
                    if failure == "release_after":
                        original_cdp(method, **params)  # IPC response can be lost after Chrome acted.
                    raise RuntimeError(TRANSPORT_ERROR)
            return original_cdp(method, **params)
        helpers["cdp"] = cdp
        async def timed_invoke(operation, **payload):
            if operation == "submit":
                armed["value"] = True
            return await invoke(operation, **payload)
        client = AuthorizedSubmissionCLI()
        monkeypatch.setattr(client, "invoke", timed_invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=authority, attempt=attempt, cli=client))
        prepress = failure.startswith("priming")
        assert armed["failed"] is True
        assert result["state"] == ("waiting_review" if prepress else "uncertain")
        assert result["retryable"] is prepress
        assert result["error_kind"] == "browser_transport"
        assert result["click_started"] is (not prepress)
        assert all(event["budget"] == 15 for event in events)
        assert all(event["guard"] for event in events if not event["clicked"])
        assert inspect("window.submissions") == (1 if failure == "release_after" else 0)
        assert inspect("window.__jhbGuard") is True and other_guard() is True
        assert not attempt.parent.joinpath("receipt.json").exists()
        if prepress:
            assert not json.loads(attempt.read_text()).get("runtime_click_started")
            assert not any(event["kind"] == "mousePressed" for event in events)
        else:
            assert json.loads(attempt.read_text())["runtime_click_started"] is True
            events_before = list(events)
            repeated = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=authority, attempt=attempt, cli=client))
            assert repeated["state"] == "uncertain" and repeated["retryable"] is False
            assert events == events_before  # Consumed attempt stops before any browser call.


@pytest.mark.parametrize("mutation", ["retained_answer", "revoke_authority"])
def test_pointer_hover_changes_are_rechecked_before_consuming_terminal_attempt(tmp_path, monkeypatch, mutation):
    job, packet, packet_path, manifest, authority, attempt = evidence(tmp_path, monkeypatch)
    with synthetic_runtime() as (_, invoke, inspect, helpers, target, other_guard, _):
        original_cdp = helpers["cdp"]
        armed = {"value": False, "mutated": False}
        presses = []
        def cdp(method, **params):
            params.pop("_response_timeout", None)
            is_terminal = method == "Input.dispatchMouseEvent" and terminal_pointer(helpers, params)
            result = original_cdp(method, **params)
            if armed["value"] and is_terminal:
                if params["type"] == "mousePressed":
                    presses.append(True)
                if params["type"] == "mouseMoved" and not armed["mutated"]:
                    armed["mutated"] = True
                    if mutation == "retained_answer":
                        helpers["js"]("document.getElementById('first_name').value='Changed by hover'")
                    else:
                        path = type(attempt)(authority["authorization_path"])
                        from jhb.applications.booklet import write_private
                        write_private(path, {**json.loads(path.read_text()), "enabled": False})
            return result
        helpers["cdp"] = cdp
        async def mutating_invoke(operation, **payload):
            if operation == "submit":
                armed["value"] = True
            return await invoke(operation, **payload)
        client = AuthorizedSubmissionCLI()
        monkeypatch.setattr(client, "invoke", mutating_invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=authority, attempt=attempt, cli=client))
        assert armed["mutated"] is True
        assert result["state"] == "waiting_review" and result["retryable"] is False
        assert presses == [] and inspect("window.submissions") == 0
        assert not json.loads(attempt.read_text()).get("runtime_click_started")
        assert inspect("window.__jhbGuard") is True and other_guard() is True


def test_geometry_settles_again_after_guarded_hover_and_all_terminal_inputs_use_budget(tmp_path, monkeypatch):
    job, packet, packet_path, manifest, authority, attempt = evidence(tmp_path, monkeypatch)
    with synthetic_runtime() as (_, invoke, inspect, helpers, target, other_guard, _):
        original_cdp = helpers["cdp"]
        helpers["jhb_cdp_timeout"] = 15
        armed = {"value": False, "moved": False}
        events = []
        def cdp(method, **params):
            budget = params.pop("_response_timeout", None)
            is_terminal = method == "Input.dispatchMouseEvent" and terminal_pointer(helpers, params)
            result = original_cdp(method, **params)
            if armed["value"]:
                if method == "DOM.getBoxModel":
                    events.append({"kind": "geometry"})
                elif is_terminal:
                    events.append({"kind": params["type"], "y": params["y"], "budget": budget,
                                   "guard": helpers["js"]("window.__jhbGuard===true")})
                    if params["type"] == "mouseMoved" and not armed["moved"]:
                        armed["moved"] = True
                        helpers["js"]("document.querySelector('#application button[type=submit]').style.marginTop='45px'")
            return result
        helpers["cdp"] = cdp
        async def moving_invoke(operation, **payload):
            if operation == "submit":
                armed["value"] = True
            return await invoke(operation, **payload)
        client = AuthorizedSubmissionCLI()
        monkeypatch.setattr(client, "invoke", moving_invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=authority, attempt=attempt, cli=client))
        assert result["state"] == "submitted" and inspect("window.submissions") == 1
        moved = next(i for i, event in enumerate(events) if event["kind"] == "mouseMoved")
        pressed = next(i for i, event in enumerate(events) if event["kind"] == "mousePressed")
        assert any(event["kind"] == "geometry" for event in events[moved+1:pressed])
        assert abs(events[moved]["y"] - events[pressed]["y"]) > 10
        assert events[moved]["guard"] is True
        assert all(event["budget"] == 15 for event in events if event["kind"] != "geometry")
        assert inspect("window.__jhbGuard") is True and other_guard() is True


@pytest.mark.parametrize("message", [TRANSPORT_ERROR, "Browser Use CLI returned no structured result"])
@pytest.mark.parametrize("clicked", [False, True])
def test_only_fixed_sanitized_cli_transport_errors_retry_before_marker(tmp_path, monkeypatch, message, clicked):
    from jhb.applications.booklet import write_private
    job, packet, packet_path, manifest, authority, attempt = evidence(tmp_path, monkeypatch)
    client = AuthorizedSubmissionCLI()
    async def failed(operation, **payload):
        if clicked:
            write_private(attempt, {**json.loads(attempt.read_text()), "runtime_click_started": True})
        raise RuntimeError(message)
    monkeypatch.setattr(client, "invoke", failed)
    result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=authority, attempt=attempt, cli=client))
    assert result["state"] == ("uncertain" if clicked else "waiting_review")
    assert result["error_kind"] == "browser_transport"
    assert result["retryable"] is (not clicked)


@pytest.mark.parametrize("case", ["paused_until_wake", "still_paused", "changed_before_wake", "changed_after_wake", "press_timeout"])
def test_exact_owned_tab_wakes_once_only_for_guarded_prepress_native_timeout(tmp_path, monkeypatch, case):
    job, packet, packet_path, manifest, authority, attempt = evidence(tmp_path, monkeypatch)
    with synthetic_runtime() as (_, invoke, inspect, helpers, target, other_guard, _):
        original_cdp, original_current = helpers["cdp"], helpers["current_tab"]
        helpers["jhb_cdp_timeout"] = 15
        state = {"armed": False, "awake": False, "changed": False, "moves": 0}
        activations, presses = [], []
        def current():
            owned = original_current()
            return {**owned, "targetId": "unowned-target"} if state["changed"] else owned
        def activate(owned):
            assert owned == target
            assert helpers["js"]("window.__jhbGuard===true") is True
            assert not json.loads(attempt.read_text()).get("runtime_click_started")
            activations.append(owned)
            state["awake"] = True
            if case == "changed_after_wake":
                state["changed"] = True
        def cdp(method, **params):
            params.pop("_response_timeout", None)
            if state["armed"] and method == "Input.dispatchMouseEvent" and terminal_pointer(helpers, params):
                if params["type"] == "mouseMoved":
                    state["moves"] += 1
                    if case != "press_timeout" and (not state["awake"] or case == "still_paused"):
                        if case == "changed_before_wake":
                            state["changed"] = True
                        raise TimeoutError("Input.dispatchMouseEvent timed out after 15s waiting for the daemon")
                if params["type"] == "mousePressed":
                    presses.append(True)
                    if case == "press_timeout":
                        raise TimeoutError("Input.dispatchMouseEvent timed out after 15s waiting for the daemon")
            return original_cdp(method, **params)
        helpers.update(cdp=cdp, current_tab=current, activate_tab=activate)
        async def paused_invoke(operation, **payload):
            if operation == "submit":
                state["armed"] = True
            return await invoke(operation, **payload)
        client = AuthorizedSubmissionCLI()
        monkeypatch.setattr(client, "invoke", paused_invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=authority, attempt=attempt, cli=client))
        if case == "paused_until_wake":
            assert result["state"] == "submitted" and inspect("window.submissions") == 1
            assert activations == [target] and state["moves"] == 2 and presses == [True]
        elif case == "press_timeout":
            assert result["state"] == "uncertain" and result["retryable"] is False
            assert activations == [] and presses == [True]
            assert json.loads(attempt.read_text())["runtime_click_started"] is True
            repeated = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=authority, attempt=attempt, cli=client))
            assert repeated["state"] == "uncertain" and presses == [True]
        else:
            assert result["state"] == "waiting_review"
            assert result["retryable"] is (case == "still_paused")
            assert activations == ([] if case == "changed_before_wake" else [target])
            assert state["moves"] == (2 if case == "still_paused" else 1)
            assert presses == [] and not json.loads(attempt.read_text()).get("runtime_click_started")
        assert inspect("window.__jhbGuard") is True and other_guard() is True
        if case != "paused_until_wake":
            assert inspect("window.submissions") == 0
