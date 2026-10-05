"""Fresh owned native catalogs for explicitly public-guided Greenhouse answers.

Public metadata selects the exact field worth inspecting. It never supplies
native choices or retention evidence; only the fixed describe operation does.
"""
from __future__ import annotations

import hashlib
import json

from . import boards
from .booklet import normalize
from .cli_browser import BrowserOperationError

MAX_PROBES = 12


def _guided_record(field, record):
    if normalize(str(record.get('question') or '')) != normalize(field['label']):
        return False
    if record.get('field_ref') and record['field_ref'] != field.get('ref'):
        return False
    from .question_metadata import public_response_context_matches
    return public_response_context_matches(field, record)


def _fields(snapshot, job, answers):
    # Unrelated applications and unproven responses do not trigger exploration.
    if not any(isinstance(record.get('source'), dict) and 'public_question_metadata_proofs' in record['source']
               for key, record in answers.items() if key.startswith('custom.')):
        return []
    identity = boards.job_identity(job.get('application_url') or job.get('url'))
    if not identity or identity[0] != 'greenhouse':
        return []
    if boards.job_identity(snapshot.get('url')) != identity:
        raise BrowserOperationError('Public-guided native inspection is outside the owned job')
    fields = [field for field in snapshot.get('fields', []) if field.get('type') in {'combobox', 'select'}
              and any(_guided_record(field, record) for key, record in answers.items() if key.startswith('custom.'))]
    if len(fields) > MAX_PROBES:
        raise BrowserOperationError('Public-guided native catalog inspection exceeds its bounded budget', retryable=True)
    return fields


def _attach(field, descriptor):
    choices = descriptor.get('choices') if isinstance(descriptor, dict) else None
    if (not isinstance(choices, list) or not choices or len(choices) > 100 or descriptor.get('truncated')
            or any(not isinstance(label, str) or not label.strip() for label in choices)
            or len(set(choices)) != len(choices) or descriptor.get('type') != field['type']):
        raise BrowserOperationError('Public-guided native dropdown catalog is unavailable', retryable=True)
    if field['type'] == 'select':
        options = [dict(option) for option in field.get('options', []) if isinstance(option, dict)
                   and not option.get('disabled') and option.get('value') != '']
        if [option.get('label') for option in options] != choices:
            raise BrowserOperationError('Public-guided native select catalog changed during inspection', retryable=True)
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
            raise BrowserOperationError('Public-guided native dropdown inspection failed', retryable=True) from exc
    return snapshot


def enrich_sync(snapshot, job, answers, describe):
    for field in _fields(snapshot, job, answers):
        try:
            _attach(field, describe(field))
        except BrowserOperationError:
            raise
        except (ValueError, RuntimeError, TimeoutError) as exc:
            raise BrowserOperationError('Public-guided native dropdown inspection failed', retryable=True) from exc
    return snapshot
