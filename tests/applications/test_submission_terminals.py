"""Synthetic Chromium evidence for native application-form terminal ownership."""
import asyncio

import pytest

from jhb.applications.authorized_submission import AuthorizedSubmissionCLI, submit_reviewed
from tests.applications.test_authorized_submission import evidence, synthetic_runtime


@pytest.mark.parametrize("navigation", [
    "<header><button type='button' id='header-apply'>Apply</button></header>",
    "<header><a href='#application' role='button' id='header-apply'>Apply</a></header>",
    "<header><form id='navigation-form'><button type='submit' id='header-apply'>Apply</button></form></header>",
    "<header><div role='button' tabindex='0' id='header-apply'>Apply now</div></header>",
])
def test_header_apply_navigation_is_never_a_terminal_and_does_not_mask_receipt(tmp_path, monkeypatch, navigation):
    job, packet, packet_path, manifest, authority, attempt = evidence(tmp_path, monkeypatch)
    with synthetic_runtime() as (_, invoke, inspect, helpers, target, other_guard, _):
        import json
        inspect("document.body.insertAdjacentHTML('afterbegin'," + json.dumps(navigation) + ")")
        inspect("window.navigationClicks=0;document.getElementById('header-apply').addEventListener('click',()=>window.navigationClicks++)")
        client = AuthorizedSubmissionCLI()
        monkeypatch.setattr(client, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=authority, attempt=attempt, cli=client))
        assert result["state"] == "submitted"
        assert inspect("window.submissions") == 1
        assert inspect("window.navigationClicks") == 0
        assert inspect("!!document.getElementById('header-apply')") is True
        assert inspect("window.__jhbGuard") is True and other_guard() is True


@pytest.mark.parametrize("additional", ["<button type='submit'>Apply</button>",
                                         "<input type='submit' value='Submit application'>"])
def test_two_true_native_application_terminals_remain_ambiguous_without_click(tmp_path, monkeypatch, additional):
    import json
    job, packet, packet_path, manifest, authority, attempt = evidence(tmp_path, monkeypatch)
    with synthetic_runtime() as (_, invoke, inspect, helpers, target, other_guard, _):
        inspect("document.getElementById('application').insertAdjacentHTML('beforeend'," + json.dumps(additional) + ")")
        client = AuthorizedSubmissionCLI()
        monkeypatch.setattr(client, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=authority, attempt=attempt, cli=client))
        assert result["state"] == "waiting_review"
        assert "ambiguous" in result["reason"]
        assert inspect("window.submissions") == 0
        assert inspect("window.__jhbGuard") is True and other_guard() is True


def test_type_button_inside_application_is_navigation_even_with_submit_label(tmp_path, monkeypatch):
    job, packet, packet_path, manifest, authority, attempt = evidence(tmp_path, monkeypatch)
    with synthetic_runtime() as (_, invoke, inspect, helpers, target, other_guard, _):
        inspect("document.querySelector('#application button[type=submit]').type='button'")
        client = AuthorizedSubmissionCLI()
        monkeypatch.setattr(client, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=authority, attempt=attempt, cli=client))
        assert result["state"] == "waiting_review"
        assert inspect("window.submissions") == 0
        assert not attempt.parent.joinpath("receipt.json").exists()


def test_native_submit_associated_by_form_attribute_is_owned_even_outside_form(tmp_path, monkeypatch):
    job, packet, packet_path, manifest, authority, attempt = evidence(tmp_path, monkeypatch)
    with synthetic_runtime() as (_, invoke, inspect, helpers, target, other_guard, _):
        inspect("(()=>{const b=document.querySelector('#application button[type=submit]');b.setAttribute('form','application');document.body.appendChild(b)})()")
        client = AuthorizedSubmissionCLI()
        monkeypatch.setattr(client, "invoke", invoke)
        result = asyncio.run(submit_reviewed(job, packet_path, manifest, authorization=authority, attempt=attempt, cli=client))
        assert result["state"] == "submitted"
        assert inspect("window.submissions") == 1
        assert inspect("window.__jhbGuard") is True and other_guard() is True
