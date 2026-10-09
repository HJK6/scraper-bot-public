"""Default-confidential fills, using synthetic data only and no production records."""
import contextlib
import json
import traceback
from types import SimpleNamespace

import pytest
import requests
from fastapi.testclient import TestClient

import client as api
import server
import trusted_input as ti

SAMPLES = [('pan', '000000000000000'), ('cvv', '963'),
           ('password', 'synthetic-password-private'), ('totp', '963852')]
READBACK = 'unrelated-private-readback'


def assert_safe(value, *contents):
    rendered = str(value)
    assert all(content not in rendered for content in contents if content), 'confidential diagnostic content escaped'


def error_surfaces(error):
    return [str(error), repr(error), repr(error.args),
            getattr(error, 'detail', None), getattr(error, 'extra', None),
            getattr(error, 'to_dict', lambda: None)(),
            ''.join(traceback.format_exception(error))]


class Element:
    def __init__(self, driver):
        self.driver = driver
    def send_keys(self, text):
        self.driver.sent = text
        if self.driver.raw_error:
            try:
                raise ValueError(READBACK)
            except ValueError as error:
                raise RuntimeError(text) from error
    def clear(self):
        pass
    def click(self):
        pass


class Driver:
    def __init__(self, readback=READBACK, raw_error=False):
        self.readback, self.raw_error, self.sent = readback, raw_error, None
    def execute_cdp_cmd(self, *args):
        return {}
    def execute_script(self, script, *args):
        if "t==='INPUT'" in script or 'document.activeElement===' in script:
            return True
        return self.readback


@pytest.fixture
def field(monkeypatch):
    driver = Driver()
    element = Element(driver)
    monkeypatch.setattr(ti, '_resolve', lambda *a: {'target': {'text': READBACK}})
    monkeypatch.setattr(ti, '_find_by_token', lambda *a: element)
    monkeypatch.setattr(ti, '_cleanup_tokens', lambda *a: None)
    return driver


@pytest.fixture
def endpoint(field, monkeypatch):
    sess = SimpleNamespace(id='synthetic', last_error=None, error_count=0,
                           last_request_at=None, last_action_at=None, action_count=0,
                           trace_path=None)
    dm = SimpleNamespace(driver=field, trace_path=None, _network_enabled=False)
    @contextlib.contextmanager
    def action(_id):
        yield sess, dm
    monkeypatch.setattr(server, '_session_action', action)
    monkeypatch.setattr(server, '_find_element', lambda *a, **k: Element(field))
    return TestClient(server.app), sess, field, dm


@pytest.mark.parametrize('label,value', SAMPLES, ids=[x[0] for x in SAMPLES])
@pytest.mark.parametrize('scenario', ['mismatch', 'partial', 'formatted', 'expect', 'nested', 'overlay', 'success'])
def test_helper_surfaces(field, monkeypatch, label, value, scenario):
    kwargs = {'text': value, 'css': '#synthetic', 'focus': False, 'mode': 'send_keys'}
    if scenario == 'partial':
        field.readback = value[:-1]
    elif scenario == 'formatted':
        field.readback = '-'.join(value)
    elif scenario == 'expect':
        kwargs.update(expect={'kind': 'value', 'css': '#synthetic', 'text': value}, verify_timeout_ms=0)
    elif scenario == 'nested':
        field.raw_error = True
    elif scenario == 'overlay':
        monkeypatch.setattr(ti, '_resolve', lambda *a: {'covered': True, 'cover': {'text': value, 'id': READBACK}})
    elif scenario == 'success':
        field.readback = value
    try:
        result = ti.type_text(field, **kwargs)
    except Exception as error:
        assert scenario != 'success'
        assert isinstance(error, ti.TrustedInputError)
        if scenario in {'mismatch', 'partial', 'formatted', 'expect'}:
            assert error.reason == 'effect_not_observed'
        for surface in error_surfaces(error):
            assert_safe(surface, value, field.readback, READBACK)
    else:
        assert scenario == 'success'
        assert result['verified'] is True
        assert field.sent == value
        assert_safe(result, value, READBACK)


@pytest.mark.parametrize('label,value', SAMPLES, ids=[x[0] for x in SAMPLES])
@pytest.mark.parametrize('route', ['input/type', 'type', 'input/select_option'])
@pytest.mark.parametrize('raw_error', [False, True], ids=['mismatch', 'nested'])
def test_http_session_logs_client(endpoint, monkeypatch, caplog, label, value, route, raw_error):
    tc, sess, field, _dm = endpoint
    field.raw_error = raw_error or route == 'type'
    monkeypatch.setattr(ti, '_scope_text', lambda *a: READBACK)
    monkeypatch.setattr(ti, 'wait_for', lambda *a, **k: {'ok': True})
    monkeypatch.setattr(ti, 'click', lambda *a, **k: {'ok': True})
    body = {'text': value, 'css': '#synthetic', 'mode': 'send_keys', 'focus': False}
    if route == 'input/select_option':
        body = {'input_css': '#synthetic', 'item_text': value, 'verify_scope_css': '#committed', 'verify_timeout_ms': 0}
    response = tc.post('/sessions/synthetic/' + route, json=body)
    assert response.status_code >= 400
    assert sess.error_count == 1
    assert_safe([response.text, sess.last_error, caplog.text], value, READBACK)
    bot = api.ScraperBot('http://unused.invalid')
    monkeypatch.setattr(bot._session, 'post', lambda *a, **k: Response(response.status_code, response.json()))
    # Legacy type must have the same safe client fallback boundary.
    if route == 'type':
        monkeypatch.setattr(bot._session, 'request', lambda *a, **k: Response(response.status_code, response.json()))
        operation = lambda: bot.type('synthetic', value, css='#synthetic')
    else:
        operation = lambda: bot._input_post('/sessions/synthetic/' + route, body)
    with pytest.raises(api.ScraperBotError) as caught:
        operation()
    for surface in error_surfaces(caught.value):
        assert_safe(surface, value, READBACK)


@pytest.mark.parametrize('label,value', SAMPLES, ids=[x[0] for x in SAMPLES])
@pytest.mark.parametrize('route', ['type', 'input/type', 'input/select_option'])
def test_validation_body_is_confidential(endpoint, label, value, route):
    tc, _sess, _field, _dm = endpoint
    response = tc.post('/sessions/synthetic/' + route, json={'text': {'private': value}, 'type_value': {'private': value}, 'item_index': {'private': value}})
    assert response.status_code == 422
    assert_safe(response.text, value)


@pytest.mark.parametrize('label,value', SAMPLES, ids=[x[0] for x in SAMPLES])
@pytest.mark.parametrize('route', ['type', 'input/type', 'input/select_option'])
def test_malformed_json_is_confidential(endpoint, label, value, route):
    tc, *_ = endpoint
    response = tc.post('/sessions/synthetic/' + route, content='{"text":"' + value + '" INVALID}', headers={'Content-Type': 'application/json'})
    assert response.status_code == 422
    assert_safe(response.text, value)


class Response:
    def __init__(self, status, body=None, malformed=False):
        self.status_code, self.ok = status, status < 400
        self.body, self.malformed = body, malformed
        self.text = READBACK
    def json(self):
        if self.malformed:
            raise ValueError(READBACK)
        return self.body


@pytest.mark.parametrize('label,value', SAMPLES, ids=[x[0] for x in SAMPLES])
@pytest.mark.parametrize('scenario', ['nested', 'unknown', 'validation', 'malformed', 'success', 'connection', 'timeout', 'raw', 'malformed_success'])
@pytest.mark.parametrize('route', ['type', 'input/type', 'input/select_option'])
def test_client_old_server_and_fallback(monkeypatch, label, value, scenario, route):
    detail = {'reason': 'effect_not_observed', 'message': value,
              'primitive': 'trusted.type', 'nested': {'value': READBACK}, 'before': value}
    if scenario == 'unknown':
        detail.update(reason=value, primitive=READBACK)
    response = Response(409, {'detail': detail})
    if scenario == 'validation':
        response = Response(422, {'detail': [{'input': value, 'ctx': {'error': READBACK}}]})
    elif scenario == 'malformed':
        response = Response(400, malformed=True)
    elif scenario == 'malformed_success':
        response = Response(200, {'ok': False, 'detail': detail})
    elif scenario == 'success':
        response = Response(200, {'ok': True, 'primitive': 'trusted.type', 'verified': True,
                                  'target': {'text': READBACK}, 'typed': value})
    bot = api.ScraperBot('http://unused.invalid')
    def request(*a, **k):
        if scenario in {'connection', 'timeout', 'raw'}:
            kind = {'connection': requests.exceptions.ConnectionError,
                    'timeout': requests.exceptions.Timeout, 'raw': requests.exceptions.RequestException}[scenario]
            raise kind(value + READBACK)
        return response
    monkeypatch.setattr(bot._session, 'post', request)
    monkeypatch.setattr(bot._session, 'request', request)
    if route == 'type':
        operation = lambda: bot.type('synthetic', value, css='#synthetic')
    else:
        operation = lambda: bot._input_post('/sessions/synthetic/' + route, {'text': value})
    if scenario == 'success':
        assert_safe(operation(), value, READBACK)
    else:
        with pytest.raises(api.ScraperBotError) as caught:
            operation()
        for surface in error_surfaces(caught.value):
            assert_safe(surface, value, READBACK)
        if scenario == 'nested' and route != 'type':
            assert caught.value.reason == 'effect_not_observed'
            assert caught.value.status_code == 409


@pytest.mark.parametrize('capture', ['trace', 'network'])
@pytest.mark.parametrize('route', ['type', 'input/type', 'input/select_option'])
def test_automatic_capture_refuses_before_dispatch(endpoint, capture, route):
    tc, sess, field, dm = endpoint
    if capture == 'trace':
        sess.trace_path = dm.trace_path = 'synthetic-trace.zip'
    else:
        dm._network_enabled = True
    response = tc.post('/sessions/synthetic/' + route, json={'text': 'synthetic-private', 'css': '#synthetic',
                      'input_css': '#synthetic', 'item_text': 'synthetic-private', 'verify_scope_css': '#committed'})
    assert response.status_code == 400
    assert response.json()['detail']['reason'] == 'bad_request'
    assert field.sent is None
    assert_safe(response.text, 'synthetic-private')


@pytest.mark.parametrize('label,value', SAMPLES, ids=[x[0] for x in SAMPLES])
def test_selection_success_and_ambiguous_readback(field, monkeypatch, label, value):
    monkeypatch.setattr(ti, 'wait_for', lambda *a, **k: {'ok': True})
    monkeypatch.setattr(ti, 'click', lambda *a, **k: {'ok': True})
    states = iter(['before', value])
    monkeypatch.setattr(ti, '_scope_text', lambda *a: next(states))
    result = ti.select_option(field, input_css='#synthetic', item_text=value, verify_scope_css='#committed')
    assert result['verified'] is True
    assert_safe(result, value, READBACK)
    monkeypatch.setattr(ti, '_scope_committed_values', lambda *a: [value, value])
    with pytest.raises(ti.TrustedInputError) as caught:
        ti.select_option(field, input_css='#synthetic', item_text=value,
                         verify_scope_css='#committed', verify_value_css='input', verify_timeout_ms=10)
    assert caught.value.reason == 'effect_not_observed'
    for surface in error_surfaces(caught.value):
        assert_safe(surface, value, READBACK)


@pytest.mark.parametrize('label,value', SAMPLES, ids=[x[0] for x in SAMPLES])
def test_driver_debug_logs_are_confidential(field, monkeypatch, caplog, label, value):
    import logging
    caplog.set_level(logging.DEBUG)
    def send(text):
        logging.getLogger('selenium.webdriver.remote.remote_connection').debug('POST %s %s', text, READBACK)
        try:
            raise ValueError(READBACK)
        except ValueError:
            logging.getLogger('selenium').debug('driver rejected %s', text, exc_info=True)
    monkeypatch.setattr(Element, 'send_keys', lambda self, text: send(text))
    with pytest.raises(ti.TrustedInputError):
        ti.type_text(field, text=value, css='#synthetic', focus=False, mode='send_keys')
    assert_safe(caplog.text, value, READBACK)


def test_default_cdp_network_capture_disabled_before_typing(field, monkeypatch):
    calls = []
    monkeypatch.setattr(field, 'execute_cdp_cmd', lambda method, params: calls.append(method))
    monkeypatch.setattr(Element, 'send_keys', lambda self, text: calls.append('send_keys'))
    with pytest.raises(ti.TrustedInputError):
        ti.type_text(field, text='synthetic-private', css='#synthetic', focus=False, mode='send_keys')
    assert calls.index('Network.disable') < calls.index('send_keys')


@pytest.mark.parametrize('label,value', SAMPLES, ids=[x[0] for x in SAMPLES])
def test_failed_capture_suppression_refuses_before_dispatch(field, monkeypatch, label, value):
    def disable(*args):
        raise RuntimeError(value + READBACK)
    monkeypatch.setattr(field, 'execute_cdp_cmd', disable)
    with pytest.raises(ti.TrustedInputError) as caught:
        ti.type_text(field, text=value, css='#synthetic', focus=False, mode='send_keys')
    assert field.sent is None
    assert caught.value.reason == 'error'
    assert caught.value.__context__ is None
    for surface in error_surfaces(caught.value):
        assert_safe(surface, value, READBACK)


@pytest.mark.parametrize('label,value', SAMPLES, ids=[x[0] for x in SAMPLES])
def test_client_debug_logs_and_raw_server_boundary(endpoint, monkeypatch, caplog, label, value):
    import logging
    tc, sess, _field, _dm = endpoint
    def raw(*args, **kwargs):
        raise RuntimeError(value + READBACK)
    monkeypatch.setattr(ti, 'type_text', raw)
    response = tc.post('/sessions/synthetic/input/type', json={'text': value, 'css': '#synthetic'})
    assert response.status_code == 400
    assert_safe([response.text, sess.last_error], value, READBACK)
    caplog.set_level(logging.DEBUG)
    bot = api.ScraperBot('http://unused.invalid')
    def request(*a, **k):
        logging.getLogger('urllib3.connectionpool').debug('request %s response %s', value, READBACK)
        raise requests.exceptions.ConnectionError(value + READBACK)
    monkeypatch.setattr(bot._session, 'post', request)
    with pytest.raises(api.ScraperBotConnectionError) as caught:
        bot.trusted_type('synthetic', value, css='#synthetic')
    assert caught.value.__context__ is None
    assert_safe(caplog.text, value, READBACK)


def test_explicit_network_enable_tracks_all_adapters_and_refuses_fill(endpoint):
    tc, _sess, field, dm = endpoint
    methods = []
    field.execute_cdp_cmd = lambda method, params: methods.append(method)
    # Adapters may enable CDP recording without their own flag.
    dm.enable_network_logging = lambda: None
    enabled = tc.post('/sessions/synthetic/network/enable')
    assert enabled.status_code == 200
    response = tc.post('/sessions/synthetic/input/type', json={'text': 'synthetic-private', 'css': '#synthetic', 'mode': 'send_keys'})
    assert response.status_code == 400
    assert response.json()['detail']['reason'] == 'bad_request'
    assert 'Network.enable' in methods
    assert field.sent is None
