"""Fresh owned catalogs for verified standard or public-guided GH answers.

Verified facts or exact public context select the field worth inspecting.
Neither supplies native retention evidence; only fixed describe does.
"""
from __future__ import annotations

import hashlib
import json

from . import boards
from .booklet import ALIASES, normalize
from .cli_browser import BrowserOperationError

MAX_PROBES = 12
KNOWN_DISCLOSURES = frozenset({'disclosure.gender', 'disclosure.hispanic',
                              'disclosure.veteran', 'disclosure.disability'})


def _known_record(field, answers):
    """Inspect only exact standard fields with an existing verified fact.

    Inspecting the full catalog does not widen a fact's scope: a current
    disability answer, for example, cannot establish past medical history.
    """
    from .known_answers import needs_catalog
    if needs_catalog(field, answers):
        return True
    label = normalize(field.get('label') or '')
    for key in KNOWN_DISCLOSURES:
        record = answers.get(key, {})
        if (label in ALIASES[key] and record.get('status') == 'verified'
                and record.get('source') and isinstance(record.get('value'), (str, bool))):
            return True
    # Phone catalogs can contain hundreds of entries. The fixed fill path
    # queries the approved country and verifies the selected calling-code flag;
    # do not truncate a global catalog and misclassify an already retained code.
    return False


def _guided_record(field, record):
    if normalize(str(record.get('question') or '')) != normalize(field['label']):
        return False
    if record.get('field_ref') and record['field_ref'] != field.get('ref'):
        return False
    from .question_metadata import public_response_context_matches
    return public_response_context_matches(field, record)


def _fields(snapshot, job, answers):
    identity = boards.job_identity(job.get('application_url') or job.get('url'))
    if not identity or identity[0] not in {'greenhouse', 'ashby'}:
        return []
    from .known_answers import PROFILE_CATALOG_QUESTIONS, needs_catalog
    if identity[0] == 'ashby':
        fields = [field for field in snapshot.get('fields', [])
                  if field.get('type') in {'combobox', 'select'}
                  and normalize(field.get('label', '')) in PROFILE_CATALOG_QUESTIONS
                  and needs_catalog(field, answers)]
    else:
        fields = [field for field in snapshot.get('fields', []) if field.get('type') in {'combobox', 'select'}
                  and (_known_record(field, answers) or any(_guided_record(field, record)
                       for key, record in answers.items() if key.startswith('custom.')))]
    if not fields:
        return []
    if boards.job_identity(snapshot.get('url')) != identity:
        raise BrowserOperationError('Approved-answer native inspection is outside the owned job')
    if len(fields) > MAX_PROBES:
        raise BrowserOperationError('Approved-answer native catalog inspection exceeds its bounded budget', retryable=True)
    return fields


def _attach(field, descriptor):
    choices = descriptor.get('choices') if isinstance(descriptor, dict) else None
    if (not isinstance(choices, list) or not choices or len(choices) > 100 or descriptor.get('truncated')
            or any(not isinstance(label, str) or not label.strip() for label in choices)
            or len(set(choices)) != len(choices) or descriptor.get('type') != field['type']):
        raise BrowserOperationError('Approved-answer native dropdown catalog is unavailable', retryable=True)
    if field['type'] == 'select':
        options = [dict(option) for option in field.get('options', []) if isinstance(option, dict)
                   and not option.get('disabled') and option.get('value') != '']
        if [option.get('label') for option in options] != choices:
            raise BrowserOperationError('Approved-answer native select catalog changed during inspection', retryable=True)
        field['options'] = options  # Preserve observed native values; never infer IDs from labels.
    else:
        field['options'] = [{'label': label} for label in choices]
    field['native_question_catalog'] = {'source': 'owned_native_dropdown', 'field_ref': field['ref'],
        'choices_sha256': hashlib.sha256(json.dumps(choices, ensure_ascii=False).encode()).hexdigest()}


async def enrich_async(snapshot, job, answers, describe):
    for field in _fields(snapshot, job, answers):
        try:
            _attach(field, await describe(field))
        except BrowserOperationError:
            raise
        except (ValueError, RuntimeError, TimeoutError) as exc:
            raise BrowserOperationError('Approved-answer native dropdown inspection failed', retryable=True) from exc
    return snapshot


def enrich_sync(snapshot, job, answers, describe):
    for field in _fields(snapshot, job, answers):
        try:
            _attach(field, describe(field))
        except BrowserOperationError:
            raise
        except (ValueError, RuntimeError, TimeoutError) as exc:
            raise BrowserOperationError('Approved-answer native dropdown inspection failed', retryable=True) from exc
    return snapshot
