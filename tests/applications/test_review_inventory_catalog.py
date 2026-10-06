"""Catalog choices remain reviewable without changing the original education."""
from copy import deepcopy

import pytest

from jhb.applications import booklet
from jhb.applications.planner import key_for_field
from jhb.applications.review_inventory import build


def catalog_case(column="school", index=1):
    ref = f"{'discipline' if column == 'major' else 'school'}--{index}"
    label = "Discipline" if column == "major" else "School"
    original = "Applied Mathematics and Computing" if column == "major" else "Example Institute"
    choice = "Mathematics" if column == "major" else "Other"
    source = {"document": "Synthetic resume", "education_record": index}
    key, fallback_key = f"education.{index}.{column}", f"standing.catalog.{index}.{column}"
    answers = {
        key: booklet.answer(original, source),
        fallback_key: booklet.answer(choice, {"policy": "Explicit synthetic catalog preference",
            "actual_value": original, "original_source": deepcopy(source)}),
    }
    fields = [{"ref": ref, "label": label, "type": "combobox", "required": True}]
    filled = [{"ref": ref, "question": label, "key": fallback_key,
               "value": choice, "source": deepcopy(answers[fallback_key]["source"])}]
    return fields, filled, answers, key, fallback_key


@pytest.mark.parametrize("column", ["school", "major"])
@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("kind", ["combobox", "select"])
def test_exact_retained_catalog_fallback_preserves_original_fact(column, index, kind):
    fields, filled, answers, key, fallback_key = catalog_case(column, index)
    fields[0]["type"] = kind
    before = deepcopy((fields, filled, answers))
    result = build(fields, filled, answers, key_for_field, complete=True)
    row = result["review_inventory"]["fields"][0]
    assert result["review_inventory"]["complete"] is True
    assert result["review_completeness"]["answered_count"] == 1
    assert result["review_completeness"]["blank_count"] == 0
    assert row["status"] == "answered" and row["category"] == "profile_fact"
    assert row["answer_key"] == fallback_key and row["source"] == answers[fallback_key]["source"]
    assert answers[key]["value"] != answers[fallback_key]["value"]
    assert (fields, filled, answers) == before


@pytest.mark.parametrize("fault", [
    "canonical_value_changed", "canonical_source_changed", "canonical_unverified", "canonical_missing",
    "fallback_unverified", "fallback_value_changed", "fallback_source_changed", "policy_missing",
    "original_value_missing", "original_source_missing", "legacy_unbound_source", "wrong_fallback_index",
    "wrong_fallback_column", "retained_value_changed", "retained_source_changed", "retained_label_changed",
    "duplicate_retained", "conflicting_retained", "duplicate_observed", "non_catalog_control",
])
def test_catalog_fallback_rejects_stale_ambiguous_or_unbound_evidence(fault):
    fields, filled, answers, key, fallback_key = catalog_case()
    if fault == "canonical_value_changed":
        answers[key]["value"] = "Updated Institute"
    elif fault == "canonical_source_changed":
        answers[key]["source"] = "Updated resume"
    elif fault == "canonical_unverified":
        answers[key]["status"] = "needs_input"
    elif fault == "canonical_missing":
        del answers[key]
    elif fault == "fallback_unverified":
        answers[fallback_key]["status"] = "needs_input"
    elif fault == "fallback_value_changed":
        answers[fallback_key]["value"] = "Other institution"
    elif fault == "fallback_source_changed":
        answers[fallback_key]["source"]["policy"] = "Changed preference"
    elif fault in {"policy_missing", "original_value_missing", "original_source_missing"}:
        part = {"policy_missing": "policy", "original_value_missing": "actual_value",
                "original_source_missing": "original_source"}[fault]
        del answers[fallback_key]["source"][part]
        filled[0]["source"] = deepcopy(answers[fallback_key]["source"])
    elif fault == "legacy_unbound_source":
        answers[fallback_key]["source"] = filled[0]["source"] = "Unbound policy"
    elif fault in {"wrong_fallback_index", "wrong_fallback_column"}:
        wrong = "standing.catalog.0.school" if fault == "wrong_fallback_index" else "standing.catalog.1.major"
        answers[wrong] = answers.pop(fallback_key)
        filled[0]["key"] = wrong
    elif fault == "retained_value_changed":
        filled[0]["value"] = "Unapproved choice"
    elif fault == "retained_source_changed":
        filled[0]["source"]["policy"] = "Unapproved policy"
    elif fault == "retained_label_changed":
        filled[0]["question"] = "Another question"
    elif fault in {"duplicate_retained", "conflicting_retained"}:
        filled.append(deepcopy(filled[0]))
        if fault == "conflicting_retained":
            filled[-1]["key"] = key
            filled[-1]["source"] = "Stale canonical source"
    elif fault == "duplicate_observed":
        fields.append(deepcopy(fields[0]))
    elif fault == "non_catalog_control":
        fields[0]["type"] = "text"
    result = build(fields, filled, answers, key_for_field, complete=True)
    assert result["review_inventory"]["complete"] is False
    assert all(row["status"] == "blank" for row in result["review_inventory"]["fields"])


@pytest.mark.parametrize("ref", ["school--0", "discipline--1", "unrelated"])
def test_canonical_key_cannot_rebind_fallback_to_a_different_native_field(ref):
    fields, filled, answers, key, _ = catalog_case()
    fields[0]["ref"] = filled[0]["ref"] = ref
    result = build(fields, filled, answers, lambda *_: key, complete=True)
    assert result["review_inventory"]["complete"] is False
    assert result["review_inventory"]["fields"][0]["status"] == "blank"


def test_unfilled_fallback_policy_does_not_invent_a_retained_answer():
    fields, _, answers, _, _ = catalog_case()
    result = build(fields, [], answers, key_for_field, complete=True)
    assert result["review_inventory"]["complete"] is False
    assert result["review_inventory"]["fields"][0]["status"] == "blank"
