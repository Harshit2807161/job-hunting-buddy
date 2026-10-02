"""Constrained reference edits and one-page XeLaTeX compilation."""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path


def latex_escape(text):
    replacements = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
                    "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(replacements.get(c, c) for c in text)


def tailor_text(original, replacements):
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
    result = original[:begin] + body + original[end:]
    # Availability and protected quantitative claims are invariants.
    for token in ["January 2027", "10.55", "50\\%", "30\\%", "3,000", "1,400", "15 minutes"]:
        if original.count(token) != result.count(token): raise ValueError("Protected claim or availability changed")
    return result


def compile_letter(template: Path, replacements: dict, output: Path):
    from pypdf import PdfReader
    template = template.resolve()
    digest = hashlib.sha256(template.read_bytes()).hexdigest()
    original = template.read_text()
    tailored = tailor_text(original, replacements)
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
    result = subprocess.run(["xelatex", "-interaction=nonstopmode", "letter.tex"], cwd=scratch,
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
