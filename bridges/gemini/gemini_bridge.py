#!/usr/bin/env python3
# gemini2codex: Google One / Gemini Pro -> OpenAI-compatible bridge (port 8794)
# Channel A: cloudcode-pa.googleapis.com v1internal (OAuth consumer client, auto-refresh)
# Channel B: gemini.google.com web StreamGenerate (cookie fallback)
import json, os, re, socket, sys, threading, time, uuid
from pathlib import Path
import urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
import _basehttp
import _googlecode

PORT = int(os.environ.get('GEMINI2CODEX_PORT', '8794'))
HOST = os.environ.get('GEMINI2CODEX_HOST', '127.0.0.1')
TOKEN_FILE = os.path.expanduser('~/.gemini/jetski-standalone-oauth-token')
COOKIE_FILE = os.path.expanduser('~/.gemini2codex/cookies.txt')


def _load_oauth_pairs():
    # Google OAuth client pairs live in fleet.env, exactly like the antigravity
    # bridge: push protection rejects Google client secrets, so the live pair
    # never lands in git and install.sh re-extracts it from the Antigravity app
    # on every re-install. The constants at the bottom are last-resort fallbacks
    # -- do_refresh() walks the whole list and keeps the first pair Google
    # accepts, so a revoked pair costs one failed round trip, not the bridge.
    pairs = []

    def add(client_id, client_secret):
        client_id = (client_id or '').strip()
        client_secret = (client_secret or '').strip()
        if client_id and client_secret and (client_id, client_secret) not in pairs:
            pairs.append((client_id, client_secret))

    add(os.environ.get('GEMINI_OAUTH_CLIENT_ID'),
        os.environ.get('GEMINI_OAUTH_CLIENT_SECRET'))
    for item in (os.environ.get('GEMINI_LEGACY_CLIENTS') or '').split(','):
        item = item.strip()
        if item:
            add(*item.split(':', 1))
    add(os.environ.get('GEMINI_LEGACY_CLIENT_ID'),
        os.environ.get('GEMINI_LEGACY_CLIENT_SECRET'))
    add('681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com',
        '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')
    add('764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com',
        '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')
    return pairs


CLIENT_CANDIDATES = _load_oauth_pairs()
MODELS = ['gemini-3-pro-preview', 'gemini-2.5-pro', 'gemini-2.5-flash', 'gemini-3-flash-preview']
UA = 'GeminiCLI/0.60.0 (MacOS; arm64)'

_tls = threading.local()


def _fresh_state():
    # file_mtime lives here, not in a shared dict: it is what tells get_access()
    # that the token file on disk changed -- a rotated account, or a refresh
    # written by another process.
    return {'at': None, 'exp': 0.0, 'project': None, 'tier': None,
            'client_ok': None, 'last_channel': None, 'file_mtime': 0.0}


# Per-request account context. Each request runs on its own thread under
# ThreadingHTTPServer, so "which Google account am I" and "what did I cache for
# it" must live in thread-local storage: two accounts in flight at the same time
# must never read each other's access token.
def _st():
    s = getattr(_tls, 'state', None)
    if s is None:
        s = _fresh_state()
        _tls.state = s
    return s


def _token_file():
    return getattr(_tls, 'token_file', TOKEN_FILE)


def _cookie_file():
    return getattr(_tls, 'cookie_file', COOKIE_FILE)


# One bridge, several Google accounts: auths/<label>/{token.json,cookies.txt}.
# With nothing imported, the pool is one synthetic account that still points at
# the official login files, so single-account use is unchanged.
AUTH_DIR = os.environ.get('GEMINI_AUTH_DIR') or os.path.expanduser('~/.gemini2codex/auths')
ACCOUNT_COOLDOWN = float(os.environ.get('GEMINI_ACCOUNT_COOLDOWN') or '120')


class _AcctFail(Exception):
    # Raised when one account is out of tries, so the next candidate gets a turn
    # instead of the whole request failing. It carries the per-channel breakdown
    # so the 502 envelope keeps the exact shape it always had.
    def __init__(self, message, channels=None):
        super().__init__(message)
        self.channels = channels or {}


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gemini_accounts as _ga

_ACCOUNT_POOL = None


def _pool():
    global _ACCOUNT_POOL
    if _ACCOUNT_POOL is None:
        _ACCOUNT_POOL = _ga.GeminiAccountPool(
            Path(AUTH_DIR), legacy_token=Path(TOKEN_FILE), legacy_cookie=Path(COOKIE_FILE))
    return _ACCOUNT_POOL


def _apply_account(cand):
    _tls.token_file = cand.token_file_for_bridge()
    _tls.cookie_file = cand.cookie_file_for_bridge()
    _tls.state = _fresh_state()

class UpstreamError(Exception):
    pass

# Google's Code Assist endpoint is the same one antigravity talks to, so the
# VALI-403 unwrap, the unclipped verification exception and the token-file
# read/write live in _googlecode.py. What stays here is what only this bridge
# does: channel B's cookies, and raising the shared exception in its own
# UpstreamError vocabulary.
AccountVerification = _googlecode.make_account_verification(UpstreamError)
google_validation_url = _googlecode.google_validation_url
_clip = _googlecode.clip


# 单次 chat 的总时限。原先 call_a(180s) 失败后再 call_b(180s)，最坏要 6 分钟
# 才想起来回 502；客户端（Codex / 探测脚本）远早于此就超时断开，于是 502 写回
# 管道时只剩 BrokenPipeError，健康检查也把这座桥误判成 BRIDGE_DOWN。
CHAT_BUDGET = float(os.environ.get('GEMINI_CHAT_BUDGET') or '60')
BRIDGE_KEY = os.environ.get('GEMINI2CODEX_KEY') or ''
# 预算烧完之后，兜底通道仍能拿到的最低时限。它只负责别把 0/负数交给
# urllib，不足以再变成一次完整超时——那正是 180s + 180s 的由来。
FALLBACK_FLOOR = float(os.environ.get('GEMINI_FALLBACK_FLOOR') or '1.0')

# Every outbound connect is charged to the request budget, so a black-holed
# address family cannot turn one 180s timeout into sixteen of them (see
# _basehttp for the measurement). Thread-local, so concurrent requests under
# ThreadingHTTPServer stay independent.
_arm_deadline = _basehttp.arm_deadline
_disarm_deadline = _basehttp.disarm_deadline
_basehttp.install_budgeted_connect()


def _left(deadline, default):
    """Wall clock left before the deadline, never below FALLBACK_FLOOR.

    deadline=None means "no budget was handed down", so the caller keeps its
    historical timeout -- that is what the out-of-request paths (smoke test,
    health) expect.
    """
    if deadline is None:
        return default
    return max(FALLBACK_FLOOR, deadline - time.time())


def _spent(deadline):
    return deadline is not None and time.time() >= deadline


# 上游代理开关。urllib 默认吃 macOS 系统代理；2026-10-02 实测本机系统代理
# （MacPacket）没有国际路由，google 域名一律 000 / ProxyError 503，与账号是否
# 验证无关。GEMINI_UPSTREAM_PROXY 显式指定出口（例如 http://127.0.0.1:7890），
# 设了就走它，没设沿用系统代理。/health 的 upstream_proxy 报告当前生效的出口。
UPSTREAM_PROXY = 'GEMINI_UPSTREAM_PROXY'


def proxy_info() -> dict:
    """当前上游出口：{'proxy': url|None, 'source': 'env'|'system'|'direct'}。"""
    explicit = (os.environ.get(UPSTREAM_PROXY) or '').strip()
    if explicit:
        return {'proxy': explicit, 'source': 'env'}
    try:
        env_proxies = urllib.request.getproxies() or {}
    except Exception:
        env_proxies = {}
    system = env_proxies.get('https') or env_proxies.get('http') or ''
    return {'proxy': system or None, 'source': 'system' if system else 'direct'}


def _urlopen(req, timeout):
    """按 UPSTREAM_PROXY 走出口；未设置时与 urllib.request.urlopen 等价。"""
    info = proxy_info()
    if info['source'] == 'env':
        handler = urllib.request.ProxyHandler({'http': info['proxy'],
                                               'https': info['proxy']})
        return urllib.request.build_opener(handler).open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def http_json(url, payload, headers=None, method='POST', timeout=90):
    hdrs = {'User-Agent': UA, 'Content-Type': 'application/json;charset=UTF-8', 'Accept-Encoding': 'identity'}
    if headers:
        hdrs.update(headers)
    data = json.dumps(payload).encode() if payload is not None else None
    if method == 'GET':
        data = None
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        r = _urlopen(req, timeout)
        raw = r.read()
        enc = (r.headers.get('Content-Encoding') or '').lower()
        if 'gzip' in enc:
            import gzip as _gz
            raw = _gz.decompress(raw)
        elif 'br' in enc:
            try:
                import brotli as _br
                raw = _br.decompress(raw)
            except Exception:
                pass
        elif 'deflate' in enc:
            import zlib as _zl
            try:
                raw = _zl.decompress(raw)
            except _zl.error:
                raw = _zl.decompress(raw, -15)
        return raw, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'ignore')
        vurl = google_validation_url(raw)
        if vurl:
            raise AccountVerification(vurl)
        raise UpstreamError('HTTP %s %s: %s' % (e.code, url, raw[:400]))
    except Exception as e:
        raise UpstreamError('%s: %s' % (type(e).__name__, str(e)[:200]))

def read_token_file(path=None):
    p = path or _token_file()
    try:
        return _googlecode.read_json_file(p)
    except Exception as e:
        # Naming the path matters: an account that carries no token file has to
        # fail channel A with a reason a human can act on, not a bare
        # FileNotFoundError.
        raise UpstreamError('cannot read token file %s: %s' % (p, str(e)[:120]))

def write_token_file(d):
    _googlecode.write_json_file(_token_file(), d)

def do_refresh(deadline=None):
    d = read_token_file()
    rt = d.get('token', {}).get('refresh_token')
    if not rt:
        raise UpstreamError('no refresh_token in ' + _token_file() + ' (run: gemini login)')
    last = None
    for cid, csec in CLIENT_CANDIDATES:
        if _spent(deadline):
            break
        payload = {'client_id': cid, 'client_secret': csec, 'refresh_token': rt, 'grant_type': 'refresh_token'}
        try:
            raw, _ = http_json('https://oauth2.googleapis.com/token', payload,
                               timeout=_left(deadline, 30.0))
            j = json.loads(raw)
            _st()['at'] = j['access_token']
            _st()['exp'] = time.time() + j.get('expires_in', 3600) - 60
            _st()['client_ok'] = cid
            d['token']['access_token'] = j['access_token']
            d['token']['expiry'] = time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(_st()['exp']))
            try:
                write_token_file(d)
            except Exception:
                pass
            return _st()['at']
        except UpstreamError as e:
            last = e
            continue
    raise UpstreamError('refresh failed for all clients: ' + str(last))

def get_access(deadline=None):
    try:
        mt = os.path.getmtime(_token_file())
        if mt > _st().get('file_mtime', 0.0):
            _st()['file_mtime'] = mt
            d = read_token_file()
            tok = d.get('token', {})
            at, exp = tok.get('access_token'), tok.get('expiry')
            if at and exp:
                try:
                    et = time.mktime(time.strptime(exp[:19], '%Y-%m-%dT%H:%M:%S'))
                except ValueError:
                    et = None
                if et and et > time.time() + 30:
                    _st()['at'], _st()['exp'] = at, et
    except Exception:
        pass
    if _st()['at'] and time.time() < _st()['exp']:
        return _st()['at']
    return do_refresh(deadline)

def load_code_assist(deadline=None):
    if _st()['project'] is not None:
        return
    at = get_access(deadline)
    body = {'metadata': {'ideType': 'GEMINI_CLI', 'pluginType': 'GEMINI', 'platform': 'PLATFORM_UNSPECIFIED'}}
    raw, _ = http_json('https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist', body,
                       headers={'Authorization': 'Bearer ' + at}, timeout=_left(deadline, 30.0))
    j = json.loads(raw)
    _st()['project'] = j.get('cloudaicompanionProject') or ''
    tier = j.get('currentTier') or {}
    _st()['tier'] = tier.get('id') or (tier.get('name') if isinstance(tier, dict) else None)

def to_contents(msgs):
    contents, sys_parts = [], []
    for m in msgs:
        role = m.get('role', 'user')
        content = m.get('content', '')
        if isinstance(content, list):
            content = ' '.join(str(c.get('text', '')) for c in content if isinstance(c, dict))
        text = str(content)
        if role == 'system':
            sys_parts.append({'text': text})
            continue
        gr = 'model' if role == 'assistant' else 'user'
        contents.append({'role': gr, 'parts': [{'text': text}]})
    if not contents:
        contents = [{'role': 'user', 'parts': [{'text': 'ping'}]}]
    return contents, ({'parts': sys_parts} if sys_parts else None)

def call_a(model, msgs, stream, timeout=180, deadline=None):
    at = get_access(deadline)
    load_code_assist(deadline)
    contents, sysinst = to_contents(msgs)
    inner = {'contents': contents, 'generationConfig': {'temperature': 0.7}}
    if sysinst:
        inner['systemInstruction'] = sysinst
    body = {'model': model, 'request': inner}
    if _st()['project']:
        body['project'] = _st()['project']
    if stream:
        url = 'https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse'
        raw, _ = http_json(url, body, headers={'Authorization': 'Bearer ' + at},
                           timeout=_left(deadline, timeout))
        return parse_sse(raw)
    raw, _ = http_json('https://cloudcode-pa.googleapis.com/v1internal:generateContent', body,
                       headers={'Authorization': 'Bearer ' + at},
                       timeout=_left(deadline, timeout))
    return text_from_gemini(json.loads(raw))

def text_from_gemini(j):
    parts = []
    try:
        for p in j['candidates'][0]['content']['parts']:
            if 'text' in p:
                parts.append(p['text'])
    except Exception:
        pass
    if not parts and 'error' in j:
        raise UpstreamError('codeassist error: ' + json.dumps(j['error'])[:300])
    return ''.join(parts)

def parse_sse(raw):
    out = []
    for line in raw.decode('utf-8', 'ignore').split('\n'):
        line = line.strip()
        if not line.startswith('data:'):
            continue
        try:
            j = json.loads(line[5:].strip())
            out.append(text_from_gemini(j))
        except UpstreamError:
            raise
        except Exception:
            continue
    return ''.join(out)

def web_cookies():
    if not os.path.exists(_cookie_file()):
        return ''
    txt = open(_cookie_file(), encoding='utf-8', errors='ignore').read()
    m = re.search(r'__Secure-1PSID=([^;\s]+)', txt)
    m2 = re.search(r'__Secure-1PSIDTS=([^;\s]+)', txt)
    if not m:
        return ''
    h = '__Secure-1PSID=' + m.group(1)
    if m2:
        h += '; __Secure-1PSIDTS=' + m2.group(1)
    return h

def call_b(prompt, timeout=180):
    ck = web_cookies()
    if not ck:
        raise UpstreamError('web cookie missing (run extract_cookies.py)')
    hdrs = {'User-Agent': UA, 'Cookie': ck}
    # This page fetch used to carry its own hardcoded 30s timeout, so a blocked
    # upstream cost 30s on top of whatever channel A had already burned --
    # more than the whole CHAT_BUDGET. Charge the budget for it as well.
    page_timeout = min(30.0, max(1.0, timeout))
    raw, _ = http_json('https://gemini.google.com/app', None, headers=hdrs,
                       method='GET', timeout=page_timeout)
    html = raw.decode('utf-8', 'ignore')
    m = re.search(r'SNlM0e[\",: ]{2,6}([A-Za-z0-9_-]{6,})', html)
    if not m:
        raise UpstreamError('SNlM0e not found (web session expired?)')
    at = m.group(1)
    inner = [None, json.dumps([prompt]), None, None, None, None, None, None, None, None, None, 1]
    body = {'f.req': json.dumps([None, json.dumps(inner)]), 'at': at}
    url = 'https://gemini.google.com/_/BardChatUi/data/assistant.lamja.BardFrontendService/StreamGenerate'
    raw2, _ = http_json(url, body, headers=hdrs, timeout=timeout)
    out = []
    for line in raw2.decode('utf-8', 'ignore').split('\n'):
        line = line.strip()
        if not line.startswith('['):
            continue
        try:
            arr = json.loads(line)
            b = json.loads(arr[0][2])
            out.append(b[4][0][1][0])
        except Exception:
            continue
    return ''.join(out)

def prompt_from_messages(msgs):
    parts = []
    for m in msgs:
        role = m.get('role', 'user')
        content = m.get('content', '')
        if isinstance(content, list):
            content = ' '.join(str(c.get('text', '')) for c in content if isinstance(c, dict))
        parts.append(('Assistant' if role == 'assistant' else 'User') + ': ' + str(content))
    return '\n\n'.join(parts) + '\n\nAssistant:'

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj, hdrs=None):
        b = obj.encode() if isinstance(obj, str) else obj
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(b)))
            if hdrs:
                for k, v in hdrs.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError):
            # The caller already timed out and hung up. Nothing to report to
            # anyone, and a traceback here just buries the real error.
            # end_headers() writes the header block through wfile as well, so
            # it has to be inside this guard too -- see the 60KB of
            # BrokenPipeError tracebacks it produced on 2026-09-29.
            pass

    def _check_key(self):
        """Same contract as _common.check_bridge_auth.

        An unset key leaves the bridge open (it only listens on 127.0.0.1);
        a set key requires the exact Authorization header. install.sh mints
        one per bridge, so the fleet tooling already sends it.
        """
        if not BRIDGE_KEY:
            return True
        return (self.headers.get('Authorization') or '') == (
            'Bearer ' + BRIDGE_KEY)

    def _deny(self):
        self._send(401, json.dumps({'error': {
            'message': 'invalid bridge key',
            'type': 'auth_error'}}))

    def do_GET(self):
        if self.path.startswith('/v1/models'):
            if not self._check_key():
                self._deny()
                return
            now = int(time.time())
            data = {'object': 'list', 'data': [{'id': m, 'object': 'model', 'created': now, 'owned_by': 'google-one'} for m in MODELS]}
            self._send(200, json.dumps(data))
        elif self.path.startswith('/__gemini/accounts'):
            if not self._check_key():
                self._deny()
                return
            self._send(200, json.dumps({'status': 'ok', 'account_pool': _pool().summary()}))
        elif self.path.startswith('/health') or self.path == '/':
            info = {'status': 'ok', 'account': os.environ.get('GEMINI_ACCOUNT_LABEL', 'google-one'),
                    'tier': _st()['tier'], 'project': bool(_st()['project']), 'last_channel': _st()['last_channel'],
                    'upstream_proxy': proxy_info()}
            self._send(200, json.dumps(info))
        else:
            self._send(404, json.dumps({'error': 'not found'}))

    def _body_json(self):
        n = int(self.headers.get('Content-Length', 0) or 0)
        raw = self.rfile.read(n) if n > 0 else b'{}'
        try:
            data = json.loads(raw or b'{}')
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {}

    def _accounts_route(self):
        # Same response contract as the workbuddy dashboard (/ui/accounts/*):
        # {"status": "ok", "account_pool": pool.summary()}, with 4xx and
        # {"detail": ...} on refusal. One client can therefore drive either.
        body = self._body_json()
        try:
            if self.path.startswith('/__gemini/accounts/primary'):
                try:
                    _pool().set_primary(str(body.get('ref') or ''))
                except KeyError:
                    self._send(404, json.dumps({'detail': 'account not found'}))
                    return
                self._send(200, json.dumps({'status': 'ok', 'account_pool': _pool().summary()}))
                return
            if self.path.startswith('/__gemini/accounts/import-current'):
                # Snapshots the account that gemini CLI is currently logged in
                # as, so a second identity costs one command, not a manual copy
                # of files out of ~/.gemini.
                label = str(body.get('label') or '').strip()
                try:
                    account = _pool().import_current(TOKEN_FILE, COOKIE_FILE, label)
                except (ValueError, RuntimeError, OSError) as exc:
                    self._send(400, json.dumps({'detail': str(exc)}))
                    return
                self._send(200, json.dumps({'status': 'ok', 'account': account,
                                            'account_pool': _pool().summary()}))
                return
            if self.path.startswith('/__gemini/accounts/remove'):
                try:
                    _pool().remove(str(body.get('ref') or ''))
                except KeyError:
                    self._send(404, json.dumps({'detail': 'account not found'}))
                    return
                except (RuntimeError, OSError) as exc:
                    self._send(400, json.dumps({'detail': str(exc)}))
                    return
                self._send(200, json.dumps({'status': 'ok', 'account_pool': _pool().summary()}))
                return
            if self.path.startswith('/__gemini/accounts/refresh'):
                _pool().reload()
                self._send(200, json.dumps({'status': 'ok', 'account_pool': _pool().summary()}))
                return
            self._send(404, json.dumps({'detail': 'not found'}))
        except Exception as exc:
            self._send(500, json.dumps({'detail': str(exc)[:300]}))

    def do_POST(self):
        if not self._check_key():
            self._deny()
            return
        if self.path.startswith('/__gemini/accounts'):
            self._accounts_route()
            return
        if not self.path.startswith('/v1/chat/completions'):
            self._send(404, json.dumps({'error': 'not found'}))
            return
        n = int(self.headers.get('Content-Length', 0))
        req = json.loads(self.rfile.read(n) or b'{}')
        stream = bool(req.get('stream'))
        model = req.get('model', MODELS[0])
        msgs = req.get('messages', [])
        cid = 'chatcmpl-' + uuid.uuid4().hex[:24]
        text, channel, err, failed_account = '', None, None, None
        deadline = time.time() + CHAT_BUDGET
        _arm_deadline(deadline)
        pool = _pool()
        # A cooling account is skipped, but if every one of them is cooling the
        # pool is still the right thing to try: a stale cooldown is better than
        # an unanswered request.
        cands = pool.candidates() or pool.candidates(ignore_cooldown=True)
        last = 'no usable gemini account'
        try:
            for cand in cands:
                _apply_account(cand)
                try:
                    try:
                        text = call_a(model, msgs, stream, deadline=deadline)
                        channel = 'code-assist'
                    except Exception as e1:
                        left = max(FALLBACK_FLOOR, deadline - time.time())
                        try:
                            text = call_b(prompt_from_messages(msgs), timeout=left)
                            channel = 'gemini-web'
                        except Exception as e2:
                            raise _AcctFail('code_assist: %s; web: %s' % (_clip(e1), _clip(e2)),
                                            {'code_assist': _clip(e1), 'web': _clip(e2)})
                    pool.mark_success(cand.ref)
                    err = None
                    break
                except _AcctFail as af:
                    last = str(af)[:300]
                    pool.mark_failure(cand.ref, last, ACCOUNT_COOLDOWN)
                    # error.channels keeps its documented keys; which credential
                    # burned travels one level up as error.account. The pool
                    # walks several of them on the way to a 502, so the first one
                    # wins: it is the account an operator pinned on purpose.
                    err = dict(af.channels)
                    if failed_account is None:
                        failed_account = cand.label
                    sys.stderr.write('[gemini-fleet] account %s failed: %s\n' % (cand.label, last[:160]))
                    continue
            else:
                # The pool only runs dry when there was nothing to try. When a
                # candidate did answer and left its per-channel breakdown in
                # err, error.channels must keep its documented {code_assist,
                # web} keys instead of collapsing to a bare message.
                if err is None:
                    err = {'message': last}
        finally:
            _disarm_deadline()
        if err:
            # error.message must be a string: an OpenAI-compatible client reads
            # it as text, and the per-channel dict it used to carry turned the
            # envelope into a nested object. The breakdown stays, one level down,
            # and the credential that failed rides along as error.account.
            detail = '; '.join('%s: %s' % (k, v) for k, v in err.items())
            envelope = {'message': detail, 'type': 'upstream_error', 'channels': err}
            if failed_account:
                envelope['account'] = failed_account
            self._send(502, json.dumps({'error': envelope}))
            return
        if not text:
            text = '[EMPTY-UPSTREAM]'
        _st()['last_channel'] = channel
        if stream:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()
            for piece in [text[i:i+40] for i in range(0, len(text), 40)]:
                ch = {'id': cid, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {'content': piece}}]}
                self.wfile.write(('data: ' + json.dumps(ch) + '\n\n').encode())
                self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        else:
            resp = {'id': cid, 'object': 'chat.completion', 'created': int(time.time()), 'model': model,
                    'channel': channel,
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': len(prompt_from_messages(msgs)) // 4, 'completion_tokens': len(text) // 4, 'total_tokens': (len(text) + len(prompt_from_messages(msgs))) // 4}}
            self._send(200, json.dumps(resp))

H = _basehttp.install_basehttp_guard(H)

if __name__ == '__main__':
    srv = ThreadingHTTPServer((HOST, PORT), H)
    sys.stderr.write('gemini2codex bridge on %s:%d (A: code-assist, B: gemini-web)\n' % (HOST, PORT))
    srv.serve_forever()
