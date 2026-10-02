"""Private answers with explicit provenance and role-specific documents."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from ..config import ROOT

DEFAULT_PATH = ROOT / "private" / "answer-booklet.json"

# Exact labels only. An unfamiliar question is a handoff, never a guessed answer.
ALIASES = {
    "identity.first_name": ["first name", "given name"],
    "identity.last_name": ["last name", "family name", "surname"],
    "identity.full_name": ["full name", "name"],
    "identity.email": ["email", "email address"],
    "identity.phone": ["phone", "phone number", "mobile phone"],
    "identity.city": ["city"], "identity.state": ["state"],
    "identity.location": ["location", "current location"],
    "identity.address": ["street address", "address", "address line 1"],
    "identity.address_line2": ["address line 2", "address 2"],
    "identity.address_line3": ["address line 3", "address 3"],
    "identity.birthday": ["birthday", "date of birth"],
    "identity.preferred_name": ["preferred name", "preferred first name"],
    "identity.suffix": ["suffix name", "suffix"],
    "identity.postal_code": ["zip code", "postal code"],
    "identity.country": ["country"],
    "links.linkedin": ["linkedin", "linkedin profile", "linkedin url"],
    "links.github": ["github", "github url"],
    "links.portfolio": ["website", "portfolio", "personal website"],
    "links.scholar": ["google scholar"],
    "documents.resume": ["resume", "resume/cv", "upload resume", "cv"],
    "documents.cover_letter": ["cover letter", "upload cover letter"],
    "education.school": ["school"],
    "education.degree": ["degree"],
    "role.skills": ["skills", "technical skills"],
    "role.languages": ["programming languages"],
    "role.experience": ["experience", "work experience"],
    "role.education": ["education", "education history"],
    "role.projects": ["projects"],
    "eligibility.authorized_us": ["are you legally authorized to work in the united states?", "work authorization"],
    "eligibility.authorized_canada": ["are you authorized to work in canada?"],
    "eligibility.authorized_uk": ["are you authorized to work in the united kingdom?"],
    "eligibility.over_18": ["are you over 18 years of age?", "are you over the age of 18 years old?"],
    "eligibility.sponsorship_now": ["do you currently require visa sponsorship?"],
    "eligibility.sponsorship_future": ["will you require visa sponsorship in the future?"],
    "eligibility.sponsorship": ["will you now or in the future require sponsorship?", "will you now or in the future require visa sponsorship?"],
    "preferences.relocation": ["are you willing to relocate?", "relocation"],
    "preferences.remote": ["remote preference"],
    "preferences.application_city": ["location (city)"],
    "preferences.salary": ["salary expectations", "desired salary"],
    "preferences.start_date": ["earliest start date", "availability", "start date"],
    "screening.previous_employee": ["have you worked here before?"],
    "screening.referral": ["how did you hear about us?"],
    "screening.non_compete": ["are you subject to a non-compete agreement?"],
    "screening.employee_relative": ["are you related to a current employee?"],
    "screening.us_government_or_military_5y": ["have you served in the u.s. armed forces or worked for u.s. government in the last five years?"],
    "disclosure.gender": ["gender"],
    "disclosure.ethnicity": ["race/ethnicity", "race and ethnicity", "ethnicity"],
    "disclosure.hispanic": ["are you hispanic/latino?"],
    "disclosure.veteran": ["veteran status", "protected veteran status"],
    "disclosure.disability": ["disability status"],
    "disclosure.pronouns": ["pronouns"],
    "disclosure.lgbtq": ["do you identify as lgbtq+?"],
    "consent.privacy": ["i agree to the privacy policy"],
    "consent.truthfulness": ["i certify that the information provided is accurate"],
}


def normalize(label: str) -> str:
    return re.sub(r"\s+", " ", label.strip().rstrip(" *")).casefold()


def write_private(path: Path, value) -> None:
    """Atomic replacement, private permissions, and no symlink destinations."""
    path = Path(path)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("Private destination must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(temp, path)
        path.chmod(0o600)
    finally:
        temp.unlink(missing_ok=True)


def answer(value=None, source="user input required", status=None):
    return {"value": value, "source": source,
            "status": status or ("verified" if value is not None else "needs_input")}


def load(path=DEFAULT_PATH) -> dict:
    book = json.loads(Path(path).read_text())
    if book.get("schema_version") != 1 or set(book.get("roles", {})) != {"sde", "ml"}:
        raise ValueError("Unsupported answer booklet")
    for collection in [book["answers"], *book["roles"].values()]:
        for key, item in collection.items():
            if not isinstance(item, dict) or item.get("status") not in {"verified", "needs_input", "declined"}:
                raise ValueError(f"Invalid answer metadata: {key}")
            if item["status"] == "verified" and (item.get("value") is None or not item.get("source")):
                raise ValueError(f"Verified answer has no value or source: {key}")
    return book


def for_role(book: dict, role: str) -> dict:
    if role not in {"sde", "ml"}:
        raise ValueError("Choose sde or ml explicitly for ambiguous jobs")
    values = {**book["answers"], **book["roles"][role]}
    records = book.get("education_records", [])
    if records and records[0].get("status") == "verified":
        for field in ("school", "degree"):
            values["education."+field] = answer(records[0][field], records[0]["source"])
    return values


def missing(book: dict) -> list[str]:
    return [key for key, item in book["answers"].items() if item["status"] == "needs_input"]


def set_answer(path: Path, key: str, value, *, role=None, decline=False):
    book = load(path)
    collection = book["roles"][role] if role else book["answers"]
    if key not in collection:
        raise ValueError("Unknown booklet key; add a question-specific answer deliberately")
    collection[key] = {**answer(None if decline else value, "explicit user input", "declined" if decline else "verified"),
                       "user_override": True}
    write_private(path, book)


def import_observations(path: Path, observations: list[dict], *, captured_at: str) -> dict:
    """Import explicitly observed answers, preserving conflicting source values.

    A caller supplies reviewed mappings from a rendered, authenticated page.
    Empty/unset display values are not answers. Role-specific resumes and skills
    remain separate from the general profile's records.
    """
    from urllib.parse import urlparse

    book = load(path)
    for observation in observations:
        key, value = observation["key"], observation["value"]
        url, label = observation["url"], observation["label"]
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "simplify.jobs" or not parsed.path.startswith("/profile/"):
            raise ValueError("Profile observation must come from a Simplify profile")
        if key not in ALIASES or key.startswith(("role.", "documents.")) or not label or not captured_at:
            raise ValueError("Unknown profile answer or missing provenance")
        if value is None or (isinstance(value, str) and value.strip() in {"", "-", "Not specified"}):
            continue
        current = book["answers"].get(key)
        item = answer(value, {"provider": "Simplify", "url": url,
                              "section": observation["section"], "label": label,
                              "captured_at": captured_at})
        if current and (current.get("user_override") or
                        isinstance(current.get("source"), str) and current["source"].startswith("explicit user")):
            book.setdefault("source_observations", {}).setdefault(key, []).append(item)
            continue
        if current and current["status"] == "verified" and current["value"] != value:
            book.setdefault("answer_history", {}).setdefault(key, []).append(current)
        book["answers"][key] = item
    write_private(path, book)
    return book


def _section(text: str, start: str, ends: list[str]) -> str:
    lines = text.splitlines()
    begin = next((i + 1 for i, l in enumerate(lines) if l.strip() == start), None)
    if begin is None:
        return ""
    end = next((i for i in range(begin, len(lines)) if lines[i].strip() in ends), len(lines))
    return "\n".join(l.strip() for l in lines[begin:end] if l.strip())


def bootstrap(source: Path, destination=DEFAULT_PATH):
    """Extract explicit resume facts only. Originals stay untouched."""
    from pypdf import PdfReader

    if Path(destination).exists():
        raise FileExistsError("Booklet already exists; refusing to replace approved answers")
    roles, sources = {}, []
    shared = {key: answer() for key in ALIASES if not key.startswith(("role.", "documents."))}
    for role, folder in [("sde", "sde-roles"), ("ml", "ml-roles")]:
        candidates = sorted((source / folder).glob("*Resume.pdf"))
        if len(candidates) != 1:
            raise ValueError(f"Expected one role resume ending in Resume.pdf under {folder}")
        path = candidates[0]
        reader = PdfReader(path)
        text = "\n".join(page.extract_text(extraction_mode="layout") for page in reader.pages)
        provenance = f"{path.resolve()} sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        sources.append({"role": role, "path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        name = next(l.strip() for l in text.splitlines() if l.strip())
        email = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
        phone = re.search(r"\+\d\s*\(\d{3}\)\s*\d{3}-\d{4}", text)
        location = re.search(r"^\s*([A-Za-z ]+),\s*([A-Z]{2})\s*$", text, re.M)
        facts = {"identity.full_name": name, "identity.first_name": name.split()[0],
                 "identity.last_name": " ".join(name.split()[1:])}
        if email: facts["identity.email"] = email.group()
        if phone: facts["identity.phone"] = phone.group()
        if location:
            facts.update({"identity.location": f"{location[1].strip()}, {location[2]}",
                          "identity.city": location[1].strip(), "identity.state": location[2]})
        for page in reader.pages:
            for ann in page.get("/Annots", []):
                uri = str(ann.get_object().get("/A", {}).get("/URI", ""))
                if "linkedin.com/in/" in uri: facts["links.linkedin"] = uri
                elif "scholar.google.com/citations" in uri: facts["links.scholar"] = uri
                elif uri.startswith("https://github.com/") and len(uri.rstrip("/").split("/")) == 4: facts["links.github"] = uri
                elif ".github.io" in uri: facts["links.portfolio"] = uri
        for key, value in facts.items():
            if shared[key]["status"] == "verified" and shared[key]["value"] != value:
                shared[key] = answer(source="conflicting resume variants; confirm with user")
            else:
                shared[key] = answer(value, provenance)
        skills = _section(text, "Technical Skills", ["Experience", "Projects"])
        langs = re.search(r"Languages:\s*(.+)", skills)
        roles[role] = {
            "documents.resume": answer(str(path.resolve()), provenance),
            "documents.cover_letter": answer(),
            "documents.cover_template": answer(str((source / folder / "cover-letter-ref.tex").resolve()), "user cover-letter reference"),
            "role.skills": answer(skills, provenance),
            "role.languages": answer(langs[1] if langs else "", provenance),
            "role.education": answer(_section(text, "Education", ["Technical Skills", "Experience"]), provenance),
            "role.experience": answer(_section(text, "Experience", ["Projects", "Selected Publications"]), provenance),
            "role.projects": answer(_section(text, "Projects", ["Selected Publications"]), provenance),
            "role.publications": answer(_section(text, "Selected Publications", []), provenance),
        }
    skill = source / "SKILL.md"
    if skill.exists() and "January 2027" in skill.read_text():
        shared["preferences.start_date"] = answer("January 2027", str(skill.resolve()))
    book = {"schema_version": 1, "answers": shared, "roles": roles, "sources": sources,
            "cover_letter_skill": str(skill.resolve()), "custom_answers": {}}
    write_private(Path(destination), book)
    return book
