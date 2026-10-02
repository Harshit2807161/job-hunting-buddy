import asyncio

from jhb.applications import booklet
from jhb.applications.planner import deterministic_plan
from jhb.applications.worker import prepare


def test_required_question_revealed_after_filling_blocks_final_review():
    class ConditionalForm:
        blocked_requests = 0
        revealed = False
        def allowed_url(self, url): return True
        async def open(self, url): pass
        async def fill(self, field, value): self.revealed = True
        async def observe(self):
            fields = [{"ref": "first", "label": "First Name", "type": "text", "required": True}]
            if self.revealed:
                fields.append({"ref": "new", "label": "Explain this employer-specific certification", "type": "text", "required": True})
            return {"fields": fields, "buttons": [{"ref": "submit", "label": "Submit application"}]}
    result, _ = asyncio.run(prepare(None, {"url": "synthetic"},
                         {"identity.first_name": booklet.answer("Sam", "synthetic user")},
                         deterministic_plan, None, cli_actions=ConditionalForm()))
    assert result["state"] == "waiting_input"
    assert result["missing"][0]["question"] == "Explain this employer-specific certification"
    assert any(e["event"] == "fields_revealed" for e in result["events"])


def test_review_packet_preserves_both_education_rows():
    class EducationForm:
        blocked_requests = 0
        def allowed_url(self, url): return True
        async def open(self, url): pass
        async def ensure_education(self, count): assert count == 2
        async def fill(self, field, value): pass
        async def observe(self):
            return {"fields": [{"ref": f"school--{i}", "label": "School", "type": "combobox", "required": False} for i in [0, 1]],
                    "buttons": [{"ref": "submit", "label": "Submit application"}]}
    answers = {"education.0.school": booklet.answer("Example Graduate School", "synthetic resume"),
               "education.1.school": booklet.answer("Example College", "synthetic resume")}
    result, _ = asyncio.run(prepare(None, {"url": "synthetic"}, answers,
                                  deterministic_plan, None, cli_actions=EducationForm()))
    assert result["state"] == "waiting_review"
    assert {r["value"] for r in result["filled"]} == {"Example Graduate School", "Example College"}
