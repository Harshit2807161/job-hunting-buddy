import json

import pytest

from jhb.applications import booklet


def make_book(tmp_path):
    path = tmp_path / "private" / "answers.json"
    booklet.write_private(path, {
        "schema_version": 1,
        "answers": {"identity.email": booklet.answer("sam@example.org", "synthetic resume")},
        "roles": {"sde": {}, "ml": {}}, "custom_answers": {},
    })
    return path


def observation(key, value):
    return {"key": key, "value": value, "label": key,
            "url": "https://simplify.jobs/profile/synthetic-candidate",
            "section": "Personal Info"}


def test_import_preserves_conflicts_false_and_unset_answers(tmp_path):
    path = make_book(tmp_path)
    result = booklet.import_observations(path, [
        observation("identity.email", "sam.new@example.org"),
        observation("eligibility.authorized_us", False),
        observation("identity.address_line2", "-"),
    ], captured_at="2026-10-01T00:00:00Z")
    assert result["answers"]["identity.email"]["value"] == "sam.new@example.org"
    assert result["answer_history"]["identity.email"][0]["value"] == "sam@example.org"
    assert result["answers"]["eligibility.authorized_us"]["value"] is False
    assert "identity.address_line2" not in result["answers"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert result["answers"]["identity.email"]["source"]["section"] == "Personal Info"
    assert booklet.load(path) == result


def test_invalid_import_does_not_partially_replace_booklet(tmp_path):
    path = make_book(tmp_path)
    before = path.read_bytes()
    invalid = observation("identity.email", "sam.other@example.org")
    invalid["url"] = "https://untrusted.example/profile/fake"
    with pytest.raises(ValueError, match="Simplify"):
        booklet.import_observations(path, [observation("identity.phone", "5550100"), invalid],
                                    captured_at="2026-10-01T00:00:00Z")
    assert path.read_bytes() == before


def test_import_does_not_mix_role_specific_documents(tmp_path):
    path = make_book(tmp_path)
    with pytest.raises(ValueError, match="Unknown"):
        booklet.import_observations(path, [observation("arbitrary.key", "value")], captured_at="now")
    assert json.loads(path.read_text())["roles"] == {"sde": {}, "ml": {}}


def test_profile_refresh_preserves_explicit_user_override(tmp_path):
    path = make_book(tmp_path)
    booklet.set_answer(path, "identity.email", "sam.approved@example.org")
    result = booklet.import_observations(path, [observation("identity.email", "sam.profile@example.org")], captured_at="now")
    assert result["answers"]["identity.email"]["value"] == "sam.approved@example.org"
    assert result["source_observations"]["identity.email"][0]["value"] == "sam.profile@example.org"
