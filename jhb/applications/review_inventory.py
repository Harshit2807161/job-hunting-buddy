"""Private, complete question inventory for explicit per-packet human review."""
from __future__ import annotations

import re
import time

from .booklet import normalize


def candidate_wording_requested(label):
    return bool(re.search(r'\b(?:no|without(?: using)?|do not use|don\x27t use)\s+(?:ai|artificial intelligence|chatgpt)\b|'
                         r'\b(?:your|my|their) own (?:words|wording)\b|\bhuman[- ]written\b', normalize(label)))


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


def build(observed,filled,answers,key_for_field,*,complete=False,step_count=0):
    """Reconcile every observed question; an optional blank is still visible."""
    records=[]
    for field in observed:
        key=key_for_field(field,answers)
        approved=answers.get(key,{})
        matches=[row for row in filled if row['ref']==field['ref'] and row.get('key')==key]
        retained=next((row for row in matches if approved.get('status')=='verified'
                       and row.get('value')==approved.get('value') and row.get('source')==approved.get('source')),None)
        wording=candidate_wording_requested(field['label'])
        if retained and wording and not candidate_authored(approved):
            retained=None
        status='answered' if retained else 'declined' if approved.get('status')=='declined' else 'blank'
        records.append({'ref':field['ref'],'question':field['label'],'type':field['type'],
                        'required':field['required'],'status':status,'answer_key':key,
                        'category':category(field,key),'source':retained.get('source') if retained else approved.get('source'),
                        'proposed':bool(retained and (retained.get('proposed') or approved.get('proposed') or
                            isinstance(retained.get('source'),dict) and retained['source'].get('kind')=='grounded_narrative')),
                        'step':field.get('observed_step',0),'choices':[o['label'] for o in field.get('options',[])],
                        'candidate_wording_required':wording})
    # Workable's saved rows are independently reopened/read by its preparer.
    # Their closed editor controls must remain reviewable too.
    seen={record['ref'] for record in records}
    for row in filled:
        if row['ref'] not in seen:
            records.append({'ref':row['ref'],'question':row['question'],'type':'saved_record','required':False,
                            'status':'answered','answer_key':row.get('key'),'category':'profile_fact',
                            'source':row.get('source'),'step':0,'choices':[], 'candidate_wording_required':False})
            seen.add(row['ref'])
    required_blank=any(row['required'] and row['status']!='answered' for row in records)
    inventory={'complete':bool(complete and records and not required_blank),'fields':records,
               'observed_at':int(time.time()),'step_count':step_count}
    counts={'schema_version':1,'inventory_verified':inventory['complete'],'all_observed_count':len(records),
            'answered_count':sum(row['status']=='answered' for row in records),
            'blank_count':sum(row['status']=='blank' for row in records),
            'declined_count':sum(row['status']=='declined' for row in records),
            'blank_substantive_count':sum(row['category']=='substantive_written' and row['status']!='answered' for row in records),
            'candidate_wording_required_count':sum(row['candidate_wording_required'] and row['status']!='answered' for row in records),
            'requires_explicit_acknowledgment':any(not row['required'] and row['status']!='answered' for row in records),
            'requires_explicit_approval':True}
    return {'review_inventory':inventory,'review_questions':records,'review_completeness':counts}
