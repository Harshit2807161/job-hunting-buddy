"""Synthetic native option replacement; never attach to candidate Chrome."""
import pytest

from jhb.applications import cli_runtime
from tests.applications.test_cli_runtime_fixture import HTML
from tests.applications.test_cli_runtime_recovery_fixture import synthetic_browser

BOX_ERROR = {'code': -32000, 'message': 'Could not compute box model.'}


@pytest.mark.parametrize('mode', [
    'recover', 'repeat_replacement', 'target_changed', 'job_changed', 'guard_changed',
    'control_changed', 'label_changed', 'retained_changed', 'missing_option',
    'duplicate_option', 'changed_option_label', 'unrelated_listbox',
    'unknown_click', 'input_box_error',
])
def test_option_rerender_retries_only_same_owned_choice_before_input(mode):
    with synthetic_browser(HTML) as (page, session, helpers, target, url, requests, switches, activations):
        field = next(f for f in cli_runtime.dispatch({'operation': 'observe'}, helpers)['fields'] if f['ref'] == 'state')
        raw, current = helpers['cdp'], {'targetId': target, 'url': url}
        helpers['current_tab'] = lambda: dict(current)
        option_backends, replacement_errors, option_presses, option_releases = [], [], [], []
        in_option_click = False
        page.evaluate("()=>{window.chosen=0;const original=chooseState;chooseState=()=>{window.chosen++;original()}}")
        def transport(method, **params):
            nonlocal in_option_click
            if method == 'DOM.getBoxModel':
                attrs = session.send('DOM.describeNode', {'backendNodeId': params['backendNodeId']})['node'].get('attributes', [])
                attributes = dict(zip(attrs[::2], attrs[1::2]))
                if attributes.get('role') == 'option':
                    in_option_click = True
                    backend = params['backendNodeId']
                    if not option_backends or option_backends[-1] != backend:
                        option_backends.append(backend)
                    replace = mode not in {'unknown_click', 'input_box_error'} and (
                        not replacement_errors or mode == 'repeat_replacement' and len(replacement_errors) == 1)
                    if replace:
                        # Invalidate the real AX node immediately before the
                        # real browser geometry command; its replacement has
                        # a new backend id and must be observed from owned AX.
                        page.evaluate('openOptions()')
                        if mode == 'target_changed': current['targetId'] = 'unrelated-target'
                        if mode == 'job_changed': current['url'] = 'https://job-boards.greenhouse.io/another/jobs/999'
                        if mode == 'guard_changed': page.evaluate('window.__jhbGuard=false')
                        if mode == 'control_changed': page.evaluate("const e=document.getElementById('state');e.replaceWith(e.cloneNode(true))")
                        if mode == 'label_changed': page.evaluate("document.getElementById('state-label').textContent='Unrelated legal question'")
                        if mode == 'retained_changed': page.evaluate("document.querySelector('.select__single-value').textContent='Manual candidate edit'")
                        if mode == 'missing_option': page.evaluate("document.getElementById('options').innerHTML=''")
                        if mode == 'duplicate_option': page.evaluate("const e=document.querySelector('#options [role=option]');e.after(e.cloneNode(true))")
                        if mode == 'changed_option_label': page.evaluate("document.querySelector('#options [role=option]').textContent='CA'")
                        if mode == 'unrelated_listbox': page.evaluate("document.getElementById('countries').append(...document.getElementById('options').children)")
                        try:
                            session.send(method, params)
                        except Exception as exc:
                            assert 'Could not compute box model' in str(exc)
                            replacement_errors.append(backend)
                            raise RuntimeError(str(BOX_ERROR)) from exc
                        pytest.fail('Synthetic replacement did not detach the original option')
            if method == 'Input.dispatchMouseEvent' and in_option_click:
                if params.get('type') == 'mousePressed':
                    option_presses.append(params)
                    if mode == 'input_box_error':
                        raise RuntimeError(str(BOX_ERROR))  # Same text, wrong boundary: never retry.
                if params.get('type') == 'mouseReleased' and option_presses:
                    option_releases.append(params)
                    if mode == 'unknown_click':
                        result = raw(method, **params)
                        raise TimeoutError('Unknown option release result')
            return raw(method, **params)
        helpers['cdp'] = transport
        request = {'operation': 'fill', 'target_id': target, 'expected_url': url, 'field': field, 'value': 'CA'}
        if mode == 'recover':
            assert cli_runtime.dispatch(request, helpers) == {'verified': True, 'selected': 'California'}
            assert page.evaluate('window.chosen') == 1
            assert len(option_backends) == 2 and option_backends[0] != option_backends[1]
            assert len(replacement_errors) == len(option_presses) == 1
        else:
            with pytest.raises((ValueError, RuntimeError, TimeoutError)):
                cli_runtime.dispatch(request, helpers)
            assert len(option_presses) == int(mode in {'unknown_click', 'input_box_error'})
            assert page.evaluate('window.chosen') == int(mode == 'unknown_click')
            assert len(option_backends) == (2 if mode == 'repeat_replacement' else 1)
            if mode == 'retained_changed':
                assert page.locator('.select__single-value').first.inner_text() == 'Manual candidate edit'
        assert activations == []
        assert page.evaluate('window.submissions') == 0
        assert requests == [url]


@pytest.mark.parametrize('error', [
    BOX_ERROR, str(BOX_ERROR), {'code': -32001, 'message': BOX_ERROR['message']},
    {'code': -32000, 'message': 'Node is detached'}, 'DOM.getBoxModel timed out',
])
def test_only_exact_box_rejection_is_typed_and_never_clicks(error):
    calls = []
    def cdp(method, **params):
        calls.append(method)
        if method == 'DOM.getBoxModel': raise RuntimeError(error)
        assert method == 'DOM.scrollIntoViewIfNeeded'
    expected = cli_runtime.PreInputGeometryUnavailable if error in (BOX_ERROR, str(BOX_ERROR)) else RuntimeError
    with pytest.raises(expected):
        cli_runtime._settled_click(42, cdp, lambda _: None, lambda *args: pytest.fail('Unexpected click'))
    assert calls == ['DOM.scrollIntoViewIfNeeded', 'DOM.getBoxModel']


def test_geometry_failure_after_native_wheel_is_not_preinput_recovery():
    wheel_sent = False
    def cdp(method, **params):
        nonlocal wheel_sent
        if method == 'DOM.getBoxModel':
            if wheel_sent: raise RuntimeError(str(BOX_ERROR))
            return {'model': {'content': [100, 500, 120, 500, 120, 520, 100, 520]}}
        if method == 'Page.getLayoutMetrics':
            return {'cssVisualViewport': {'clientWidth': 600, 'clientHeight': 600}}
        if method == 'DOM.resolveNode': return {'object': {'objectId': 'owned-option'}}
        if method == 'Runtime.callFunctionOn':
            return {'result': {'value': {'hit': False, 'wheel': {'x': 300, 'y': 300}}}}
        if method == 'Input.dispatchMouseEvent':
            assert params['type'] == 'mouseWheel'
            wheel_sent = True
        return {}
    with pytest.raises(RuntimeError) as caught:
        cli_runtime._settled_click(42, cdp, lambda _: None, lambda *args: pytest.fail('Unexpected click'))
    assert type(caught.value) is RuntimeError and wheel_sent
