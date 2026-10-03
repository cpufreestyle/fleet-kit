#!/usr/bin/env python3
"""kimi2codex — 把 Kimi Code（coding 套餐）模型暴露成标准 OpenAI 兼容 API。

背景（2026-10-03 核实）：
  * 上游 https://api.kimi.com/coding/v1 是 OpenAI 兼容协议，/v1/models 与
    /v1/chat/completions 都在这个前缀下（不要写成 /v1 直挂，那是另一个服务）。
  * 实测模型目录（2026-10-03，Bearer 鉴权）：
        kimi-for-coding            K2.8 Preview    1M 上下文，仅思考模式
        kimi-for-coding-highspeed  K2.7 Code Highspeed   256K
        k3                        K3              1M
        k3-256k                   K3-256k         256K
  * 鉴权：Authorization: Bearer <key>；x-api-key 也认（plan_credits 两种都测过）。
  * 本机: 127.0.0.1:8802（OpenAI 兼容：/v1/models、/v1/chat/completions）。

关于 key 来源：Kimi 桌面版把签发出去的 coding key 存在自己的 user data 里
（daimon-share/daimon/kimi-code-key.json）。本桥在环境变量没给 key 时回落到
那份文件——只读不写，和 plan_credits.py 同一套做法，省掉用户手贴一把已经在
机器上的 key。KIMI_UPSTREAM_KEY_FILE 可改路径，KIMI_NO_APP_KEY=1 关掉回落。

账号池（bridges/plan_key_pool.py）：一把 key 一个账号，落在本桥 auths/ 目录
下，请求按池内候选逐个尝试，key 被拒/套餐失效/限流自动切下一把并给失败 key
记冷却。第一把由环境变量或桌面 app key 文件播种，更多 key 运行时可加：

    curl -X POST http://127.0.0.1:8802/admin/pool/add \
      -H "Authorization: Bearer <KIMI2CODEX_KEY>" -d '{"key":"sk-kimi-..."}'

一个必须分清的失败：套餐没生效时 /v1/models 照样 200（key 是好的），只有
/v1/chat/completions 返回 403 access_terminated_error。把这种情况说成"key 失效"
会让用户去找一把根本没坏的 key，所以本桥把这一类单独措辞。

依赖：fastapi + uvicorn + httpx。用法：python3 kimi_bridge.py [--port 8802]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import _common
import plan_key_pool

BRIDGE_VERSION = "1.2.0"

UPSTREAM_BASE = (os.environ.get("KIMI_UPSTREAM")
                 or "https://api.kimi.com/coding/v1").rstrip("/")
BRIDGE_KEY = os.environ.get("KIMI2CODEX_KEY") or ""
UPSTREAM_PROXY = (os.environ.get("KIMI_UPSTREAM_PROXY") or "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("KIMI_CALL_TIMEOUT") or "300")
CATALOG_PREFIX = "kimi/"
POINTS_TTL = float(os.environ.get("KIMI_POINTS_TTL") or "300")
BASE_DIR = Path(__file__).resolve().parent

# The Kimi desktop app's own key file, per platform. Read, never written: this
# is the user's file on their own machine, and reading it is what spares them
# pasting a key that is already there. An explicit env key always wins.
KIMI_APP_KEY_FILES = (
    ("darwin", "Library/Application Support/kimi-desktop/daimon-share/daimon/kimi-code-key.json"),
    ("win32", "AppData/Roaming/kimi-desktop/daimon-share/daimon/kimi-code-key.json"),
    ("linux", ".config/kimi-desktop/daimon-share/daimon/kimi-code-key.json"),
)

# Measured 2026-10-03 against the live /v1/models with a real key. These are
# the rows served when the pool has no ready account, so the names have to be
# the real ones.
FALLBACK_MODELS = ["kimi-for-coding", "kimi-for-coding-highspeed", "k3", "k3-256k"]

# Kimi's catalog also carries non-chat rows (audio, image edit). A chat bridge
# advertising them hands the picker rows that can only fail.
_JUNK_SUBSTR = ("image", "video", "tts", "asr", "audio", "embedding", "embed",
                "rerank", "ocr", "moderation", "realtime", "music")

app = _common.make_app("kimi2codex", BRIDGE_VERSION)

check_bridge_auth = _common.make_auth_checker(BRIDGE_KEY)

client = _common.make_client_getter(
    **_common.client_kwargs(DEFAULT_TIMEOUT, proxy=UPSTREAM_PROXY))

remap_model = _common.make_model_remapper(CATALOG_PREFIX)


def app_key_file():
    """(path, why-not) for this platform's Kimi app key file."""
    forced = os.environ.get("KIMI_UPSTREAM_KEY_FILE")
    if forced:
        return os.path.expanduser(forced), ""
    for platform, rel in KIMI_APP_KEY_FILES:
        if sys.platform.startswith(platform):
            return os.path.join(os.path.expanduser("~"), *rel.split("/")), ""
    return "", "no known Kimi app key path on %s" % sys.platform


def key_from_app():
    """(key, where) read out of the Kimi desktop app, or (None, why)."""
    if os.environ.get("KIMI_NO_APP_KEY"):
        return None, "KIMI_NO_APP_KEY is set"
    path, why = app_key_file()
    if not path:
        return None, why
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)
    keys = data.get("keys") if isinstance(data, dict) else None
    if isinstance(keys, list) and keys and isinstance(keys[0], dict):
        key = keys[0].get("apiKey")
        if key:
            return key, path
    return None, "%s carries no apiKey" % path


def auth_pool_dir() -> Path:
    return Path(os.environ.get("KIMI_AUTH_POOL_DIR") or BASE_DIR / "auths")


def seed_keys():
    """[(key, where)] for a first run, best source first.

    The environment wins and the Kimi desktop app is the fallback, the same
    order resolve_key() used before the pool: an operator who set a key on
    purpose must not be overridden by whatever the app happens to hold.
    """
    pairs = []
    single = os.environ.get("KIMI_CODING_API_KEY") or ""
    if single:
        pairs.append((single, "env KIMI_CODING_API_KEY"))
    many = os.environ.get("KIMI_CODING_API_KEYS") or ""
    for key in many.replace(",", " ").split():
        if key and key not in [item[0] for item in pairs]:
            pairs.append((key, "env KIMI_CODING_API_KEYS"))
    key, where = key_from_app()
    if key and key not in [item[0] for item in pairs]:
        pairs.append((key, where))
    return pairs


def plan_credits_module():
    """plan_credits.py, imported from the fleet's tools/ directory.

    It is stdlib-only (urllib) and already owns every Kimi quota endpoint,
    auth shape and payload shape, so the bridge borrows it instead of keeping
    a second copy that would drift the first time Kimi moves a route.
    """
    tools = Path(__file__).resolve().parents[2] / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    import plan_credits
    return plan_credits


def kimi_points(key: str) -> dict:
    """Quota for one key, normalised for the pool and the panel.

    /v1/usages answers {} for an account with no coding plan -- a real answer
    that reads exactly like a broken reader -- so the plan name from /v1/me
    travels with it and the detail sentence says the windows are empty
    instead of guessing at a number.
    """
    plan_credits = plan_credits_module()
    status, out = plan_credits.kimi(key, spend=False)
    body = out.get("body") if isinstance(out.get("body"), dict) else {}
    plan = out.get("plan") if isinstance(out.get("plan"), dict) else {}
    numbers = plan_key_pool.walk_numbers(body)
    points, unit = plan_key_pool.headline_number(numbers)
    plan_name = str(plan.get("user_level_name") or "")
    windows = ("；".join("%s=%s" % (path, value) for path, value in numbers[:4])
               or "无用量窗口")
    detail = "plan %s；%s" % (plan_name or "未读到的套餐", windows)
    if status != 200:
        return {"points": None, "unit": "", "plan": plan_name,
                "detail": "GET %s -> HTTP %s" % (out.get("endpoint"), status),
                "error": "key 被拒：HTTP %s" % status}
    if not plan_name or (plan.get("goods_version") in (0, "0", None)
                         and not numbers):
        detail += "（无 coding 套餐时该接口就是空的）"
    return {"points": points, "unit": unit, "plan": plan_name,
            "detail": detail, "error": ""}


def is_chat_model(mid: str) -> bool:
    low = mid.lower()
    return not any(j in low for j in _JUNK_SUBSTR)


POOL = plan_key_pool.KeyPool(
    "kimi", auth_pool_dir(),
    lambda: [(key, where) for key, where in seed_keys()],
    read_points=kimi_points, points_ttl=POINTS_TTL)


def classify(status: int, body: str):
    """(reason, cooldown, tag) when one key should stop serving; None to pass through.

    403 access_terminated_error is the one that must not cool the key as if
    it were dead: the key is accepted everywhere else, only the plan is over.
    It still cannot serve, so it is cooled -- just under its own tag, which
    is what turns the final error into the renewal page instead of "get a new
    key".
    """
    text = (body or "").lower()
    if status == 401:
        return "上游拒绝该 key（401）", 3600, "key_dead"
    if status == 403 and "access_terminated_error" in text:
        return "套餐未生效，需续费", 3600, "plan_inactive"
    if status == 403:
        return "上游拒绝该请求（403）", 600, "forbidden"
    if status == 429 or "rate limit" in text or "too many requests" in text:
        return "触发限流", 60, "rate_limit"
    if status >= 500:
        return "上游 HTTP %d" % status, 30, "upstream_5xx"
    return None


def _add_key_hint() -> str:
    return ("add a real Kimi Code key at runtime: POST "
            "http://127.0.0.1:8802/admin/pool/add with {\"key\": \"sk-...\"}")


def _models_pool_error(exc: plan_key_pool.PoolExhausted):
    """The 401/403 every pooled key answered, phrased per failure kind."""
    tags = [failure[4] for failure in exc.failures]
    bodies = [failure[2] for failure in exc.failures]
    if "plan_inactive" in tags:
        index = tags.index("plan_inactive")
        renew = "https://www.kimi.com/code/#pricing"
        return _common.upstream_error_response(
            403, bodies[index], "kimi", "kimi_plan_inactive",
            message="kimi code has no active plan for this key (the key "
                    f"itself is accepted): {bodies[index][:200]}"
                    f" -- renew at {renew}")
    return JSONResponse(
        {"error": {"message": "kimi upstream refused every pooled API key "
                              f"(HTTP {exc.status}); set KIMI_CODING_API_KEY "
                              f"to a real Kimi Code key, or {_add_key_hint()}",
                   "type": "upstream_auth_error"}},
        status_code=401)


def _chat_pool_error(exc: plan_key_pool.PoolExhausted):
    tags = [failure[4] for failure in exc.failures]
    bodies = [failure[2] for failure in exc.failures]
    if "plan_inactive" in tags:
        index = tags.index("plan_inactive")
        renew = "https://www.kimi.com/code/#pricing"
        return _common.upstream_error_response(
            403, bodies[index], "kimi", "kimi_plan_inactive",
            message="kimi code has no active plan for this key (the key "
                    f"itself is accepted): {bodies[index][:200]}"
                    f" -- renew at {renew}")
    if "key_dead" in tags:
        return _models_pool_error(exc)
    return _common.upstream_error_response(exc.status, exc.body, "kimi",
                                           "kimi_upstream_error")


def _transport_detail(exc: plan_key_pool.PoolExhausted) -> str:
    """Why every candidate failed on the transport side, for _fallback.

    The failure body carries the exception text but not its type, and
    "ConnectError" is the part that says which layer broke; the pool already
    recorded the type in each account's reason, so that is what is read.
    """
    reasons = {item["ref"]: item.get("reason") or "" for item in POOL.status()}
    return "、".join(reasons.get(failure[0]) or failure[2] or "transport error"
                    for failure in exc.failures)


@app.get("/health")
async def health():
    accounts = POOL.status()
    info = {
        "ok": True,
        "version": BRIDGE_VERSION,
        "proxy": UPSTREAM_PROXY or "direct",
        "upstream": UPSTREAM_BASE,
        "models": FALLBACK_MODELS,
        "has_api_key": bool(accounts),
        "key_source": (accounts[0].get("source") if accounts else "无 key"),
        "account_pool": POOL.summary(),
    }
    if POOL.points_stale():
        # The reader is a blocking urllib call; it must not stall the loop.
        import asyncio
        asyncio.ensure_future(POOL.refresh_points())
    return info


def _send_models(key: str):
    return client().get(f"{UPSTREAM_BASE}/models",
                        headers=_upstream_headers(key))


@app.get("/v1/models")
async def list_models(request: Request):
    check_bridge_auth(request)
    ids = list(dict.fromkeys(FALLBACK_MODELS))
    detail = ""
    if POOL.status():
        try:
            _candidate, r = await plan_key_pool.request_with_pool(
                POOL, _send_models, classify)
        except plan_key_pool.PoolUnavailable:
            # Every key is cooling: the static catalog is still better than
            # an error, because most of these rows are documented ones.
            detail = "account pool cooling"
        except plan_key_pool.PoolExhausted as exc:
            if all(failure[4] == "transport" for failure in exc.failures):
                # An unreachable upstream is not the accounts' fault; the
                # measured catalog still beats an error page.
                detail = "upstream unreachable: " + _transport_detail(exc)
            else:
                return _models_pool_error(exc)
        else:
            if r.status_code == 200:
                up = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
                kept = [m for m in up if is_chat_model(m)]
                if kept:
                    ids = kept
            else:
                detail = f"upstream {r.status_code}"
            await r.aclose()
    data = {
        "object": "list",
        "data": [
            {"id": f"{CATALOG_PREFIX}{m}", "object": "model", "created": 0,
             "owned_by": "kimi-code"}
            for m in ids
        ],
    }
    if detail:
        data["_fallback"] = detail
    return JSONResponse(data)


def _upstream_headers(key: str) -> dict:
    return {"Content-Type": "application/json", "Accept": "application/json",
            "Authorization": f"Bearer {key}"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    check_bridge_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
    body["model"] = remap_model(body.get("model"))
    stream = bool(body.get("stream"))

    if not POOL.status():
        return _common.upstream_error_response(
            503, "", "kimi", "kimi_key_missing",
            message="kimi bridge has no upstream key: set KIMI_CODING_API_KEY, "
                    "or let it read the Kimi desktop app's key file "
                    "(KIMI_NO_APP_KEY=1 turns that off); " + _add_key_hint())

    async def _send(key: str):
        req = client().build_request(
            "POST", f"{UPSTREAM_BASE}/chat/completions", json=body,
            headers=_upstream_headers(key))
        return await client().send(req, stream=True)

    try:
        _candidate, resp = await plan_key_pool.request_with_pool(
            POOL, _send, classify)
    except plan_key_pool.PoolUnavailable:
        return _common.upstream_error_response(
            503, "", "kimi", "kimi_pool_unavailable",
            message="every kimi account in the pool is cooling down; reasons "
                    "are in /health account_pool.accounts[].reason")
    except plan_key_pool.PoolExhausted as exc:
        return _chat_pool_error(exc)

    return await _common.stream_response(resp, stream=stream,
                                         upstream_name="kimi",
                                         error_type="kimi_upstream_error",
                                         error_chars=500)


# ---------------- 账号池管理 ----------------

def _keys_from_payload(payload) -> list:
    """[(key, source)] from an admin add body, JSON or bare text.

    curl -d '{"key":"sk-..."}' is the documented shape, but so is
    curl -d 'sk-a,sk-b' -- a second key is usually pasted straight from a
    console, and rejecting that because of a missing brace is how a pool
    stays at one account.
    """
    if isinstance(payload, dict):
        keys = []
        for value in [payload.get("key")] + list(payload.get("keys") or []):
            keys += [part for part in str(value or "").replace(",", " ").split() if part]
        return [(key, str(payload.get("source") or "admin")) for key in keys]
    if isinstance(payload, str):
        return [(key, "admin") for key in payload.replace(",", " ").split() if key]
    return []


@app.post("/admin/pool/add")
async def admin_add(request: Request):
    check_bridge_auth(request)
    try:
        payload = await request.json()
    except Exception:
        payload = (await request.body()).decode("utf-8", "replace")
    added, skipped = [], []
    for key, source in _keys_from_payload(payload):
        if any(account["key_tail"] == key[-4:] and account["ref"]
               == plan_key_pool.account_ref(key) for account in POOL.status()):
            skipped.append(key[-4:])
            continue
        try:
            added.append(POOL.add(key, source=source))
        except (OSError, ValueError, RuntimeError) as exc:
            skipped.append("%s (%s)" % (key[-4:], exc))
    return {"status": "ok", "added": [item["ref"] for item in added],
            "skipped": skipped, "account_pool": POOL.summary()}


@app.post("/admin/pool/{ref}/primary")
async def admin_set_primary(ref: str, request: Request):
    check_bridge_auth(request)
    try:
        POOL.set_primary(ref)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"account not found: {ref}")
    return {"status": "ok", "account_pool": POOL.summary()}


@app.delete("/admin/pool/{ref}")
async def admin_remove(ref: str, request: Request):
    check_bridge_auth(request)
    try:
        POOL.remove(ref)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"account not found: {ref}")
    return {"status": "ok", "account_pool": POOL.summary()}


@app.post("/admin/pool/reload")
async def admin_reload(request: Request):
    check_bridge_auth(request)
    POOL.reload()
    return {"status": "ok", "account_pool": POOL.summary()}


@app.post("/admin/pool/points")
async def admin_points(request: Request):
    check_bridge_auth(request)
    summary = await POOL.refresh_points(force=True)
    return {"status": "ok", "account_pool": summary}


def main():
    import asyncio
    accounts = POOL.status()
    _common.serve(
        app, 8802,
        f"[kimi2codex] v{BRIDGE_VERSION} on http://%s:%s  upstream={UPSTREAM_BASE} "
        f"proxy={UPSTREAM_PROXY or 'direct'} pool={len(accounts)} "
        f"key={'set' if accounts else 'MISSING'}")


if __name__ == "__main__":
    main()
