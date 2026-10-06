"""Constrained reference edits and one-page XeLaTeX compilation."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from datetime import date, datetime
from calendar import month_name


def latex_escape(text):
    replacements = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
                    "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(replacements.get(c, c) for c in text)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def availability_from_answers(answers):
    """Only the current explicit user availability may override reference prose.

    Resume, graduation, assistant-derived dates and unsigned policies cannot
    authorize this exception. The reference file itself remains immutable.
    """
    record = answers.get("preferences.start_date", {})
    source = record.get("source", {})
    if (record.get("status") != "verified" or not isinstance(source, dict)
            or str(source.get("provider", "")).casefold() not in {
                "explicit user response", "explicit user question response"}
            or not isinstance(record.get("value"), str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", record["value"])):
        return None
    scope = str(source.get("scope", "")) + " " + str(source.get("question", ""))
    if not re.search(r"\b(?:availability|start date|available to start)\b", scope, re.I):
        return None
    try:
        value = date.fromisoformat(record["value"])
        stamp = datetime.fromisoformat(source["answered_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        stored = json.loads(json.dumps(record, sort_keys=True, ensure_ascii=False))
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return {"answer_key": "preferences.start_date", "record": stored,
            "display_date": f"{month_name[value.month]} {value.day}, {value.year}",
            "record_sha256": _digest(stored), "source_sha256": _digest(source)}


def _availability_copy(text, availability):
    if availability is None:
        return text
    if (not isinstance(availability, dict) or availability_from_answers({
            "preferences.start_date": availability.get("record", {})}) != availability):
        raise ValueError("Availability override requires verified explicit user provenance")
    old = "starting in January 2027"
    if text.count(old) != 1:
        raise ValueError("Reference availability clause is not uniquely recognized")
    return text.replace(old, "starting on " + availability["display_date"], 1)


def tailor_text(original, replacements, *, availability=None):
    """Replace company/role references and bounded why-them clauses, preserving all else."""
    allowed = {"company", "role", "why_opening", "why_closing"}
    if set(replacements) - allowed: raise ValueError("Only approved company/role/why-them zones are supported by v1")
    company_match = re.search(r"Dear (.+?) (?:Recruiting|Hiring) Team,", original)
    role_match = re.search(r"interest in the (.+?) position at ", original)
    if not company_match or not role_match: raise ValueError("Unrecognized cover-letter template")
    begin = company_match.start()
    end = original.find(r"\vspace", role_match.end())
    if end == -1: raise ValueError("Unrecognized letter body boundary")
    body = original[begin:end]
    for zone, pattern in [
        ("why_opening", r"I believe [^.]+\."),
        ("why_closing", r"I am particularly interested [^.]+\.|I am excited by .+?(?= and would love)")]:
        if zone in replacements:
            matched = re.search(pattern, body)
            if not matched: raise ValueError("Editable why-them zone not found")
            value = replacements[zone]
            if not isinstance(value, str) or "\n" in value or "January" in value:
                raise ValueError("Why-them replacement must be a plain clause/sentence")
            body = body[:matched.start()] + latex_escape(value) + body[matched.end():]
    if "company" in replacements:
        old_company = company_match[1]
        new_company = latex_escape(replacements["company"])
        body = body.replace(old_company, new_company)
        short = old_company.removesuffix(" Company")
        if short != old_company: body = body.replace(short, new_company)
    if "role" in replacements:
        body = body.replace(role_match[1], latex_escape(replacements["role"]))
        if role_match[1] == "AI/ML Engineer" and replacements["role"] != role_match[1]:
            # This original role reference is not evidence that every employer
            # using the ML resume has an AI/ML engineering team.
            body = body.replace("your AI/ML engineering team", "your team")
    result = original[:begin] + body + original[end:]
    # Availability and protected quantitative claims are invariants.
    for token in ["January 2027", "10.55", "50\\%", "30\\%", "3,000", "1,400", "15 minutes"]:
        if original.count(token) != result.count(token): raise ValueError("Protected claim or availability changed")
    return _availability_copy(result, availability)


def compile_letter(template: Path, replacements: dict, output: Path, *, availability=None):
    from pypdf import PdfReader
    template = template.resolve()
    digest = hashlib.sha256(template.read_bytes()).hexdigest()
    original = template.read_text()
    tailored = tailor_text(original, replacements, availability=availability)
    output = output.resolve()
    if output == template or output.suffix != ".pdf": raise ValueError("Output must be a new PDF")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.parent.chmod(0o700)
    scratch = output.parent / (output.stem + "-build")
    scratch.mkdir(exist_ok=True)
    scratch.chmod(0o700)
    tex = scratch / "letter.tex"
    tex.write_text(tailored)
    tex.chmod(0o600)
    diff = "".join(difflib.unified_diff(original.splitlines(True), tailored.splitlines(True), fromfile="reference", tofile="tailored"))
    (scratch / "changes.diff").write_text(diff)
    # The local launch agent has a minimal PATH; reuse the installed TinyTeX
    # executable rather than downloading tools or changing the reference.
    executable = shutil.which("xelatex")
    installed = Path.home() / "Library" / "TinyTeX" / "bin" / "universal-darwin" / "xelatex"
    if not executable and installed.is_file() and os.access(installed, os.X_OK):
        executable = str(installed)
    if not executable:
        raise FileNotFoundError("Installed XeLaTeX is unavailable")
    result = subprocess.run([executable, "-interaction=nonstopmode", "letter.tex"], cwd=scratch,
                            capture_output=True, timeout=90)
    (scratch / "build-output.log").write_bytes(result.stdout + result.stderr)
    pdf = scratch / "letter.pdf"
    if not pdf.exists(): raise RuntimeError("XeLaTeX did not produce a PDF; inspect private build log")
    if result.returncode != 0 and b"There's no line here to end" not in result.stdout:
        raise RuntimeError("Cover-letter compiler failed; inspect private build log")
    if len(PdfReader(pdf).pages) != 1: raise RuntimeError("Cover letter exceeds one page; candidate review required")
    if hashlib.sha256(template.read_bytes()).hexdigest() != digest: raise RuntimeError("Reference was modified")
    shutil.copyfile(pdf, output)
    output.chmod(0o600)
    return output
