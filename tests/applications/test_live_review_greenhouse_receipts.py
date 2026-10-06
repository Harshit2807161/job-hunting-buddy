"""Current Greenhouse upload ownership across native/synthetic field refs."""
import copy
import hashlib

import pytest

from jhb.applications import boards
from jhb.applications.live_review import project

URL = 'https://job-boards.greenhouse.io/synthetic/jobs/123'


@pytest.fixture
def uploads(tmp_path):
    from pypdf import PdfWriter
    rows, controls = [], []
    for key, label, native_ref in [('documents.resume', 'Resume/CV', 'resume'),
                                   ('documents.cover_letter', 'Cover Letter', 'cover_letter')]:
        for generation, width in [('old', 612), ('current', 640)]:
            path = tmp_path / generation / (native_ref + '.pdf')
            path.parent.mkdir(exist_ok=True)
            pdf = PdfWriter(); pdf.add_blank_page(width=width, height=792); pdf.write(path)
            rows.append({'key': key, 'question': label, 'type': 'file',
                         'ref': native_ref if generation == 'old' else 'uploaded:' + label,
                         'value': str(path), 'source': 'synthetic verified upload',
                         'upload_receipt': generation + '-' + native_ref,
                         'document_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        controls.append({'field': {'ref': 'uploaded:' + label, 'label': label, 'type': 'file',
                                   'required': key == 'documents.resume'},
                         'state': {'value': path.name, 'receipt': 'current-' + native_ref}})
    packet = {'job': {'url': URL, 'dedupe_hash': boards.application_hash(URL)}, 'filled': rows, 'events': []}
    observation = {'url': URL, 'target_id': 'owned-fixture', 'controls': controls,
                   'buttons': [{'label': 'Submit Application'}], 'read_only': True}
    return packet, observation


@pytest.mark.parametrize('reverse', [False, True])
def test_current_control_receipts_disambiguate_snapshot_history_without_editing_it(uploads, reverse):
    packet, observation = uploads
    if reverse:
        packet['filled'].reverse()
    original = copy.deepcopy(packet); before = copy.deepcopy(observation)
    result = project(packet, observation)
    assert packet == original and observation == before
    assert len(packet['filled']) == 4 and len(result['filled']) == 2
    for row in result['filled']:
        current = next(old for old in packet['filled'] if old['key'] == row['key'] and old['ref'] == row['ref'])
        assert row['value'] == current['value'] and row['upload_receipt'] == current['upload_receipt']
        assert row['document_sha256'] == current['document_sha256']
    assert result['state'] == 'waiting_review' and result['submitted'] is False


@pytest.mark.parametrize('problem', ['duplicate_current', 'conflicting_current', 'receipt', 'hash', 'changed_pdf',
                                    'unrecorded_replacement', 'new_filename', 'wrong_semantic_key'])
def test_ambiguous_changed_or_replaced_current_upload_never_falls_back_to_old_row(uploads, problem):
    packet, observation = uploads
    old, current = packet['filled'][:2]
    if problem in {'duplicate_current', 'conflicting_current'}:
        duplicate = copy.deepcopy(current)
        if problem == 'conflicting_current': duplicate['upload_receipt'] = 'conflicting'
        packet['filled'].append(duplicate)
    elif problem == 'receipt':
        # An old row matching the receipt must not override a conflicting row
        # already bound to this exact current control.
        old['upload_receipt'] = observation['controls'][0]['state']['receipt']
        current['upload_receipt'] = 'wrong-current-receipt'
    elif problem == 'hash': current['document_sha256'] = '0' * 64
    elif problem == 'changed_pdf':
        from pathlib import Path
        path = Path(current['value']); path.write_bytes(path.read_bytes() + b'\nchanged')
    elif problem == 'unrecorded_replacement': observation['controls'][0]['state']['receipt'] = 'new-user-upload'
    elif problem == 'new_filename': observation['controls'][0]['state']['value'] = 'replacement.pdf'
    else: current['key'] = 'documents.cover_letter'
    before, observed = copy.deepcopy(packet), copy.deepcopy(observation)
    with pytest.raises(ValueError, match='document verification'):
        project(packet, observation)
    assert packet == before and observation == observed


@pytest.mark.parametrize('ambiguous', [False, True])
def test_unique_semantic_fallback_only_when_exact_current_ref_is_absent(uploads, ambiguous):
    packet, observation = uploads
    # Legacy preparation packets can record only the original native input.
    packet['filled'] = [row for row in packet['filled'] if row['ref'].startswith('uploaded:')]
    for row in packet['filled']:
        row['ref'] = row['ref'].replace('uploaded:', 'legacy:')
    if ambiguous:
        packet['filled'].append(copy.deepcopy(packet['filled'][0]))
        with pytest.raises(ValueError, match='document verification'):
            project(packet, observation)
    else:
        assert len(project(packet, observation)['filled']) == 2
