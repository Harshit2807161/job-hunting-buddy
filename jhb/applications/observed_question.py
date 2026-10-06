"""Keep derived answers bound to the complete question that was reviewed.

This validates context, never the truth or authority of an answer. Records
without an observed-question proof retain the existing legacy policies.
"""
from copy import deepcopy

FILL_PROOF = "_observed_question_proof"
DESCRIPTOR_KEYS = ("ref", "label", "type", "required", "description", "description_truncated", "options")


def guarded(key, record):
    source = record.get("source")
    if not key.startswith("custom.") or not isinstance(source, dict) or "observed_question" not in source:
        return False
    observed = source["observed_question"]
    # Existing curated narratives use a separate, partial writing descriptor.
    # Full descriptors and every profile projection use the stronger binding.
    return key.startswith("custom.profile.") or isinstance(observed, dict) and "ref" in observed


def _choices(options):
    if not isinstance(options, list) or len(options) > 100:
        return None
    if any(not isinstance(option, dict) or not isinstance(option.get("label"), str)
           or not option["label"].strip() for option in options):
        return None
    labels = [option["label"] for option in options if not option.get("disabled") and option.get("value") != ""]
    return labels if len(labels) == len(set(labels)) else None


def matches(field, observed, *, require_catalog=True):
    """Compare owned meaning exactly; ignore only incidental native option IDs."""
    if (not isinstance(observed, dict) or any(key not in observed for key in DESCRIPTOR_KEYS)
            or not isinstance(observed["description"], str)
            or type(observed["required"]) is not bool or observed["description_truncated"] is not False
            or field.get("description_truncated", False) is not False
            or type(field.get("required")) is not bool
            or any(observed[key] != field.get(key) for key in ("ref", "label", "type", "required"))
            or observed["description"] != field.get("description", "")
            or observed.get("country_context") != field.get("country_context")
            or observed.get("max_length", observed.get("maxlength")) != field.get("max_length", field.get("maxlength"))):
        return False
    expected = _choices(observed["options"])
    actual = _choices(field.get("options", []))
    return expected is not None and (not require_catalog or actual is not None and expected == actual)


def fill_field(field, key, record):
    """Pass the reviewed descriptor to the native fill without changing facts."""
    if not guarded(key, record):
        return field
    observed = record["source"]["observed_question"]
    if not matches(field, observed):
        raise ValueError("Derived answer question context changed")
    return {**field, FILL_PROOF: deepcopy(observed)}
