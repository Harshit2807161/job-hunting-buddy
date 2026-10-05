"""Limited qualitative answers grounded in the selected verified role catalog.

This module never supplies screening facts, preferences, or eligibility answers.
It has no browser, model, network, or persistence access. Missing source facts
produce no proposal; callers decide whether a prompt needs a factual handoff or
an operational drafting review.
"""
from __future__ import annotations

import hashlib
import re

from ..eligibility import verified_description
from .booklet import answer, normalize

ACCOMPLISHMENTS_PROMPT = (
    "please provide three examples of accomplishments that highlight your exceptional ability. "
    "provide quantitative details and metrics to describe your impact. first example:"
)
_NUMBERED = {ACCOMPLISHMENTS_PROMPT: 0, "second example:": 1, "third example:": 2}
_MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_EXPERIENCE_HEADER = re.compile(
    rf"^(.+?)\s+{_MONTH}\s+\d{{4}}\s*[–—-]\s*(?:{_MONTH}\s+\d{{4}}|Present|Current)$", re.I)
_METRIC = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:%|\+?\s*(?:hours?|minutes?|seconds?|milliseconds?|"
    r"users?|students?|rooms?|developers?|customers?|requests?|transactions?)\b)", re.I)


def _verified_text(answers, key):
    record = answers.get(key, {})
    if (record.get("status") == "verified" and record.get("source")
            and isinstance(record.get("value"), str) and record["value"].strip()):
        return record
    return None


def _sections(text, key):
    """Read resume section headers and join only PDF-wrapped bullet lines."""
    sections, current = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        is_bullet = line.startswith("•")
        header = _EXPERIENCE_HEADER.fullmatch(line) if key == "role.experience" else None
        project = line.split("|", 1)[0].strip() if key == "role.projects" and "|" in line and not is_bullet else None
        if not is_bullet and (header or project):
            current = {"section": header[1].strip() if header else project, "bullets": []}
            sections.append(current)
        elif is_bullet and current is not None:
            current["bullets"].append("• " + line.lstrip("• ").strip())
        elif current is not None and current["bullets"]:
            current["bullets"][-1] += " " + line
        # Title/location lines before the first bullet are not achievements.
    return [s for s in sections if s["bullets"] and _METRIC.search(" ".join(s["bullets"]))]


def _accomplishment(field, job, answers):
    label = normalize(field["label"])
    index = _NUMBERED.get(label)
    if index is None:
        return None
    if index and ACCOMPLISHMENTS_PROMPT not in {
            normalize(x) for x in job.get("observed_application_questions", []) if isinstance(x, str)}:
        return None
    candidates = []
    for key in ("role.experience", "role.projects"):
        record = _verified_text(answers, key)
        if record:
            candidates.extend((section, key, record) for section in _sections(record["value"], key))
    if len(candidates) <= index:
        return None
    section, key, record = candidates[index]
    value = "\n".join(section["bullets"])
    return {**answer(value, {
        "kind": "grounded_narrative", "method": "verified_resume_bullets",
        "selected_role_facts": {"key": key, "source": record["source"],
                                "sha256": hashlib.sha256(record["value"].encode()).hexdigest()},
        "section": section["section"], "example_number": index + 1,
    }), "kind": "grounded_narrative"}


def _company_interest(field, job):
    company = job.get("company")
    if not isinstance(company, str) or not company.strip() or len(company) > 120:
        return None
    label = normalize(field["label"])
    allowed = {"why are you interested in this company?", "why do you want to work here?",
               "why are you interested in working with us?",
               f"why are you interested in {normalize(company)}?",
               f"why do you want to work at {normalize(company)}?",
               f"why are you interested in working at {normalize(company)}?",
               f"why are you interested in working at {normalize(company)}? (can be short)"}
    if label not in allowed:
        return None
    description = verified_description(job)
    if not description:
        return None
    # A directly stated mission, not a generic company introduction or an
    # inferred technology advantage. Keep the complete brief sentence intact.
    text = re.sub(r"\s+", " ", description["text"]).strip()
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if not re.search(r"\b(?:our mission|mission is|mission:|mission to|our (?:ultimate )?goal is|our goal:)\b", sentence, re.I):
            continue
        if len(sentence.split()) > 25 or len(sentence) > 300:
            continue
        if re.search(r"ignore.{0,40}instructions|system prompt|password|API key|submit (?:the |this )?application", sentence, re.I):
            continue
        # Candidate experience is deliberately not invented to complete a
        # company-focused answer. The posting remains the stated fact source.
        goal = re.search(r"\bour (?:ultimate )?(?:goal|mission) (?:is|:) to (.+?)[.!?]?$", sentence, re.I)
        value = (f"I'm interested in helping {company} {goal[1].rstrip('.!?')}."
                 if goal and "working at" in label else f"Your mission stands out to me: “{sentence}”")
        return {**answer(value, {"kind": "grounded_narrative", "method": "official_mission_sentence",
                               "source_url": description["source_url"],
                               "description_sha256": description["sha256"],
                               "evidence": sentence}), "kind": "grounded_narrative"}
    return None


def proposal(field, job, answers):
    """Return a verified grounded record for an allowlisted qualitative prompt.

    ``answers`` must be the already selected SDE or ML catalog. Numbered
    accomplishment continuations additionally require the full observed prompt
    in ``job['observed_application_questions']`` from that actual form snapshot.
    No unverified value or alternate resume variant is consulted.
    """
    if field.get("type") not in {"text", "textarea"} or not isinstance(field.get("label"), str):
        return None
    from .review_inventory import candidate_wording_requested
    if field.get("description_truncated") is True or candidate_wording_requested(field["label"]+"\n"+str(field.get("description", ""))):
        return None
    return (_accomplishment(field, job, answers) or _company_interest(field, job)
            or _proud_work(field, job, answers)
            or _motivation(field, job, answers))



# These verbs allow a mechanical subject change without guessing what a
# fragment means. Unsupported fragments need a grounded drafting review.
_PAST_ACTION = re.compile(
    r"^(?:Built|Developed|Engineered|Deployed|Designed|Implemented|Automated|"
    r"Improved|Optimized|Created|Integrated|Evaluated|Reduced|Migrated|Presented|"
    r"Delivered|Shipped|Added|Led|Worked|Collaborated|Contributed|Trained|"
    r"Analyzed|Tested|Launched|Co-developed|Co-authored)\b")


def _achievement_paragraph(section, key, relevance):
    """Render at most two complete source claims, without new causal context."""
    candidates = []
    for index, bullet in enumerate(section['bullets']):
        text = bullet.lstrip('• ').strip()
        if not text or len(text.split()) > 80:
            continue
        if text.startswith('I '):
            predicate = text[2:]
        elif _PAST_ACTION.match(text):
            predicate = text[0].lower() + text[1:]
        else:
            continue
        candidates.append((index, predicate, bullet))
    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda item: (relevance(item[2]), bool(_METRIC.search(item[2]))), reverse=True)
    selected = sorted(ranked[:2], key=lambda item: item[0])
    lead = f"At {section['section']}, I " if key == 'role.experience' else f"For {section['section']}, I "
    sentences, evidence = [], []
    for _, predicate, bullet in selected:
        sentence = (lead if not sentences else 'I also ') + predicate
        if sentence[-1] not in '.!?':
            sentence += '.'
        if len((' '.join(sentences + [sentence])).split()) > 120:
            continue  # Keep complete facts and metric qualifiers; never clip a claim.
        sentences.append(sentence)
        evidence.append(bullet)
    if not sentences:
        return None
    return ' '.join(sentences), evidence


def _proud_work(field, job, answers):
    if normalize(field['label']) not in {"what's something you worked on that you were proud of?",
                                         "what is something you worked on that you were proud of?",
                                         "tell us about something you’ve built/done that you think is genuinely cool, big or small, work or personal.",
                                         "tell us about something you've built/done that you think is genuinely cool, big or small, work or personal."}:
        return None
    description = verified_description(job)
    if not description:
        return None
    vocabulary = {"python", "pytorch", "tensorflow", "transformer", "embedding", "search", "retrieval",
                  "model", "learning", "training", "evaluation", "dataset", "database", "api", "cloud",
                  "infrastructure", "frontend", "react", "typescript", "deployment", "recommender", "quantum"}
    def tokens(text):
        return {word.rstrip('s') for word in re.findall(r'\b[a-z][a-z0-9]{2,}\b',text.lower())} & vocabulary
    desired = tokens(description['text'])
    candidates = []
    for key in ('role.experience','role.projects'):
        record = _verified_text(answers,key)
        if not record:
            continue
        for section in _sections(record['value'],key):
            relevance = len(tokens(' '.join(section['bullets'])) & desired)
            aws = bool(re.search(r'\b(?:amazon|aws|amazon web services)\b',section['section'],re.I))
            candidates.append((relevance,aws,section,key,record))
    if not candidates:
        return None
    _,_,section,key,record = max(candidates,key=lambda item:(item[0],item[1]))
    rendered = _achievement_paragraph(section, key, lambda bullet: len(tokens(bullet) & desired))
    if not rendered:
        return None
    value, evidence = rendered
    return {**answer(value,{'kind':'grounded_narrative','method':'verified_resume_achievement',
                          'rendering':'brief_first_person_paragraph','evidence_bullets':evidence,
                          'review_status':'proposed','section':section['section'],
                          'description_sha256':description['sha256'],
                          'selected_role_facts':{'key':key,'source':record['source'],
                                                'sha256':hashlib.sha256(record['value'].encode()).hexdigest()}}),
            'kind':'grounded_narrative','proposed':True}


def _motivation(field, job, answers):
    if normalize(field['label']) != 'what motivates you?':
        return None
    role = job.get('selected_role')
    record = _verified_text(answers,'role.experience')
    if role not in {'sde','ml'} or not record:
        return None
    focus = 'applied ML research with reliable software engineering' if role == 'ml' else 'software engineering with reliable delivery'
    value = ("I'm motivated by turning technical ideas into working systems and measuring whether they improve on the baseline. "
             f"I'm looking for work where I can combine {focus}.")
    return {**answer(value,{'kind':'grounded_narrative','method':'proposed_motivation_from_role_selection',
                          'review_status':'proposed','selected_role':role,
                          'selected_role_facts':{'key':'role.experience','source':record['source'],
                                                'sha256':hashlib.sha256(record['value'].encode()).hexdigest()}}),
            'kind':'grounded_narrative','proposed':True}
