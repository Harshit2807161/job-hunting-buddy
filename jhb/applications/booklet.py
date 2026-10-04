"""Private answers with explicit provenance and role-specific documents."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path

from ..config import ROOT

DEFAULT_PATH = ROOT / "private" / "answer-booklet.json"

# Exact labels only. An unfamiliar question is a handoff, never a guessed answer.
ALIASES = {
    "identity.first_name": ["first name", "given name", "what is your legal first name?"],
    "identity.last_name": ["last name", "family name", "surname", "what is your legal last name?"],
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
    "links.github": ["github", "github url", "additional profiles (e.g. github, stack overflow)"],
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
    "eligibility.authorized_us": ["are you legally authorized to work in the united states?"],
    "eligibility.authorized_canada": ["are you authorized to work in canada?"],
    "eligibility.authorized_uk": ["are you authorized to work in the united kingdom?"],
    "eligibility.over_18": ["are you over 18 years of age?", "are you over the age of 18 years old?"],
    "eligibility.sponsorship_now": ["do you currently require visa sponsorship?"],
    "eligibility.sponsorship_future": ["will you require visa sponsorship in the future?"],
    "eligibility.sponsorship": ["will you now or in the future require sponsorship for work authorization? (this information will not be used in assessing your qualifications for any position.)","will you now or in the future require sponsorship?", "will you now or in the future require visa sponsorship?",
        "will you now or in the future require sponsorship for employment?",
        "will you now or in the future require sponsorship for employment visa status (e.g., h-1b, tn, f-1 opt, etc.)?"],
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
    "disclosure.race": ["race", "please identify your race"],
    "disclosure.hispanic": ["are you hispanic/latino?"],
    "disclosure.veteran": ["veteran status", "protected veteran status", "are you a veteran or active member of the united states armed forces?"],
    "disclosure.subgroup": ["how would you describe your racial/ethnic background? (mark all that apply)"],
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


def job_excluded(book, job):
    """A verified, sourced user exclusion is bound to this exact application."""
    record = book.get("job_exclusions", {}).get(job.get("dedupe_hash"), {})
    return isinstance(record, dict) and record.get("status") == "verified" and bool(record.get("source"))


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


def _education_year(value):
    """Extract a year only from a valid, explicitly supplied calendar date."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?", value):
        return None
    parts = [int(part) for part in value.split("-")]
    try:
        date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
    except ValueError:
        return None
    return value[:4]


def for_role(book: dict, role: str) -> dict:
    if role not in {"sde", "ml"}:
        raise ValueError("Choose sde or ml explicitly for ambiguous jobs")
    values = {**book["answers"], **book["roles"][role]}
    # Broad skills prompts retain the full chosen resume skill list. Include
    # its coursework only when the candidate explicitly requested this policy;
    # never borrow skills/courses from the other role or an unverified source.
    policy = book.get("workflow_preferences", {}).get("skill_set_answers", {})
    skills, education = values.get("role.skills", {}), book["roles"][role].get("role.education", {})
    if (policy.get("include_coursework") is True and skills.get("status") == "verified"
            and education.get("status") == "verified" and isinstance(skills.get("value"), str)
            and isinstance(education.get("value"), str)):
        courses = re.search(r"\bCoursework:\s*(.+)\Z", education["value"], re.DOTALL)
        if courses:
            text = re.sub(r"\s+", " ", courses[1]).strip()
            values["role.skills"] = answer(skills["value"] + "\nRelevant coursework: " + text,
                {"skills": skills["source"], "coursework": education["source"],
                 "policy": policy.get("source", "explicit candidate coursework preference")})
    records = book.get("education_records", [])
    for index, record in enumerate(records):
        if record.get("status") == "verified":
            for field in ("school", "degree", "major", "start_date", "end_date"):
                if field in record:
                    values[f"education.{index}.{field}"] = answer(record[field], record["source"])
                    if index == 0:
                        values["education."+field] = values[f"education.{index}.{field}"]
            for column in ("start", "end"):
                date_key = column + "_date"
                original = record.get(date_key)
                year = _education_year(original)
                if year is not None and record.get("source"):
                    values[f"education.{index}.{column}_year"] = answer(year, {
                        "rule": "Calendar year extracted from verified original education date",
                        "derived_from": f"education.{index}.{date_key}", "original_date": original,
                        "original_source": record["source"], "expected": record.get("expected"),
                    })
    for index, record in enumerate(experience_records(book, role)):
        for field in ("company", "title", "location", "start_date", "end_date", "summary", "current"):
            values[f"experience.{index}.{field}"] = answer(record[field], record["source"])
    return values


def experience_records(book, role):
    """Parse only verified, structured resume sections without inventing days.

    Dates retain month precision. PDF-wrapped bullet text is joined within its
    original bullet; separate achievement bullets remain separate lines.
    """
    from datetime import datetime
    item = book.get("roles", {}).get(role, {}).get("role.experience", {})
    if item.get("status") != "verified" or not item.get("source") or not isinstance(item.get("value"), str):
        return []
    header = re.compile(r"^(.+?)\s{2,}([A-Za-z]{3,9} \d{4})\s*[–—-]\s*([A-Za-z]{3,9} \d{4}|Present|Current)$", re.I)
    sections, current = [], None
    for raw in item["value"].splitlines():
        line = raw.strip()
        match = header.fullmatch(line)
        if match:
            current = {"company": match[1].strip(), "dates": (match[2], match[3]), "title_line": None, "bullets": []}
            sections.append(current)
        elif current and line.startswith('•'):
            current['bullets'].append('• ' + line.lstrip('• ').strip())
        elif current and line:
            if current['bullets']:
                current['bullets'][-1] += ' ' + line
            elif current['title_line'] is None:
                current['title_line'] = line
    records = []
    def month(value):
        for fmt in ('%b %Y', '%B %Y'):
            try: return datetime.strptime(value, fmt).strftime('%Y-%m')
            except ValueError: pass
        raise ValueError('Unsupported original resume date')
    for section in sections:
        parts = re.split(r'\s{2,}', section['title_line'] or '')
        if len(parts) != 2 or not section['bullets']:
            continue
        try:
            start = month(section['dates'][0])
            current = section['dates'][1].casefold() in {'present', 'current'}
            end = '' if current else month(section['dates'][1])
        except ValueError:
            continue
        if end and end < start:
            continue
        records.append({"company": section['company'], "title": re.sub(r'\s*[—–-]\s*\[Code\].*$', '', parts[0]).strip(),
                        "location": parts[1], "start_date": start, "end_date": end,
                        "current": current, "summary": '\n'.join(section['bullets']), "status": "verified",
                        "source": {"method": "verified_resume_experience_section", "resume_role": role,
                                   "original_source": item['source'], "section": section['company'],
                                   "date_precision": "month", "source_text_sha256": hashlib.sha256(item['value'].encode()).hexdigest()}})
    return records


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
