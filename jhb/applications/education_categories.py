"""Exact-job employer categories preserve the original verified degree.

This is a scoped proposal binding, not a degree-equivalence or CIP mapping.
Native selection/retention and independent review remain separate checks.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from . import boards

BASIS = "_current_education_category_basis"


def projection(record):
    source = record.get("source", {})
    return isinstance(source, dict) and source.get("kind") == "employer_category_projection"


def scoped(record, book, job):
    """Rebuild prerequisites from current data; never trust a saved basis."""
    result = deepcopy(record)
    result.pop(BASIS, None)
    if not projection(record):
        return result
    from .questions import _scope
    match = re.fullmatch(r"discipline--(\d+)", str(record.get("field_ref", "")))
    records = book.get("education_records", [])
    identity = boards.job_identity(job.get("application_url") or job.get("url"))
    if (not match or not isinstance(records, list) or int(match[1]) >= len(records)
            or not identity or identity[0] != "greenhouse"
            or boards.application_hash(job.get("application_url") or job.get("url")) != job.get("dedupe_hash")
            or record.get("job_hash") != job.get("dedupe_hash") or record.get("scope") != _scope(job)):
        return result
    result[BASIS] = {"record": deepcopy(records[int(match[1])]), "job_hash": job["dedupe_hash"],
                     "application_identity": list(identity), "scope": _scope(job), "field_ref": record["field_ref"]}
    return result


def valid(field, record):
    """Require the exact source record and complete unchanged native context."""
    if not projection(record):
        return False
    source, basis = record["source"], record.get(BASIS, {})
    if not isinstance(basis, dict):
        return False
    original = basis.get("record")
    descriptor = source.get("field_descriptor")
    if (not isinstance(original, dict) or original.get("status") != "verified" or not original.get("source")
            or not isinstance(original.get("major"), str) or not original["major"].strip()
            or not isinstance(descriptor, dict) or field.get("type") not in {"combobox", "select"}
            or not re.fullmatch(r"discipline--\d+", str(field.get("ref", "")))
            or record.get("status") != "verified" or record.get("proposed") is not True
            or record.get("user_override") or source.get("candidate_confirmation") is not False
            or source.get("category_only") is not True or source.get("credential_rewritten") is not False
            or source.get("method") != "exact_job_observed_education_category"
            or source.get("original_education_record") != original
            or source.get("original_source") != original["source"]
            or source.get("actual_value") != original["major"]
            or source.get("application_identity") != basis.get("application_identity")
            or record.get("job_hash") != basis.get("job_hash") or source.get("job_hash") != basis.get("job_hash")
            or record.get("scope") != basis.get("scope")
            or not isinstance(record.get("value"), str) or not record["value"].strip()
            or record["value"] != source.get("observed_choice") or record["value"] == original["major"]
            or record.get("question") != field.get("label")
            or any(item != field.get("ref") for item in (record.get("field_ref"), source.get("field_ref"), basis.get("field_ref")))):
        return False
    digest = hashlib.sha256(json.dumps(original, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if (source.get("original_record_sha256") != digest
            or any(not isinstance(source.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", source[key])
                   for key in ("assessment_sha256", "native_evidence_sha256"))):
        return False
    if (any(key not in descriptor for key in ("ref", "label", "type", "required", "options", "description", "description_truncated"))
            or any(descriptor[key] != field.get(key) for key in ("ref", "label", "type", "required"))
            or descriptor["description_truncated"] is not False or field.get("description_truncated", False) is not False
            or descriptor["description"] != (field.get("description") or "")
            or descriptor.get("calendar_format") != field.get("calendar_format")):
        return False
    # A closed combobox exposes no options. Do not mistake that for proof that
    # the original major is absent; the native exact-major probe is required
    # before creating/using this proposal. A populated catalog must agree.
    options, observed = field.get("options", []), descriptor["options"]
    if not isinstance(options, list) or not isinstance(observed, list) or (observed and observed != options):
        return False
    if options:
        if any(not isinstance(o, dict) or not isinstance(o.get("label"), str) for o in options):
            return False
        choices = [o["label"] for o in options if not o.get("disabled")]
        if choices.count(record["value"]) != 1 or any(label.casefold() == original["major"].casefold() for label in choices):
            return False
    elif field["type"] != "combobox":
        return False
    return True
