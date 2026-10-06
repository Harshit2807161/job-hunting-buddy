"""Current upload receipts reach review without mutating the approved packet."""
import asyncio
import copy
import hashlib
import json

import pytest

from jhb.applications import authorized_submission as submission, boards
from jhb.applications.booklet import write_private
from tests.applications.test_authorized_submission import (
    ASHBY_HTML, ASHBY_URL, ashby_evidence, independent_approval, synthetic_runtime,
)


def upload_context(tmp_path):
    path = tmp_path / 'synthetic.pdf'
    path.write_bytes(b'%PDF-synthetic-approved-bytes')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    field = {'ref': '_systemfield_resume', 'label': 'Resume', 'type': 'file', 'answer_key': 'documents.resume'}
    proof = {'schema_version': 1, 'kind': 'ashby_saved_file', 'job_identity': list(boards.job_identity(ASHBY_URL)),
             'field_ref': field['ref'], 'field_path': 'resume', 'document_key': 'documents.resume',
             'saved_file_id': 'aaaaaaaa-bbbb-cccc-dddd-000000000002', 'filename': path.name,
             'size': path.stat().st_size, 'sha256': digest, 'upload_receipt': 'new-receipt', 'verified_at': 1}
    result = {'verified': True, 'filename': path.name, 'sha256': digest,
              'upload_receipt': proof['upload_receipt'], 'ashby_upload_proof': proof}
    document = {'path': str(path), 'filename': path.name, 'sha256': digest, 'receipt': 'new-receipt'}
    snapshot = {'fields': [field], 'retained': [{'ref': field['ref'], 'answer_key': 'documents.resume',
        'state': {'value': path.name, 'invalid': False, 'receipt': 'new-receipt', 'ashby_saved_file': {
            'field_path': 'resume', 'other_invalid': False, 'displayed_filename': path.name,
            'saved_file': {'id': proof['saved_file_id'], 'filename': path.name, 'typename': 'File'}}}}]}
    return field, proof, result, document, snapshot


@pytest.mark.parametrize('change', ['job', 'ref', 'key', 'sha', 'receipt', 'filename', 'size',
                                  'result_sha', 'result_filename', 'local_bytes', 'missing'])
def test_returned_upload_proof_must_bind_exact_approved_document(tmp_path, change):
    field, proof, result, document, _ = upload_context(tmp_path)
    if change == 'job': proof['job_identity'][-1] = 'different-job'
    elif change == 'ref': proof['field_ref'] = 'different-file'
    elif change == 'key': proof['document_key'] = 'documents.cover_letter'
    elif change == 'sha': proof['sha256'] = '0' * 64
    elif change == 'receipt': proof['upload_receipt'] = 'old-receipt'
    elif change == 'filename': proof['filename'] = 'other.pdf'
    elif change == 'size': proof['size'] += 1
    elif change == 'result_sha': result['sha256'] = '0' * 64
    elif change == 'result_filename': result['filename'] = 'other.pdf'
    elif change == 'local_bytes': (tmp_path / 'synthetic.pdf').write_bytes(b'%PDF-changed')
    elif change == 'missing': result.pop('ashby_upload_proof')
    with pytest.raises(ValueError, match='upload proof'):
        submission._retain_upload_proof(document, result, field, 'documents.resume', ASHBY_URL, required=True)
    assert 'ashby_upload_proof' not in document


@pytest.mark.parametrize('change', ['server_id', 'server_name', 'server_type', 'field_path', 'display_name',
                                  'invalid', 'other_invalid', 'receipt', 'value', 'row_ref', 'field_ref',
                                  'duplicate_field', 'duplicate_row', 'missing_row'])
def test_fresh_readback_must_match_returned_upload_proof(tmp_path, change):
    field, _, result, document, snapshot = upload_context(tmp_path)
    submission._retain_upload_proof(document, result, field, 'documents.resume', ASHBY_URL, required=True)
    state = snapshot['retained'][0]['state']
    observed = state['ashby_saved_file']
    if change == 'server_id': observed['saved_file']['id'] = 'aaaaaaaa-bbbb-cccc-dddd-000000000001'
    elif change == 'server_name': observed['saved_file']['filename'] = 'other.pdf'
    elif change == 'server_type': observed['saved_file']['typename'] = 'Other'
    elif change == 'field_path': observed['field_path'] = 'another-control'
    elif change == 'display_name': observed['displayed_filename'] = 'other.pdf'
    elif change == 'invalid': state['invalid'] = True
    elif change == 'other_invalid': observed['other_invalid'] = True
    elif change == 'receipt': state['receipt'] = 'old-receipt'
    elif change == 'value': state['value'] = 'other.pdf'
    elif change == 'row_ref': snapshot['retained'][0]['ref'] = 'other-control'
    elif change == 'field_ref': field['ref'] = 'other-control'
    elif change == 'duplicate_field': snapshot['fields'].append(copy.deepcopy(field))
    elif change == 'duplicate_row': snapshot['retained'].append(copy.deepcopy(snapshot['retained'][0]))
    elif change == 'missing_row': snapshot['retained'] = []
    with pytest.raises(ValueError):
        submission._check_upload_proofs({'documents.resume': document}, snapshot, ASHBY_URL)


@pytest.mark.parametrize('mode', ['reupload', 'reuse', 'tampered_proof', 'changed_server'])
def test_native_saved_upload_replacement_reaches_reviewer_with_immutable_original_packet(tmp_path, monkeypatch, mode):
    job, packet, packet_path, manifest, auth, attempt_path = ashby_evidence(tmp_path, monkeypatch)
    with synthetic_runtime(html=ASHBY_HTML, url=ASHBY_URL) as (_, invoke, inspect, _, _, _, _):
        inspect("""(()=>{const input=document.getElementById('_systemfield_resume'), owner=input.closest('[data-field-path]');
          const name=document.createElement('p');name.className='ashby-application-form-input-file-item-name';owner.append(name);
          window.uploads=0;window.fileProps={field:{path:'resume'},savedFile:null};
          window.bindFile=input=>{input.__reactFiberFixture={memoizedProps:window.fileProps,return:{stateNode:owner}};
            input.addEventListener('change',()=>{window.uploads++;window.fileProps.savedFile={__typename:'File',
              id:'aaaaaaaa-bbbb-cccc-dddd-'+String(window.uploads).padStart(12,'0'),filename:input.files[0].name};
              name.textContent=input.files[0].name})};window.bindFile(input);})()""")
        cli = submission.AuthorizedSubmissionCLI()
        monkeypatch.setattr(cli, 'invoke', invoke)
        context = {'authorization_path': auth['authorization_path'], 'attempt_path': str(attempt_path)}
        asyncio.run(invoke('locate', **context))
        documents = submission.document_manifest(manifest, packet)
        before = asyncio.run(invoke('check', **context, documents=documents, require_receipts=False))
        field = next(field for field in before['fields'] if field.get('answer_key') == 'documents.resume')
        first = asyncio.run(invoke('document', **context, field=field, value=documents['documents.resume']['path']))
        row = next(row for row in packet['filled'] if row['key'] == 'documents.resume')
        row.update(upload_receipt=first['upload_receipt'], document_sha256=first['sha256'],
                   ashby_upload_proof=first['ashby_upload_proof'])
        write_private(packet_path, packet)
        attempt = json.loads(attempt_path.read_text())
        attempt['packet_sha256'] = hashlib.sha256(packet_path.read_bytes()).hexdigest()
        write_private(attempt_path, attempt)
        immutable = packet_path.read_bytes()
        original_manifest = copy.deepcopy(manifest)
        # Real Ashby restores savedFile while clearing its native File/receipt.
        if mode != 'reuse':
            inspect("""(()=>{const old=document.getElementById('_systemfield_resume'),input=document.createElement('input');
              for(const attribute of old.attributes)input.setAttribute(attribute.name,attribute.value);
              old.replaceWith(input);window.bindFile(input)})()""")
        async def current_upload(operation, **payload):
            result = await invoke(operation, **payload)
            if operation == 'document':
                if mode == 'tampered_proof': result['ashby_upload_proof']['sha256'] = '0' * 64
                if mode == 'changed_server':
                    inspect("window.fileProps.savedFile.id='aaaaaaaa-bbbb-cccc-dddd-000000000099'")
            return result
        monkeypatch.setattr(cli, 'invoke', current_upload)
        reviewed = []
        def reviewer(current_job, current_manifest, checks, authority):
            current = current_manifest['documents']['documents.resume']
            proof = current['ashby_upload_proof']
            assert proof['saved_file_id'].endswith('000000000001' if mode == 'reuse' else '000000000002')
            assert current['receipt'] == proof['upload_receipt']
            assert (current['receipt'] == first['upload_receipt']) == (mode == 'reuse')
            assert current['sha256'] == proof['sha256'] == first['sha256']
            assert current_manifest['filled'] == packet['filled']
            assert current_manifest['filled'][-1]['ashby_upload_proof'] == first['ashby_upload_proof']
            submission._check_upload_proofs(current_manifest['documents'], checks, ASHBY_URL)
            reviewed.append(copy.deepcopy(current))
            return independent_approval(current_job, current_manifest, checks, authority)
        outcome = asyncio.run(submission.submit_reviewed(job, packet_path, manifest, authorization=auth,
                                                         attempt=attempt_path, cli=cli, reviewer=reviewer))
        assert packet_path.read_bytes() == immutable and manifest == original_manifest
        if mode in {'tampered_proof', 'changed_server'}:
            assert outcome['state'] == 'waiting_review' and outcome['click_started'] is False
            assert reviewed == [] and inspect('window.submissions') == 0
            assert not (attempt_path.parent / 'independent-review.json').exists()
            return
        assert outcome['state'] == 'submitted', outcome
        assert len(reviewed) == 1 and inspect('window.uploads') == (1 if mode == 'reuse' else 2)
        assert inspect('window.submissions') == 1
        final = json.loads((attempt_path.parent / 'checks.json').read_text())
        assert final['documents']['documents.resume']['ashby_upload_proof'] == reviewed[0]['ashby_upload_proof']
