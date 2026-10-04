"""Compose full review captures from the top using official native scrolling."""
from __future__ import annotations

import math
from pathlib import Path
import time

from .boards import job_identity

WHEEL_POINT = r"""(()=>{
 const w=innerWidth,h=innerHeight;
 for(const [x,y] of [[8,h*.5],[w-8,h*.5],[w*.5,h*.5],[8,h*.35],[w-8,h*.65]]){
   const e=document.elementFromPoint(x,y);
   if(!e||e.closest('input,textarea,select,button,a,iframe,[contenteditable=true],[role=combobox],[role=listbox],[role=option]'))continue;
   let blocked=false;
   for(let p=e;p&&p!==document.scrollingElement;p=p.parentElement){
     const s=getComputedStyle(p);
     if(['sticky','fixed'].includes(s.position)||
        (/(auto|scroll)/.test(s.overflowY)&&p.scrollHeight>p.clientHeight)){blocked=true;break;}
   }
   if(!blocked&&x>0&&x<w&&y>0&&y<h)return {x,y};
 }
 return null;
})()"""


def capture_from_top(request, helpers):
    """Only scroll the attached exact job; never type, hide UI, or click controls."""
    expected = job_identity(request.get('expected_url') or request.get('approved_url') or
                            request.get('scope', {}).get('origin', '')+request.get('scope', {}).get('path', ''))
    target = request.get('target_id')
    cdp, js, wait = helpers['cdp'], helpers['js'], helpers['wait']
    def guard():
        if (not target or not expected or helpers['current_tab']().get('targetId') != target
                or job_identity(js('location.href')) != expected):
            raise ValueError('Review capture target changed from the exact application')
    def position():
        metrics = cdp('Page.getLayoutMetrics')
        viewport = metrics.get('cssLayoutViewport') or metrics.get('layoutViewport', {})
        values = {key: viewport.get(key) for key in ('pageX','pageY','clientWidth','clientHeight')}
        if not all(type(v) in {int,float} and math.isfinite(v) for v in values.values()):
            raise ValueError('Review capture scroll geometry is unavailable')
        return values
    guard()
    deadline, stable, emulated = time.monotonic()+12, 0, False
    try:
        for _ in range(12):
            guard(); value = position()
            if abs(value['pageX']) <= .5 and abs(value['pageY']) <= .5:
                stable += 1
                if stable >= 3:
                    break
                wait(.05); continue
            stable = 0
            if time.monotonic() >= deadline:
                raise ValueError('Review capture page did not settle at the top')
            point = js(WHEEL_POINT)
            if (not isinstance(point, dict) or not all(type(point.get(k)) in {int,float}
                    and math.isfinite(point[k]) for k in ('x','y'))
                    or not 0 < point['x'] < value['clientWidth'] or not 0 < point['y'] < value['clientHeight']):
                raise ValueError('Review capture has no unobstructed native scroll point')
            args = {'x':point['x'],'y':point['y'], 'dy':-value['pageY']-value['clientHeight'],
                    'dx':-value['pageX'] if abs(value['pageX'])>.5 else 0}
            guard()
            try:
                helpers['scroll'](**args)
            except (TimeoutError, RuntimeError) as exc:
                if emulated or not (isinstance(exc, TimeoutError) or 'timed out' in str(exc)):
                    raise
                # Current official scrolling guidance permits one native retry
                # with focus emulation, preserving the user's visible tab.
                guard(); emulated=True; cdp('Emulation.setFocusEmulationEnabled', enabled=True)
                helpers['scroll'](**args)
            wait(.1)
        else:
            raise ValueError('Review capture page did not settle at the top')
        guard()
        path = Path(request['path'])
        helpers['capture_screenshot'](str(path), full=True)
        guard()
        path.chmod(0o600)
        return {'screenshot':str(path),'composition':'settled_page_top','full_page':True}
    finally:
        if emulated:
            cdp('Emulation.setFocusEmulationEnabled', enabled=False)
