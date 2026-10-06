"""Synthetic native education-row retention; no live candidate actions."""
import pytest
from jhb.applications.cli_runtime import dispatch


@pytest.mark.parametrize('mode', ['normal','background','delayed','target_changed','guard_changed',
                                  'changed_after_activation','guard_after_activation','extra_delayed_rows',
                                  'still_missing','visible_failure','unknown_click'])
def test_only_failed_hidden_education_postcondition_wakes_once_and_never_duplicates(mode):
    from playwright.sync_api import sync_playwright
    url='https://job-boards.greenhouse.io/synthetic/jobs/1234'
    html='''<form><div class=education--container><input id=school--0>
    <button type=button onclick="window.clicks++;if(window.awake)add()">Add another</button></div>
    <button type=submit>Submit</button></form><script>
    window.clicks=0;window.awake=false;window.submissions=0;
    function add(){const e=document.createElement('input');e.id='school--'+document.querySelectorAll('input').length;document.querySelector('.education--container button').before(e)}
    document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submissions++};</script>'''
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        try:
            page=browser.new_page();page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=html));page.goto(url)
            session=page.context.new_cdp_session(page)
            state={'targetId':'owned','url':url};activations=[];scrolls=[]
            def cdp(method,**params):
                if method=='DOM.scrollIntoViewIfNeeded':scrolls.append(params['backendNodeId'])
                if method=='Input.dispatchMouseEvent' and params.get('type')=='mousePressed' and mode=='unknown_click':
                    session.send(method,params)
                    raise TimeoutError('Unknown actual click result')
                return session.send(method,params)
            def js(expression):
                if expression=='document.visibilityState':
                    if mode=='target_changed':state['targetId']='other'
                    if mode=='guard_changed':page.evaluate('window.__jhbGuard=false')
                    return 'visible' if mode in {'normal','visible_failure'} else 'hidden'
                return page.evaluate(expression)
            def activate(target):
                activations.append(target)
                if mode=='changed_after_activation':state['targetId']='other'
                if mode=='guard_after_activation':page.evaluate('window.__jhbGuard=false')
                if mode!='still_missing':page.evaluate('window.awake=true')
                if mode in {'delayed','extra_delayed_rows'}:page.evaluate('add()')
                if mode=='extra_delayed_rows':page.evaluate('add()')
            helpers={'cdp':cdp,'js':js,'wait':lambda seconds:page.wait_for_timeout(seconds*1000),
                     'current_tab':lambda:dict(state),'switch_tab':lambda target:None,
                     'list_tabs':lambda:[dict(state)],'activate_tab':activate,'click_at_xy':page.mouse.click}
            dispatch({'operation':'open','url':url},helpers)
            if mode=='normal':page.evaluate('window.awake=true')
            request={'operation':'education','count':2,'target_id':'owned','expected_url':url}
            if mode in {'normal','background','delayed'}:
                assert dispatch(request,helpers)=={'supported':True,'rows':2}
                # Repeated ensure sees the row already retained; no extra click.
                assert dispatch(request,helpers)=={'supported':True,'rows':2}
                assert page.locator('input').count()==2
            else:
                with pytest.raises((ValueError,TimeoutError)):dispatch(request,helpers)
            expected_clicks=2 if mode in {'background','still_missing'} else 0 if mode=='unknown_click' else 1
            assert page.evaluate('window.clicks')==expected_clicks
            assert len(scrolls)==(2 if mode in {'background','still_missing'} else 1)
            assert activations==(['owned'] if mode in {'background','delayed','changed_after_activation','guard_after_activation','guard_changed','extra_delayed_rows','still_missing'} else [])
            assert page.evaluate('window.submissions')==0
        finally:browser.close()
