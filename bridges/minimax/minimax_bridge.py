#!/usr/bin/env python3
"""minimax2codex — 把 MiniMax 语言模型暴露成标准 OpenAI 兼容 API。

背景（2026-10-03 核实）：
  * 上游 https://api.minimaxi.com/v1 是 OpenAI 兼容协议：/v1/chat/completions
    实测认 Bearer 鉴权，无 key 时返回 authorized_error(1004)。
  * MiniMax 同时有一套 Anthropic 兼容端点（https://api.minimax.cn/anthropic，
    官方 quickstart 给的示例走这条，鉴权头是 X-Api-Key）。本桥面向机队的
    OpenAI 兼容面，所以走 /v1；要 Anthropic 面的自己挂 ANTHROPIC_BASE_URL。
  * 本机: 127.0.0.1:8803（OpenAI 兼容：/v1/models、/v1/chat/completions）。

模型目录取自官方文档（platform.minimaxi.com/docs/guides/models-intro.md），
上游 /v1/models 在没 key 时打不开，所以静态目录是唯一的兜底：

    MiniMax-M3.1-Flash-Preview   1M 上下文，仅 M Plan / MiniMax Code 提供
    MiniMax-M3                  1M 上下文 Frontier Coding
    MiniMax-M2.7 / -highspeed
    MiniMax-M2.5 / -highspeed
    MiniMax-M2.1 / -highspeed
    MiniMax-M2

注意最后一项的历史性：官方把 M2.5 及更早收进了"历史模型"折叠区，但按量计费
仍然通，所以这里照列，让老 key 也能用。

关于 key：MiniMax 的 key 分两种，官方明示不能混用——按量计费 API Key 走标准
/v1 接口，订阅 Key 走 Token Plan（额度接口是
https://www.minimax.cn/v1/token_plan/remains，见 plan_credits.py）。本桥要的是
按量计费 API Key，所以额度读取对按量 key 来说是"查不到"而不是"key 坏了"。
海外账号的 key 在 CN 主机上会被拒，可用 MINIMAX_UPSTREAM 指到
https://api.minimax.io/v1。

账号池（bridges/plan_key_pool.py）：一把 key 一个账号，落在本桥 auths/ 目录
下，请求按池内候选逐个尝试，key 被拒/限流自动切下一把并给失败 key 记冷却。
第一把由 fleet.env 的 MINIMAX_API_KEY / MINIMAX_API_KEYS 播种（绝不回落
BRIDGE_KEY：那是本机回环 token，发给第三方就是泄露），更多 key 运行时可加：

    curl -X POST http://127.0.0.1:8803/admin/pool/add \\
      -H "Authorization: Bearer <MINIMAX2CODEX_KEY>" -d '{"key":"sk-..."}'

依赖：fastapi + uvicorn + httpx。用法：python3 minimax_bridge.py [--port 8803]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import _common
import plan_key_pool

BRIDGE_VERSION = "1.2.0"

UPSTREAM_BASE = (os.environ.get("MINIMAX_UPSTREAM")
                 or "https://api.minimaxi.com/v1").rstrip("/")
BRIDGE_KEY = os.environ.get("MINIMAX2CODEX_KEY") or ""
# Never fall back to BRIDGE_KEY as an upstream key: it is a local loopback
# token and sending it to a third party leaks it. An unset upstream key must
# stay visibly unset, which is why the seed below reads the MiniMax env vars
# and nothing else.
UPSTREAM_PROXY = (os.environ.get("MINIMAX_UPSTREAM_PROXY") or "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("MINIMAX_CALL_TIMEOUT") or "300")
CATALOG_PREFIX = "minimax/"
POINTS_TTL = float(os.environ.get("MINIMAX_POINTS_TTL") or "300")
BASE_DIR = Path(__file__).resolve().parent

# Documented chat models, strongest first. The -highspeed rows are the same
# quality as their sibling at lower latency, which is why they sit directly
# under it rather than at the end.
FALLBACK_MODELS = [
    "MiniMax-M3.1-Flash-Preview",
    "MiniMax-M3",
    "MiniMax-M2.7",
    "MiniMax-M2.7-highspeed",
    "MiniMax-M2.5",
    "MiniMax-M2.5-highspeed",
    "MiniMax-M2.1",
    "MiniMax-M2.1-highspeed",
    "MiniMax-M2",
]

# MiniMax's /v1 also carries speech, video, image and music models. A chat
# bridge advertising them hands the picker rows that can only 4xx.
_JUNK_SUBSTR = ("speech", "tts", "music", "video", "image", "img", "hailuo",
                "embed", "rerank", "ocr", "moderation", "realtime")

app = _common.make_app("minimax2codex", BRIDGE_VERSION)

check_bridge_auth = _common.make_auth_checker(BRIDGE_KEY)

client = _common.make_client_getter(
    **_common.client_kwargs(DEFAULT_TIMEOUT, proxy=UPSTREAM_PROXY))

remap_model = _common.make_model_remapper(CATALOG_PREFIX)


def is_chat_model(mid: str) -> bool:
    low = mid.lower()
    return not any(j in low for j in _JUNK_SUBSTR)


def auth_pool_dir() -> Path:
    return Path(os.environ.get("MINIMAX_AUTH_POOL_DIR") or BASE_DIR / "auths")


def seed_keys():
    """[(key, where)] for a first run: fleet.env and nothing else.

    MiniMax issues no desktop-app key file to read, so the environment is the
    whole story: MINIMAX_API_KEYS seeds a whole pool from one line and
    MINIMAX_API_KEY stays the single-key spelling the docs already use.
    """
    return plan_key_pool.keys_from_env("MINIMAX_API_KEY", "MINIMAX_API_KEYS")


plan_credits_module = plan_key_pool.plan_credits_module


def minimax_points(key: str) -> dict:
    """Token Plan quota for one key, normalised for the pool and the panel.

    The remains endpoint wants a *subscription* key, so a pay-as-you-go key
    (the kind this bridge asks for) answers 401 here. That is the expected
    answer, not a broken key: the row says so instead of showing a zero.
    """
    plan_credits = plan_credits_module()
    status, out = plan_credits.minimax_remains(key)
    endpoint = out.get("endpoint")
    if out.get("answered"):
        body = out.get("body") if isinstance(out.get("body"), dict) else {}
        numbers = plan_key_pool.walk_numbers(body)
        points, unit = plan_key_pool.headline_number(numbers)
        detail = ("；".join("%s=%s" % (path, value) for path, value in numbers[:4])
                  or "Token Plan 接口未返回数字")
        return {"points": points, "unit": unit, "plan": "Token Plan",
                "detail": detail, "error": ""}
    return {"points": None, "unit": "", "plan": "",
            "detail": "GET %s -> HTTP %s" % (endpoint, status),
            "error": ("" if status == 200 else
                      "HTTP %s：订阅 Key 才查得到 Token Plan，按量计费 key 读不了" % status)}


POOL = plan_key_pool.KeyPool(
    "minimax", auth_pool_dir(),
    seed_keys,
    read_points=minimax_points, points_ttl=POINTS_TTL)


def classify(status: int, body: str):
    """(reason, cooldown, tag) when one key should stop serving; None to pass through.

    MiniMax has no "plan lapsed but key fine" shape of its own: a 401 is the
    key (or the wrong region's key, which is why the final error names the
    international endpoint), and a 403 is the request. Anything else comes
    back untouched as the provider's answer to the call.
    """
    text = (body or "").lower()
    if status == 401:
        return "上游拒绝该 key（401）", 3600, "key_dead"
    if status == 403:
        return "上游拒绝该请求（403）", 600, "forbidden"
    if status == 429 or "rate limit" in text or "too many requests" in text:
        return "触发限流", 60, "rate_limit"
    if status >= 500:
        return "上游 HTTP %d" % status, 30, "upstream_5xx"
    return None


def _add_key_hint() -> str:
    return ("add a real MiniMax key at runtime: POST "
            "http://127.0.0.1:8803/admin/pool/add with {\"key\": \"sk-...\"}")


def _region_hint() -> str:
    return ("if the key is from an international account, point "
            "MINIMAX_UPSTREAM at https://api.minimax.io/v1")


def _models_pool_error(exc: plan_key_pool.PoolExhausted):
    """The 401/403 every pooled key answered, with both fixes named.

    A key minted for the international console is refused by the CN host in
    exactly the same shape as a dead key, and the two have different fixes,
    so the message names the region switch next to the key advice.
    """
    tags = [failure[4] for failure in exc.failures]
    if "key_dead" not in tags:
        return _common.upstream_error_response(exc.status, exc.body, "minimax",
                                               "minimax_upstream_error")
    return JSONResponse(
        {"error": {"message": "minimax upstream refused every pooled API key "
                              f"(HTTP {exc.status}); set MINIMAX_API_KEY to a "
                              f"real MiniMax key ({_region_hint()}), or {_add_key_hint()}",
                   "type": "upstream_auth_error"}},
        status_code=401)


def _chat_pool_error(exc: plan_key_pool.PoolExhausted):
    tags = [failure[4] for failure in exc.failures]
    if "key_dead" in tags:
        return _models_pool_error(exc)
    return _common.upstream_error_response(exc.status, exc.body, "minimax",
                                           "minimax_upstream_error")


def _transport_detail(exc: plan_key_pool.PoolExhausted) -> str:
    """Why every candidate failed on the transport side, for _fallback.

    The failure body carries the exception text but not its type, and
    "ConnectError" is the part that says which layer broke; the pool already
    recorded the type in each account's reason, so that is what is read.
    """
    reasons = {item["ref"]: item.get("reason") or "" for item in POOL.status()}
    return "；".join(reasons.get(failure[0]) or failure[2] or "transport error"
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


def _upstream_headers(key: str) -> dict:
    return {"Content-Type": "application/json", "Accept": "application/json",
            "Authorization": f"Bearer {key}"}


@app.get("/v1/models")
async def list_models(request: Request):
    check_bridge_auth(request)
    ids = list(dict.fromkeys(FALLBACK_MODELS))
    detail = ""
    if POOL.status():
        try:
            _candidate, r = await plan_key_pool.request_with_pool(
                POOL, lambda key: client().get(f"{UPSTREAM_BASE}/models",
                                               headers=_upstream_headers(key)),
                classify)
        except plan_key_pool.PoolUnavailable:
            # Every key is cooling: the static catalog is still better than
            # an error, because most of these rows are documented ones.
            detail = "account pool cooling"
        except plan_key_pool.PoolExhausted as exc:
            if all(failure[4] == "transport" for failure in exc.failures):
                # An unreachable upstream is not the accounts' fault; the
                # documented catalog still beats an error page.
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
             "owned_by": "minimax"}
            for m in ids
        ],
    }
    if detail:
        data["_fallback"] = detail
    return JSONResponse(data)


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
            503, "", "minimax", "minimax_key_missing",
            message="minimax bridge has no MINIMAX_API_KEY; set a MiniMax "
                    "pay-as-you-go key in fleet.env and re-run "
                    "bash bridges/finish.sh minimax; " + _add_key_hint())

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
            503, "", "minimax", "minimax_pool_unavailable",
            message="every minimax account in the pool is cooling down; reasons "
                    "are in /health account_pool.accounts[].reason")
    except plan_key_pool.PoolExhausted as exc:
        return _chat_pool_error(exc)

    return await _common.stream_response(resp, stream=stream,
                                         upstream_name="minimax",
                                         error_type="minimax_upstream_error",
                                         error_chars=500)


# ---------------- 账号池管理 ----------------

_keys_from_payload = plan_key_pool.keys_from_payload


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
    accounts = POOL.status()
    _common.serve(
        app, 8803,
        f"[minimax2codex] v{BRIDGE_VERSION} on http://%s:%s  upstream={UPSTREAM_BASE} "
        f"proxy={UPSTREAM_PROXY or 'direct'} pool={len(accounts)} "
        f"key={'set' if accounts else 'MISSING'}")


if __name__ == "__main__":
    main()
