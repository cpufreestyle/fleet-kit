'''GEMINI_OAUTH_CLIENT_* env injection, the way the bridge sees it.

Google killed the two hand-baked OAuth pairs that used to live in this bridge:
refresh now answers 401 unauthorized_client and every chat call arrives
holding a stale access token. The pair therefore comes from fleet.env, the same
contract antigravity_bridge.py has, with the dead constants demoted to a
last-resort tail. do_refresh() stops at the first pair Google accepts, so the
order is the whole feature:

  * GEMINI_OAUTH_CLIENT_ID/SECRET first, then GEMINI_LEGACY_CLIENTS, and only
    then the built-in fallbacks;
  * with nothing injected the bridge still boots and keeps the two constants,
    because a stale pair costs one round trip, not the bridge.
'''

import importlib.util
import itertools
import json
import os
import sys
import time

BRIDGES = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, 'bridges'))
sys.path.insert(0, BRIDGES)

ENV_KEYS = ('GEMINI_OAUTH_CLIENT_ID', 'GEMINI_OAUTH_CLIENT_SECRET',
            'GEMINI_LEGACY_CLIENTS', 'GEMINI_LEGACY_CLIENT_ID',
            'GEMINI_LEGACY_CLIENT_SECRET')

DEAD_PAIR = ('681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com',
             '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')

_counter = itertools.count()


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BRIDGES, rel))
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses on 3.14 resolves cls.__module__ through
    # sys.modules, so a module loaded by spec alone is not enough.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _bridge_with(env):
    """Load a fresh gemini bridge with env set; restore the environment after."""
    saved = {k: os.environ.pop(k, None) for k in ENV_KEYS}
    os.environ.update({k: v for k, v in env.items() if v})
    try:
        # gemini_accounts first under its own name, like the failover test: the
        # bridge imports it by that name and both must share one object.
        _load('gemini_accounts', os.path.join('gemini', 'gemini_accounts.py'))
        tag = next(_counter)
        return _load('gemini_bridge_oauth_env_%d' % tag,
                     os.path.join('gemini', 'gemini_bridge.py'))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _write_token(path, label):
    payload = {'token': {'access_token': 'at-' + label, 'refresh_token': 'rt-' + label,
                         'expiry': '2030-01-01T00:00:00'}, 'auth_method': 'oauth'}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f)


def test_the_env_pair_leads_and_the_dead_constants_follow():
    mod = _bridge_with({'GEMINI_OAUTH_CLIENT_ID': 'env-id',
                        'GEMINI_OAUTH_CLIENT_SECRET': 'env-secret'})
    assert mod.CLIENT_CANDIDATES[0] == ('env-id', 'env-secret')
    assert mod.CLIENT_CANDIDATES[1] == DEAD_PAIR
    assert DEAD_PAIR in mod.CLIENT_CANDIDATES
    assert len(mod.CLIENT_CANDIDATES) == 3


def test_legacy_clients_sit_between_the_env_pair_and_the_constants():
    mod = _bridge_with({'GEMINI_OAUTH_CLIENT_ID': 'env-id',
                        'GEMINI_OAUTH_CLIENT_SECRET': 'env-secret',
                        'GEMINI_LEGACY_CLIENTS': 'legacy-a:sec-a,legacy-b:sec-b'})
    assert mod.CLIENT_CANDIDATES[0] == ('env-id', 'env-secret')
    assert mod.CLIENT_CANDIDATES[1] == ('legacy-a', 'sec-a')
    assert mod.CLIENT_CANDIDATES[2] == ('legacy-b', 'sec-b')
    assert DEAD_PAIR in mod.CLIENT_CANDIDATES


def test_no_env_still_boots_on_the_two_constants():
    mod = _bridge_with({})
    assert mod.CLIENT_CANDIDATES == [DEAD_PAIR,
                                     ('764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com',
                                      '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')]


def test_do_refresh_offers_the_env_pair_before_the_dead_one(monkeypatch, tmp_path):
    mod = _bridge_with({'GEMINI_OAUTH_CLIENT_ID': 'env-id',
                        'GEMINI_OAUTH_CLIENT_SECRET': 'env-secret'})
    token = os.path.join(str(tmp_path), 'jetski-standalone-oauth-token')
    _write_token(token, 'env')
    monkeypatch.setattr(mod, 'TOKEN_FILE', token)
    offered = []

    def fake_http_json(url, payload, timeout=None, headers=None):
        offered.append(payload['client_id'])
        return (json.dumps({'access_token': 'fresh-token', 'expires_in': 3600}), None)

    monkeypatch.setattr(mod, 'http_json', fake_http_json)
    assert mod.do_refresh(deadline=time.time() + 10) == 'fresh-token'
    # One 401 saved per refresh is the point: the env pair is offered first and
    # the walk stops there, so the dead constants never see a request.
    assert offered == ['env-id']

