"""Bounded ownership and receipt-backed cleanup inside the Browser Use lane.

Only a trusted helper's returned, newly created target establishes ownership.
URLs, arbitrary new targets, reused blanks and application packets do not.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit

from .. import config
from . import boards
from .booklet import normalize, write_private

DISPATCHERS = {"jhb.applications.cli_runtime", "jhb.applications.manual_runtime",
               "jhb.applications.submission_runtime", "jhb.applications.linkedin_runtime",
               "jhb.applications.workday_runtime"}
CAPACITY_MESSAGE = "Worker-owned browser tab capacity reached"
PNG = b"\x89PNG\r\n\x1a\n"
TERMINAL = re.compile(r"(?:submit(?: application)?|apply(?: now)?|send application|finish application)", re.I)
EMPTY_TECHNICAL_KINDS = {"browser_transport", "browser_mechanics", "TimeoutError", "TimeoutExpired", "ConnectionError", "ConnectionResetError"}

# Only the observed native Greenhouse form is supported. Never interpret an
# unknown widget, opaque frame or an incomplete page as proof of emptiness.
EMPTY_GREENHOUSE_FORM = r"""(() => {
 const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const forms=[...document.querySelectorAll('form.application--form#application-form')];
 const form=forms.length===1?forms[0]:null;
 const controls=form?[...form.querySelectorAll('input,textarea,select,[contenteditable=true]')]:[];
 const interactive=controls.filter(e=>visible(e)&&!['hidden','button','submit','reset','file'].includes(e.type));
 const hasValues=controls.filter(e=>!['hidden','button','submit','reset','file'].includes(e.type)).some(e=>e.type==='checkbox'||e.type==='radio'?e.checked:
   !!String(e.isContentEditable?e.innerText:e.value||'').trim());
 const checked=!!form&&form.querySelectorAll('input:checked,[aria-checked=true]').length>0;
 const customValues=!!form&&[...form.querySelectorAll('.select__single-value,.select__multi-value,[role=option][aria-selected=true]')]
   .some(e=>visible(e)&&!!e.textContent.trim());
 const attachments=!!form&&(controls.some(e=>e.type==='file'&&(e.files.length||e.value))||
   [...form.querySelectorAll('.file-upload__filename,a[download]')].some(e=>!!e.textContent.trim())||
   [...form.querySelectorAll('.file-upload')].some(e=>!!e.__jhbUploadReceipt));
 const unknownControls=!!form&&[...form.querySelectorAll('[role=textbox],[role=combobox],[role=slider]')]
   .some(e=>visible(e)&&!e.matches('input,textarea,select,[contenteditable=true]'));
 const frames=[...document.querySelectorAll('iframe')].filter(visible);
 const unknownFrames=frames.some(e=>{try{const u=new URL(e.src);return !(e.closest('.grecaptcha-badge')&&
   u.protocol==='https:'&&['www.google.com','www.recaptcha.net'].includes(u.hostname)&&
   /^\/recaptcha\/(api2|enterprise)\/anchor$/.test(u.pathname));}catch{return true}});
 const dialogs=[...document.querySelectorAll('dialog[open],[role=dialog],[aria-modal=true],[role=listbox]')].some(visible);
 const validation=!!form&&[...form.querySelectorAll('[aria-invalid=true],[role=alert],.error-message,.field-error')]
   .some(e=>visible(e)&&(e.getAttribute('aria-invalid')==='true'||!!e.textContent.trim()));
 const authChallenge=[...document.querySelectorAll('input[type=password],input[autocomplete=one-time-code]')].some(visible)||
   /verify (?:that )?you are human|complete (?:the )?security check|verify your (?:identity|email)/i.test(document.body?.innerText||'');
 const shadowControls=!!form&&[...form.querySelectorAll('*')].some(e=>e.shadowRoot&&visible(e));
 return {schema_version:1,url:location.href,ready:document.readyState==='complete',
   known_form:!!form&&['first_name','last_name','email'].every(id=>form.querySelector('input#'+id)),
   control_count:interactive.length,has_values:hasValues,checked,custom_values:customValues,attachments,
   unknown_controls:unknownControls,unknown_frames:unknownFrames,dialogs,validation,auth_challenge:authChallenge,shadow_controls:shadowControls};
})()"""


class TabCapacityReached(ValueError):
    """No new target or candidate mutation has occurred."""


def _private(path, root, *, limit=16*1024*1024):
    path = Path(path)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("Tab evidence must remain in a private directory")
    path = path.resolve(strict=True)
    if not path.is_relative_to((root / "private").resolve()) or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Tab evidence must remain in a private directory")
    return path, path.read_bytes()


def _blank(url):
    value = str(url or "")
    return (value in {"", "about:blank", "data:text/html,"} or value.startswith("about:blank#")
            or value.startswith(("chrome://newtab", "chrome://new-tab-page", "edge://newtab", "about:newtab")))


def _source_location(url, source_id):
    from .linkedin_runtime import linkedin_id
    if linkedin_id(url) == source_id:
        return True
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in {"linkedin.com", "www.linkedin.com"}
                and not parsed.username and not parsed.password and parsed.port in {None, 443}
                and re.match(r"^/(?:login|authwall|uas/login|checkpoint)(?:/|$)", parsed.path) is not None)
    except (ValueError, TypeError):
        return False


def _cap():
    try:
        value = int(os.environ.get("JHB_MAX_OWNED_TABS", "6"))
    except ValueError:
        raise ValueError("Owned browser tab cap must be an integer between 1 and 12") from None
    if not 1 <= value <= 12:
        raise ValueError("Owned browser tab cap must be an integer between 1 and 12")
    return value


class OwnedTabs:
    def __init__(self, helpers, root):
        self.helpers, self.root = helpers, Path(root).resolve()
        self.path = self.root / "private" / "browser-tab-ledger.json"
        if self.path.exists():
            _, data = _private(self.path, self.root)
            try:
                self.ledger = json.loads(data)
            except ValueError:
                raise ValueError("Browser tab ownership ledger is invalid") from None
            if self.ledger.get("schema_version") != 1 or not isinstance(self.ledger.get("tabs"), dict):
                raise ValueError("Browser tab ownership ledger is invalid")
            if any(not isinstance(target, str) or not target or not isinstance(row, dict)
                   or row.get("state") not in {"active", "closed", "departed", "close_unconfirmed"}
                   or row.get("creation_proof") not in {"official_new_tab_returned_new_target", "native_linkedin_apply_opener"}
                   or row.get("job_identity") != list(boards.job_identity(row.get("requested_url")) or ())
                   or not row.get("job_identity") for target, row in self.ledger["tabs"].items()):
                raise ValueError("Browser tab ownership ledger is invalid")
        else:
            self.ledger = {"schema_version": 1, "tabs": {}, "unclaimed_destinations": {}}
        self.tabs = self.ledger["tabs"]
        self.unclaimed = self.ledger.setdefault("unclaimed_destinations", {})
        self._apply_before, self._apply_source = None, None
        self._apply_clicked = False
        if not isinstance(self.unclaimed, dict):
            raise ValueError("Browser tab ownership ledger is invalid")

    def save(self):
        write_private(self.path, self.ledger)

    def refresh(self):
        live = {tab["targetId"]: tab for tab in self.helpers["list_tabs"]()}
        changed = False
        for target, row in self.tabs.items():
            if row.get("state") in {"active", "close_unconfirmed"} and target not in live:
                row.update(state="departed", departed_at=time.time())
                changed = True
        for target, row in self.unclaimed.items():
            if row.get("state") == "active" and target not in live:
                row.update(state="departed", departed_at=time.time())
                changed = True
        if changed:
            self.save()
        return live

    def _count(self, live):
        return sum(target in live and row.get("state") in {"active", "close_unconfirmed"}
                   for target, row in self.tabs.items())

    def before_apply_click(self):
        live = self.refresh()
        if self._apply_before is None:
            self._apply_before = set(live)
            self._apply_source = self.helpers["current_tab"]().get("targetId")
        else:
            self.observe_unclaimed_popups()
        if self._count(live) >= _cap() or any(row.get("state") == "active" and target in live
                                             for target, row in self.unclaimed.items()):
            raise TabCapacityReached(CAPACITY_MESSAGE)
        # Invalidate the read-only witness only after the no-mutation capacity
        # guard succeeds. A blocked native route can still release its harmless
        # source through the existing terminal-source evidence path.
        row = self.tabs.get(self._apply_source, {})
        if row.get("state") == "active" and row.get("purpose") == "source_readonly":
            row.pop("readonly_observation", None)
            row["purpose"] = "source"
            self.save()

    def after_apply_click(self):
        self._apply_clicked = True

    def observe_unclaimed_popups(self):
        if self._apply_before is None:
            return
        for target, tab in self.refresh().items():
            if target in self._apply_before or target in self.tabs:
                continue
            try:
                info = self.helpers["cdp"]("Target.getTargetInfo", targetId=target)["targetInfo"]
                possible = (info.get("openerId") == self._apply_source or self._apply_clicked
                            and not info.get("openerId") and boards.job_identity(tab.get("url")) is not None)
            except Exception:
                # Preserve uncertain destinations and pause more source clicks;
                # lack of opener evidence never establishes ownership.
                possible = boards.job_identity(tab.get("url")) is not None
            if possible:
                self.unclaimed.setdefault(target, {"state": "active", "first_seen": time.time(),
                    "url": tab.get("url"), "reason": "popup_ownership_not_proven"})
        self.save()

    def new_tab(self, url="about:blank", *, purpose="application"):
        identity = boards.job_identity(url)
        # Authentication/mail tabs do not become disposable application tabs.
        if identity is None:
            return self.helpers["new_tab"](url)
        before = self.refresh()
        if purpose == "source" and any(row.get("state") == "active" and target in before
                                        for target, row in self.unclaimed.items()):
            raise TabCapacityReached(CAPACITY_MESSAGE)
        try:
            current = self.helpers["current_tab"]()
        except Exception:
            current = {}
        prior = set(before) | {current.get("targetId")}
        matching = [t for t in before.values() if boards.job_identity(t.get("url")) == identity]
        if len(matching) == 1:
            self.helpers["switch_tab"](matching[0]["targetId"])
            return matching[0]["targetId"]
        if len(matching) > 1:
            raise ValueError("Multiple exact job tabs need disambiguation")
        count = self._count(before)
        if purpose == "source_readonly":
            # Unknown popups remain preserved and consume capacity. A read of
            # an existing exact source tab adds no tab; a fresh source needs one.
            count += sum(target in before and row.get("state") == "active" and target not in self.tabs
                         for target, row in self.unclaimed.items())
        if purpose in {"source", "source_readonly"}:
            source_count = sum(target in before and row.get("state") in {"active", "close_unconfirmed"}
                               and row.get("purpose") in {"source", "source_readonly"}
                               for target, row in self.tabs.items())
            # Source classification must not occupy every application slot.
            # Exact existing-tab reuse above needs no additional budget.
            if source_count >= min(1, max(0, _cap()-1)):
                raise TabCapacityReached(CAPACITY_MESSAGE)
        if purpose == "application":
            draft_count = sum(target in before and row.get("state") in {"active", "close_unconfirmed"}
                              and row.get("purpose") == "application" for target, row in self.tabs.items())
            # Reserve one of the existing slots for authenticated read-only
            # source routing. Existing drafts and exact-tab reuse stay intact.
            if draft_count >= max(1, _cap()-1) and not (current.get("targetId") and _blank(current.get("url"))):
                raise TabCapacityReached(CAPACITY_MESSAGE)
        reserve = 2 if purpose == "source" else 1
        if count + reserve > _cap() and not (current.get("targetId") and _blank(current.get("url"))):
            raise TabCapacityReached(CAPACITY_MESSAGE)
        returned = self.helpers["new_tab"](url)
        target = returned.get("targetId") if isinstance(returned, dict) else returned
        after = {t["targetId"]: t for t in self.helpers["list_tabs"]()}
        if isinstance(target, str) and target and target not in prior and target in after and target not in self.tabs:
            self.tabs[target] = {"state": "active", "created_at": time.time(), "purpose": purpose,
                                 "job_identity": list(identity), "requested_url": url,
                                 "creation_proof": "official_new_tab_returned_new_target"}
            self.save()
        return returned

    def cleanup_verified_source(self, request):
        """Close one proven transient source after exact isolated JD validation."""
        from .authorized_submission import private_file
        from .job_context import valid_description
        from .linkedin_runtime import linkedin_id, outbound
        try:
            path = private_file(request.get("evidence_path", ""))
            raw = path.read_bytes()
            proof = json.loads(raw)
            source_id = linkedin_id(request.get("approved_url"))
            result = proof.get("resolved", {})
            url = result.get("application_url")
            identity = boards.job_identity(url)
            target = proof.get("source_target_id")
            row = self.tabs.get(target, {})
            live = self.refresh()
            if (hashlib.sha256(raw).hexdigest() != request.get("evidence_sha256")
                    or proof.get("provider") != "readonly_linkedin_apply_href_and_isolated_mcp"
                    or proof.get("native_apply_clicked") is not False or not source_id
                    or linkedin_id(proof.get("source_url")) != source_id
                    or proof.get("observed_apply_url") != result.get("source_url")
                    or not outbound(proof.get("observed_apply_url"))
                    or not isinstance(proof.get("recorded_at"), (int, float))
                    or not 0 <= time.time()-proof["recorded_at"] <= 300
                    or result.get("state") not in {"greenhouse", "not_greenhouse"}
                    or result.get("closed") or not identity or identity[0] == "linkedin"
                    or not valid_description(result.get("verified_job_description"), url)
                    or row.get("state") != "active" or row.get("purpose") not in {"source", "source_readonly"}
                    or row.get("creation_proof") != "official_new_tab_returned_new_target"
                    or row.get("job_identity") != ["linkedin", source_id]
                    or target not in live or linkedin_id(live[target].get("url")) != source_id
                    or not any(e.get("kind") == "mcp_navigation" and boards.job_identity(e.get("url")) == identity
                               for e in result.get("evidence", []) if isinstance(e, dict))):
                return []
            original = self.helpers["current_tab"]().get("targetId")
            if self.close(target, "verified_readonly_apply_href_destination", expected_url=live[target]["url"]):
                if original != target and original in {t["targetId"] for t in self.helpers["list_tabs"]()}:
                    self.helpers["switch_tab"](original)
                return [target]
        except (OSError, ValueError, TypeError, KeyError):
            return []
        return []

    def cleanup_source_terminal(self, request):
        """Close only a proven owned read-only source with a fresh safe DOM.

        Classification may end at login, unsupported, filtered or unknown. None
        of those outcomes needs a disposable LinkedIn listing to occupy a draft
        slot. The outcome never grants authority over user tabs or destinations.
        """
        from .linkedin_runtime import linkedin_id
        original = None
        try:
            _, raw = _private(request.get("evidence_path", ""), self.root, limit=2_000_000)
            proof = json.loads(raw)
            source_id = linkedin_id(request.get("approved_url"))
            observation = proof.get("readonly_observation", {})
            target = observation.get("target_id")
            row, live = self.tabs.get(target, {}), self.refresh()
            outcome = proof.get("classification", {})
            if (hashlib.sha256(raw).hexdigest() != request.get("evidence_sha256")
                    or proof.get("provider") != "readonly_linkedin_terminal_classifier"
                    or proof.get("native_apply_clicked") is not False
                    or not source_id or linkedin_id(proof.get("source_url")) != source_id
                    or observation.get("native_apply_clicked") is not False
                    or observation.get("operation") != "resolve_link"
                    or linkedin_id(observation.get("source_url")) != source_id
                    or observation.get("target_id") != proof.get("source_target_id")
                    or not isinstance(proof.get("recorded_at"), (int, float))
                    or not 0 <= time.time()-proof["recorded_at"] <= 300
                    or not isinstance(observation.get("recorded_at"), (int, float))
                    or not 0 <= proof["recorded_at"]-observation["recorded_at"] <= 300
                    or outcome.get("state") not in {"greenhouse", "not_greenhouse", "blocked", "ambiguous", "filtered", "unsupported", "closed"}
                    or row.get("state") != "active" or row.get("purpose") != "source_readonly"
                    or row.get("creation_proof") != "official_new_tab_returned_new_target"
                    or row.get("job_identity") != ["linkedin", source_id]
                    or observation != row.get("readonly_observation")
                    or outcome.get("handoff") == "waiting_captcha"
                    or not _source_location(observation.get("observed_url"), source_id)
                    or target not in live or live[target].get("url") != observation.get("observed_url")):
                return []
            # A user may have begun a form or login after source classification.
            # Read DOM state immediately before closing; never clear these values.
            original = self.helpers["current_tab"]().get("targetId")
            self.helpers["switch_tab"](target)
            if self.helpers["current_tab"]().get("targetId") != target:
                return []
            state = self.helpers["js"]("""(() => {
                const visible=e=>!!(e.getClientRects().length)&&getComputedStyle(e).visibility!=='hidden';
                const dialogs=[...document.querySelectorAll('dialog[open],[role=dialog],[aria-modal=true],.jobs-easy-apply-modal')].some(visible);
                const edits=[...document.querySelectorAll('input,textarea,select,[contenteditable=true]')].some(e=>{
                    if(e.tagName==='INPUT'&&e.type==='file') return e.files.length>0;
                    if(e.isContentEditable) return !!e.innerText.trim();
                    if(e.tagName==='SELECT') {
                        const index=[...e.options].findIndex(o=>o.defaultSelected);
                        return e.selectedIndex!==(index<0?0:index);
                    }
                    if(e.type==='checkbox'||e.type==='radio') return e.checked!==e.defaultChecked;
                    if(e.type==='hidden'||e.type==='button'||e.type==='submit') return false;
                    return e.value!==e.defaultValue || (e.type==='password'&&!!e.value);
                });
                const applications=[...document.querySelectorAll('form[action*=apply],.jobs-easy-apply-content,iframe[src*=recaptcha],iframe[src*=hcaptcha]')].some(visible);
                return {url:location.href, ready:document.readyState==='complete', dialogs, edits, applications};
            })()""")
            if (not isinstance(state, dict) or state.get("url") != observation["observed_url"]
                    or state.get("ready") is not True or any(state.get(key) is not False for key in ("dialogs", "edits", "applications"))):
                return []
            if self.close(target, "terminal_readonly_source_without_draft", expected_url=observation["observed_url"]):
                return [target]
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            return []
        finally:
            # CDP may briefly list the source after close_tab has removed it.
            # Never reattach the target this operation just attempted to close.
            if original and original != target:
                try:
                    if original in {t["targetId"] for t in self.helpers["list_tabs"]()}:
                        self.helpers["switch_tab"](original)
                except RuntimeError:
                    # A different original tab can also disappear between the
                    # target listing and attach; cleanup evidence remains valid.
                    pass
        return []

    def record_readonly_observation(self, request, result):
        """Capture a no-click classifier witness in the private ownership ledger."""
        from .linkedin_runtime import linkedin_id
        current = self.helpers["current_tab"]()
        source_id = linkedin_id(request.get("approved_url"))
        row = self.tabs.get(current.get("targetId"), {})
        if (not source_id or row.get("state") != "active" or row.get("purpose") != "source_readonly"
                or row.get("job_identity") != ["linkedin", source_id]):
            return result
        observation = {"operation": "resolve_link", "native_apply_clicked": False,
                       "target_id": current["targetId"], "observed_url": current.get("url"),
                       "source_url": request["approved_url"], "recorded_at": time.time(),
                       "classification_state": result.get("state")}
        row["readonly_observation"] = observation
        self.save()
        return {**result, "readonly_observation": observation}

    def retain_review(self, request):
        target = request.get("target_id")
        row = self.tabs.get(target, {})
        if row.get("state") != "active" or row.get("purpose") != "application":
            return
        try:
            image, _ = _private(request["path"], self.root)
            if image.name != "browser.png" and not (image.name.startswith(".capture-") and image.suffix == ".png"):
                return
            current = self.helpers["current_tab"]()
            if current.get("targetId") != target or list(boards.job_identity(current.get("url")) or ()) != row["job_identity"]:
                return
        except (KeyError, OSError, ValueError, TypeError):
            return
        # The worker has not yet atomically published its fresh packet. Remember
        # only the directory; cleanup reads its new manifest after publication.
        row["review_directory"] = str(image.parent)
        self.save()

    def _review(self, row, target):
        from .capture import valid
        directory = Path(row["review_directory"])
        image, shot = _private(directory / "browser.png", self.root)
        packet_path, packet_bytes = _private(directory / "packet.json", self.root)
        packet = json.loads(packet_bytes)
        if (packet.get("state") != "waiting_review" or packet.get("submitted") is not False
                or packet.get("missing") or packet.get("review_inventory", {}).get("complete") is not True
                or list(boards.job_identity(packet.get("job", {}).get("url")) or ()) != row["job_identity"]
                or packet.get("capture", {}).get("target_id") != target or not valid(packet, shot)):
            return None
        return {"screenshot_path": str(image), "screenshot_sha256": hashlib.sha256(shot).hexdigest(),
                "packet_path": str(packet_path), "packet_sha256": hashlib.sha256(packet_bytes).hexdigest()}

    def _confirmed(self):
        path = self.root / "data" / "jobs.sqlite3"
        if not path.is_file() or path.is_symlink() or any(p.is_symlink() for p in path.parents):
            return []
        try:
            with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                return [dict(row) for row in conn.execute("SELECT application_url,job_json,proof_json FROM confirmed_submissions")]
        except sqlite3.Error:
            return []

    def _receipt(self, row, target, records):
        from .tracking import _proof
        for record in records:
            if list(boards.job_identity(record["application_url"]) or ()) != row.get("job_identity"):
                continue
            try:
                stored = json.loads(record["proof_json"])
                job = json.loads(record["job_json"])
                job["url"] = record["application_url"]
                _, _, validated = _proof(job, stored["receipt_path"])
                _, content = _private(stored["receipt_path"], self.root)
                receipt = json.loads(content)
                review = self._review(row, target)
                if review is None:
                    continue
                _, shot = _private(review["screenshot_path"], self.root)
                _, packet_bytes = _private(review["packet_path"], self.root)
                if (validated["kind"] != "live_success_page" or validated["receipt_sha256"] != stored.get("receipt_sha256")
                        or receipt.get("target_id") != target or not receipt.get("observed_url")
                        or hashlib.sha256(shot).hexdigest() != review["screenshot_sha256"] or not shot.startswith(PNG)
                        or hashlib.sha256(packet_bytes).hexdigest() != review["packet_sha256"]):
                    continue
                return receipt
            except (ImportError, KeyError, OSError, ValueError, TypeError):
                continue
        return None

    def _success_visible(self, target, receipt):
        try:
            self.helpers["switch_tab"](target)
            if self.helpers["current_tab"]().get("targetId") != target:
                return False
            if self.helpers["js"]("location.href") != receipt["observed_url"]:
                return False
            if receipt["confirmation"] not in self.helpers["js"]("document.body.innerText"):
                return False
            nodes = self.helpers["cdp"]("Accessibility.getFullAXTree")["nodes"]
            return not any(not node.get("ignored") and node.get("role", {}).get("value") == "button"
                           and TERMINAL.fullmatch(normalize(str(node.get("name", {}).get("value", "")))) for node in nodes)
        except Exception:
            return False

    def close(self, target, reason, *, expected_url):
        row = self.tabs[target]
        current = next((tab for tab in self.helpers["list_tabs"]() if tab["targetId"] == target), None)
        if current is None or current.get("url") != expected_url:
            return False
        # Persist the exact request before calling the official helper. An
        # unconfirmed close is never replayed automatically.
        row.update(state="close_unconfirmed", close_requested_at=time.time(), close_reason=reason)
        self.save()
        try:
            self.helpers["close_tab"](target)
            gone = target not in {tab["targetId"] for tab in self.helpers["list_tabs"]()}
        except Exception:
            gone = False
        if gone:
            row.update(state="closed", closed_at=time.time())
            self.save()
        return gone

    def cleanup(self):
        live, records, closed = self.refresh(), self._confirmed(), []
        try:
            original = self.helpers["current_tab"]().get("targetId")
        except Exception:
            original = None
        try:
            for target, row in self.tabs.items():
                if target not in live or row.get("state") != "active" or row.get("purpose") != "application":
                    continue
                receipt = self._receipt(row, target, records)
                if receipt and live[target].get("url") == receipt["observed_url"] and self._success_visible(target, receipt):
                    if self.close(target, "durable_confirmed_submission_and_retained_review", expected_url=receipt["observed_url"]):
                        closed.append(target)
        finally:
            if original and original in {t["targetId"] for t in self.helpers["list_tabs"]()}:
                self.helpers["switch_tab"](original)
        return closed

    def _empty_technical_evidence(self, row, target):
        """Prove a captured technical attempt has no retained or terminal work."""
        from . import capture, historical
        try:
            identity = row.get("job_identity")
            if (row.get("state") != "active" or row.get("purpose") != "application"
                    or not identity or identity[0] != "greenhouse" or not row.get("review_directory")):
                return None
            directory = Path(row["review_directory"])
            packet_path, raw = _private(directory / "packet.json", self.root)
            image, shot = _private(directory / "browser.png", self.root)
            packet = json.loads(raw); job = packet.get("job", {}); job_hash = boards.application_hash(job.get("url"))
            if (not job_hash or job_hash != job.get("dedupe_hash") or list(boards.job_identity(job.get("url")) or ()) != identity
                    or packet.get("state") != "failed" or packet.get("retryable") is not True
                    or packet.get("error_kind") not in EMPTY_TECHNICAL_KINDS or packet.get("filled") != []
                    or packet.get("submitted") is not False or packet.get("runtime_click_started")
                    or packet.get("capture", {}).get("method") != "browser_use_cli"
                    or packet.get("capture", {}).get("target_id") != target or not capture.valid(packet, shot)):
                return None
            inventory = packet.get("review_inventory", {}).get("fields")
            questions = packet.get("review_questions", [])
            if (not isinstance(inventory, list) or not isinstance(questions, list)
                    or any(not isinstance(f, dict) or f.get("status") not in {"blank", "declined"}
                           for f in inventory + questions)):
                return None
            # File markers may precede a database insert after a crash.
            for folder in ("application-approvals", "authorized-submissions", "application-discards"):
                marker = self.root / "private" / folder / (job_hash + ".json" if folder == "application-discards" else job_hash)
                if marker.exists() or marker.is_symlink():
                    return None
            db = self.root / "data" / "jobs.sqlite3"
            if not db.is_file() or db.is_symlink() or any(p.is_symlink() for p in db.parents):
                return None
            with closing(sqlite3.connect(db.resolve().as_uri()+"?mode=ro", uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"applications", "application_approvals", "authorized_submission_attempts", "confirmed_submissions"} <= tables:
                    return None
                saved = conn.execute("SELECT * FROM applications WHERE job_hash=?", (job_hash,)).fetchone()
                if (not saved or saved["state"] not in {"retry", "failed"} or not saved["packet"]
                        or Path(saved["packet"]).parent != packet_path.parent
                        or saved["error_kind"] not in {packet["error_kind"], None}
                        or saved["state"] == "retry" and saved["error_kind"] != packet["error_kind"]
                        or list(boards.job_identity(json.loads(saved["job_json"]).get("url")) or ()) != identity
                        or any(conn.execute(f"SELECT 1 FROM {table} WHERE job_hash=?", (job_hash,)).fetchone()
                               for table in ("application_approvals", "authorized_submission_attempts"))
                        or historical.match(conn, job, root=self.root)):
                    return None
                queue_revision = hashlib.sha256(json.dumps(dict(saved), sort_keys=True).encode()).hexdigest()
            return {"job_hash": job_hash, "target_id": target, "queue_revision": queue_revision,
                    "packet_path": str(packet_path), "packet_sha256": hashlib.sha256(raw).hexdigest(),
                    "screenshot_path": str(image), "screenshot_sha256": hashlib.sha256(shot).hexdigest()}
        except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
            return None

    def cleanup_empty_technical(self):
        """Park only a fresh, genuinely empty technical attempt; preserve work."""
        live = self.refresh()
        if any(target in live and row.get("state") == "active" for target, row in self.unclaimed.items()):
            return []
        closed = []
        try:
            original = self.helpers["current_tab"]().get("targetId")
        except Exception:
            return []
        try:
            for target, row in self.tabs.items():
                if (target not in live or list(boards.job_identity(live[target].get("url")) or ()) != row.get("job_identity")):
                    continue
                evidence = self._empty_technical_evidence(row, target)
                if not evidence:
                    continue
                self.helpers["switch_tab"](target)
                if self.helpers["current_tab"]().get("targetId") != target:
                    continue
                observed = self.helpers["js"](EMPTY_GREENHOUSE_FORM)
                flags = ("has_values", "checked", "custom_values", "attachments", "unknown_controls", "unknown_frames",
                         "dialogs", "validation", "auth_challenge", "shadow_controls")
                if (not isinstance(observed, dict) or observed.get("schema_version") != 1
                        or observed.get("ready") is not True or observed.get("known_form") is not True
                        or type(observed.get("control_count")) is not int or observed["control_count"] < 3
                        or observed.get("url") != live[target]["url"]
                        or any(observed.get(key) is not False for key in flags)
                        or self._empty_technical_evidence(row, target) != evidence):
                    continue
                # Keep the last read next to the official close. Human edits or
                # site validation appearing during evidence checks cancel it.
                if (self.helpers["js"](EMPTY_GREENHOUSE_FORM) != observed
                        or self._empty_technical_evidence(row, target) != evidence):
                    continue
                path = self.root / "private" / "browser-empty-attempt-cleanup" / f"{time.time_ns()}-{evidence['job_hash']}.json"
                proof = {"schema_version": 1, "reason": "empty_captured_technical_attempt", "recorded_at": time.time(),
                         **evidence, "live_observation": {k: v for k, v in observed.items() if k != "url"},
                         "observed_url_sha256": hashlib.sha256(observed["url"].encode()).hexdigest(), "state": "close_requested"}
                write_private(path, proof)
                success = self.close(target, "empty_captured_technical_attempt", expected_url=observed["url"])
                write_private(path, {**proof, "state": "closed" if success else "close_unconfirmed"})
                if success:
                    closed.append(target)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            pass  # Unknown or changed evidence preserves the target.
        finally:
            if original and original not in closed:
                try:
                    if original in {t["targetId"] for t in self.helpers["list_tabs"]()}:
                        self.helpers["switch_tab"](original)
                except RuntimeError:
                    pass
        return closed

    def record_popup(self, request, result, before):
        if result.get("state") != "destination":
            return
        target = self.helpers["current_tab"]()
        identity = boards.job_identity(result.get("application_url"))
        target_id = target.get("targetId")
        if not identity or boards.job_identity(target.get("url")) != identity:
            return
        nav = result.get("tab_navigation", {})
        if (nav.get("native_apply_clicked") is False and nav.get("destination_target_id") == target_id
                and target_id in nav.get("before_target_ids", [])):
            return
        existing = self.tabs.get(target_id, {})
        if (existing.get("state") == "active" and existing.get("purpose") in {"source", "source_readonly"}
                and nav.get("native_apply_clicked") is True
                and nav.get("source_target_id") == target_id == nav.get("destination_target_id")
                and nav.get("expected_identity") == list(identity)
                and existing.get("job_identity") == list(boards.job_identity(request.get("approved_url")) or ())
                and boards.job_identity(nav.get("source_url")) == boards.job_identity(request.get("approved_url"))):
            existing.update(purpose="application", previous_source_identity=existing["job_identity"],
                            job_identity=list(identity), original_source_url=existing["requested_url"],
                            requested_url=result["application_url"])
            self.save()
            return
        if target_id in before or target_id in self.tabs:
            return
        proven = False
        try:
            info = self.helpers["cdp"]("Target.getTargetInfo", targetId=target_id)["targetInfo"]
            proven = (nav.get("native_apply_clicked") is True and nav.get("destination_target_id") == target_id
                      and isinstance(nav.get("before_target_ids"), list) and target_id not in nav["before_target_ids"]
                      and nav.get("expected_identity") == list(identity)
                      and boards.job_identity(nav.get("source_url")) == boards.job_identity(request.get("approved_url"))
                      and info.get("openerId") == nav.get("source_target_id") and bool(info.get("openerId"))
                      and info.get("targetId") == target_id and info.get("type") == "page"
                      and boards.job_identity(info.get("url")) == identity)
        except Exception:
            pass
        if proven:
            self.tabs[target_id] = {"state": "active", "created_at": time.time(), "purpose": "application",
                "job_identity": list(identity), "requested_url": result["application_url"],
                "creation_proof": "native_linkedin_apply_opener", "source_target_id": nav["source_target_id"]}
            self.unclaimed.pop(target_id, None)
        else:
            self.unclaimed.setdefault(target_id, {"state": "active", "first_seen": time.time(),
                "url": target.get("url"), "reason": "popup_ownership_not_proven"})
        self.save()

    def cleanup_source(self, request, result):
        if result.get("state") != "destination" or boards.job_identity(result.get("application_url")) is None:
            return []
        destination = self.helpers["current_tab"]()
        if boards.job_identity(destination.get("url")) != boards.job_identity(result["application_url"]):
            return []
        # The destination must be actually observed, not a guessed href.
        if not any(e.get("operation") in {"observed_apply_destination", "reused_observed_apply_destination"}
                   and boards.job_identity(e.get("url")) == boards.job_identity(result["application_url"])
                   for e in result.get("evidence", []) if isinstance(e, dict)):
            return []
        identity, live, closed = boards.job_identity(request.get("approved_url")), self.refresh(), []
        for target, row in self.tabs.items():
            if (row.get("state") == "active" and row.get("purpose") in {"source", "source_readonly"}
                    and row.get("job_identity") == list(identity or ()) and target != destination.get("targetId")
                    and target in live and boards.job_identity(live[target].get("url")) == identity):
                if self.close(target, "verified_external_apply_destination", expected_url=live[target]["url"]):
                    closed.append(target)
        return closed


def dispatch_owned(request, helpers, dispatcher, *, dispatcher_name, root=None):
    """Called only by the fixed CLI wrapper while its browser lane is held."""
    if dispatcher_name not in DISPATCHERS:
        raise ValueError("Unsupported browser dispatcher")
    owner = OwnedTabs(helpers, root or config.ROOT)
    before = set(owner.refresh())
    operation = request.get("operation")
    if operation == "discard_application_tab":
        from .application_discard import close_dispatch
        return close_dispatch(request, helpers, root=root or config.ROOT, owner=owner)
    if operation == "cleanup_source_terminal":
        if dispatcher_name != "jhb.applications.linkedin_runtime":
            raise ValueError("Source cleanup is restricted to LinkedIn classification")
        return {"closed_targets": owner.cleanup_source_terminal(request)}
    if operation == "cleanup_source_verified":
        if dispatcher_name != "jhb.applications.linkedin_runtime":
            raise ValueError("Verified source cleanup is restricted to LinkedIn classification")
        return {"closed_targets": owner.cleanup_verified_source(request)}
    if operation in {"open", "resolve", "resolve_link", "cleanup_tabs"}:
        closed = owner.cleanup()
        closed += owner.cleanup_empty_technical()
        if operation == "cleanup_tabs":
            return {"closed_targets": closed, "capacity": _cap(), "mutations": "owned_receipts_or_proven_empty_technical_attempts"}
    scoped = dict(helpers)
    purpose = ("source_readonly" if operation == "resolve_link" else "source") if dispatcher_name == "jhb.applications.linkedin_runtime" else "application"
    scoped["new_tab"] = lambda url="about:blank": owner.new_tab(url, purpose=purpose)
    scoped["jhb_before_apply_click"] = owner.before_apply_click
    scoped["jhb_after_apply_click"] = owner.after_apply_click
    try:
        result = dispatcher(request, scoped)
    except BaseException:
        # A navigation/read failure after a native source click must not lose
        # track of its new destinations. Observation never closes these tabs.
        try:
            if dispatcher_name == "jhb.applications.linkedin_runtime" and operation == "resolve":
                owner.observe_unclaimed_popups()
        except Exception:
            pass  # Preserve the original browser failure for its repair handoff.
        raise
    owner.refresh()
    if dispatcher_name == "jhb.applications.linkedin_runtime" and operation == "resolve_link" and isinstance(result, dict):
        result = owner.record_readonly_observation(request, result)
    if operation == "screenshot" and isinstance(result, dict) and not result.get("error"):
        owner.retain_review(request)
    if dispatcher_name == "jhb.applications.linkedin_runtime" and operation == "resolve" and isinstance(result, dict):
        owner.record_popup(request, result, before)
        owner.observe_unclaimed_popups()
        owner.cleanup_source(request, result)
    return result
