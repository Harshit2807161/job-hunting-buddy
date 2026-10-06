"""A retained Greenhouse file replacement is one question, with full history."""
import asyncio
import copy
import hashlib

import pytest

from jhb.applications import booklet, review_inventory, worker
from jhb.applications.planner import deterministic_plan, key_for_field

URL = 'https://job-boards.greenhouse.io/synthetic/jobs/1234'


def evidence():
    fields = [{'ref': ref, 'label': 'Cover Letter', 'type': 'file',
               'required': False, 'observed_step': step}
              for ref, step in [('cover_letter', 0), ('uploaded:Cover Letter', 1)]]
    record = booklet.answer('/synthetic/Letter.pdf', {'provider': 'verified synthetic document'})
    rows = [{'ref': f['ref'], 'question': f['label'], 'key': 'documents.cover_letter',
             'value': record['value'], 'source': record['source'],
             'document_sha256': 'a'*64, 'upload_receipt': 'same-native-receipt'} for f in fields]
    return fields, rows, {'documents.cover_letter': record}


def build(fields, rows, answers, url=URL):
    return review_inventory.build(fields, rows, answers, key_for_field,
                                  complete=True, step_count=2, job_url=url)


def test_proven_native_upload_replacement_is_one_question_without_erasing_history():
    fields, rows, answers = evidence()
    original = copy.deepcopy((fields, rows))
    result = build(fields, rows, answers)
    assert [r['ref'] for r in result['review_inventory']['fields']] == ['uploaded:Cover Letter']
    assert result['review_inventory']['superseded_upload_refs'] == {'cover_letter': 'uploaded:Cover Letter'}
    assert result['review_completeness']['all_observed_count'] == 1
    assert result['review_completeness']['answered_count'] == 1
    assert (fields, rows) == original


@pytest.mark.parametrize('change', [
    'other_board', 'no_job', 'same_step', 'missing_step', 'new_not_current',
    'two_current_owners', 'duplicate_old_ref', 'duplicate_new_ref', 'duplicate_row',
    'missing_row', 'sha', 'receipt', 'source', 'path', 'key', 'missing_proof',
    'required', 'instructions', 'new_question', 'other_native_ref',
])
def test_uncertain_or_changed_documents_never_coalesce(change):
    fields, rows, answers = evidence(); url = URL
    if change == 'other_board': url = 'https://jobs.ashbyhq.com/synthetic/11111111-2222-3333-4444-555555555555'
    elif change == 'no_job': url = None
    elif change == 'same_step': fields[0]['observed_step'] = 1
    elif change == 'missing_step': fields[0].pop('observed_step')
    elif change == 'new_not_current': fields.append({'ref': 'next', 'label': 'New question', 'type': 'text', 'required': False, 'observed_step': 2})
    elif change == 'two_current_owners': fields.append({**fields[1], 'ref': 'other_upload'})
    elif change == 'duplicate_old_ref': fields.append(dict(fields[0]))
    elif change == 'duplicate_new_ref': fields.append(dict(fields[1]))
    elif change == 'duplicate_row': rows.append(dict(rows[0]))
    elif change == 'missing_row': rows.pop(0)
    elif change == 'sha': rows[1]['document_sha256'] = 'b'*64
    elif change == 'receipt': rows[1]['upload_receipt'] = 'replacement-native-receipt'
    elif change == 'source': rows[1]['source'] = {'provider': 'another source'}
    elif change == 'path': rows[1]['value'] = '/synthetic/Other.pdf'
    elif change == 'key': rows[1]['key'] = 'documents.resume'
    elif change == 'missing_proof': rows[0].pop('upload_receipt'); rows[1].pop('upload_receipt')
    elif change == 'required': fields[1]['required'] = True
    elif change == 'instructions': fields[1]['description'] = 'Attach a different document.'
    elif change == 'new_question': fields[1]['label'] = 'Writing sample'
    elif change == 'other_native_ref': fields[0]['ref'] = 'other_cover_letter'
    result = build(fields, rows, answers, url)
    assert not result['review_inventory'].get('superseded_upload_refs')
    assert fields[0]['ref'] in {r['ref'] for r in result['review_inventory']['fields']}


def test_worker_fresh_upload_transition_finishes_with_single_current_question(tmp_path):
    path = tmp_path/'Letter.pdf'; path.write_bytes(b'%PDF-1.4\nsynthetic document')
    class CLI:
        blocked_requests = 0
        def __init__(self): self.uploaded = False
        def allowed_url(self, url): return url == URL
        async def open(self, url): pass
        async def observe(self):
            return {'url': URL, 'fields': [{'ref': 'uploaded:Cover Letter' if self.uploaded else 'cover_letter',
                    'label': 'Cover Letter', 'type': 'file', 'required': False}],
                    'buttons': [{'ref': 'submit', 'label': 'Submit application'}]}
        async def fill(self, field, value):
            assert value == str(path); self.uploaded = True
            return {'verified': True, 'upload_receipt': 'synthetic-upload-receipt'}
    result, _ = asyncio.run(worker.prepare(None, {'url': URL},
        {'documents.cover_letter': booklet.answer(str(path), 'synthetic verified letter')},
        deterministic_plan, None, cli_actions=CLI()))
    assert result['state'] == 'waiting_review'
    assert len(result['filled']) == 2  # Original receipt evidence is retained.
    assert all(r['document_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest() for r in result['filled'])
    assert result['review_completeness']['all_observed_count'] == 1
    assert [r['ref'] for r in result['review_inventory']['fields']] == ['uploaded:Cover Letter']
