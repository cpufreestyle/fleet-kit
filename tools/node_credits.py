#!/usr/bin/env python3
"""Per-node account and credits view for the whole FleetKit fleet.

One row per node, answering three questions nothing else answered together:
is the node up, which account is behind it, and what does the platform say its
credits/quota are.

Why this exists
---------------
Measured 2026-10-01: the picker's credits annotation (tools/free_models.py +
free-windows.json) says *how* a model bills -- client credits, a rate limit, an
independent key -- but never *how much is left*. The check-in daemon
(tools/checkin.py) covered 2 nodes out of 15, so "签到和积分" was answerable
for xhx and workbuddy and silently missing for the other thirteen. Every node
below therefore gets a row, and every row carries the source its number came
from, so a node with no balance API says so instead of showing a zero.

Sources, strongest first
------------------------
* the bridge's own endpoints -- it already holds the credential and talks to
  the upstream, so nothing here duplicates a login:
    - workbuddy / workbuddy-gpt: GET / hands out the dashboard session cookie,
      then /ui/status for the account and /ui/checkin for the real Buddy 加油
      站 state plus the per-account credit; POST /ui/checkin/claim takes it
    - zcode: /health for the login, /entitlements for the token grant
    - every other bridge: /health (or /) for up / account / plan / login
* the official account API, read-only with the credential the bridge already
  stores: the SenseTime Raccoon points balance
* free-windows.json: the credits *kind* per provider, which is all most
  vendors publish

Stdlib only (urllib, no httpx): the status UI shells out to this file under
whatever interpreter launchd gave it, so the view must not need a venv.

Usage
  python3 node_credits.py            # one table for the whole fleet
  python3 node_credits.py --json     # machine-readable
  python3 node_credits.py --node zcode --node workbuddy
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Ports and the gateway-only providers come from fleet_probe.py, the module the
# reachability sweep already trusts, so a node added there cannot be forgotten
# here. The import is defensive: the view still renders if fleet_probe moves.
try:
    import fleet_probe
    PORTS = dict(getattr(fleet_probe, "PORTS", {}))
    GATEWAY = dict(getattr(fleet_probe, "GATEWAY", {}))
    GATEWAY_PORT = int(getattr(fleet_probe, "GATEWAY_PORT", 10100))
except Exception:                                        # pragma: no cover
    PORTS = {"workbuddy": 8787, "workbuddy-gpt": 8788, "qoder": 8789,
             "codely": 8790, "trae": 8791, "lingxi": 8792, "xhx": 8793,
             "gemini": 8794, "catpaw": 8795, "antigravity": 8797,
             "qwen": 8798, "cline": 8799, "zcode": 8800}
    GATEWAY = {"stepfun": "stepfun", "tokendance": "tokendance"}
    GATEWAY_PORT = 10100

NODE_ORDER = ("workbuddy", "workbuddy-gpt", "xhx", "zcode", "qoder", "codely",
              "trae", "lingxi", "cline", "qwen", "gemini", "catpaw",
              "antigravity", "stepfun", "tokendance",
              "kimi-code", "minimax")

VENDORS = {
    "workbuddy": "腾讯云代码助手 CodeBuddy（国内版）",
    "workbuddy-gpt": "腾讯 WorkBuddy（海外版）",
    "qoder": "阿里巴巴 Qoder",
    "codely": "团结AI Tuanjie AI",
    "trae": "字节跳动 TRAE",
    "lingxi": "灵犀AI LingXi",
    "xhx": "商汤小浣熊 RaccoonWork",
    "gemini": "Google Gemini Code Assist",
    "catpaw": "美团 CatPawAI",
    "antigravity": "Google Antigravity",
    "qwen": "阿里 Qwen（MaaS）",
    "cline": "Cline（cline.bot 免费档）",
    "zcode": "智谱 Z.AI Coding（ZCode）",
    "stepfun": "阶跃星辰 StepFun（ocx 原生）",
    "tokendance": "TokenDance 词元跳动（ocx 原生）",
    "kimi-code": "月之暗面 Kimi Code（coding 套餐）",
    "minimax": "MiniMax（编程套餐 / Agent）",
}

# Which upstream facts each bridge reports, and where. The bridges disagree on
# shape ("status" vs "ok", "account" vs "ide_account" vs "user_id"), so the
# reader takes the first key the payload actually carries rather than assuming
# one schema for fifteen codebases.
ACCOUNT_KEYS = ("account", "ide_account", "user_id", "phone", "login", "email")
PLAN_KEYS = ("plan", "edition", "tier", "region", "subscription")
LOGIN_KEYS = ("logged_in", "ide_logged_in", "session_alive")

HEALTH_TIMEOUT = 6.0
# /ui/checkin walks the whole account pool against the vendor, so it is the one
# call that is allowed to be slow.
CHECKIN_TIMEOUT = 30.0

NO_CHECKIN_NOTE = "上游无每日签到端点（见 docs/codex-checkin-runbook.md）"
NO_BALANCE_NOTE = "上游未提供余额查询接口"


def _get(url, timeout=HEALTH_TIMEOUT, headers=None, opener=None):
    """GET -> (status, payload). Never raises; status 0 means unreachable."""
    req = urllib.request.Request(url, headers=headers or {})
    op = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=timeout) as resp:
            body = resp.read(4 * 1024 * 1024).decode("utf-8", "ignore")
            return resp.status, _maybe_json(body)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, _maybe_json(exc.read(2000).decode("utf-8", "ignore"))
        except Exception:
            return exc.code, {}
    except Exception as exc:
        return 0, {"__err": "%s: %s" % (type(exc).__name__, str(exc)[:120])}


def _post(url, payload=None, timeout=CHECKIN_TIMEOUT, headers=None, opener=None):
    data = json.dumps(payload or {}).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    op = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=timeout) as resp:
            return resp.status, _maybe_json(resp.read(4 * 1024 * 1024).decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, _maybe_json(exc.read(2000).decode("utf-8", "ignore"))
        except Exception:
            return exc.code, {}
    except Exception as exc:
        return 0, {"__err": "%s: %s" % (type(exc).__name__, str(exc)[:120])}


def _maybe_json(body):
    try:
        return json.loads(body)
    except Exception:
        return {"__raw": body[:200]}


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return None


def _pretty(value):
    if value is None:
        return "-"
    if isinstance(value, float) and value == int(value):
        value = int(value)
    return "{:,}".format(value) if isinstance(value, int) else str(value)


def _pick(payload, keys):
    for key in keys:
        if isinstance(payload, dict) and payload.get(key) not in (None, ""):
            return payload[key]
    return None


def _account_of(payload):
    value = _pick(payload, ACCOUNT_KEYS)
    return "" if value is None else str(value)


def _plan_of(payload):
    value = _pick(payload, PLAN_KEYS)
    return "" if value is None else str(value)


def _login_of(payload):
    value = _pick(payload, LOGIN_KEYS)
    if value is None:
        return None
    return bool(value)


def _base(name):
    port = PORTS.get(name)
    return None if not port else "http://127.0.0.1:%d" % port


# ---------------- 本机凭据：桥不上报账号时，读它自己的登录态 ----------------
#
# Two bridges keep their login as a JWT on disk and never echo an account name
# on /health (measured 2026-10-01: xhx and lingxi both answered account=""),
# so the panel drew a dash for a bridge that is signed in. The payload of a
# JWT is the identity the vendor itself issued -- it is not a credential, and
# reading it needs neither a second login nor an upstream call the vendor may
# refuse (gemini answers 403 on the account level today).
LOCAL_AUTH_FILES = {
    # node: (env var, default dir, file, token key)
    "xhx": ("BOX_AGENT_CONFIG_DIR",
            os.path.join("~", ".box-agent", "config"), "auth.json",
            "access_token"),
    "lingxi": ("LINGXI_HOME", os.path.join("~", ".LingXi"), "auth.json", "token"),
}


def _jwt_claims(token):
    """Payload claims of a JWT, read without verifying the signature."""
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def local_account(name):
    """Which account this bridge is signed in as, from its own credential file.

    "" when the file is absent (never logged in on this machine), the token is
    opaque, or the claims carry neither a display name nor an id: the caller
    then keeps the dash instead of inventing one.
    """
    env_key, default_dir, filename, token_key = LOCAL_AUTH_FILES.get(
        name, (None, "", "", ""))
    if not env_key:
        return ""
    root = os.environ.get(env_key) or default_dir
    try:
        auth = json.loads((Path(os.path.expanduser(root)) / filename)
                          .read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(auth, dict):
        return ""
    claims = _jwt_claims(auth.get(token_key))
    # Display name first -- it is what the vendor's own UI shows. Fall back to
    # the subject id, which is the account's stable identity for vendors that
    # issue no name at all (lingxi writes name="" next to its token).
    for value in (auth.get("name"), claims.get("name"), claims.get("user_id"),
                  claims.get("sub")):
        if value and str(value).strip():
            return str(value).strip()[:80]
    return ""


# ---------------- workbuddy / workbuddy-gpt：Buddy 加油站 ----------------

def workbuddy_opener(name):
    """An opener holding the dashboard session cookie the bridge hands out.

    The bridge protects its account endpoints with a cookie it only sets on
    GET / (secrets.token_urlsafe(32), per process), so the cookie has to be
    collected first. Requesting it is not a bypass: it is the same cookie the
    browser dashboard gets, and the endpoints still demand loopback, JSON and
    a matching Origin (see _check_dashboard_management in bridges/workbuddy).
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.ProxyHandler({}))
    base = _base(name)
    if base:
        _get(base + "/", opener=opener)
    return opener


def workbuddy_checkin(name):
    """Live Buddy 加油站 state: activity, per-account credit, checked-in flag."""
    base = _base(name)
    if not base:
        return {"ok": False, "detail": "bridge port unknown"}
    opener = workbuddy_opener(name)
    status, payload = _get(base + "/ui/checkin", timeout=CHECKIN_TIMEOUT,
                           headers={"Content-Type": "application/json",
                                    "Origin": base},
                           opener=opener)
    if status != 200 or not isinstance(payload, dict) or not payload.get("ok"):
        return {"ok": False, "status": status,
                "detail": (payload.get("detail") if isinstance(payload, dict) else None)
                          or "ui/checkin http %s" % status}
    return payload


def workbuddy_claim(name, ref=None):
    """Take today's credits for one account (ref) or the whole pool."""
    base = _base(name)
    if not base:
        return {"ok": False, "detail": "bridge port unknown"}
    opener = workbuddy_opener(name)
    status, payload = _post(base + "/ui/checkin/claim", {"ref": ref or ""},
                            headers={"Origin": base}, opener=opener)
    return {"ok": status == 200 and isinstance(payload, dict) and payload.get("ok"),
            "status": status, "payload": payload,
            "detail": (payload.get("detail") if isinstance(payload, dict) else None)
                      or "claim http %s" % status}


# ---------------- zcode：一次性 token 额度 ----------------

def zcode_entitlements():
    status, payload = _get(_base("zcode") + "/entitlements")
    if status != 200 or not isinstance(payload, dict):
        return {}
    return payload


# ---------------- xhx：小浣熊积分余额（只读） ----------------

def xhx_auth_file():
    root = os.environ.get("BOX_AGENT_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".box-agent", "config")
    return Path(root) / "auth.json"


def xhx_points():
    """The official balance, read-only.

    Deliberately does not refresh the token: refreshing rotates a
    single-use refresh_token and belongs to the check-in action, not to a view
    that a dashboard may poll. An expired token reports as such.
    """
    path = xhx_auth_file()
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "detail": "auth.json 不可读: %s" % str(exc)[:80]}
    token = auth.get("access_token")
    if not token:
        return {"ok": False, "detail": "小浣熊未登录"}
    base = os.environ.get("XHX_WEB_BASE_URL") or "https://xiaohuanxiong.com"
    status, payload = _get(base + "/api/web/points/v1/balance",
                           headers={"Authorization": "Bearer " + token})
    if status != 200 or not isinstance(payload, dict):
        return {"ok": False, "status": status,
                "detail": "balance http %s" % status}
    return {"ok": True, "data": payload.get("data") or {}}


# ---------------- generic：桥自带的账号/登录信息 ----------------

def generic_health(name):
    base = _base(name)
    if not base:
        return {}
    for path in ("/health", "/"):
        status, payload = _get(base + path)
        if status == 200 and isinstance(payload, dict) and "__raw" not in payload:
            payload["__path"] = path
            return payload
    return {}


# ---------------- free-windows.json：厂商公示的积分口径 ----------------

def credits_db():
    """The annotation database, or {} when it is not deployed.

    tools/free_models.py is the reader used everywhere else; importing it keeps
    one definition of what a credits kind means instead of a second copy here.
    """
    path = HERE / "free_models.py"
    if not path.is_file():
        return {}
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("free_models_reader", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        db = module.load_db()
        return {"providers": db.get("providers", {}),
                "kinds": module.CREDITS_KINDS,
                "badge": module.CREDITS_BADGE}
    except Exception:
        return {}


_DB_CACHE = {}


def credits_kind(name):
    if "db" not in _DB_CACHE:
        _DB_CACHE["db"] = credits_db()
    providers = (_DB_CACHE["db"].get("providers") or {})
    entry = providers.get(name) or {}
    kind = entry.get("credits")
    if kind in (_DB_CACHE["db"].get("kinds") or ()):
        return kind
    return "unknown"


# ---------------- 编程套餐账号（Kimi Code / MiniMax） ----------------
# Neither of these is a bridge: they are coding plans, and the fleet runs no
# bridge process for them. What each platform answers is different -- Kimi
# publishes a usage endpoint, MiniMax publishes nothing but a chat call -- so
# plan_credits.py owns the calls and this view only renders what came back. A
# refused key is reported as refused: a dead key shown as 0 credits is how a
# plan gets cancelled without anyone noticing.
PLAN_ACCOUNTS = {
    "kimi-code": {"vendor": "月之暗面 Kimi Code（coding 套餐）",
                  "env": "KIMI_CODING_API_KEY", "plan": "kimi"},
    "minimax": {"vendor": "MiniMax（编程套餐 / Agent）",
                "env": "MINIMAX_API_KEY", "plan": "minimax"},
}


def cc_switch_kimi_key():
    """The Kimi coding key CC Switch still holds, or "" when there is none."""
    try:
        import sqlite3
        db = os.path.expanduser("~/.cc-switch/cc-switch.db")
        if not os.path.exists(db):
            return ""
        con = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
        try:
            row = con.execute(
                "select settings_config from providers"
                " where app_type='claude' and name='default'").fetchone()
        finally:
            con.close()
        if not row:
            return ""
        cfg = json.loads(row[0] or "{}")
        return str((cfg.get("env") or {}).get("ANTHROPIC_AUTH_TOKEN") or "")
    except Exception:
        return ""


def plan_key(name):
    """(key, where it came from) for a plan account, ("", "") when none."""
    spec = PLAN_ACCOUNTS[name]
    key = os.environ.get(spec["env"]) or ""
    if key:
        return key, "env " + spec["env"]
    if spec["plan"] == "kimi":
        stored = cc_switch_kimi_key()
        if stored:
            return stored, "cc-switch default provider"
    return "", ""


def _plan_numbers(body, limit=6):
    """Every number in a usage payload, as "path=value" pairs.

    The payload shape is only knowable once a live key answers, so the
    reader reports what is there instead of hard-coding keys that may
    not exist in the reply.
    """
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, "%s/%s" % (path, key))
        elif isinstance(node, list):
            for value in node:
                walk(value, path + "[]")
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            found.append((path, node))

    walk(body, "")
    return found[:limit]


def _refusal_text(out):
    """The upstream message from a refused call, trimmed for one cell."""
    body = out.get("body")
    if isinstance(body, dict):
        err = body.get("error") or {}
        return str(err.get("message") or body)[:80]
    return str(body)[:80]


def read_plan_account(name):
    row = _row(name)
    row["vendor"] = PLAN_ACCOUNTS[name]["vendor"]
    row["credits_kind"] = "subscription"
    row["checkin"] = "上游无每日签到端点（额度只读，见 plan_credits.py）"
    key, source = plan_key(name)
    row["credits_source"] = "plan_credits（%s）" % (source or "无凭据")
    if not key:
        row["credits_note"] = ("未配置 key：export %s=<key>"
                               % PLAN_ACCOUNTS[name]["env"])
        row["detail"] = "没有可用凭据，未发起调用"
        return row
    row["account"] = "%s…%s" % (key[:4], key[-4:])
    import plan_credits
    if PLAN_ACCOUNTS[name]["plan"] == "kimi":
        status, out = plan_credits.kimi(key)
        row["up"] = status == 200
        row["logged_in"] = status == 200
        row["detail"] = "GET %s -> HTTP %s" % (out.get("endpoint"), status)
        if status == 200:
            numbers = _plan_numbers(out.get("body"))
            row["credits_note"] = ("；".join("%s=%s" % (p, _pretty(v))
                                            for p, v in numbers)
                                   or "接口未返回额度数字")
            for path, value in numbers:
                if any(word in path.lower()
                       for word in ("remain", "left", "quota", "balance")):
                    row["credits_value"] = value
                    row["credits_unit"] = path.rsplit("/", 1)[-1]
                    break
        else:
            row["credits_note"] = "key 被拒：HTTP %s %s" % (
                status, _refusal_text(out))
        return row
    status, out = plan_credits.minimax(key)
    row["up"] = status == 200
    row["logged_in"] = status == 200
    row["detail"] = "POST %s (%s) -> HTTP %s" % (
        out.get("endpoint"), out.get("model"), status)
    row["credits_note"] = (
        "MiniMax 无余额接口；1-token 调用已发出，扣减以控制台为准"
        if status == 200 else
        "key 被拒：HTTP %s %s" % (status, _refusal_text(out)))
    return row


# ---------------- 每节点一行 ----------------

def _row(name):
    return {"node": name, "vendor": VENDORS.get(name, name), "up": False,
            "account": "", "plan": "", "logged_in": None,
            "credits_kind": credits_kind(name), "credits_value": None,
            "credits_unit": "", "credits_source": "", "credits_note": "",
            "checked_in": None, "checkin": "", "streak_days": None,
            "detail": ""}


def read_node(name):
    row = _row(name)
    base = _base(name)

    if name in PLAN_ACCOUNTS:
        return read_plan_account(name)

    if name in GATEWAY:
        # No local bridge: ocx forwards these straight to the vendor, so the
        # only thing observable from here is whether the gateway answers.
        status, _payload = _get("http://127.0.0.1:%d/v1/models" % GATEWAY_PORT)
        row["up"] = status != 0
        row["credits_note"] = NO_BALANCE_NOTE + "（ocx 网关 provider，账号额度在官方控制台）"
        row["credits_source"] = "free-windows.json"
        row["checkin"] = NO_CHECKIN_NOTE
        row["detail"] = ("ocx 网关 127.0.0.1:%d http %s" % (GATEWAY_PORT, status)
                         if status else "ocx 网关不可达（Codex 未启动或网关未运行）")
        return row

    if not base:
        row["detail"] = "未知节点"
        return row

    if name in ("workbuddy", "workbuddy-gpt"):
        state = workbuddy_checkin(name)
        row["up"] = bool(state.get("ok"))
        if not row["up"]:
            row["detail"] = state.get("detail") or "桥不可达"
            row["credits_note"] = NO_BALANCE_NOTE
            row["checkin"] = NO_CHECKIN_NOTE
            return row
        accounts = state.get("accounts") or []
        names = [str(a.get("name") or a.get("ref") or "") for a in accounts]
        row["account"] = ", ".join(n for n in names if n)[:80]
        activity = state.get("activity") or {}
        row["plan"] = str(activity.get("activity_name")
                          or activity.get("theme_name") or "")[:60]
        credits = sum(_num(a.get("credit")) or 0 for a in accounts)
        streak = max((a.get("streak_days") or 0) for a in accounts) if accounts else 0
        # An account that cannot read its own state is not "checked in": the
        # overseas bridge reports ok=false with 状态读取失败 while its activity
        # is unavailable, and calling that a claim would be a false green.
        broken = [a for a in accounts if not a.get("ok")]
        # Count what is owed from the per-account flags rather than the
        # summary field: they are the same thing today, and the flags are
        # what each account actually reported.
        owed = [a for a in accounts if a.get("ok") and not a.get("today_checked_in")]
        row["logged_in"] = bool(accounts) and not broken
        row["credits_value"] = credits
        row["credits_unit"] = "credits"
        row["credits_source"] = "bridge /ui/checkin"
        row["credits_note"] = (
            "Buddy 加油站每日 %s credits；桥启动时自动领取" % _pretty(
                _num(activity.get("daily_credit")))
            if activity.get("active") else
            "Buddy 加油站活动当前不可用（available=false）")
        row["streak_days"] = streak or None
        row["checked_in"] = bool(accounts) and not broken and not owed
        if not accounts:
            row["checkin"] = "账号池为空"
        elif broken:
            row["checkin"] = str(broken[0].get("message") or "状态读取失败")
        elif owed:
            row["checkin"] = "有 %d 个账号待领取" % len(owed)
        else:
            row["checkin"] = "今日已领（连签 %s 天）" % streak
        row["detail"] = "账号 %d 个（异常 %d）；活动 %s@%s" % (
            len(accounts), len(broken), activity.get("theme_name") or "-",
            activity.get("season") or "-")
        return row

    health = generic_health(name)
    if not health:
        row["detail"] = "桥不可达（127.0.0.1:%s）" % PORTS.get(name)
        row["credits_note"] = NO_BALANCE_NOTE
        row["checkin"] = NO_CHECKIN_NOTE
        row["credits_source"] = "free-windows.json"
        return row

    row["up"] = True
    row["account"] = _account_of(health)[:80]
    account_from = "health"
    if not row["account"]:
        # The bridge names no identity (measured: xhx and lingxi both answer
        # account=""), so read the one its own credential file already holds
        # rather than drawing a dash for a bridge that is signed in.
        row["account"] = local_account(name)[:80]
        account_from = "credential file" if row["account"] else "health"
    row["plan"] = _plan_of(health)[:60]
    row["logged_in"] = _login_of(health)
    row["detail"] = "health %s v%s" % (health.get("__path"),
                                       health.get("version") or "?")
    if account_from == "credential file":
        row["detail"] += "；account 本机凭据"
    row["credits_source"] = "free-windows.json"

    if name == "zcode":
        ent = zcode_entitlements()
        if ent:
            units = sum(_num(v.get("grant_units")) or 0
                        for v in ent.values() if isinstance(v, dict))
            plans = sorted({str(v.get("plan") or "") for v in ent.values()
                            if isinstance(v, dict) and v.get("plan")})
            active = all(str(v.get("status") or "").lower() in ("active", "valid", "")
                        for v in ent.values() if isinstance(v, dict))
            row["credits_value"] = units
            row["credits_unit"] = "tokens"
            row["credits_source"] = "bridge /entitlements"
            row["credits_note"] = "，".join(plans) or "ZCode plan"
            row["logged_in"] = bool(health.get("logged_in", True)) and active
            row["detail"] += "；plan %s" % "，".join(plans)
        else:
            row["credits_note"] = NO_BALANCE_NOTE
        row["checkin"] = NO_CHECKIN_NOTE
        row["detail"] += "；captcha %s" % (health.get("captcha") or "?")
        return row

    if name == "xhx":
        points = xhx_points()
        if points.get("ok"):
            data = points.get("data") or {}
            row["credits_value"] = _num(data.get("available_points"))
            row["credits_unit"] = "points"
            row["credits_source"] = "xiaohuanxiong /api/web/points/v1/balance"
            row["credits_note"] = "每日签到发放；llm/v2 调用不结算积分"
            row["detail"] += "；models %d" % len(health.get("models") or [])
        else:
            row["credits_note"] = "%s（%s）" % (NO_BALANCE_NOTE,
                                              points.get("detail") or "未知")
        row["checkin"] = "见签到面板（每日登录积分）"
        return row

    row["credits_note"] = NO_BALANCE_NOTE
    row["checkin"] = NO_CHECKIN_NOTE
    return row


def read_all(names=None):
    wanted = list(names) if names else [n for n in NODE_ORDER if n]
    return [read_node(name) for name in wanted]


def render(rows):
    head = "%-13s %-4s %-22s %-6s %-11s %-14s %s" % (
        "节点", "状态", "账号", "登录", "积分口径", "积分/额度", "来源")
    lines = [head, "-" * len(head)]
    for row in rows:
        credits = ("%s %s" % (_pretty(row["credits_value"]), row["credits_unit"])
                   if row["credits_value"] is not None else "-")
        lines.append("%-13s %-4s %-22s %-6s %-11s %-14s %s" % (
            row["node"], "up" if row["up"] else "down",
            (row["account"] or "-")[:22],
            {True: "是", False: "否", None: "?"}[row["logged_in"]],
            row["credits_kind"], credits.strip()[:14],
            row["credits_source"] or "-"))
    return chr(10).join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--node", action="append", default=[], help="only this node")
    args = ap.parse_args(argv)
    rows = read_all(args.node or None)
    if args.json:
        print(json.dumps({"nodes": rows}, ensure_ascii=False, indent=1))
    else:
        print(render(rows))
        print()
        for row in rows:
            print("%-13s %s | 签到: %s | %s" % (row["node"], row["detail"],
                                               row["checkin"] or "-",
                                               row["credits_note"] or ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
