#!/usr/bin/env python3
"""Read the Google OAuth client pair out of the Antigravity language_server binary.

FleetKit never puts the pair in git (GitHub push protection rejects Google OAuth
client ids and secrets), but the bridge needs it to refresh the jetski token.
The pair ships inside Antigravity itself, so take it from the install instead of
hardcoding it.

usage:
  python3 extract_client.py                  every candidate, tab separated (id, secret)
  python3 extract_client.py --verify         only the pairs that can really refresh
  python3 extract_client.py /path/to/bin     explicit binary
  python3 extract_client.py --limit 8        cap the number of pairs
  python3 extract_client.py --check          exit 1 when no pair is found

The binary stores every string back to back, so a GOCSPX- run can hold several
secrets glued together. Splitting the run at internal GOCSPX- markers recovers the
individual secrets; the ids and secrets are then emitted as a cross product because
offset proximity cannot decide which id belongs to which secret. --verify settles
that by refreshing the jetski refresh_token for real (install.sh uses that first).

install.sh calls this and writes the result into fleet.env, which launchd passes to
the bridge as ANTIGRAVITY_OAUTH_CLIENT_ID / ANTIGRAVITY_OAUTH_CLIENT_SECRET.
"""
import os
import json
import re
import sys

DEFAULT_BIN = '/Applications/Antigravity.app/Contents/Resources/bin/language_server'
DEFAULT_LIMIT = 8
CID_RE = re.compile(r'[0-9]{6,}-[a-z0-9]{20,}\.apps\.googleusercontent\.com')
SEC_RE = re.compile(r'GOCSPX-[A-Za-z0-9_-]{10,}')
SEC_SPLIT = 'GOCSPX-'


def split_runs(text):
    """One entry per individual secret: GOCSPX- runs often hold several of them."""
    out = []
    for match in SEC_RE.finditer(text):
        for part in match.group(0).split(SEC_SPLIT):
            if part:
                secret = SEC_SPLIT + part
                if secret not in out:
                    out.append(secret)
    return out


def find_pairs(path):
    with open(path, 'rb') as fh:
        blob = fh.read()
    text = blob.decode('utf-8', 'ignore')
    ids = list(dict.fromkeys(m.group(0) for m in CID_RE.finditer(text)))
    secs = split_runs(text)
    return [(cid, secret) for cid in ids for secret in secs]


def refresh_works(pair, refresh_token):
    import urllib.error
    import urllib.request
    payload = {'client_id': pair[0], 'client_secret': pair[1],
               'refresh_token': refresh_token, 'grant_type': 'refresh_token'}
    body = json.dumps(payload).encode()
    req = urllib.request.Request('https://oauth2.googleapis.com/token', data=body,
                                 headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read()).get('access_token') is not None
    except Exception:
        return False


def verified_pairs(pairs):
    token_file = os.path.expanduser('~/.gemini/jetski-standalone-oauth-token')
    if not os.path.exists(token_file):
        return []
    try:
        with open(token_file) as fh:
            doc = json.load(fh)
    except Exception:
        return []
    refresh_token = (doc.get('token') or {}).get('refresh_token')
    if not refresh_token:
        return []
    return [pair for pair in pairs if refresh_works(pair, refresh_token)]


def main(argv):
    args = [a for a in argv[1:] if not a.startswith('--')]
    verify = '--verify' in argv
    try:
        limit = int(argv[argv.index('--limit') + 1]) if '--limit' in argv else DEFAULT_LIMIT
    except (IndexError, ValueError):
        limit = DEFAULT_LIMIT
    path = args[0] if args else DEFAULT_BIN
    if not os.path.exists(path):
        print('not found: ' + path + ' (pass the language_server path explicitly)', file=sys.stderr)
        return 2
    try:
        pairs = find_pairs(path)
    except OSError as exc:
        print('unreadable: ' + path + ' (' + str(exc) + ')', file=sys.stderr)
        return 2
    if not pairs:
        print('no google oauth client pair found in ' + path, file=sys.stderr)
        return 1
    if verify:
        good = verified_pairs(pairs)
        if not good:
            print('no candidate can refresh the jetski token'
                  ' (no network, or every pair was rejected)', file=sys.stderr)
            return 1
        pairs = good
    for cid, secret in pairs[:limit]:
        print(cid + chr(9) + secret)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
