"""Opt-in public Greenhouse descriptors for existing pending question contexts.

Informational metadata is not a native form observation or an approved answer.
No dashboard GET, queue transition, browser action or submission uses this helper.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from html.parser import HTMLParser
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .. import config
from . import boards, booklet, questions

MAX_BYTES = 1024 * 1024
ACTIVE = {"waiting_input", "waiting_review"}
TYPES = {"multi_value_single_select": {"combobox", "select"},
         "multi_value_multi_select": {"multiselect", "checkboxes"},
         "input_text": {"text"}, "input_textarea": {"textarea"}, "input_file": {"file"}}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Public question metadata must not redirect")


def fetch(url, *, opener=None):
    identity = boards.job_identity(url)
    if not identity or identity[0] != "greenhouse":
        return {}
    _, region, board, job_id = identity
    host = "boards-api.eu.greenhouse.io" if region == "eu" else "boards-api.greenhouse.io"
    api = f"https://{host}/v1/boards/{board}/jobs/{job_id}?questions=true"
    try:
        request = Request(api, headers={"Accept": "application/json"}, method="GET")
        with (opener or build_opener(ProxyHandler({}), NoRedirect()).open)(request, timeout=10) as response:
            if getattr(response, "status", 200) != 200 or getattr(response, "geturl", lambda: api)() != api:
                return {}
            body = response.read(MAX_BYTES + 1)
        if not isinstance(body, bytes) or len(body) > MAX_BYTES:
            return {}
        data = json.loads(body)
        if (not isinstance(data, dict) or str(data.get("id")) != job_id
                or not isinstance(data.get("questions"), list) or len(data["questions"]) > 300):
            return {}
        return {"source_url": api, "questions": data["questions"]}
    except (OSError, ValueError, TypeError):
        return {}


def _heading(value):
    # Visible required markers are presentation, not a different question.
    return " ".join(value.split()).rstrip("*").strip() if isinstance(value, str) else None


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden and tag in {"br", "p", "div", "li", "ul", "ol"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in {"p", "div", "li", "ul", "ol"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _description(value):
    parser = _Text()
    parser.feed(value)
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def descriptor(record, context, data):
    ref = context.get("ref")
    if not isinstance(ref, str) or not re.fullmatch(r"question_\d+", ref):
        return None
    matches = [(question, field) for question in data.get("questions", []) if isinstance(question, dict)
               for field in (question.get("fields") if isinstance(question.get("fields"), list) else []) if isinstance(field, dict) and field.get("name") == ref]
    if len(matches) != 1:
        return None
    question, field = matches[0]
    if (_heading(question.get("label")) != _heading(record.get("question"))
            or not isinstance(question.get("required"), bool)
            or question["required"] is not context.get("required")
            or not isinstance(field.get("type"), str)
            or context.get("type") not in TYPES.get(field["type"], set())):
        return None
    raw = question.get("description")
    if raw is not None and not isinstance(raw, str):
        return None
    text = _description(raw or "")
    if len(text) > 4096:  # Incomplete instructions must not become a complete choice view.
        return None
    values = field.get("values", [])
    if not isinstance(values, list) or len(values) > 100:
        return None
    labels = []
    for value in values:
        label = value.get("label") if isinstance(value, dict) else None
        if not isinstance(label, str) or not label.strip() or len(label) > 300:
            return None
        labels.append(label.strip())
    if len(labels) != len(set(labels)):
        return None
    if "select" in field["type"] and not labels:
        return None
    item = {"source": "official_public_question_metadata", "source_url": data["source_url"],
            "field_ref": ref, "label": question["label"], "type": field["type"],
            "required": question["required"], "description": text, "choices": labels}
    item["sha256"] = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
    return item


def enrich(connection, bookpath=booklet.DEFAULT_PATH, *, opener=None, limit=10):
    """Only enrich unchanged, unresolved pending contexts in active queue rows."""
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("Metadata maintenance limit must be between one and ten jobs")
    book = booklet.load(bookpath)
    selected = []
    urls = {}
    for qid, record in book.get("question_handoffs", {}).items():
        if record.get("status") != "pending" or questions._SECRET.search(record.get("question", "")):
            continue
        for job_hash, context in record.get("contexts", {}).items():
            if not isinstance(context, dict) or context.get("resolved"):
                continue
            row = connection.execute("SELECT state,job_json FROM applications WHERE job_hash=?", (job_hash,)).fetchone()
            if not row or row[0] not in ACTIVE:
                continue
            job = json.loads(row[1])
            url = context.get("url")
            identity = boards.job_identity(url)
            if (not identity or identity[0] != "greenhouse" or boards.application_hash(url) != job_hash
                    or boards.job_identity(job.get("url")) != identity or questions._scope(job) != record.get("scope")):
                continue
            if job_hash not in urls and len(urls) >= limit:
                continue
            urls[job_hash] = url
            selected.append((qid, job_hash, record, context))
    data = {job_hash: fetch(url, opener=opener) for job_hash, url in urls.items()}
    changed = 0
    with questions._locked(bookpath):
        current = booklet.load(bookpath)
        revisions = {qid: record.get("updated_at") for qid, record in current.get("question_handoffs", {}).items()}
        for qid, job_hash, original, context in selected:
            record = current.get("question_handoffs", {}).get(qid, {})
            row = connection.execute("SELECT state,job_json FROM applications WHERE job_hash=?", (job_hash,)).fetchone()
            if (record.get("status") != "pending" or revisions.get(qid) != original.get("updated_at")
                    or record.get("contexts", {}).get(job_hash) != context or not row or row[0] not in ACTIVE
                    or boards.job_identity(json.loads(row[1]).get("url")) != boards.job_identity(context.get("url"))):
                continue
            item = descriptor(original, context, data[job_hash]) if data[job_hash] else None
            previous = context.get("public_question_metadata")
            if item is None or isinstance(previous, dict) and previous.get("sha256") == item["sha256"]:
                continue
            target = record["contexts"][job_hash]
            target["public_question_metadata"] = {**item, "retrieved_at": questions._now()}
            # Never replace a native observed catalog with public metadata.
            if not target.get("choices") or target.get("choices_source") == "official_public_question_metadata":
                target["choices"] = item["choices"]
                target["choices_source"] = "official_public_question_metadata"
            record["updated_at"] = questions._now()
            changed += 1
        if changed:
            booklet.write_private(Path(bookpath), current)
    return {"jobs_checked": len(urls), "contexts_enriched": changed}


def description_digest(text):
    # Rendering whitespace (paragraphs/NBSP) may vary. All words, case and
    # punctuation remain intact; no substring, case or semantic normalization.
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def public_response_allowed(field, record, *, require_catalog=True):
    """A public-guided response needs equivalent fresh owned form evidence.

    No public catalog is injected into native observations. The worker must
    actually inspect a closed combobox before a nonempty catalog can match.
    """
    source = record.get("source")
    if not isinstance(source, dict) or "public_question_metadata_proofs" not in source:
        return True  # Existing native/context policies remain responsible.
    proofs = source["public_question_metadata_proofs"]
    if (source.get("provider") != "explicit user question response"
            or not re.fullmatch(r"q_[a-f0-9]{24}", str(source.get("question_id", "")))
            or record.get("user_override") is not True or record.get("status") not in {"verified", "declined"}
            or not isinstance(proofs, list) or field.get("description_truncated")
            or not isinstance(field.get("description", ""), str) or not isinstance(field.get("required"), bool)):
        return False
    options = field.get("options")
    offered = ([item["label"] for item in options if isinstance(item, dict) and not item.get("disabled")
                and isinstance(item.get("label"), str)] if isinstance(options, list) and options else field.get("choices", []))
    if (not isinstance(offered, list) or len(offered) > 100 or any(not isinstance(x, str) for x in offered)
            or len(offered) != len(set(offered))):
        return False
    expected = {"source": "official_public_question_metadata", "field_ref": field.get("ref"),
                "country_context": booklet.normalize(str(field.get("country_context") or "")),
                "description_sha256": description_digest(field.get("description", "")),
                "required": field["required"], "observed_type": field.get("type")}
    if require_catalog:
        expected["choices_sha256"] = hashlib.sha256(json.dumps(offered, ensure_ascii=False).encode()).hexdigest()
    return any(isinstance(proof, dict) and re.fullmatch(r"[a-f0-9]{64}", str(proof.get("metadata_sha256", "")))
               and all(proof.get(key) == value for key, value in expected.items()) for proof in proofs)


def public_response_context_matches(field, record):
    """Select explicit public-guided responses for real owned catalog inspection."""
    source = record.get("source")
    return (isinstance(source, dict) and "public_question_metadata_proofs" in source
            and public_response_allowed(field, record, require_catalog=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Enrich pending Greenhouse questions from public metadata only")
    parser.add_argument("--booklet", type=Path, default=booklet.DEFAULT_PATH)
    parser.add_argument("--database", type=Path, default=config.DB_PATH)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)
    if os.environ.get("CI", "").lower() in {"1", "true", "yes"}:
        print(json.dumps({"state": "disabled", "reason_code": "ci_disabled"}))
        return
    path = args.database.absolute()
    if path.is_symlink() or not path.is_file():
        raise SystemExit("Existing local ledger required")
    with sqlite3.connect(path.as_uri()+"?mode=ro", uri=True) as conn:
        print(json.dumps(enrich(conn, args.booklet, limit=args.limit)))


if __name__ == "__main__":
    main()
