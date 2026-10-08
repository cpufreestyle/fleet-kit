# Gemini 多账号池 (FleetKit)
#
# 目录结构:
#   <auth_dir>/<label>/token.json     官方 jetski-standalone-oauth-token 的副本 (通道 A)
#   <auth_dir>/<label>/cookies.txt    可选, 通道 B web cookies 的副本
#
# 未导入任何账号时, 合成一个 legacy 账号, 直接引用官方登录文件,
# 因此单账号使用者的行为完全不变。
#
# 选择顺序: primary -> active -> 其余按 LRU; 处于冷却期的账号默认跳过,
# candidates(ignore_cooldown=True) 时全部参与排序 (用于兜底)。
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import os as _os
import sys as _sys

_SYS_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), _os.pardir)
if _SYS_DIR not in _sys.path:
    _sys.path.insert(0, _SYS_DIR)
import _account_pool

STATE_FILE_NAME = 'pool-state.json'
TOKEN_FILE_NAME = 'token.json'
COOKIE_FILE_NAME = 'cookies.txt'
LEGACY_LABEL = 'legacy'


def _ref(label):
    return hashlib.sha256(label.encode('utf-8')).hexdigest()[:16]


def _iso(value):
    if not value:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _harden(path):
    """Owner-only for a credential directory/file; a no-op on Windows."""
    if os.name == 'nt' or not path:
        return
    try:
        return _account_pool.harden_private_path(Path(path))
    except OSError:
        pass


def _atomic_json(path, payload):
    try:
        return _account_pool.atomic_json(Path(path), payload)
    except OSError:
        _harden(path)


def _token_state(token_path):
    # 返回 (expired, exp_epoch)。token 缺失时返回 (False, 0.0),
    # 让只带 cookie 的账号不被误判为过期。
    if not token_path or not os.path.exists(token_path):
        return (False, 0.0)
    try:
        with open(token_path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return (True, 0.0)
    expiry = None
    try:
        expiry = data['token']['expiry']
    except Exception:
        expiry = None
    if not expiry:
        return (False, 0.0)
    try:
        dt = datetime.strptime(str(expiry)[:19], '%Y-%m-%dT%H:%M:%S')
        et = time.mktime(dt.timetuple())
    except Exception:
        return (False, 0.0)
    return (et <= time.time() + 30, et)


@dataclass(frozen=True)
class AccountCandidate:
    ref: str
    label: str
    token_path: object
    cookie_path: object
    account_dir: object

    def token_file_for_bridge(self):
        if self.token_path:
            return str(self.token_path)
        if self.account_dir:
            return str(Path(self.account_dir) / TOKEN_FILE_NAME)
        return str(Path('.'))

    def cookie_file_for_bridge(self):
        if self.cookie_path:
            return str(self.cookie_path)
        if self.account_dir:
            return str(Path(self.account_dir) / COOKIE_FILE_NAME)
        return str(Path('.') / COOKIE_FILE_NAME)


class GeminiAccountPool:
    def __init__(self, auth_dir, legacy_token=None, legacy_cookie=None):
        self.auth_dir = str(Path(auth_dir).expanduser())
        self.legacy_token = str(Path(legacy_token).expanduser()) if legacy_token else None
        self.legacy_cookie = str(Path(legacy_cookie).expanduser()) if legacy_cookie else None
        self._lock = threading.RLock()
        self.accounts = []
        self.state = {'primary_ref': None, 'active_ref': None, 'accounts': {}}
        try:
            os.makedirs(self.auth_dir, exist_ok=True)
            _harden(self.auth_dir)
        except OSError:
            pass
        self._load_state()
        self.reload()

    # ---------- 持久化 ----------
    @property
    def _state_file(self):
        return Path(self.auth_dir) / STATE_FILE_NAME

    def _load_state(self):
        try:
            with open(str(self._state_file), encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.state = {
                    'primary_ref': data.get('primary_ref'),
                    'active_ref': data.get('active_ref'),
                    'accounts': data.get('accounts') or {},
                }
        except Exception:
            pass

    def _save_locked(self):
        try:
            _atomic_json(self._state_file, self.state)
        except Exception:
            pass

    # ---------- 扫描 ----------
    def _scan(self):
        out = []
        try:
            entries = sorted(os.listdir(self.auth_dir))
        except OSError:
            entries = []
        for name in entries:
            d = Path(self.auth_dir) / name
            if name.startswith('.') or not d.is_dir():
                continue
            token_path = d / TOKEN_FILE_NAME
            cookie_path = d / COOKIE_FILE_NAME
            out.append({
                'ref': _ref(name),
                'label': name,
                'dir': str(d),
                'token_path': str(token_path) if token_path.exists() else None,
                'cookie_path': str(cookie_path) if cookie_path.exists() else None,
                'legacy': False,
            })
        if self.legacy_token and os.path.exists(self.legacy_token):
            out.append({
                'ref': _ref(LEGACY_LABEL),
                'label': LEGACY_LABEL,
                'dir': None,
                'token_path': self.legacy_token,
                'cookie_path': self.legacy_cookie if (self.legacy_cookie and os.path.exists(self.legacy_cookie)) else None,
                'legacy': True,
            })
        return out

    def _refs(self):
        return set(a['ref'] for a in self.accounts)

    def reload(self):
        with self._lock:
            scanned = self._scan()
            old = self.state.get('accounts') or {}
            refs = set(a['ref'] for a in scanned)
            acc = {}
            for a in scanned:
                prev = old.get(a['ref'])
                acc[a['ref']] = dict(prev) if isinstance(prev, dict) else {}
            self.accounts = scanned
            self.state['accounts'] = acc
            primary = self.state.get('primary_ref')
            if primary not in refs:
                imported = [a for a in scanned if not a['legacy']]
                if imported:
                    primary = imported[0]['ref']
                elif scanned:
                    primary = scanned[0]['ref']
                else:
                    primary = None
                self.state['primary_ref'] = primary
            active = self.state.get('active_ref')
            if active not in refs:
                self.state['active_ref'] = None
            self._save_locked()

    # ---------- 导入 / 移除 ----------
    def import_current(self, token_source, cookie_source, label, make_primary_if_first=True):
        label = str(label or '').strip()
        if not label or label in ('.', '..') or '/' in label or os.sep in label:
            raise ValueError('invalid label: %r' % label)
        if label.startswith('.'):
            raise ValueError('invalid label: %r' % label)
        if label == LEGACY_LABEL:
            raise ValueError('label %r is reserved' % label)
        with self._lock:
            had_imported = any(not a['legacy'] for a in self.accounts)
            target = Path(self.auth_dir) / label
            src_token = str(token_source) if (token_source and os.path.exists(str(token_source))) else None
            if src_token is None and not (target / TOKEN_FILE_NAME).exists():
                raise RuntimeError('no token to import for label %s' % label)
            created = not target.exists()
            try:
                target.mkdir(parents=True, exist_ok=True)
                _harden(str(target))
                if src_token:
                    dst_token = target / TOKEN_FILE_NAME
                    shutil.copyfile(src_token, str(dst_token))
                    _harden(str(dst_token))
                if cookie_source and os.path.exists(str(cookie_source)):
                    dst_cookie = target / COOKIE_FILE_NAME
                    shutil.copyfile(str(cookie_source), str(dst_cookie))
                    _harden(str(dst_cookie))
            except Exception:
                # A failed import must not leave a phantom account behind that
                # would sort into the pool with no credential inside it.
                for name in (TOKEN_FILE_NAME, COOKIE_FILE_NAME):
                    try:
                        (target / name).unlink()
                    except OSError:
                        pass
                if created:
                    try:
                        target.rmdir()
                    except OSError:
                        pass
                raise
            self._load_state()
            self.reload()
            ref = _ref(label)
            self.state['accounts'].setdefault(ref, {})
            if make_primary_if_first and not had_imported:
                self.state['primary_ref'] = ref
            self._save_locked()
            return self.get_account(ref)

    def remove(self, ref):
        with self._lock:
            target = None
            for a in self.accounts:
                if a['ref'] == ref:
                    target = a
                    break
            if target is None:
                raise KeyError('unknown account: %r' % ref)
            if target['legacy']:
                raise RuntimeError('cannot remove the legacy account')
            d = Path(target['dir'])
            for name in (TOKEN_FILE_NAME, COOKIE_FILE_NAME, STATE_FILE_NAME):
                try:
                    (d / name).unlink()
                except OSError:
                    pass
            try:
                d.rmdir()
            except OSError:
                pass
            self._load_state()
            self.reload()

    # ---------- 选择 ----------
    def _candidate(self, acct):
        return AccountCandidate(
            ref=acct['ref'],
            label=acct['label'],
            token_path=acct.get('token_path'),
            cookie_path=acct.get('cookie_path'),
            account_dir=acct.get('dir'),
        )

    def candidates(self, ignore_cooldown=False):
        with self._lock:
            now = time.time()
            rows = []
            for a in self.accounts:
                acct = dict(a)
                st = (self.state.get('accounts') or {}).get(acct['ref']) or {}
                acct['cooldown_until'] = float(st.get('cooldown_until') or 0.0)
                acct['failures'] = int(st.get('failures') or 0)
                acct['reason'] = st.get('reason')
                acct['last_used'] = float(st.get('last_used') or 0.0)
                if acct['ref'] == self.state.get('primary_ref'):
                    preferred = 2
                elif acct['ref'] == self.state.get('active_ref'):
                    preferred = 1
                else:
                    preferred = 0
                acct['cooling'] = acct['cooldown_until'] > now
                if acct['cooling'] and not ignore_cooldown:
                    continue
                rows.append(((preferred, acct['last_used'], acct['label'], acct['ref']), acct))
            # primary first, then whoever worked most recently, then by name.
            # Sorting by ref hash instead made every fresh pool pick the same
            # account first in an order nobody could predict or reproduce.
            rows.sort(key=lambda item: (-item[0][0], -item[0][1], item[0][2], item[0][3]))
            return [self._candidate(acct) for _, acct in rows]

    # ---------- 结果回写 ----------
    def mark_success(self, ref):
        with self._lock:
            st = self.state['accounts'].setdefault(ref, {})
            st['last_used'] = time.time()
            st['cooldown_until'] = 0.0
            st['failures'] = 0
            st['reason'] = None
            if ref in self._refs():
                self.state['active_ref'] = ref
            self._save_locked()

    def mark_failure(self, ref, reason, cooldown):
        with self._lock:
            st = self.state['accounts'].setdefault(ref, {})
            st['failures'] = int(st.get('failures') or 0) + 1
            st['reason'] = str(reason)[:300] if reason else None
            st['cooldown_until'] = time.time() + max(1, cooldown)
            if self.state.get('active_ref') == ref:
                self.state['active_ref'] = None
            self._save_locked()

    def set_primary(self, ref):
        with self._lock:
            if ref not in self._refs():
                raise KeyError('unknown account: %r' % ref)
            self.state['primary_ref'] = ref
            self.state.setdefault('accounts', {}).setdefault(ref, {})
            self._save_locked()

    # ---------- 查询 ----------
    def get_account(self, ref):
        with self._lock:
            now = time.time()
            for a in self.accounts:
                if a['ref'] != ref:
                    continue
                acct = dict(a)
                st = (self.state.get('accounts') or {}).get(ref) or {}
                acct['ref'] = ref
                acct['failures'] = int(st.get('failures') or 0)
                acct['reason'] = st.get('reason')
                acct['cooldown_until'] = float(st.get('cooldown_until') or 0.0)
                acct['last_used'] = float(st.get('last_used') or 0.0)
                acct['last_used_at'] = _iso(acct['last_used'])
                acct['primary'] = self.state.get('primary_ref') == ref
                acct['active'] = self.state.get('active_ref') == ref
                acct['cooling'] = acct['cooldown_until'] > now
                expired, exp_epoch = _token_state(acct.get('token_path'))
                acct['token_expired'] = expired
                acct['token_expires_at'] = _iso(exp_epoch)
                acct['has_web_cookie'] = bool(acct.get('cookie_path'))
                if acct['token_expired']:
                    acct['state'] = 'expired'
                elif acct['cooling']:
                    acct['state'] = 'cooling'
                else:
                    acct['state'] = 'ready'
                return acct
        return None

    def status(self):
        with self._lock:
            rows = [self.get_account(a['ref']) for a in self.accounts]
            rows = [a for a in rows if a]
            rows.sort(key=lambda a: (
                0 if a['primary'] else (1 if a['active'] else 2),
                a['label'].lower(),
                a['ref'],
            ))
            return rows

    def summary(self):
        with self._lock:
            rows = self.status()
            return {
                'ok': True,
                'auth_dir': self.auth_dir,
                'count': len(rows),
                'ready': sum(1 for a in rows if a['state'] == 'ready'),
                'cooling': sum(1 for a in rows if a['state'] == 'cooling'),
                'expired': sum(1 for a in rows if a['state'] == 'expired'),
                'accounts': rows,
            }
