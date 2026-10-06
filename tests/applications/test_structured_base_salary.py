"""Exact public JobPosting salary metadata survives the real source boundary."""
import asyncio
import copy
import io
import json

import pytest

from jhb.applications import greenhouse_source,job_context,source_refresh

URL='https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555'
OTHER=URL.replace('11111111','99999999')
TEXT='Build reliable backend services using Python, SQL and cloud infrastructure. Work with a product team on reliable customer-facing applications.'


def record():
    return {'@type':'JobPosting','url':URL,'identifier':{'value':'11111111-2222-3333-4444-555555555555'},
            'title':'Synthetic Software Engineer','description':TEXT,'jobLocation':{'address':{'addressCountry':'US'}},
            'baseSalary':{'@type':'MonetaryAmount','currency':'USD','value':{
                '@type':'QuantitativeValue','unitText':'YEAR','minValue':125000,'maxValue':155000}}}


def test_public_metadata_range_reaches_cached_worker_context_without_salary_prose():
    html='<script type="application/ld+json">'+json.dumps(record())+'</script>'
    d=job_context.fetch_public_description(URL,opener=lambda *a,**k:io.BytesIO(html.encode()))
    def forbidden(*a,**k):pytest.fail('Fresh exact metadata should avoid another network request')
    result=asyncio.run(source_refresh.refresh({'url':URL,'verified_job_description':d},fetcher=forbidden,resolver=forbidden))
    ranges=result['context']['advertised_salary_ranges']
    assert len(ranges)==1 and (ranges[0]['lower'],ranges[0]['upper'])==(125000,155000)
    assert ranges[0]['source_url']==URL and ranges[0]['job_identity']==d['job_identity']
    assert 'salary' not in TEXT.lower()


@pytest.mark.parametrize('change',['wrong_identifier','wrong_url','unbound','cad','hour','monthly','reversed','boolean',
                                  'nonnumeric','nonfinite','missing_upper','single_value','oversized'])
def test_ambiguous_or_nonannual_usd_salary_does_not_become_an_answer(change):
    r=record();v=r['baseSalary']['value']
    if change=='wrong_identifier':r['identifier']['value']='unrelated-job'
    if change=='wrong_url':r['url']=OTHER
    if change=='unbound':r.pop('url');r.pop('identifier')
    if change=='cad':r['baseSalary']['currency']='CAD'
    if change=='hour':v['unitText']='HOUR'
    if change=='monthly':v['unitText']='MONTH'
    if change=='reversed':v['minValue']=175000
    if change=='boolean':v['minValue']=True
    if change=='nonnumeric':v['maxValue']='155000 dollars'
    if change=='nonfinite':v['maxValue']=float('inf')
    if change=='missing_upper':v.pop('maxValue')
    if change=='single_value':v.clear();v.update(unitText='YEAR',value=140000)
    if change=='oversized':v['maxValue']=10000000
    assert job_context.structured_base_salary(r,URL)==[]


def test_isolated_observer_keeps_salary_metadata_and_conflicting_ranges_separate():
    r=record();second=copy.deepcopy(r);second['baseSalary']['value'].update(minValue=165000,maxValue=185000)
    d=greenhouse_source._observed_description({'url':URL,'job_postings':[r,second]})
    assert d and len(d['advertised_salary_ranges'])==2
    assert len(source_refresh._context(d)['advertised_salary_ranges'])==2
    wrong=record();wrong['identifier']['value']='unrelated-job'
    assert greenhouse_source._observed_description({'url':URL,'job_postings':[wrong]}) is None


@pytest.mark.parametrize('change',['source','identity','currency','period','range'])
def test_cached_metadata_cannot_borrow_another_job_or_an_invalid_range(change):
    d=greenhouse_source._observed_description({'url':URL,'job_postings':[record()]});salary=d['advertised_salary_ranges'][0]
    if change=='source':salary['source_url']=OTHER
    if change=='identity':salary['job_identity'][-1]='other-job'
    if change=='currency':salary['currency']='AUD'
    if change=='period':salary['period']='monthly'
    if change=='range':salary['lower']=salary['upper']+1
    assert source_refresh._context(d)['advertised_salary_ranges']==[]


def test_same_range_in_prose_and_structured_metadata_deduplicates_with_provenance():
    d=greenhouse_source._observed_description({'url':URL,'job_postings':[record()]})
    d['text']+=' Annual USD base salary range: $125,000 - $155,000.'
    ranges=source_refresh._context(d)['advertised_salary_ranges']
    assert len(ranges)==1 and ranges[0]['source_url']==URL


def test_fixed_dom_observer_extracts_only_explicit_jobposting_fields():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page()
        html='<h1>Synthetic Software Engineer</h1><script type="application/ld+json">'+json.dumps(record())+'</script>'
        page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=html));page.goto(URL)
        observed=page.evaluate(greenhouse_source._OBSERVE)
        assert observed['job_postings'][0]['baseSalary']==record()['baseSalary']
        assert observed['job_postings'][0]['identifier']==record()['identifier']
        d=greenhouse_source._observed_description(observed)
        assert source_refresh._context(d)['advertised_salary_ranges'][0]['lower']==125000
        browser.close()
