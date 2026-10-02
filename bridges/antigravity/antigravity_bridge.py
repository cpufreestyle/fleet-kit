#!/usr/bin/env python3
# antigravity2codex: Google Antigravity IDE models -> OpenAI-compatible bridge (port 8797)
# Upstream: cloudcode-pa.googleapis.com v1internal (Antigravity OAuth client, auto-refresh)
# Catalog extracted from /Applications/Antigravity.app/Contents/Resources/bin/language_server
# Token: ~/.gemini/jetski-standalone-oauth-token (shared with gemini2codex)
import json, os, socket, sys, threading, time, uuid
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
import _basehttp

PORT = int(os.environ.get('ANTIGRAVITY2CODEX_PORT', '8797'))
HOST = os.environ.get('ANTIGRAVITY2CODEX_HOST', '127.0.0.1')
TOKEN_FILE = os.path.expanduser('~/.gemini/jetski-standalone-oauth-token')
BASE = 'https://cloudcode-pa.googleapis.com/v1internal:'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
UA = 'Antigravity/2.12.2 (MacOS; arm64)'
IDE_TYPES = ('ANTIGRAVITY', 'GEMINI_CLI')
# The OAuth client pair is deliberately NOT hardcoded here: GitHub push protection
# rejects Google OAuth client ids/secrets, and every Antigravity install already
# ships the pair in its own binary. install.sh reads it out of
# /Applications/Antigravity.app/Contents/Resources/bin/language_server with
# extract_client.py and injects it through fleet.env -> launchd. To set it by hand:
#   ANTIGRAVITY_OAUTH_CLIENT_ID / ANTIGRAVITY_OAUTH_CLIENT_SECRET   (primary pair)
#   ANTIGRAVITY_LEGACY_CLIENTS     id:secret,id:secret,...          (fallbacks)
#   ANTIGRAVITY_LEGACY_CLIENT_ID / ANTIGRAVITY_LEGACY_CLIENT_SECRET (single fallback)
# do_refresh() walks the list and keeps the first pair Google accepts, so stale
# pairs cost one failed round trip instead of taking the bridge down.
def _load_oauth_pairs():
    pairs = []

    def add(client_id, client_secret):
        client_id = (client_id or '').strip()
        client_secret = (client_secret or '').strip()
        if client_id and client_secret and (client_id, client_secret) not in pairs:
            pairs.append((client_id, client_secret))

    add(os.environ.get('ANTIGRAVITY_OAUTH_CLIENT_ID'),
        os.environ.get('ANTIGRAVITY_OAUTH_CLIENT_SECRET'))
    for item in (os.environ.get('ANTIGRAVITY_LEGACY_CLIENTS') or '').split(','):
        item = item.strip()
        if item:
            add(*item.split(':', 1))
    add(os.environ.get('ANTIGRAVITY_LEGACY_CLIENT_ID'),
        os.environ.get('ANTIGRAVITY_LEGACY_CLIENT_SECRET'))
    return pairs


CLIENT_CANDIDATES = _load_oauth_pairs()
OAUTH_MISSING = (
    'antigravity oauth client missing: set ANTIGRAVITY_OAUTH_CLIENT_ID and '
    'ANTIGRAVITY_OAUTH_CLIENT_SECRET (see bridges/antigravity/extract_client.py)'
)
# Slack id / @alias as extracted from the Antigravity language_server binary.
MODELS = [
    'claude-opus-4-8@default',
    'claude-opus-4-6@default',
    'claude-opus-4-5@20251101',
    'claude-sonnet-4-5@20250929',
    'claude-haiku-4-5@20251001',
    'gemini-3.1-pro-preview',
    'gemini-3-pro-preview',
    'gemini-3-flash-preview',
    'gemini-2.5-pro',
    'gemini-2.5-flash',
    'gpt-oss-120b-maas',
    'gpt-oss-20b-maas',
]

ST = {'at': None, 'exp': 0.0, 'project': None, 'tier': None, 'client_ok': None,
      'ide': IDE_TYPES[0], 'mtime': 0.0, 'calls': 0, 'last_model': None, 'last_ok': None}

class UpstreamError(Exception):
    pass


def google_validation_url(body):
    """The verification link inside a Google 403, when the gate asks for one.

    Measured 2026-10-02: cloudcode-pa answers the VALI gate with 403 plus
    ErrorInfo{reason: VALI, metadata.validation_url}. That link is the whole
    fix -- the login itself is fine and only the account has to pass a browser
    check -- and it sits deep in a JSON body the 502 envelope clips to 300
    chars, so a truncated body reads as a bare "verify your account" dead end.
    """
    try:
        j = json.loads(body)
    except Exception:
        return None
    try:
        for d in j["error"]["details"]:
            u = (d.get("metadata") or {}).get("validation_url")
            if u:
                return u
    except Exception:
        pass
    return None


class AccountVerification(UpstreamError):
    """Google wants the account verified in a browser before more calls.

    Raised instead of a raw UpstreamError so the link survives the 300-char
    clip in do_POST: a truncated accounts.google.com/signin/continue/... URL
    is worthless to the operator reading the 502.
    """

    def __init__(self, url):
        super().__init__("HTTP 403 VALIDATION_REQUIRED; account verification "
                         "required, open: " + url)
        self.validation_url = url


def _clip(exc):
    """Message for the 502 envelope -- long enough to keep a verify link."""
    if getattr(exc, "validation_url", None):
        return str(exc)
    return str(exc)[:300]

# 单次 chat 的总时限。call_model 会依次试 model_variants × IDE_TYPES，每次都带
# timeout=180，加上 get_access() 里的多客户端 refresh，最坏能挂好几分钟；客户端
# 远早于此就断开，只剩 BrokenPipeError，健康检查于是误判 BRIDGE_DOWN。
CHAT_BUDGET = float(os.environ.get('ANTIGRAVITY_CHAT_BUDGET') or '60')
BRIDGE_KEY = os.environ.get('ANTIGRAVITY2CODEX_KEY') or ''
# 同上：预算烧完后的最低时限，只为避免把 0/负数当 timeout 交给上游。
FALLBACK_FLOOR = float(os.environ.get('ANTIGRAVITY_FALLBACK_FLOOR') or '1.0')

# Same rule as the gemini bridge: urlopen() gives every address getaddrinfo()
# returns the full timeout (cloudcode-pa.googleapis.com resolves to 16), so a
# blackholed address family turns one 180s timeout into 16 of them. Cap each
# connect attempt at the time left on this request's deadline. Thread-local so
# concurrent requests under ThreadingHTTPServer stay independent.
_tls = threading.local()
_real_create_connection = socket.create_connection


def _arm_deadline(when):
    _tls.deadline = when


def _disarm_deadline():
    _tls.deadline = None


def _budgeted_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                                source_address=None, **kwargs):
    """socket.create_connection that charges every address to the deadline.

    The stdlib hands each address getaddrinfo() returns the same timeout, so
    one blocked urlopen() costs N x timeout -- and cloudcode-pa.googleapis.com
    resolves to 16 of them (8 IPv6 first). Measured 2026-09-29 behind this VPN:
    a 20s timeout cost 40s on oauth2.googleapis.com's two addresses, which is
    how a 60s CHAT_BUDGET still produced a 90s request. Walk the addresses here
    and cap each attempt at the time that is actually left, so the total -- not
    just the first connect -- stays inside the budget.
    """
    deadline = getattr(_tls, 'deadline', None)
    if deadline is None:
        return _real_create_connection(address, timeout, source_address, **kwargs)
    host, port = address[:2]
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except OSError:
        infos = []
    requested = None if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
    last = None
    for af, socktype, proto, _canon, sa in infos:
        left = deadline - time.time()
        if left <= 0:
            last = OSError('request budget exhausted')
            break
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            sock.settimeout(left if requested is None else min(requested, left))
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)
            return sock
        except OSError as exc:
            if sock is not None:
                sock.close()
            last = exc
    if last is not None:
        raise last
    # getaddrinfo itself failed; resolution surfaces immediately, so let the
    # stdlib raise the familiar error.
    return _real_create_connection(address, timeout, source_address, **kwargs)


socket.create_connection = _budgeted_create_connection


# 上游代理开关：与 gemini 桥同一用意。urllib 默认吃 macOS 系统代理，而本机
# 2026-10-02 的系统代理（MacPacket）没有国际路由，cloudcode-pa.googleapis.com
# 一律 000 / ProxyError 503。ANTIGRAVITY_UPSTREAM_PROXY 可显式指定出口。
UPSTREAM_PROXY = 'ANTIGRAVITY_UPSTREAM_PROXY'


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
        return r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'ignore')
        vurl = google_validation_url(raw)
        if vurl:
            raise AccountVerification(vurl)
        raise UpstreamError('HTTP %s %s: %s' % (e.code, url, raw[:400]))
    except Exception as e:
        raise UpstreamError('%s: %s' % (type(e).__name__, str(e)[:200]))

def read_token_file():
    with open(TOKEN_FILE) as f:
        return json.load(f)

def write_token_file(d):
    tmp = TOKEN_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, TOKEN_FILE)

def do_refresh():
    d = read_token_file()
    tok = d.get('token', {})
    rt = tok.get('refresh_token')
    if not rt:
        raise UpstreamError('no refresh_token in ' + TOKEN_FILE + ' (log in to Antigravity or Gemini CLI first)')
    if not CLIENT_CANDIDATES:
        raise UpstreamError(OAUTH_MISSING)
    last = None
    for cid, csec in CLIENT_CANDIDATES:
        payload = {'client_id': cid, 'client_secret': csec, 'refresh_token': rt, 'grant_type': 'refresh_token'}
        try:
            raw, _ = http_json(TOKEN_URL, payload, timeout=30)
        except UpstreamError as e:
            last = e
            continue
        j = json.loads(raw)
        ST['at'] = j['access_token']
        ST['exp'] = time.time() + j.get('expires_in', 3600) - 60
        ST['client_ok'] = cid
        tok['access_token'] = j['access_token']
        tok['expiry'] = time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(ST['exp']))
        d['token'] = tok
        try:
            write_token_file(d)
        except Exception:
            pass
        return ST['at']
    raise UpstreamError('refresh failed for all clients: ' + str(last))

def get_access():
    try:
        mt = os.path.getmtime(TOKEN_FILE)
        if mt > ST['mtime']:
            ST['mtime'] = mt
            d = read_token_file()
            tok = d.get('token', {})
            at, exp = tok.get('access_token'), tok.get('expiry')
            if at and exp:
                try:
                    et = time.mktime(time.strptime(exp[:19], '%Y-%m-%dT%H:%M:%S'))
                except ValueError:
                    et = None
                if et and et > time.time() + 30:
                    ST['at'], ST['exp'] = at, et
    except Exception:
        pass
    if ST['at'] and time.time() < ST['exp']:
        return ST['at']
    return do_refresh()

def meta_for(ide=None):
    return {'ideType': ide or ST['ide'], 'pluginType': 'GEMINI', 'platform': 'PLATFORM_UNSPECIFIED'}

def load_code_assist(ide=None):
    # loadCodeAssist is the only endpoint that accepts a metadata field;
    # the chat body must never carry one. Pass ide to re-resolve under
    # that identity (the IDE fallback chain uses it).
    if ide is None and ST['project'] is not None:
        return
    at = get_access()
    last = None
    for candidate in ([ide] if ide else IDE_TYPES):
        try:
            raw, _ = http_json(BASE + 'loadCodeAssist', {'metadata': meta_for(candidate)},
                               headers={'Authorization': 'Bearer ' + at}, timeout=30)
            j = json.loads(raw)
        except UpstreamError as e:
            last = e
            continue
        ST['project'] = j.get('cloudaicompanionProject') or ''
        tier = j.get('currentTier') or {}
        ST['tier'] = tier.get('id') if isinstance(tier, dict) else None
        ST['ide'] = candidate
        return
    raise UpstreamError('loadCodeAssist failed: ' + str(last))

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

def text_from_codeassist(j):
    parts = []
    try:
        for p in j['candidates'][0]['content']['parts']:
            if 'text' in p:
                parts.append(p['text'])
    except Exception:
        pass
    return ''.join(parts)

def parse_sse(raw):
    out = []
    for line in raw.decode('utf-8', 'ignore').split(chr(10)):
        line = line.strip()
        if not line.startswith('data:'):
            continue
        try:
            j = json.loads(line[5:].strip())
        except Exception:
            continue
        if 'error' in j:
            raise UpstreamError('codeassist error: ' + json.dumps(j['error'])[:300])
        out.append(text_from_codeassist(j))
    return ''.join(out)

def model_variants(model):
    if '@' in model:
        return [model, model.split('@', 1)[0]]
    return [model, model + '@default']

def looks_like_model_error(err):
    msg = str(err)
    return ('404' in msg or 'not found' in msg.lower() or 'not supported' in msg.lower()
            or 'invalid model' in msg.lower() or 'unknown model' in msg.lower())

def call_upstream(model, msgs, stream, timeout=180, ide=None):
    at = get_access()
    load_code_assist(ide)
    contents, sysinst = to_contents(msgs)
    inner = {'contents': contents, 'generationConfig': {'temperature': 0.7}}
    if sysinst:
        inner['systemInstruction'] = sysinst
    # v1internal:generateContent has no metadata field: Google answers 400
    # INVALID_ARGUMENT (Unknown name metadata) before it even looks at the
    # model, so every chat request used to be rejected outright. The IDE
    # identity is applied on loadCodeAssist instead.
    body = {'model': model, 'request': inner}
    if ST['project']:
        body['project'] = ST['project']
    if stream:
        url = BASE + 'streamGenerateContent?alt=sse'
        raw, _ = http_json(url, body, headers={'Authorization': 'Bearer ' + at}, timeout=timeout)
        return parse_sse(raw)
    raw, _ = http_json(BASE + 'generateContent', body,
                       headers={'Authorization': 'Bearer ' + at}, timeout=timeout)
    j = json.loads(raw)
    if not text_from_codeassist(j) and 'error' in j:
        raise UpstreamError('codeassist error: ' + json.dumps(j['error'])[:300])
    return text_from_codeassist(j)

def call_model(model, msgs, stream, budget=None):
    """Try each variant, but never longer than one overall budget.

    Every attempt used to carry timeout=180, so a blocked upstream turned into
    a multi-minute hang whose only visible trace was a BrokenPipeError when the
    client gave up. The deadline bounds the whole fallback chain instead.
    """
    budget = CHAT_BUDGET if budget is None else budget
    deadline = time.time() + budget
    _arm_deadline(deadline)

    def left():
        return max(FALLBACK_FLOOR, deadline - time.time())

    def spent():
        return time.time() >= deadline

    try:
        return _walk_variants(model, msgs, stream, left, spent)
    finally:
        _disarm_deadline()


def _walk_variants(model, msgs, stream, left, spent):
    first = None
    # 1) model-id alias fallback (@default form vs bare id)
    for variant in model_variants(model):
        try:
            out = call_upstream(variant, msgs, stream, left())
            ST['last_model'] = variant
            ST['last_ok'] = True
            return out
        except UpstreamError as e:
            if first is None:
                first = e
            if not looks_like_model_error(e) or spent():
                break
    # 2) ide metadata fallback (ANTIGRAVITY -> GEMINI_CLI)
    for ide in IDE_TYPES[1:]:
        if spent():
            break
        try:
            out = call_upstream(model, msgs, stream, left(), ide=ide)
            ST['last_model'] = model
            ST['last_ok'] = True
            return out
        except UpstreamError:
            pass
    ST['last_ok'] = False
    raise first or UpstreamError('all variants failed')

def prompt_from_messages(msgs):
    parts = []
    for m in msgs:
        role = m.get('role', 'user')
        content = m.get('content', '')
        if isinstance(content, list):
            content = ' '.join(str(c.get('text', '')) for c in content if isinstance(c, dict))
        parts.append(('Assistant' if role == 'assistant' else 'User') + ': ' + str(content))
    return (chr(10) * 2).join(parts) + chr(10) * 2 + 'Assistant:'

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
            # The caller already timed out and hung up; a traceback here only
            # buries the real upstream error in the log. end_headers() writes
            # the header block through wfile too, so it belongs inside this
            # guard -- that is where the BrokenPipeError actually surfaced.
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
            data = {'object': 'list', 'data': [{'id': m, 'object': 'model', 'created': now, 'owned_by': 'antigravity'} for m in MODELS]}
            self._send(200, json.dumps(data))
        elif self.path.startswith('/health') or self.path == '/':
            info = {'status': 'ok', 'account': os.environ.get('ANTIGRAVITY_ACCOUNT_LABEL', 'google-antigravity'),
                    'tier': ST['tier'], 'project': bool(ST['project']), 'ide': ST['ide'],
                    'client_ok': bool(ST['client_ok']), 'models': len(MODELS),
                    'oauth': len(CLIENT_CANDIDATES),
                    'calls': ST['calls'], 'last_model': ST['last_model'], 'last_ok': ST['last_ok'],
                    'upstream_proxy': proxy_info()}
            self._send(200, json.dumps(info))
        else:
            self._send(404, json.dumps({'error': 'not found'}))

    def do_POST(self):
        if not self._check_key():
            self._deny()
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
        ST['calls'] += 1
        try:
            text = call_model(model, msgs, stream)
        except Exception as e:
            self._send(502, json.dumps({'error': {'message': _clip(e), 'type': 'upstream_error'}}))
            return
        if not text:
            text = '[EMPTY-UPSTREAM]'
        if stream:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()
            for piece in [text[i:i+40] for i in range(0, len(text), 40)]:
                ch = {'id': cid, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {'content': piece}}]}
                self.wfile.write(('data: ' + json.dumps(ch) + chr(10) * 2).encode())
                self.wfile.flush()
            self.wfile.write(b'data: [DONE]' + (chr(10) * 2).encode())
            self.wfile.flush()
        else:
            resp = {'id': cid, 'object': 'chat.completion', 'created': int(time.time()), 'model': model,
                    'channel': 'antigravity-code-assist',
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': len(prompt_from_messages(msgs)) // 4, 'completion_tokens': len(text) // 4, 'total_tokens': (len(text) + len(prompt_from_messages(msgs))) // 4}}
            self._send(200, json.dumps(resp))

H = _basehttp.install_basehttp_guard(H)

if __name__ == '__main__':
    srv = ThreadingHTTPServer((HOST, PORT), H)
    sys.stderr.write('antigravity2codex bridge on %s:%d (%d models, A: code-assist)' % (HOST, PORT, len(MODELS)) + chr(10))
    srv.serve_forever()
