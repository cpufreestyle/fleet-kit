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

一个必须分清的失败：套餐没生效时 /v1/models 照样 200（key 是好的），只有
/v1/chat/completions 返回 403 access_terminated_error。把这种情况说成"key 失效"
会让用户去找一把根本没坏的 key，所以本桥把这一类单独措辞。

依赖：fastapi + uvicorn + httpx。用法：python3 kimi_bridge.py [--port 8802]
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import _common

BRIDGE_VERSION = "1.0.0"

UPSTREAM_BASE = (os.environ.get("KIMI_UPSTREAM")
                 or "https://api.kimi.com/coding/v1").rstrip("/")
BRIDGE_KEY = os.environ.get("KIMI2CODEX_KEY") or ""
API_KEY = os.environ.get("KIMI_CODING_API_KEY", "")
UPSTREAM_PROXY = (os.environ.get("KIMI_UPSTREAM_PROXY") or "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("KIMI_CALL_TIMEOUT") or "300")
CATALOG_PREFIX = "kimi/"

# The Kimi desktop app's own key file, per platform. Read, never written: this
# is the user's file on their own machine, and reading it is what spares them
# pasting a key that is already there. An explicit env key always wins.
KIMI_APP_KEY_FILES = (
    ("darwin", "Library/Application Support/kimi-desktop/daimon-share/daimon/kimi-code-key.json"),
    ("win32", "AppData/Roaming/kimi-desktop/daimon-share/daimon/kimi-code-key.json"),
    ("linux", ".config/kimi-desktop/daimon-share/daimon/kimi-code-key.json"),
)

# Measured 2026-10-03 against the live /v1/models with a real key. These are
# the rows served when there is no key or the upstream will not list, so the
# names have to be the real ones.
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


def resolve_key():
    """(key, where) for the upstream key, or (None, why).

    The environment wins; the Kimi desktop app is the fallback. Where is
    returned so the startup banner and /health can say which one answered,
    which is the difference between "set a key" and "the key you already have
    is being used".
    """
    if API_KEY:
        return API_KEY, "KIMI_CODING_API_KEY"
    key, where = key_from_app()
    return (key, where) if key else (None, where)


KEY, KEY_WHERE = resolve_key()


def is_chat_model(mid: str) -> bool:
    low = mid.lower()
    return not any(j in low for j in _JUNK_SUBSTR)


def upstream_headers() -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if KEY:
        h["Authorization"] = f"Bearer {KEY}"
    return h


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": BRIDGE_VERSION,
        "proxy": UPSTREAM_PROXY or "direct",
        "has_api_key": bool(KEY),
        "key_source": KEY_WHERE,
        "upstream": UPSTREAM_BASE,
        "models": FALLBACK_MODELS,
    }


@app.get("/v1/models")
async def list_models(request: Request):
    check_bridge_auth(request)
    ids = list(dict.fromkeys(FALLBACK_MODELS))
    detail = ""
    if KEY:
        try:
            r = await client().get(f"{UPSTREAM_BASE}/models",
                                   headers=upstream_headers())
            if r.status_code == 200:
                up = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
                kept = [m for m in up if is_chat_model(m)]
                if kept:
                    ids = kept
            else:
                detail = f"upstream {r.status_code}"
                if r.status_code in (401, 403):
                    return JSONResponse(
                        {"error": {"message": f"kimi upstream refused the API key (HTTP {r.status_code}); "
                                              f"set KIMI_CODING_API_KEY to a real Kimi Code key",
                                   "type": "upstream_auth_error"}},
                        status_code=401)
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
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


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    check_bridge_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
    body["model"] = remap_model(body.get("model"))
    stream = bool(body.get("stream"))

    if not KEY:
        return _common.upstream_error_response(
            503, "", "kimi", "kimi_key_missing",
            message="kimi bridge has no upstream key: set KIMI_CODING_API_KEY, "
                    "or let it read the Kimi desktop app's key file "
                    "(KIMI_NO_APP_KEY=1 turns that off)")

    url = f"{UPSTREAM_BASE}/chat/completions"
    try:
        req = client().build_request("POST", url, json=body,
                                     headers=upstream_headers())
        resp = await client().send(req, stream=True)
    except Exception as e:
        return _common.upstream_error_response(
            502, "", "kimi", "kimi_upstream_error",
            message=f"kimi upstream unreachable: {type(e).__name__}: {e}")

    if resp.status_code == 403:
        # A terminated plan is not a dead key. /v1/models answers 200 with the
        # same key, so telling the user to hunt a fresh one sends them after a
        # credential that is already fine; the renewal page is the real answer.
        text = (await resp.aread()).decode("utf-8", "replace")
        await resp.aclose()
        try:
            err = (json.loads(text).get("error") or {})
        except ValueError:
            err = {}
        if err.get("type") == "access_terminated_error":
            renew = "https://www.kimi.com/code/#pricing"
            return _common.upstream_error_response(
                403, text, "kimi", "kimi_plan_inactive",
                message="kimi code has no active plan for this key (the key "
                        f"itself is accepted): {err.get('message') or text[:200]}"
                        f" -- renew at {renew}")
        return _common.upstream_error_response(403, text, "kimi",
                                               "kimi_upstream_error")

    return await _common.stream_response(resp, stream=stream,
                                         upstream_name="kimi",
                                         error_type="kimi_upstream_error",
                                         error_chars=500)


def main():
    _common.serve(
        app, 8802,
        f"[kimi2codex] v{BRIDGE_VERSION} on http://%s:%s  upstream={UPSTREAM_BASE} "
        f"proxy={UPSTREAM_PROXY or 'direct'} key={'set' if KEY else 'MISSING'}"
        f" via={KEY_WHERE}")


if __name__ == "__main__":
    main()

