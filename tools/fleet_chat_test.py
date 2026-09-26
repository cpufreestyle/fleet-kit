#!/usr/bin/env python3
"""Fleet chat test: probe every bridge /v1/models + one chat completion.
Keys are read from launchctl plists; only md5 is printed."""
import urllib.request, urllib.error, json, ssl, hashlib, time, glob, re, sys, os

ctx = ssl.create_default_context()
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def plist_env_keys():
    out = {}
    prefix = os.environ.get('FLEET_LABEL_PREFIX', 'com.local')
    for p in glob.glob(os.path.expanduser('~/Library/LaunchAgents/%s.*2codex*.plist' % prefix)):
        name = p.split('/')[-1].replace(prefix + '.', '').replace('.plist', '')
        try:
            raw = open(p).read()
        except Exception:
            continue
        m = re.search(r'<key>([A-Z0-9_]*(?:KEY|TOKEN)[A-Z0-9_]*)</key>\s*<string>([^<]+)</string>', raw)
        if m:
            out[name] = m.group(2)
    return out

def parse_args():
    base = 8787
    for a in sys.argv[1:]:
        if a.startswith('--port-base='):
            base = int(a.split('=', 1)[1])
        elif a in ('-h', '--help'):
            print(__doc__)
            sys.exit(0)
        else:
            print('unknown arg: %s' % a, file=sys.stderr)
            sys.exit(2)
    return base


BRIDGE_NAMES = [
    ('workbuddy',     0, 'hy4-preview'),
    ('workbuddy-gpt', 1, 'gpt-5.6'),
    ('qoder',         2, 'auto'),
    ('codely',        3, 'codely-core'),
    ('trae',          4, 'DeepSeek-V4-Pro'),
    ('lingxi',        5, 'glm-5.3-flash'),
    ('xhx',           6, 'raccoon-8c4485'),
    ('gemini',        7, 'gemini-3-pro-preview'),
    ('catpaw',        8, 'glm-5.2'),
    ('antigravity',  10, 'claude-opus-4-8@default'),
]


def bridges(port_base):
    return [(name, port_base + off, model) for name, off, model in BRIDGE_NAMES]
PROBE = '请只回复两个字：正常'

def get_json(url, key=None, timeout=8):
    h = {'User-Agent': 'fleet-test/1.0'}
    if key:
        h['Authorization'] = 'Bearer ' + key
    req = urllib.request.Request(url, headers=h)
    resp = NO_PROXY.open(req, timeout=timeout)
    return resp.status, json.loads(resp.read().decode('utf-8', 'replace'))

def chat(port, model, key=None, timeout=90):
    url = 'http://127.0.0.1:%d/v1/chat/completions' % port
    payload = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': PROBE}], 'max_tokens': 1024, 'stream': False}).encode()
    h = {'Content-Type': 'application/json', 'User-Agent': 'fleet-test/1.0'}
    if key:
        h['Authorization'] = 'Bearer ' + key
    req = urllib.request.Request(url, data=payload, headers=h)
    t0 = time.time()
    try:
        resp = NO_PROXY.open(req, timeout=timeout)
        out = json.loads(resp.read().decode('utf-8', 'replace'))
        msg = out.get('choices', [{}])[0].get('message', {}).get('content', '')
        usage = out.get('usage', {})
        return resp.status, round(time.time() - t0, 1), (msg or '')[:60].replace('\n', ' '), usage.get('total_tokens')
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'replace')[:160].replace('\n', ' ')
        return e.code, round(time.time() - t0, 1), body, None
    except Exception as e:
        return None, round(time.time() - t0, 1), repr(e)[:120], None

def main():
    base = parse_args()
    keys = plist_env_keys()
    print('port base %d' % base)
    print('%-14s %-5s %-28s %-6s %s' % ('BRIDGE', 'PORT', 'MODEL', 'HTTP', 'RESULT'))
    print('-' * 100)
    for name, port, model in bridges(base):
        key = None
        for pname, k in keys.items():
            if name.replace('-gpt', '') in pname or pname.startswith(name.split('-')[0]):
                if 'gpt' in name and 'gpt' not in pname:
                    continue
                key = k
                break
        kmd5 = hashlib.md5(key.encode()).hexdigest()[:8] if key else 'none'
        try:
            st, data = get_json('http://127.0.0.1:%d/v1/models' % port, key)
            ids = [m.get('id') for m in data.get('data', [])]
            pick = model if model in ids else (ids[0] if ids else model)
            minfo = 'models=%d key=%s' % (len(ids), kmd5)
        except urllib.error.HTTPError as e:
            pick = model
            minfo = 'models-HTTP-%d key=%s' % (e.code, kmd5)
        except Exception as e:
            pick = model
            minfo = 'models-ERR %s key=%s' % (repr(e)[:40], kmd5)
        code, secs, content, toks = chat(port, pick, key)
        verdict = 'PASS' if code == 200 and content and 'error' not in content.lower() else 'FAIL'
        print('%-14s %-5d %-28s %-6s %s | %ss | %s | %s' % (name, port, str(pick)[:28], code, verdict, secs, minfo, content[:60]))

if __name__ == '__main__':
    main()
