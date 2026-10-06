"""Deterministic observed repeater binding preserves original record indexes."""
import pytest
from jhb.applications.booklet import answer
from jhb.applications.planner import key_for_field

@pytest.mark.parametrize('column,kind',[('title','text'),('company','text'),('location','text'),('summary','textarea'),('current','checkbox'),('start_date','date'),('end_date','date')])
def test_generated_workday_row_ids_bind_the_second_verified_original_experience(column,kind):
    answers={f'experience.{i}.{column}':answer(f'record-{i}','synthetic chosen resume') for i in [0,1]}
    field={'ref':'workday-generated-row-23','label':'Provider label','type':kind,'record_kind':'experience','record_index':1,'record_column':column}
    assert key_for_field(field,answers)=='experience.1.'+column

@pytest.mark.parametrize('metadata',[{'record_kind':'experience','record_index':True,'record_column':'title'},
                                   {'record_kind':'experience','record_index':-1,'record_column':'title'},
                                   {'record_kind':'experience','record_index':10,'record_column':'title'},
                                   {'record_kind':'experience','record_index':0,'record_column':'citizenship'},
                                   {'record_kind':'profile','record_index':0,'record_column':'title'}])
def test_unrecognized_repeater_metadata_cannot_bind_sensitive_or_other_record(metadata):
    field={'ref':'generated','label':'Unsupported question','type':'text',**metadata}
    assert key_for_field(field,{'experience.0.title':answer('Engineer','synthetic')}) is None


def test_date_and_checkbox_types_are_required_for_structured_columns():
    field={'ref':'generated','label':'Unknown control','record_kind':'experience','record_index':0}
    answers={'experience.0.current':answer(False,'synthetic verified original resume'),'experience.0.start_date':answer('2025-02','synthetic verified original resume')}
    assert key_for_field({**field,'record_column':'current','type':'text'},answers) is None
    assert key_for_field({**field,'record_column':'start_date','type':'text'},answers) is None

@pytest.mark.parametrize('metadata',[{'record_kind':[],'record_index':0,'record_column':'title'},
                                   {'record_kind':'experience','record_index':0,'record_column':[]}])
def test_malformed_repeater_metadata_is_not_a_binding(metadata):
    assert key_for_field({'ref':'generated','label':'Unknown','type':'text',**metadata},{'experience.0.title':answer('Engineer','synthetic')}) is None
