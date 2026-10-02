'''The gemini account pool the way the request path sees it.

test_gemini_account_pool.py covers the pool in isolation. These tests drive the
bridge itself over real HTTP, because that is where the pool can still be got
wrong:

  * a 502 keeps the envelope it always had -- error.message is a string,
    error.channels is still {code_assist, web} -- and now also names the
    credential that burned as error.account;
  * with two accounts imported, the first one failing both channels is put on
    cooldown and the same request is answered by the second account;
  * GET /__gemini/accounts plus POST /__gemini/accounts/{primary,
    import-current, remove, refresh} drive the pool with the same response
    contract the workbuddy dashboard uses.
'''

import importlib.util
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

BRIDGES = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, 'bridges'))
sys.path.insert(0, BRIDGES)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BRIDGES, rel))
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses on 3.14 resolves cls.__module__ through
    # sys.modules, so a module loaded by spec alone is not enough.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# gemini_accounts first, under its own name: gemini_bridge imports it by that
# name, and both halves of the bridge have to share one object here.
ga = _load('gemini_accounts', os.path.join('gemini', 'gemini_accounts.py'))
gemini = _load('gemini_bridge_failover', os.path.join('gemini', 'gemini_bridge.py'))

TOKEN_NAME = ga.TOKEN_FILE_NAME
MSGS = [{'role': 'user', 'content': 'ping'}]


def _write_token(path, label, expiry='2030-01-01T00:00:00'):
    payload = {'token': {'access_token': 'at-' + label, 'refresh_token': 'rt-' + label,
                         'expiry': expiry}, 'auth_method': 'oauth'}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f)
    return path


class _Env:
    '''A tmpdir standing in for ~/.gemini plus an auths/ directory.'''

    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        self.token = _write_token(os.path.join(self.tmp, 'jetski-standalone-oauth-token'),
                                  'legacy')
        self.cookie = os.path.join(self.tmp, 'cookies.txt')
        with open(self.cookie, 'w', encoding='utf-8') as f:
            f.write('placeholder')
        self.auths = os.path.join(self.tmp, 'auths')

    def pool(self):
        return ga.GeminiAccountPool(self.auths, legacy_token=self.token,
                                    legacy_cookie=self.cookie)

    def add(self, label):
        src = _write_token(os.path.join(self.tmp, label + '.json'), label)
        self.pool().import_current(src, None, label)
        return self.account_dir(label)

    def account_dir(self, label):
        return os.path.join(self.auths, label)

    def token_of(self, label):
        return os.path.join(self.account_dir(label), TOKEN_NAME)

    def rows(self):
        return {r['label']: r for r in self.pool().status()}

    def labels(self):
        return [r['label'] for r in self.pool().status()]


class _Bridge:
    '''The real gemini handler, over HTTP, on an ephemeral port.'''

    def __init__(self, mod):
        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), mod.H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def post(self, path, payload, timeout=30):
        req = urllib.request.Request(self._url(path), data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'})
        return self._call(req, timeout)

    def get(self, path, timeout=30):
        return self._call(urllib.request.Request(self._url(path)), timeout)

    def _url(self, path):
        return 'http://127.0.0.1:%d%s' % (self.port, path)

    def _call(self, req, timeout):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _chat(monkeypatch, fails=lambda path: False, answer='pong'):
    '''call_a/call_b that burn one account and answer with the other one.'''
    seen = []

    def fake_a(model, msgs, stream, timeout=180, deadline=None):
        path = gemini._token_file()
        seen.append(path)
        if fails(path):
            raise gemini.UpstreamError(
                'codeassist error: HTTP 403 Verify your account to continue.')
        return answer

    def fake_b(prompt, timeout=180):
        path = gemini._token_file()
        seen.append(path)
        if fails(path):
            raise RuntimeError('gemini-web: HTTP 403')
        return answer

    monkeypatch.setattr(gemini, 'call_a', fake_a)
    monkeypatch.setattr(gemini, 'call_b', fake_b)
    return seen


def _wire(monkeypatch, env):
    monkeypatch.setattr(gemini, '_pool', lambda: env.pool())
    monkeypatch.setattr(gemini, 'BRIDGE_KEY', '')
    monkeypatch.setattr(gemini, 'TOKEN_FILE', env.token)
    monkeypatch.setattr(gemini, 'COOKIE_FILE', env.cookie)


def test_the_502_envelope_keeps_channels_and_names_the_burned_account(monkeypatch):
    env = _Env()
    env.add('main-1')
    env.add('main-2')
    _wire(monkeypatch, env)
    _chat(monkeypatch, fails=lambda path: True)
    bridge = _Bridge(gemini)
    try:
        code, body = bridge.post('/v1/chat/completions',
                                 {'model': 'gemini-2.5-flash', 'messages': MSGS})
    finally:
        bridge.close()

    assert code == 502
    err = body['error']
    # an OpenAI-compatible client reads error.message as text
    assert isinstance(err['message'], str)
    assert 'code_assist' in err['message']
    assert 'web' in err['message']
    assert 'HTTP 403' in err['message']
    # the breakdown survives, one level down, with its documented keys
    assert err['type'] == 'upstream_error'
    assert set(err['channels']) == {'code_assist', 'web'}
    # and the credential that burned rides along, which the old envelope never
    # had: a 502 that cannot say which account it blamed is a 502 nobody can fix.
    assert err['account'] == 'main-1'
    assert env.rows()['main-1']['state'] == 'cooling'


def test_a_burned_account_hands_the_request_to_the_next_one(monkeypatch):
    env = _Env()
    env.add('main-1')
    env.add('main-2')
    _wire(monkeypatch, env)
    burned = env.token_of('main-1')
    # the legacy account is a candidate too and it would happily answer, so it
    # has to burn alongside main-1: that is the two-identity case the pool for.
    seen = _chat(monkeypatch, fails=lambda path: path in (burned, env.token))
    bridge = _Bridge(gemini)
    try:
        code, body = bridge.post('/v1/chat/completions',
                                 {'model': 'gemini-2.5-flash', 'messages': MSGS})
    finally:
        bridge.close()

    rows = env.rows()
    # main-1 sorts first as primary, so it is the one that burns: A and B
    assert seen[:2] == [burned, burned]
    assert code == 200
    assert body['choices'][0]['message']['content'] == 'pong'
    assert body['channel'] == 'code-assist'
    # the failure lands on the pool, not on the caller
    assert rows['main-1']['state'] == 'cooling'
    assert rows['main-1']['failures'] == 1
    assert rows['main-2']['state'] == 'ready'
    assert rows['main-2']['last_used'] > 0


def test_the_accounts_routes_list_import_pin_remove_and_refresh(monkeypatch):
    env = _Env()
    _wire(monkeypatch, env)
    bridge = _Bridge(gemini)
    try:
        code, body = bridge.get('/__gemini/accounts')
        assert code == 200
        assert body['status'] == 'ok'
        assert env.labels() == ['legacy']
        only_legacy = body['account_pool']['accounts'][0]
        assert only_legacy['legacy'] is True

        code, body = bridge.post('/__gemini/accounts/import-current', {'label': 'second'})
        assert code == 200
        assert body['account']['label'] == 'second'
        ref = body['account']['ref']
        # the first import is primary, so status() puts it ahead of legacy
        assert env.labels() == ['second', 'legacy']
        assert _primary_refs(body) == [ref]

        # pinning the other way is a real switch, not a no-op
        legacy_ref = [a['ref'] for a in body['account_pool']['accounts'] if a['legacy']][0]
        code, body = bridge.post('/__gemini/accounts/primary', {'ref': legacy_ref})
        assert code == 200
        assert _primary_refs(body) == [legacy_ref]

        code, body = bridge.post('/__gemini/accounts/refresh', {})
        assert code == 200
        assert _primary_refs(body) == [legacy_ref]

        code, body = bridge.post('/__gemini/accounts/remove', {'ref': ref})
        assert code == 200
        assert env.labels() == ['legacy']
        # removing twice is a 404 with a detail, not a 500
        code, body = bridge.post('/__gemini/accounts/remove', {'ref': ref})
        assert code == 404
        assert body['detail'] == 'account not found'
        # and the legacy account is not a directory to delete
        code, body = bridge.post('/__gemini/accounts/remove', {'ref': legacy_ref})
        assert code == 400
        assert 'legacy' in body['detail']
        assert env.labels() == ['legacy']
    finally:
        bridge.close()


def _primary_refs(body):
    return [a['ref'] for a in body['account_pool']['accounts'] if a['primary']]


def test_a_known_account_goes_first_when_everything_else_is_cooling(monkeypatch):
    '''A stale cooldown must not turn into an unanswered request.'''
    env = _Env()
    env.add('main-1')
    env.add('main-2')
    _wire(monkeypatch, env)
    for row in env.pool().status():
        env.pool().mark_failure(row['ref'], 'burned', 600)
    pool = env.pool()
    assert pool.candidates() == []
    cands = pool.candidates(ignore_cooldown=True)
    assert [c.label for c in cands][0] == 'main-1'
    assert len(cands) == 3

    _chat(monkeypatch, fails=lambda path: False)
    bridge = _Bridge(gemini)
    try:
        code, body = bridge.post('/v1/chat/completions',
                                 {'model': 'gemini-2.5-flash', 'messages': MSGS})
    finally:
        bridge.close()
    assert code == 200
    assert body['choices'][0]['message']['content'] == 'pong'
