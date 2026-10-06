import pytest

from jhb.applications import linkedin_runtime as runtime


@pytest.mark.parametrize('url',[
    'http://www.linkedin.com/jobs/view/123/', 'https://attacker.example/jobs/view/123/',
    'https://www.linkedin.com.evil.example/jobs/view/123/', 'https://user@www.linkedin.com/jobs/view/123/',
    'https://www.linkedin.com/jobs/', 'https://www.linkedin.com/analytics/profile-views/',
])
def test_wrong_or_non_job_scope_cannot_navigate(url):
    assert runtime.linkedin_id(url) is None
    with pytest.raises(ValueError,match='exact LinkedIn'):
        runtime.dispatch({'operation':'resolve','approved_url':url},{})


def test_numeric_and_title_slug_paths_are_same_job():
    assert runtime.linkedin_id('https://www.linkedin.com/jobs/view/1234567890/')=='1234567890'
    assert runtime.linkedin_id('https://www.linkedin.com/jobs/view/engineer-at-example-1234567890/')=='1234567890'


def test_linkedin_safety_link_decodes_exact_https_destination_without_private_targets():
    assert runtime.outbound('https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fjobs.lever.co%2Fexample%2Fjob')=='https://jobs.lever.co/example/job'
    assert runtime.outbound('https://www.linkedin.com/safety/go/?url=https%3A%2F%2Flocalhost%2Fadmin') is None
    assert runtime.outbound('https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fexample.org&url=https%3A%2F%2Fevil.example') is None


def test_resolver_never_exposes_form_fill_or_submit_operations():
    with pytest.raises(ValueError,match='does not fill or submit'):
        runtime.dispatch({'operation':'submit','approved_url':'https://www.linkedin.com/jobs/view/123/'},{})


def helpers(control_names):
    tabs=[{'targetId':'owned','url':'https://www.linkedin.com/jobs/view/123/'}]
    return {'list_tabs':lambda:tabs,'switch_tab':lambda t:None,'wait':lambda t:None,
        'current_tab':lambda:tabs[0],
        'js':lambda expression:'https://www.linkedin.com/jobs/view/123/',
        'cdp':lambda *args,**kwargs:{'nodes':[{'backendDOMNodeId':i+1,'role':{'value':'button'},'name':{'value':label}} for i,label in enumerate(control_names)]}}


def test_easy_apply_is_classified_without_clicking_or_opening_modal():
    h=helpers(['Easy Apply'])
    r=runtime.dispatch({'operation':'resolve','approved_url':'https://www.linkedin.com/jobs/view/123/'},h)
    assert r['board_type']=='linkedin_easy_apply' and r['application_url'].endswith('/123/')


def test_two_apply_controls_are_ambiguous_and_neither_is_clicked():
    r=runtime.dispatch({'operation':'resolve','approved_url':'https://www.linkedin.com/jobs/view/123/'},helpers(['Apply','Easy Apply']))
    assert r['state']=='ambiguous'
