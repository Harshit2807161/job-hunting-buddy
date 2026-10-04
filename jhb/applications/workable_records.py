"""Bounded Workable record editors observed in the live manual validation.

Only Add/Edit/Update actions are exposed. Each saved record is reopened and
read back; an unsupported editor stops with the guarded draft preserved.
"""
import json
import re
from datetime import date

from .cli_runtime import _settled_click


def prepare_records(request, helpers):
    cdp, js, wait = helpers['cdp'], helpers['js'], helpers['wait']
    check_only=request.get('check_only',False)
    if not isinstance(check_only,bool):raise ValueError('Record audit mode must be boolean')
    def backend(expr):
        obj=cdp('Runtime.evaluate',expression=expr)['result'].get('objectId')
        if not obj:raise ValueError('Workable record control is unavailable')
        try:return cdp('DOM.describeNode',objectId=obj)['node']['backendNodeId']
        finally:cdp('Runtime.releaseObject',objectId=obj)
    def buttons(label):
        return [n for n in cdp('Accessibility.getFullAXTree')['nodes'] if not n.get('ignored')
                and n.get('role',{}).get('value')=='button' and n.get('name',{}).get('value','').strip()==label
                and n.get('backendDOMNodeId')]
    def click(label):
        found=buttons(label)
        if len(found)!=1:raise ValueError('Workable record button is absent or ambiguous')
        _settled_click(found[0]['backendDOMNodeId'],cdp,wait,helpers['click_at_xy']);wait(.2)
    def expr(selector):
        return "(()=>{const a=[...document.querySelectorAll("+json.dumps(selector)+")].filter(e=>!e.disabled&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden');return a.length===1?a[0]:null})()"
    def value(selector):return js('('+expr(selector)+')?.value??null')
    def type_value(selector,wanted):
        expression=expr(selector);node=backend(expression)
        cdp('DOM.focus',backendNodeId=node)
        if not js('document.activeElement===('+expression+')'):raise ValueError('Workable record input did not receive focus')
        cdp('Input.dispatchKeyEvent',type='keyDown',key='a',code='KeyA',modifiers=4,commands=['selectAll'])
        cdp('Input.dispatchKeyEvent',type='keyUp',key='a',code='KeyA')
        cdp('Input.dispatchKeyEvent',type='keyDown',key='Backspace',code='Backspace',windowsVirtualKeyCode=8)
        cdp('Input.dispatchKeyEvent',type='keyUp',key='Backspace',code='Backspace',windowsVirtualKeyCode=8)
        cdp('Input.insertText',text=wanted)
        cdp('Input.dispatchKeyEvent',type='keyDown',key='Tab',code='Tab',windowsVirtualKeyCode=9)
        cdp('Input.dispatchKeyEvent',type='keyUp',key='Tab',code='Tab',windowsVirtualKeyCode=9)
        wait(.2)
        if value(selector)!=wanted:raise ValueError('Workable record input did not retain the verified value')
    def month(raw):
        if not isinstance(raw,str) or not re.fullmatch(r'\d{4}-\d{2}(?:-\d{2})?',raw):
            raise ValueError('Verified record date is unsupported')
        date.fromisoformat(raw if len(raw)==10 else raw+'-01')
        return raw[5:7]+'/'+raw[:4]
    filled=[]
    for kind,records in [('education',request.get('education',[])),('experience',request.get('experience',[]))]:
        if not isinstance(records,list) or len(records)>10:raise ValueError('Record count is unsupported')
        add='Add Education' if kind=='education' else 'Add Experience'
        first='#school' if kind=='education' else '#company'
        # Some Workable applications omit these optional sections entirely.
        # Their absence is not an instruction to navigate another form.
        if not check_only and not buttons(add) and value(first) is None and not any(buttons('Edit '+str(r.get('school' if kind=='education' else 'company',''))) for r in records):
            continue
        for record in records:
            if not isinstance(record,dict) or record.get('status')!='verified' or not record.get('source'):
                raise ValueError('Workable records require verified provenance')
            columns={'school':'#school','major':'#field_of_study','degree':'#degree'} if kind=='education' else {
                'company':'#company','title':'#title','summary':'textarea[name="summary"]:not([data-ui="summary"])'}
            if any(not isinstance(record.get(key),str) or not record[key].strip() for key in columns):
                raise ValueError('Verified record is incomplete')
            title=record['school' if kind=='education' else 'company']
            edit='Edit '+title
            if check_only and (value(first) is not None or len(buttons(edit))!=1):
                raise ValueError('Saved Workable record cannot be independently audited without changing an open editor')
            if buttons(edit):click(edit)
            elif value(first) is None:click(add)
            for _ in range(10):
                if value(first) is not None:break
                wait(.15)
            if value(first) is None:raise ValueError('Workable record editor did not open')
            expected={selector:record[key] for key,selector in columns.items()}
            expected['input[name="start_date"]']=month(record['start_date'])
            if record.get('current') is True:
                # A hidden end date is legitimate only if the original record
                # explicitly says current; no inferred current-employment click.
                current='input[type="checkbox"][name="current"]'
                if not check_only and not js('('+expr(current)+')?.checked===true'):
                    node=backend(expr(current));_settled_click(node,cdp,wait,helpers['click_at_xy']);wait(.15)
                if not js('('+expr(current)+')?.checked===true'):raise ValueError('Current-employment choice was not retained')
            else:
                if record.get('current') is False:
                    # End-date visibility alone does not prove a completed
                    # record: some editors expose it while "current" remains
                    # checked. Do not infer False for records lacking this fact.
                    current="(()=>{const a=[...document.querySelectorAll('input[type=checkbox][name=current]')];if(a.length>1)throw Error('Ambiguous current-employment control');return a[0]??null})()"
                    if js('('+current+')?.checked===true'):
                        if check_only:raise ValueError('Saved completed employment is incorrectly marked current')
                        node=backend(current);_settled_click(node,cdp,wait,helpers['click_at_xy']);wait(.15)
                        if not js('('+current+')?.checked===false'):
                            raise ValueError('Completed-employment choice was not retained')
                expected['input[name="end_date"]']=month(record['end_date'])
            if not check_only:
                for selector,wanted in expected.items():type_value(selector,wanted)
                click('Update')
                for _ in range(12):
                    if buttons(edit) and value(first) is None:break
                    wait(.2)
                if value(first) is not None or len(buttons(edit))!=1:
                    raise ValueError('Workable record was not saved; preserve the editor for review')
                click(edit)
                for _ in range(10):
                    if value(first) is not None:break
                    wait(.15)
            if any(value(selector)!=wanted for selector,wanted in expected.items()):
                raise ValueError('Saved Workable record did not retain its verified fields')
            if record.get('current') is False and js('('+current+')?.checked===true'):
                raise ValueError('Saved completed employment is incorrectly marked current')
            click('Cancel' if check_only else 'Update')
            for _ in range(12):
                if value(first) is None and len(buttons(edit))==1:break
                wait(.2)
            if value(first) is not None or len(buttons(edit))!=1:
                raise ValueError('Workable record editor did not close after retained verification')
            for key in columns:
                filled.append({'question':kind.title()+' '+key,'ref':f'workable:{kind}:{record["index"]}:{key}',
                               'key':f'{kind}.{record["index"]}.{key}','value':record[key],'source':record['source']})
            for key in ('start_date','end_date'):
                if key=='end_date' and record.get('current') is True:continue
                filled.append({'question':kind.title()+' '+key,'ref':f'workable:{kind}:{record["index"]}:{key}',
                               'key':f'{kind}.{record["index"]}.{key}','value':record[key],'source':record['source']})
    return {'supported':True,'verified':True,'filled':filled}
