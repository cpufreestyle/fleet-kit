#!/usr/bin/env python3
# gemini2codex: Google One / Gemini Pro -> OpenAI-compatible bridge (port 8794)
# Channel A: cloudcode-pa.googleapis.com v1internal (OAuth consumer client, auto-refresh)
# Channel B: gemini.google.com web StreamGenerate (cookie fallback)
import json, os, re, socket, sys, threading, time, uuid
import urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get('GEMINI2CODEX_PORT', '8794'))
HOST = os.environ.get('GEMINI2CODEX_HOST', '127.0.0.1')
TOKEN_FILE = os.path.expanduser('~/.gemini/jetski-standalone-oauth-token')
COOKIE_FILE = os.path.expanduser('~/.gemini2codex/cookies.txt')
CLIENT_CANDIDATES = [
    ('681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com', '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'),
    ('764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com', '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'),
]
MODELS = ['gemini-3-pro-preview', 'gemini-2.5-pro', 'gemini-2.5-flash', 'gemini-3-flash-preview']
UA = 'GeminiCLI/0.60.0 (MacOS; arm64)'

ST = {'at': None, 'exp': 0.0, 'project': None, 'tier': None, 'client_ok': None, 'last_channel': None}

class UpstreamError(Exception):
    pass

# 单次 chat 的总时限。原先 call_a(180s) 失败后再 call_b(180s)，最坏要 6 分钟
# 才想起来回 502；客户端（Codex / 探测脚本）远早于此就超时断开，于是 502 写回
# 管道时只剩 BrokenPipeError，健康检查也把这座桥误判成 BRIDGE_DOWN。
CHAT_BUDGET = float(os.environ.get('GEMINI_CHAT_BUDGET') or '60')
# 预算烧完之后，兜底通道仍能拿到的最低时限。它只负责别把 0/负数交给
# urllib，不足以再变成一次完整超时——那正是 180s + 180s 的由来。
FALLBACK_FLOOR = float(os.environ.get('GEMINI_FALLBACK_FLOOR') or '1.0')

# urlopen() hands the same timeout to every address getaddrinfo() returns, and
# cloudcode-pa.googleapis.com resolves to 16 of them (8 IPv6 first). Measured
# 2026-09-29 behind this VPN: a 20s timeout cost 40s on oauth2.googleapis.com's
# two addresses, so a 60s CHAT_BUDGET could turn into 16 x 60s before IPv4 was
# even tried. Every connect attempt is therefore capped at the time left on the
# current request's deadline, which bounds the whole call. The deadline is
# thread-local: each request runs in its own thread under ThreadingHTTPServer,
# so concurrent requests cannot clobber one another.
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


def http_json(url, payload, headers=None, method='POST', timeout=90):
    hdrs = {'User-Agent': UA, 'Content-Type': 'application/json;charset=UTF-8', 'Accept-Encoding': 'identity'}
    if headers:
        hdrs.update(headers)
    data = json.dumps(payload).encode() if payload is not None else None
    if method == 'GET':
        data = None
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
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
        body = e.read().decode('utf-8', 'ignore')[:400]
        raise UpstreamError('HTTP %s %s: %s' % (e.code, url, body))
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

def do_refresh(deadline=None):
    d = read_token_file()
    rt = d.get('token', {}).get('refresh_token')
    if not rt:
        raise UpstreamError('no refresh_token in ' + TOKEN_FILE + ' (run: gemini login)')
    last = None
    for cid, csec in CLIENT_CANDIDATES:
        if _spent(deadline):
            break
        payload = {'client_id': cid, 'client_secret': csec, 'refresh_token': rt, 'grant_type': 'refresh_token'}
        try:
            raw, _ = http_json('https://oauth2.googleapis.com/token', payload,
                               timeout=_left(deadline, 30.0))
            j = json.loads(raw)
            ST['at'] = j['access_token']
            ST['exp'] = time.time() + j.get('expires_in', 3600) - 60
            ST['client_ok'] = cid
            d['token']['access_token'] = j['access_token']
            d['token']['expiry'] = time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(ST['exp']))
            try:
                write_token_file(d)
            except Exception:
                pass
            return ST['at']
        except UpstreamError as e:
            last = e
            continue
    raise UpstreamError('refresh failed for all clients: ' + str(last))

def get_access(deadline=None):
    try:
        mt = os.path.getmtime(TOKEN_FILE)
        if mt > ST.get('file_mtime', 0.0):
            ST['file_mtime'] = mt
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
    return do_refresh(deadline)

def load_code_assist(deadline=None):
    if ST['project'] is not None:
        return
    at = get_access(deadline)
    body = {'metadata': {'ideType': 'GEMINI_CLI', 'pluginType': 'GEMINI', 'platform': 'PLATFORM_UNSPECIFIED'}}
    raw, _ = http_json('https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist', body,
                       headers={'Authorization': 'Bearer ' + at}, timeout=_left(deadline, 30.0))
    j = json.loads(raw)
    ST['project'] = j.get('cloudaicompanionProject') or ''
    tier = j.get('currentTier') or {}
    ST['tier'] = tier.get('id') or (tier.get('name') if isinstance(tier, dict) else None)

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
    if ST['project']:
        body['project'] = ST['project']
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
    if not os.path.exists(COOKIE_FILE):
        return ''
    txt = open(COOKIE_FILE, encoding='utf-8', errors='ignore').read()
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

    def do_GET(self):
        if self.path.startswith('/v1/models'):
            now = int(time.time())
            data = {'object': 'list', 'data': [{'id': m, 'object': 'model', 'created': now, 'owned_by': 'google-one'} for m in MODELS]}
            self._send(200, json.dumps(data))
        elif self.path.startswith('/health') or self.path == '/':
            info = {'status': 'ok', 'account': os.environ.get('GEMINI_ACCOUNT_LABEL', 'google-one'),
                    'tier': ST['tier'], 'project': bool(ST['project']), 'last_channel': ST['last_channel']}
            self._send(200, json.dumps(info))
        else:
            self._send(404, json.dumps({'error': 'not found'}))

    def do_POST(self):
        if not self.path.startswith('/v1/chat/completions'):
            self._send(404, json.dumps({'error': 'not found'}))
            return
        n = int(self.headers.get('Content-Length', 0))
        req = json.loads(self.rfile.read(n) or b'{}')
        stream = bool(req.get('stream'))
        model = req.get('model', MODELS[0])
        msgs = req.get('messages', [])
        cid = 'chatcmpl-' + uuid.uuid4().hex[:24]
        text, channel, err = '', None, None
        deadline = time.time() + CHAT_BUDGET
        _arm_deadline(deadline)
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
                    err = {'code_assist': str(e1)[:300], 'web': str(e2)[:300]}
        finally:
            _disarm_deadline()
        if err:
            self._send(502, json.dumps({'error': {'message': err, 'type': 'upstream_error'}}))
            return
        if not text:
            text = '[EMPTY-UPSTREAM]'
        ST['last_channel'] = channel
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

if __name__ == '__main__':
    srv = ThreadingHTTPServer((HOST, PORT), H)
    sys.stderr.write('gemini2codex bridge on %s:%d (A: code-assist, B: gemini-web)\n' % (HOST, PORT))
    srv.serve_forever()
