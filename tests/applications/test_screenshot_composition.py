"""Synthetic native scrolling/capture; no candidate Chrome or image editing."""
import base64
import io
import os
from pathlib import Path

import pytest

from jhb.applications import cli_runtime
from jhb.applications.screenshot_runtime import capture_from_top

URL = 'https://job-boards.greenhouse.io/synthetic/jobs/123'
IMAGE = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC')


class NativeHelpers:
    def __init__(self, path, mode='normal'):
        self.y, self.mode, self.path = 400, mode, path
        self.url, self.target = URL, 'owned'
        self.scrolls, self.focus, self.captures = [], [], []
    def cdp(self, method, **args):
        if method == 'Emulation.setFocusEmulationEnabled':
            self.focus.append(args['enabled']); return {}
        assert method == 'Page.getLayoutMetrics'
        return {'cssLayoutViewport':{'pageX':0,'pageY':self.y,'clientWidth':800,'clientHeight':500}}
    def scroll(self, **args):
        self.scrolls.append(args)
        if self.mode == 'timeout' and len(self.scrolls) == 1: raise TimeoutError('Synthetic native scroll timeout')
        if self.mode == 'retry_timeout': raise TimeoutError('Synthetic retry timeout')
        if self.mode == 'redirect': self.url = URL.replace('/123','/124')
        if self.mode != 'stalled': self.y = 0
    def capture(self, path, *, full):
        assert full is True and self.y == 0
        self.captures.append(path); self.path.write_bytes(IMAGE)
    def helpers(self):
        return {'cdp':self.cdp,'js':lambda expression:self.url if expression=='location.href' else {'x':8,'y':250},
                'wait':lambda seconds:None,'scroll':self.scroll,'capture_screenshot':self.capture,
                'current_tab':lambda:{'targetId':self.target,'url':self.url},
                'activate_tab':lambda *a:pytest.fail('Screenshot composition foregrounded the user browser')}


def request(path):
    return {'operation':'screenshot','path':str(path),'target_id':'owned','expected_url':URL}


@pytest.mark.parametrize('mode',['normal','timeout'])
def test_official_native_scroll_settles_top_and_captures_full_without_foreground(tmp_path,mode):
    path=tmp_path/'image.png'; native=NativeHelpers(path,mode)
    result=capture_from_top(request(path),native.helpers())
    assert result['composition']=='settled_page_top' and path.read_bytes()==IMAGE
    assert len(native.scrolls)==(2 if mode=='timeout' else 1)
    assert native.focus==([True,False] if mode=='timeout' else [])
    assert len(native.captures)==1 and path.stat().st_mode&0o777==0o600


@pytest.mark.parametrize('mode',['stalled','retry_timeout','redirect'])
def test_failed_settlement_or_changed_job_never_publishes_a_capture(tmp_path,mode):
    path=tmp_path/'image.png'; native=NativeHelpers(path,mode)
    with pytest.raises((ValueError,TimeoutError)):
        capture_from_top(request(path),native.helpers())
    assert not path.exists() and native.captures==[]
    if mode=='retry_timeout':assert native.focus==[True,False]


def test_wrong_target_cannot_scroll_or_capture(tmp_path):
    path=tmp_path/'image.png'; native=NativeHelpers(path); native.target='another-job'
    with pytest.raises(ValueError,match='exact application'):
        capture_from_top(request(path),native.helpers())
    assert native.scrolls==native.captures==[]


def test_native_full_capture_moves_sticky_header_away_from_email_without_changing_fields(tmp_path):
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(Path(__file__).resolve().parents[2]/'.local-browsers'))
    from playwright.sync_api import sync_playwright
    from PIL import Image
    html='''<!doctype html><style>body{margin:0}header{position:sticky;top:0;height:100px;background:rgb(220,50,50);z-index:20}
    main{padding-top:600px}.email{height:100px;background:rgb(40,220,70)}footer{height:1200px}</style>
    <header>Sticky navigation</header><main><div class=email><label for=email>Email</label><input id=email value=synthetic@example.invalid>
    <input id=count type=number value=7></div></main><footer></footer><script>window.changed=0;
    document.addEventListener('change',()=>changed++);window.submissions=0;</script>'''
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page(viewport={'width':800,'height':500})
        page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=html))
        page.goto(URL);session=page.context.new_cdp_session(page)
        def cdp(method,**args):return session.send(method,args)
        def image_bytes():return base64.b64decode(cdp('Page.captureScreenshot',format='png',captureBeyondViewport=True)['data'])
        try:
            # Fixture-only setup reproduces the actual mid-page sticky header.
            page.locator('#count').focus();page.evaluate('window.scrollTo(0,650)');page.wait_for_timeout(100)
            before=image_bytes()
            assert Image.open(io.BytesIO(before)).convert('RGB').getpixel((780,720))==(220,50,50)
            scrolls=[]
            def scroll(x,y,dy=-300,dx=0):
                scrolls.append((x,y,dy,dx))
                cdp('Input.dispatchMouseEvent',type='mouseWheel',x=x,y=y,deltaY=dy,deltaX=dx)
            def capture(path,*,full):
                assert full is True
                with open(path,'wb') as handle:handle.write(image_bytes())
            helpers={'cdp':cdp,'js':page.evaluate,'wait':lambda seconds:page.wait_for_timeout(seconds*1000),
                     'scroll':scroll,'capture_screenshot':capture,'switch_tab':lambda target:None,
                     'current_tab':lambda:{'targetId':'owned','url':URL}}
            path=tmp_path/'review.png'
            result=cli_runtime.dispatch(request(path),helpers)
            assert result['composition']=='settled_page_top' and scrolls
            assert page.evaluate('scrollY')==0
            image=Image.open(path).convert('RGB')
            assert image.getpixel((780,50))==(220,50,50)
            assert image.getpixel((780,720))==(40,220,70)
            assert page.locator('#email').input_value()=='synthetic@example.invalid'
            assert page.locator('#count').input_value()=='7' and page.evaluate('changed')==0
            assert page.locator('header').is_visible() and page.evaluate('submissions')==0
        finally:browser.close()
