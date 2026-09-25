#!/usr/bin/env python3
# catpaw2codex v2: Meituan CatPawAI (miaoshou AI IDE) -> OpenAI-compatible bridge (port 8795)
# API map extracted from app bundle 2026-09-25:
#   mt-idekit.mt-idekit-code/out/extension.js + out/ui/agent/js/main.c98fb807.js
# endpoints:
#   models : POST {catpaw}/api/agent/maas/model-types   {tenant, scene}
#   chat A : POST {mcopilot}/api/gpt/chat/completions  (openai-ish, triggerMode/userModelTypeCode)
#   chat B : POST {catpaw}/api/agent/conversation/create -> /api/agent/stream/connect (SSE)
# auth    : Catpaw-Auth: <accessToken> + Cookie 1d47d6ff96_ssoid=<at>; f32a546874_ssoid=<at>
import json, os, sqlite3, sys, time, uuid, threading
import urllib.request, urllib.parse, urllib.error, ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get('CATPAW_PORT', '8795'))
HOST = os.environ.get('CATPAW_HOST', '127.0.0.1')
BASE = os.environ.get('CATPAW_BASE', 'https://catpaw.sankuai.com')
MCOPILOT = os.environ.get('CATPAW_MCOPILOT', 'https://mcopilot-emb.sankuai.com')
PUBLIC_BASE = 'https://catpaw.meituan.com'
STATE_DB = os.path.expanduser('~/Library/Application Support/CatPawAI/User/globalStorage/state.vscdb')
IDEKIT_KEY = 'mt-idekit.mt-idekit-code'
MODE = os.environ.get('CATPAW_MODE', 'completions')  # completions | agent
MIS_ID = os.environ.get('CATPAW_MIS_ID', '13661621468')
TENANT = os.environ.get('CATPAW_TENANT', 'catpaw')
IDE_VER = '1.101.0'
UA = 'CatPawAI/' + IDE_VER
SSO_A = '1d47d6ff96_ssoid'
SSO_B = 'f32a546874_ssoid'

STATIC_MODELS = [('longcat-flash', 22), ('LongCat-2.0', 77), ('glm-5v-turbo', 60), ('glm-5.3-flashx', 98), ('glm-5.2', 75), ('glm-5.1', 59), ('glm-5', 46), ('MiniMax-M2.7', 56), ('MiniMax-M2.5', 48), ('deepseek-v3.2', 9)]
DEFAULT_MODEL = os.environ.get('CATPAW_DEFAULT_MODEL', 'glm-5.3-flashx')

urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

ST = {'at': None, 'ts': 0.0, 'mis': MIS_ID, 'models': None, 'models_ts': 0.0, 'lock': threading.Lock()}

def log(*a):
    sys.stderr.write('[catpaw] ' + ' '.join(str(x) for x in a) + '\n')
    sys.stderr.flush()

def http_req(url, data=None, headers=None, method='GET', timeout=60, stream=False):
    hdrs = {'User-Agent': UA, 'Accept': 'application/json, text/plain, */*'}
    if headers:
        hdrs.update(headers)
    body = json.dumps(data).encode() if data is not None else None
    if body is not None and 'Content-Type' not in hdrs:
        hdrs['Content-Type'] = 'application/json;charset=UTF-8'
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout, context=CTX)
        if stream:
            return r.status, r, dict(r.headers)
        return r.status, r.read().decode('utf-8', 'ignore'), dict(r.headers)
    except urllib.error.HTTPError as e:
        if stream:
            return e.code, e.read().decode('utf-8', 'ignore'), {}
        return e.code, e.read().decode('utf-8', 'ignore'), {}
    except Exception as e:
        return 0, type(e).__name__ + ': ' + str(e)[:150], {}

def read_state():
    out = {}
    try:
        con = sqlite3.connect('file:' + STATE_DB + '?mode=ro', uri=True)
        row = con.execute('SELECT value FROM ItemTable WHERE key = ?', (IDEKIT_KEY,)).fetchone()
        con.close()
        if row:
            j = json.loads(row[0])
            out['at_idekit'] = j.get('accessTokenprod')
            out['mis'] = (j.get('userInfoprod') or {}).get('misId')
            raw = j.get('mcopilot_agent_context_state__getAvailableModelListprod')
            if raw:
                try:
                    out['models'] = json.loads(raw).get('data')
                except Exception:
                    pass
    except Exception as e:
        log('state idekit read err', e)
    try:
        con = sqlite3.connect('file:' + STATE_DB + '?mode=ro', uri=True)
        row = con.execute('SELECT value FROM ItemTable WHERE key = ?', ('catpaw.mt-authentication',)).fetchone()
        con.close()
        if row:
            o = json.loads(row[0])
            inner = json.loads(o['mt.auth'])
            sess = (inner.get('sessions') or [{}])[0]
            out['at_auth'] = sess.get('accessToken')
            out['rt'] = inner.get('refreshToken')
    except Exception as e:
        log('state auth read err', e)
    return out

def refresh(base, rt):
    st, body, _ = http_req(base + '/api/login/refreshToken', data={}, headers={'Catpaw-Auth': rt}, method='POST', timeout=20)
    if st == 200 and '"status":200' in body.replace(' ', ''):
        try:
            j = json.loads(body)
            d = j.get('data', {})
            return d.get('accessToken') or d.get('token')
        except Exception:
            return None
    log('refresh', base, 'failed', st, body[:100])
    return None

def get_token(force=False):
    with ST['lock']:
        if ST['at'] and not force and time.time() - ST['ts'] < 1500:
            return ST['at']
        stt = read_state()
        if stt.get('mis'):
            ST['mis'] = stt['mis']
        if stt.get('models'):
            ST['models'] = stt['models']
        cands = [stt.get('at_idekit'), stt.get('at_auth')]
        for at in cands:
            if not at:
                continue
            for base in (BASE, MCOPILOT, PUBLIC_BASE):
                st, body, _ = http_req(base + '/api/login/userInfo', headers=hdrs(at), timeout=12)
                if st == 200 and 'auth failed' not in body:
                    ST['at'], ST['ts'] = at, time.time()
                    log('token ok via', base)
                    return at
                log('userInfo', base, '->', st, body[:80])
        rt = stt.get('rt')
        if rt:
            for base in (BASE, MCOPILOT, PUBLIC_BASE):
                new_at = refresh(base, rt)
                if new_at:
                    ST['at'], ST['ts'] = new_at, time.time()
                    log('refreshed via', base)
                    return new_at
        raise RuntimeError('session invalid everywhere: connect company VPN + re-login in IDE')

def hdrs(at):
    mis = ST['mis'] or MIS_ID
    return {
        'Catpaw-Auth': at,
        'Cookie': SSO_A + '=' + at + '; ' + SSO_B + '=' + at,
        'ide-type': 'CatPaw IDE',
        'client-type': 'CatPaw IDE',
        'ide-version': IDE_VER,
        'user-mis-id': mis,
        'user-uid': mis,
        'mis-id': mis,
        'tenant': TENANT,
        'platform-info': 'darwin',
    }

def model_type(name):
    models = ST['models'] or [{'modelTypeName': n, 'modelType': t} for n, t in STATIC_MODELS]
    for m in models:
        if str(m.get('modelTypeName')) == name:
            return m.get('modelType')
    for n, t in STATIC_MODELS:
        if n == name:
            return t
    return None

def fetch_models(at):
    st, body, _ = http_req(BASE + '/api/agent/maas/model-types', data={'tenant': TENANT, 'scene': 'CATPAW_AGENT'}, headers=hdrs(at), method='POST', timeout=15)
    if st == 200:
        try:
            j = json.loads(body)
            d = j.get('data')
            if isinstance(d, dict):
                d = d.get('list') or d.get('data')
            if isinstance(d, list) and d:
                ST['models'], ST['models_ts'] = d, time.time()
                return d
        except Exception:
            pass
    log('maas model-types ->', st, body[:100])
    return None

def list_models():
    try:
        at = get_token()
        if time.time() - ST['models_ts'] > 600 or not ST['models']:
            fetch_models(at)
    except Exception as e:
        log('list_models token err', e)
    if ST['models']:
        return [str(m.get('modelTypeName')) for m in ST['models'] if m.get('modelTypeName')]
    return [n for n, _ in STATIC_MODELS]

def oai_chunk(model, cid, delta=None, finish=None):
    return {'id': cid, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}]}

def to_openai(content, model, cid, stream):
    if stream:
        lines = ['data: ' + json.dumps(oai_chunk(model, cid, {'role': 'assistant'})),
                 'data: ' + json.dumps(oai_chunk(model, cid, {'content': content})),
                 'data: ' + json.dumps(oai_chunk(model, cid, {}, 'stop')),
                 'data: [DONE]']
        return ('\n\n'.join(lines) + '\n\n').encode()
    return json.dumps({'id': cid, 'object': 'chat.completion', 'created': int(time.time()), 'model': model,
                       'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}],
                       'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}).encode()

def flatten_messages(msgs):
    out = []
    for m in msgs:
        role = m.get('role', 'user')
        content = m.get('content', '')
        if isinstance(content, list):
            content = ' '.join(str(c.get('text', '')) for c in content if isinstance(c, dict))
        out.append({'role': role, 'content': str(content)})
    return out

def chat_via_completions(at, req, cid, do_stream):
    name = req.get('model') or DEFAULT_MODEL
    mt = model_type(name)
    payload = {'stream': bool(do_stream), 'messages': flatten_messages(req.get('messages', [])),
               'triggerMode': 'CHAT', 'userModelTypeCode': mt, 'gitUrl': '', 'remoteBranch': '',
               'filePath': '', 'selectedCode': ''}
    st, body, rh = http_req(MCOPILOT + '/api/gpt/chat/completions', data=payload, headers=hdrs(at), method='POST', timeout=180)
    if st != 200:
        return st if 400 <= st < 600 else 502, json.dumps({'error': {'message': body[:400], 'type': 'upstream_error'}}).encode(), {}
    ctype = rh.get('Content-Type') or ''
    if 'text/event-stream' in ctype:
        return 200, body.encode(), {'Content-Type': 'text/event-stream'}
    try:
        j = json.loads(body)
        content = (j.get('data') or {}).get('content') or (j.get('data') or {}).get('text') or ''
    except Exception:
        content = body
    return 200, to_openai(content, name, cid, do_stream), {}

def sse_to_text(events):
    parts = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        for k in ('content', 'delta', 'text', 'message', 'chunk'):
            v = ev.get(k)
            if isinstance(v, str) and v:
                parts.append(v)
                break
    return ''.join(parts)

def chat_via_agent(at, req, cid, do_stream):
    name = req.get('model') or DEFAULT_MODEL
    mt = model_type(name)
    prompt = '\n'.join((m.get('content') if isinstance(m.get('content'), str) else str(m.get('content', ''))) for m in req.get('messages', []) if m.get('role') != 'system')
    create = {'modelType': mt, 'gitRepoUrl': '', 'gitBaseBranch': '', 'gitCheckoutBranch': '',
              'prompt': prompt, 'mode': 'REMOTE_AGENT', 'autoDeploy': False, 'autoPullRequest': False,
              'source': 'CatPaw', 'appkeys': [], 'imageUrls': [], 'contexts': [], 'editorContextStates': {}}
    st, body, _ = http_req(BASE + '/api/agent/conversation/create', data=create, headers=hdrs(at), method='POST', timeout=60)
    if st != 200:
        return st if 400 <= st < 600 else 502, json.dumps({'error': {'message': body[:400], 'type': 'create_error'}}).encode(), {}
    try:
        j = json.loads(body)
        conv = (j.get('data') or {}).get('conversationId')
    except Exception:
        conv = None
    if not conv:
        return 502, json.dumps({'error': {'message': 'no conversationId: ' + body[:300], 'type': 'shape_error'}}).encode(), {}
    st, r, rh = http_req(BASE + '/api/agent/stream/connect', data={'timestamp': int(time.time() * 1000), 'conversationId': conv, 'messageIndex': 0}, headers=hdrs(at), method='POST', timeout=300, stream=True)
    if st != 200 or not hasattr(r, 'read'):
        return st if 400 <= st < 600 else 502, json.dumps({'error': {'message': str(body)[:400], 'type': 'connect_error'}}).encode(), {}
    events = []
    buf = b''
    try:
        while True:
            chunk = r.read(4096)
            if not chunk:
                break
            buf += chunk
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                line = line.strip()
                if not line or line == b'data: [DONE]':
                    continue
                if line.startswith(b'data:'):
                    line = line[5:].strip()
                try:
                    events.append(json.loads(line.decode('utf-8', 'ignore')))
                except Exception:
                    pass
    except Exception as e:
        log('agent stream read err', e)
    text = sse_to_text(events)
    return 200, to_openai(text, name, cid, do_stream), {}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj, ctype='application/json'):
        b = obj.encode() if isinstance(obj, str) else obj
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        try:
            self.wfile.write(b)
        except Exception:
            pass

    def do_GET(self):
        if self.path.startswith('/v1/models'):
            names = list_models()
            now = int(time.time())
            data = {'object': 'list', 'data': [{'id': n, 'object': 'model', 'created': now, 'owned_by': 'catpaw-miaoshou'} for n in names]}
            self._send(200, json.dumps(data))
        elif self.path.startswith('/health') or self.path == '/':
            at, err, reach = None, None, {}
            for base in (BASE, MCOPILOT, PUBLIC_BASE):
                st, body, _ = http_req(base + '/api/ping', headers={}, method='GET', timeout=5)
                reach[base.split('//')[1]] = (st, ('auth-failed' in body and 'reachable-auth-gate') or ('ok' if st == 200 else 'unreachable'))
            try:
                at = get_token()
            except Exception as e:
                err = str(e)
            info = {'status': 'ok' if at else 'auth-error', 'account': ST['mis'], 'mode': MODE,
                    'base': BASE, 'mcopilot': MCOPILOT, 'models': len(list_models()),
                    'reach': reach, 'error': err}
            self._send(200, json.dumps(info))
        else:
            self._send(404, json.dumps({'error': 'not found'}))

    def do_POST(self):
        if not self.path.startswith('/v1/chat/completions'):
            self._send(404, json.dumps({'error': 'not found'}))
            return
        try:
            n = int(self.headers.get('Content-Length', 0))
            req = json.loads(self.rfile.read(n) or b'{}')
        except Exception as e:
            self._send(400, json.dumps({'error': {'message': 'bad json ' + str(e)}}))
            return
        cid = 'chatcmpl-' + uuid.uuid4().hex[:24]
        do_stream = bool(req.get('stream'))
        try:
            at = get_token()
        except Exception as e:
            self._send(502, json.dumps({'error': {'message': str(e), 'type': 'auth_error'}}))
            return
        try:
            if MODE == 'agent':
                fn = chat_via_agent
            else:
                fn = chat_via_completions
            code, out, extra = fn(at, req, cid, do_stream)
            if code == 401 or (code == 200 and isinstance(out, bytes) and b'auth failed' in out):
                at = get_token(force=True)
                code, out, extra = fn(at, req, cid, do_stream)
            self._send(code, out, extra.get('Content-Type', 'application/json'))
        except Exception as e:
            self._send(500, json.dumps({'error': {'message': str(e)[:300], 'type': 'bridge_error'}}))

if __name__ == '__main__':
    srv = ThreadingHTTPServer((HOST, PORT), H)
    log('catpaw2codex v2 on %s:%d mode=%s catpaw=%s mcopilot=%s' % (HOST, PORT, MODE, BASE, MCOPILOT))
    srv.serve_forever()
