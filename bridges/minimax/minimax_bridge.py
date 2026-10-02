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
按量计费 API Key。海外账号的 key 在 CN 主机上会被拒，可用 MINIMAX_UPSTREAM
指到 https://api.minimax.io/v1。

依赖：fastapi + uvicorn + httpx。用法：python3 minimax_bridge.py [--port 8803]
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import _common

BRIDGE_VERSION = "1.0.0"

UPSTREAM_BASE = (os.environ.get("MINIMAX_UPSTREAM")
                 or "https://api.minimaxi.com/v1").rstrip("/")
BRIDGE_KEY = os.environ.get("MINIMAX2CODEX_KEY") or ""
# Never fall back to BRIDGE_KEY here: it is a local loopback token and sending
# it to a third party leaks it. An unset upstream key must stay visibly unset.
API_KEY = os.environ.get("MINIMAX_API_KEY", "")
UPSTREAM_PROXY = (os.environ.get("MINIMAX_UPSTREAM_PROXY") or "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("MINIMAX_CALL_TIMEOUT") or "300")
CATALOG_PREFIX = "minimax/"

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


def upstream_headers() -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": BRIDGE_VERSION,
        "proxy": UPSTREAM_PROXY or "direct",
        "has_api_key": bool(API_KEY),
        "upstream": UPSTREAM_BASE,
        "models": FALLBACK_MODELS,
    }


@app.get("/v1/models")
async def list_models(request: Request):
    check_bridge_auth(request)
    ids = list(dict.fromkeys(FALLBACK_MODELS))
    detail = ""
    if API_KEY:
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
                    # A region mismatch reads exactly like a refused key, and
                    # the two have different fixes, so name both rather than
                    # sending the operator to regenerate a key that is fine.
                    hint = ("set MINIMAX_API_KEY to a real MiniMax key; if the "
                            "key is from an international account, point "
                            "MINIMAX_UPSTREAM at https://api.minimax.io/v1")
                    return JSONResponse(
                        {"error": {"message": f"minimax upstream refused the API key (HTTP {r.status_code}); {hint}",
                                   "type": "upstream_auth_error"}},
                        status_code=401)
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
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

    if not API_KEY:
        return _common.upstream_error_response(
            503, "", "minimax", "minimax_key_missing",
            message="minimax bridge has no MINIMAX_API_KEY; set a MiniMax "
                    "pay-as-you-go key in fleet.env and re-run "
                    "bash bridges/finish.sh minimax")

    url = f"{UPSTREAM_BASE}/chat/completions"
    try:
        req = client().build_request("POST", url, json=body,
                                     headers=upstream_headers())
        resp = await client().send(req, stream=True)
    except Exception as e:
        return _common.upstream_error_response(
            502, "", "minimax", "minimax_upstream_error",
            message=f"minimax upstream unreachable: {type(e).__name__}: {e}")

    return await _common.stream_response(resp, stream=stream,
                                         upstream_name="minimax",
                                         error_type="minimax_upstream_error",
                                         error_chars=500)


def main():
    _common.serve(
        app, 8803,
        f"[minimax2codex] v{BRIDGE_VERSION} on http://%s:%s  upstream={UPSTREAM_BASE} "
        f"proxy={UPSTREAM_PROXY or 'direct'} api_key={'set' if API_KEY else 'MISSING'}")


if __name__ == "__main__":
    main()

