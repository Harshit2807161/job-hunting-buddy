"""Worker row decorations do not change an exact native custom answer binding."""
import copy
import hashlib
import json

import pytest

from jhb.applications import submission_runtime
from jhb.applications.authorized_submission import document_manifest
from jhb.applications.booklet import write_private
from tests.applications.test_authorized_submission import HTML, evidence, synthetic_runtime


SOURCE = {'kind': 'employer_category_projection', 'candidate_confirmation': False,
          'actual_value': 'Synthetic original major', 'category_only': True,
          'original_source': 'Synthetic verified education record'}
NATIVE = {'ref': 'discipline--0', 'label': 'Discipline*', 'type': 'select', 'required': True,
          'options': [{'label': 'Synthetic Computing Category'}, {'label': 'Other'}],
          'description': '', 'description_truncated': False}
ROW = {'ref': 'discipline--0', 'question': 'Discipline* (education record 1)',
       'key': 'custom.catalog.example.major', 'value': 'Synthetic Computing Category',
       'source': SOURCE, 'proposed': True}
INVENTORY = {'ref': 'discipline--0', 'question': 'Discipline*', 'type': 'select', 'required': True,
             'choices': ['Synthetic Computing Category', 'Other'], 'description': '',
             'description_truncated': False, 'status': 'answered',
             'answer_key': ROW['key'], 'source': SOURCE, 'proposed': True}
CONTROL = '''<label for="discipline--0">Discipline*</label>
<select id="discipline--0" required><option selected>Synthetic Computing Category</option><option>Other</option></select>'''


@pytest.mark.parametrize('column', ['school', 'degree', 'discipline', 'start_date', 'end_date'])
def test_only_expected_indexed_worker_decoration_is_equivalent(column):
    field = {**NATIVE, 'ref': column+'--2'}
    row = {**ROW, 'ref': field['ref'], 'question': 'Discipline* (education record 3)'}
    inventory = [{**INVENTORY, 'ref': field['ref']}]
    assert submission_runtime._retained_question_matches(row, field, inventory)
    for suffix in ('1', '2', '4', '03', '-3', '3.0'):
        assert not submission_runtime._retained_question_matches(
            {**row, 'question': 'Discipline* (education record '+suffix+')'}, field, inventory)


@pytest.mark.parametrize('change', ['ref', 'question', 'type', 'required', 'description', 'truncated',
    'choices', 'source', 'key', 'blank', 'missing_inventory', 'duplicate_inventory', 'non_education_ref'])
def test_translation_requires_unique_unchanged_native_metadata_and_provenance(change):
    field, row, inventory = copy.deepcopy(NATIVE), copy.deepcopy(ROW), [copy.deepcopy(INVENTORY)]
    if change == 'ref': field['ref'] = 'discipline--1'
    if change == 'question': field['label'] = 'Exact transcript major*'
    if change == 'type': field['type'] = 'combobox'
    if change == 'required': field['required'] = False
    if change == 'description': field['description'] = 'Enter your exact CIP code.'
    if change == 'truncated': field['description_truncated'] = True
    if change == 'choices': field['options'].append({'label': 'New category'})
    if change == 'source': row['source'] = {**SOURCE, 'actual_value': 'Changed original major'}
    if change == 'key': row['key'] = 'custom.catalog.different.major'
    if change == 'blank': inventory[0]['status'] = 'blank'
    if change == 'missing_inventory': inventory = []
    if change == 'duplicate_inventory': inventory += copy.deepcopy(inventory)
    if change == 'non_education_ref':
        field['ref'] = row['ref'] = inventory[0]['ref'] = 'screening-question'
    assert not submission_runtime._retained_question_matches(row, field, inventory)


@pytest.mark.parametrize('change', [None, 'wrong_suffix', 'question', 'options', 'retained_value',
                                  'source', 'packet_value_after_binding', 'packet_source_after_binding'])
def test_native_submission_check_preserves_custom_value_without_accepting_drift(tmp_path, monkeypatch, change):
    job, packet, pp, manifest, auth, ap = evidence(tmp_path, monkeypatch)
    row, inventory = copy.deepcopy(ROW), copy.deepcopy(INVENTORY)
    if change == 'wrong_suffix': row['question'] = 'Discipline* (education record 2)'
    if change == 'source': row['source']['original_source'] = 'Changed source'
    packet['filled'].append(row)
    packet['review_inventory'] = {'complete': True, 'fields': [inventory]}
    write_private(pp, packet)
    attempt = json.loads(ap.read_text())
    attempt['packet_sha256'] = hashlib.sha256(pp.read_bytes()).hexdigest()
    write_private(ap, attempt)
    before = pp.read_bytes()
    context = {'authorization_path': auth['authorization_path'], 'attempt_path': str(ap)}
    if change == 'packet_value_after_binding': packet['filled'][-1]['value'] = 'Other'; write_private(pp, packet)
    if change == 'packet_source_after_binding': packet['filled'][-1]['source']['actual_value'] = 'Changed'; write_private(pp, packet)
    html = HTML.replace('<button type="submit">Submit application', CONTROL+'<button type="submit">Submit application')
    with synthetic_runtime(html=html) as (call, _, inspect, _, target, other_guard, _):
        if change in {'packet_value_after_binding', 'packet_source_after_binding'}:
            with pytest.raises(ValueError, match='changed'):
                call({'operation': 'locate', **context})
        else:
            assert call({'operation': 'locate', **context})['target_id'] == target
            if change == 'question': inspect("document.querySelector('label[for=\"discipline--0\"]').textContent='Exact transcript major*'")
            if change == 'options': inspect("document.getElementById('discipline--0').add(new Option('New category','new'))")
            if change == 'retained_value': inspect("document.getElementById('discipline--0').value='Other'")
            result = call({'operation': 'check', **context, 'target_id': target,
                           'documents': document_manifest(manifest, packet), 'require_receipts': False,
                           'approved_phone_national': manifest['approved_phone_national']})
            if change is None:
                assert not result.get('state') and result['double_check_count'] == 11
                assert next(f for f in result['fields'] if f['ref'] == 'discipline--0')['answer_key'] == ROW['key']
                assert inspect("document.getElementById('discipline--0').value") == ROW['value']
            else:
                assert result['state'] in {'waiting_input', 'waiting_review'} and result['click_started'] is False
            assert pp.read_bytes() == before
        assert inspect('window.submissions') == 0
        assert other_guard() is True
