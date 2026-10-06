"""Private, complete question inventory for explicit per-packet human review."""
from __future__ import annotations

import re
import time

from .booklet import normalize


def candidate_wording_requested(label):
    text = normalize(label).replace('’', "'")
    tools = r'(?:generative\s+)?(?:ai|artificial intelligence)|(?:large\s+)?language models?|llms?|chatgpt'
    prohibition = r'(?:no|without(?:\s+(?:using|(?:the )?use of|(?:assistance|help) from))?|do not (?:use|utilize|rely on)|don\x27t (?:use|utilize|rely on)|refrain from(?: using)?|avoid(?: using)?)'
    if re.search(r'\b(?:your|my|their) own (?:words|wording)\b|\bhuman[- ]written\b', text):
        return True
    for match in re.finditer(r'\b'+prohibition+r'\s+(?:(?:any|a|an|the)\s+)?(?:'+tools+r')\b', text):
        # A relative clause describing the applicant's AI workflow is not an
        # instruction about authorship of this answer. Evaluate other clauses
        # separately so an explicit writing prohibition still takes precedence.
        descriptive = (match[0].startswith(('do not ', "don't ")) and re.search(
            r'\b(?:where|when|in which) you (?:deliberately |intentionally |typically )?$', text[:match.start()]))
        if not descriptive:
            return True
    return False


def candidate_response(record):
    source=record.get('source')
    return (record.get('status') in {'verified','declined'} and isinstance(source,dict)
            and source.get('provider')=='explicit user question response'
            and isinstance(source.get('question_id'),str) and bool(source['question_id']))


def candidate_authored(record):
    return record.get('status')=='verified' and candidate_response(record)


def category(field,key=None):
    label=normalize(field['label'])
    if str(key or '').startswith('disclosure.') or label in {'gender','race','race/ethnicity','ethnicity','pronouns','veteran status','disability status'}:
        return 'voluntary_disclosure'
    if 'communicationConsent' in field['ref'] or re.search(r'\b(marketing|promotional|newsletter)\b',label):
        return 'communications'
    if field['type']=='file':
        return 'document'
    if str(key or '').startswith(('identity.','links.','education.','experience.')):
        return 'profile_fact'
    if field['type']=='textarea' or (field['type']=='text' and re.match(r'(?:why\b|describe\b|explain\b|tell us\b|what (?:is|are) your\b|anything else\b)',label)):
        return 'substantive_written'
    return 'application_question'


def _retained_catalog_fallback(field, key, filled, answers):
    """Accept only a retained native catalog choice bound to its original fact.

    The worker selects this fallback only after the actual education value is
    absent from the native dropdown. Do not infer it from an empty catalog or
    overwrite the candidate's original school/major during review.
    """
    native = re.fullmatch(r'(school|discipline)--(\d+)', field['ref'])
    if not native or field['type'] not in {'combobox', 'select'}:
        return None
    column = 'major' if native[1] == 'discipline' else 'school'
    if key != f'education.{native[2]}.{column}':
        return None
    rows = [row for row in filled if row['ref'] == field['ref']]
    if len(rows) != 1:
        return None
    retained = rows[0]
    fallback_key = f'standing.catalog.{native[2]}.{column}'
    original, fallback = answers.get(key, {}), answers.get(fallback_key, {})
    source = fallback.get('source')
    if (original.get('status') != 'verified' or not original.get('value') or not original.get('source')
            or fallback.get('status') != 'verified' or not fallback.get('value')
            or not isinstance(source, dict) or not source.get('policy')
            or source.get('actual_value') != original['value']
            or source.get('original_source') != original['source']
            or retained.get('question') != field['label'] or retained.get('key') != fallback_key
            or retained.get('value') != fallback['value'] or retained.get('source') != source):
        return None
    return retained


def _superseded_greenhouse_uploads(observed, filled, job_url):
    """Retire only a proved native-input -> current uploaded-file alias.

    The original observations and retained rows remain unchanged as history.
    Same-label documents alone cannot establish that they are one control.
    """
    from .boards import job_identity
    identity = job_identity(job_url)
    if not identity or identity[0] != 'greenhouse':
        return {}
    # The worker re-plans at a later step whenever observed refs change.
    steps = [f.get('observed_step') for f in observed]
    if not steps or any(type(step) is not int or step < 0 for step in steps):
        return {}
    latest = max(steps)
    superseded = {}
    for old in observed:
        label = normalize(old.get('label', ''))
        expected = {'cover letter': ('cover_letter', 'documents.cover_letter'),
                    'resume': ('resume', 'documents.resume'),
                    'resume/cv': ('resume', 'documents.resume')}.get(label)
        if (not expected or old.get('type') != 'file' or old.get('ref') != expected[0]
                or old['observed_step'] >= latest):
            continue
        current = [f for f in observed if f.get('type') == 'file'
                   and normalize(f.get('label', '')) == label and f['observed_step'] == latest]
        if len(current) != 1:
            continue
        new = current[0]
        if (new.get('ref') != 'uploaded:'+new.get('label', '')
                or old.get('label') != new.get('label')
                or bool(old.get('required')) != bool(new.get('required'))
                # Standard upload aliases omit empty metadata after upload.
                # Instructions or truncated context require a separate review.
                or old.get('description') not in (None, '') or new.get('description') not in (None, '')
                or bool(old.get('description_truncated')) or bool(new.get('description_truncated'))
                or sum(f.get('ref') == old['ref'] for f in observed) != 1
                or sum(f.get('ref') == new['ref'] for f in observed) != 1):
            continue
        before = [r for r in filled if r.get('ref') == old['ref']]
        after = [r for r in filled if r.get('ref') == new['ref']]
        if len(before) != 1 or len(after) != 1:
            continue
        a, b = before[0], after[0]
        if (a.get('key') != expected[1] or a.get('question') != old['label']
                or not a.get('source') or not isinstance(a.get('value'), str)
                or not a['value'].lower().endswith('.pdf')
                or not isinstance(a.get('upload_receipt'), str) or not a['upload_receipt']
                or not isinstance(a.get('document_sha256'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', a['document_sha256'])
                or any(a.get(k) != b.get(k) for k in
                       ('key', 'question', 'value', 'source', 'document_sha256', 'upload_receipt'))):
            continue
        superseded[old['ref']] = new['ref']
    return superseded


def build(observed,filled,answers,key_for_field,*,complete=False,step_count=0,job_url=None):
    """Reconcile every observed question; an optional blank is still visible."""
    superseded = _superseded_greenhouse_uploads(observed, filled, job_url)
    records=[]
    for field in observed:
        if field['ref'] in superseded:
            continue
        key=key_for_field(field,answers)
        canonical_key=key
        approved=answers.get(key,{})
        matches=[row for row in filled if row['ref']==field['ref'] and row.get('key')==key]
        retained=next((row for row in matches if approved.get('status')=='verified'
                       and row.get('value')==approved.get('value') and row.get('source')==approved.get('source')),None)
        if retained is None and sum(row['ref'] == field['ref'] for row in observed) == 1:
            retained = _retained_catalog_fallback(field, key, filled, answers)
            if retained is not None:
                key = retained['key']
                approved = answers[key]
        description=field.get('description','')
        description=description if isinstance(description,str) else ''
        description_truncated=field.get('description_truncated') is True or len(description)>4096
        description=description[:4096]
        wording=candidate_wording_requested(field['label']+'\n'+description)
        if retained and wording and not candidate_authored(approved):
            retained=None
        status='answered' if retained else 'declined' if approved.get('status')=='declined' else 'blank'
        records.append({'ref':field['ref'],'question':field['label'],'type':field['type'],
                        'required':field['required'],'status':status,'answer_key':key,
                        'category':category(field,canonical_key),'source':retained.get('source') if retained else approved.get('source'),
                        'proposed':bool(retained and (retained.get('proposed') or approved.get('proposed') or
                            isinstance(retained.get('source'),dict) and retained['source'].get('kind')=='grounded_narrative')),
                        'step':field.get('observed_step',0),'choices':[o['label'] for o in field.get('options',[])],
                        'candidate_wording_required':wording,'description':description,
                        'description_truncated':description_truncated})
        if field.get('calendar_format'):
            records[-1]['calendar_format'] = field['calendar_format']
    # Workable's saved rows are independently reopened/read by its preparer.
    # Their closed editor controls must remain reviewable too.
    seen={record['ref'] for record in records}
    for row in filled:
        if row['ref'] not in seen and row['ref'] not in superseded:
            records.append({'ref':row['ref'],'question':row['question'],'type':'saved_record','required':False,
                            'status':'answered','answer_key':row.get('key'),'category':'profile_fact',
                            'source':row.get('source'),'step':0,'choices':[], 'candidate_wording_required':False})
            seen.add(row['ref'])
    required_blank=any(row['required'] and row['status']!='answered' for row in records)
    inventory={'complete':bool(complete and records and not required_blank),'fields':records,
               'observed_at':int(time.time()),'step_count':step_count}
    if superseded:
        inventory['superseded_upload_refs'] = superseded
    counts={'schema_version':1,'inventory_verified':inventory['complete'],'all_observed_count':len(records),
            'answered_count':sum(row['status']=='answered' for row in records),
            'blank_count':sum(row['status']=='blank' for row in records),
            'declined_count':sum(row['status']=='declined' for row in records),
            'blank_substantive_count':sum(row['category']=='substantive_written' and row['status']!='answered' for row in records),
            'candidate_wording_required_count':sum(row['candidate_wording_required'] and row['status']!='answered' for row in records),
            'requires_explicit_acknowledgment':any(not row['required'] and row['status']!='answered' for row in records),
            'requires_explicit_approval':True}
    return {'review_inventory':inventory,'review_questions':records,'review_completeness':counts}
