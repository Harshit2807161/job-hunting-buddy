"""Submission audits permit decimal serialization only for native number fields."""
import pytest

from jhb.applications import submission_runtime as runtime


@pytest.mark.parametrize('actual,expected',[
    ('170000',170000.0),('170000.0',170000),('1.7e5',170000),('0.10','0.100'),('-12.50',-12.5),
    ('9007199254740993','9007199254740993.0'),
])
def test_exact_decimal_equivalence(actual,expected):
    assert runtime._same_native_number(actual,expected)


@pytest.mark.parametrize('actual,expected',[
    ('170001',170000.0),('9007199254740993',9007199254740992),('0.10000000000000001','0.1'),
    ('170000',True),('170000',False),(170000,170000),('',170000),('1_70000',170000),
    ('170000 dollars',170000),(' 170000',170000),('NaN','NaN'),('Infinity',float('inf')),
    ('170000',float('nan')),('0','-Infinity'),('1e9999999999999999999999999999',1),('1'*129,'1'*129),
])
def test_nonfinite_nonnumeric_boolean_or_changed_values_still_fail(actual,expected):
    assert not runtime._same_native_number(actual,expected)


@pytest.mark.parametrize('kind,actual,invalid,passes',[
    ('number','170000',False,True),('number','170001',False,False),
    ('number','170000',True,False),('text','170000',False,False),('text','170000.0',False,True),
])
def test_retained_audit_changes_only_native_number_comparison(monkeypatch,kind,actual,invalid,passes):
    url='https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555/application'
    field={'ref':'salary','label':'Salary Expectations','type':kind,'required':True}
    snapshot={'fields':[field],'buttons':[{'ref':'1','label':'Submit application'}]}
    monkeypatch.setattr(runtime,'_board_dispatch',lambda *args:snapshot)
    monkeypatch.setattr(runtime,'_control_state',lambda *args:{'value':actual,'invalid':invalid})
    monkeypatch.setattr(runtime,'_native_form_submit',lambda *args:True)
    helpers={'current_tab':lambda:{'targetId':'synthetic'},
             'js':lambda code:url if code=='location.href' else code=='window.__jhbGuard===true'}
    packet={'job':{'url':url},'filled':[{'ref':'salary','question':'Salary Expectations','key':'preferences.salary',
                                     'value':170000.0,'source':'Synthetic candidate salary expectation'}]}
    result=runtime._checks({'target_id':'synthetic','documents':{}},helpers,packet,{'application_url':url})
    if passes:assert result['double_check_count']==1
    else:assert result['state']=='waiting_review' and result['click_started'] is False
