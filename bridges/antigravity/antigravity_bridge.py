#!/usr/bin/env python3
# antigravity2codex: Google Antigravity IDE models -> OpenAI-compatible bridge (port 8797)
# Upstream: cloudcode-pa.googleapis.com v1internal (Antigravity OAuth client, auto-refresh)
# Catalog extracted from /Applications/Antigravity.app/Contents/Resources/bin/language_server
# Token: ~/.gemini/jetski-standalone-oauth-token (shared with gemini2codex)
import json, os, sys, time, uuid
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
        return r.read(), dict(r.headers)
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

def load_code_assist():
    if ST['project'] is not None:
        return
    at = get_access()
    last = None
    for ide in IDE_TYPES:
        try:
            raw, _ = http_json(BASE + 'loadCodeAssist', {'metadata': meta_for(ide)},
                               headers={'Authorization': 'Bearer ' + at}, timeout=30)
            j = json.loads(raw)
        except UpstreamError as e:
            last = e
            continue
        ST['project'] = j.get('cloudaicompanionProject') or ''
        tier = j.get('currentTier') or {}
        ST['tier'] = tier.get('id') if isinstance(tier, dict) else None
        ST['ide'] = ide
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
    load_code_assist()
    contents, sysinst = to_contents(msgs)
    inner = {'contents': contents, 'generationConfig': {'temperature': 0.7}}
    if sysinst:
        inner['systemInstruction'] = sysinst
    body = {'model': model, 'request': inner, 'metadata': meta_for(ide)}
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

def call_model(model, msgs, stream, timeout=180):
    # 1) model-id alias fallback (@default form vs bare id)
    first = None
    for variant in model_variants(model):
        try:
            out = call_upstream(variant, msgs, stream, timeout)
            ST['last_model'] = variant
            ST['last_ok'] = True
            return out
        except UpstreamError as e:
            if first is None:
                first = e
            if not looks_like_model_error(e):
                break
    # 2) ide metadata fallback (ANTIGRAVITY -> GEMINI_CLI)
    for ide in IDE_TYPES[1:]:
        try:
            out = call_upstream(model, msgs, stream, timeout, ide=ide)
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
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        if hdrs:
            for k, v in hdrs.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith('/v1/models'):
            now = int(time.time())
            data = {'object': 'list', 'data': [{'id': m, 'object': 'model', 'created': now, 'owned_by': 'antigravity'} for m in MODELS]}
            self._send(200, json.dumps(data))
        elif self.path.startswith('/health') or self.path == '/':
            info = {'status': 'ok', 'account': os.environ.get('ANTIGRAVITY_ACCOUNT_LABEL', 'google-antigravity'),
                    'tier': ST['tier'], 'project': bool(ST['project']), 'ide': ST['ide'],
                    'client_ok': bool(ST['client_ok']), 'models': len(MODELS),
                    'oauth': len(CLIENT_CANDIDATES),
                    'calls': ST['calls'], 'last_model': ST['last_model'], 'last_ok': ST['last_ok']}
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
        ST['calls'] += 1
        try:
            text = call_model(model, msgs, stream)
        except Exception as e:
            self._send(502, json.dumps({'error': {'message': str(e)[:300], 'type': 'upstream_error'}}))
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

if __name__ == '__main__':
    srv = ThreadingHTTPServer((HOST, PORT), H)
    sys.stderr.write('antigravity2codex bridge on %s:%d (%d models, A: code-assist)' % (HOST, PORT, len(MODELS)) + chr(10))
    srv.serve_forever()
