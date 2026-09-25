#!/usr/bin/env python3
# Extract gemini.google.com web cookies from Chrome -> ~/.gemini2codex/cookies.txt
# tries two key derivations: Safe-Storage-direct AES-128 and PBKDF2(saltysalt,1003)
import os, sqlite3, shutil, glob, subprocess, tempfile, sys, base64, hashlib

def keychain_password():
    out = subprocess.run(['security', 'find-generic-password', '-w', '-s', 'Chrome Safe Storage', '-a', 'Chrome'], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit('keychain fail: ' + out.stderr[:150])
    return out.stdout.strip()

def dump_enc():
    base = os.path.expanduser('~/Library/Application Support/Google/Chrome')
    out = {}
    for db in [base + '/Default/Cookies'] + sorted(glob.glob(base + '/Profile */Cookies')):
        if not os.path.exists(db):
            continue
        tmp = tempfile.mktemp()
        shutil.copy(db, tmp)
        for suf in ('-wal', '-shm'):
            if os.path.exists(db + suf):
                shutil.copy(db + suf, tmp + suf)
        con = sqlite3.connect(tmp)
        for name, host, enc in con.execute("SELECT name, host_key, encrypted_value FROM cookies WHERE name IN ('__Secure-1PSID','__Secure-1PSIDTS') AND host_key = '.google.com'"):
            if name not in out:
                out[name] = enc
        con.close()
        os.unlink(tmp)
        if len(out) == 2:
            break
    return out

def aes_ecb_unpad(ct, key):
    # CBC via openssl subprocess (no pycryptodome dependency)
    tmp_in = tempfile.mktemp(); tmp_out = tempfile.mktemp()
    open(tmp_in, 'wb').write(ct)
    r = subprocess.run(['openssl', 'enc', '-d', '-aes-128-cbc', '-K', key.hex(), '-iv', '20' * 16, '-in', tmp_in, '-out', tmp_out], capture_output=True)
    if r.returncode != 0:
        return None
    data = open(tmp_out, 'rb').read()
    for f in (tmp_in, tmp_out):
        os.unlink(f)
    return data

def printable(s):
    try:
        t = s.decode('utf-8')
    except Exception:
        return False
    return all(32 <= ord(c) < 127 for c in t) and len(t) > 10

def main():
    pw = keychain_password()
    encs = dump_enc()
    if '__Secure-1PSID' not in encs:
        raise SystemExit('no __Secure-1PSID cookie found in any Chrome profile')
    keys = {'direct': pw.encode()[:16],
            'pbkdf2': hashlib.pbkdf2_hmac('sha1', pw.encode(), b'saltysalt', 1003, 16)}
    vals = {}
    for name, enc in encs.items():
        if enc[:3] != b'v10':
            vals[name] = enc
            continue
        ct = enc[3:]
        for tag, key in keys.items():
            pt = aes_ecb_unpad(ct, key)
            if pt and printable(pt):
                vals[name] = pt.rstrip(b'\n')
                break
    if '__Secure-1PSID' not in vals:
        raise SystemExit('decrypt failed: Chrome cookie scheme may have changed')
    line = '__Secure-1PSID=' + vals['__Secure-1PSID'].decode()
    if '__Secure-1PSIDTS' in vals:
        line += '; __Secure-1PSIDTS=' + vals['__Secure-1PSIDTS'].decode()
    dst = os.path.expanduser('~/.gemini2codex/cookies.txt')
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    open(dst, 'w').write(line + '\n')
    os.chmod(dst, 0o600)
    print('OK wrote ' + dst + ' psid_len=' + str(len(vals['__Secure-1PSID'])) + ' ts_len=' + str(len(vals.get('__Secure-1PSIDTS', b''))))

if __name__ == '__main__':
    main()
