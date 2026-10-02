#!/usr/bin/env python3
"""qwen2codex — 把 Qwen Cloud（千问海外托管 API）模型暴露成标准 OpenAI 兼容 API。

背景（2026-09-26 核实）：
  * 「Qwen4」尚未作为正式产品发布。官方所称的「Qwen4 架构预览」是开放权重模型
    Qwen3.8-Flash-Next（huggingface.co/Qwen/Qwen3.8-Flash-Next，
    内部架构名 Qwen4ExpForConditionalGeneration，HF 未设门槛、无需邀请码，
    NVFP4 权重约 124GB，自托管门槛高）。
  * 无自托管条件时，官方托管版即 qwen3.8-flash（Qwen Cloud / Model Studio 海外站
    www.qwencloud.com）：同架构的生产版，原生 1M 上下文（最大输入 991K / 输出 131K），
    兼容 OpenAI 与 Anthropic 两套协议，官方明示支持 Codex。新用户有免费额度
    （70M+ tokens），无需邀请码，注册即用。
  * 上游: https://maas.qwencloudapi.com/compatible-mode/v1 （OpenAI 兼容，Bearer API key）。
  * 本机: 127.0.0.1:8798（OpenAI 兼容：/v1/models、/v1/chat/completions）。
  * 上游是国际站点，默认直连；网络受限的机器可设 QWEN_UPSTREAM_PROXY=<url> 走代理。

依赖：fastapi + uvicorn + httpx。用法：python3 qwen_bridge.py [--port 8798]
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import _common

BRIDGE_VERSION = "1.0.0"

UPSTREAM_BASE = (os.environ.get("QWEN_UPSTREAM") or "https://maas.qwencloudapi.com/compatible-mode/v1").rstrip("/")
BRIDGE_KEY = os.environ.get("QWEN2CODEX_KEY") or ""
# Never fall back to BRIDGE_KEY here. Measured 2026-09-29: fleet.env carries
# QWEN2CODEX_KEY but no QWEN_API_KEY, so the bridge shipped its own local key
# to maas.qwencloudapi.com on every request. That leaks a local secret to a
# third party, /health answered has_api_key=true, and upstream answered 401 --
# an operator reading the health row then hunts an expired session that does
# not exist. An unset key must stay visibly unset.
API_KEY = os.environ.get("QWEN_API_KEY", "")
UPSTREAM_PROXY = (os.environ.get("QWEN_UPSTREAM_PROXY") or "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("QWEN_CALL_TIMEOUT") or "300")
CATALOG_PREFIX = "qwen/"

# Qwen3.8-Flash = Qwen4 架构预览（Flash-Next）的生产版；无 key / 上游不可用时的静态兜底目录
FALLBACK_MODELS = ["qwen3.8-flash", "qwen3.8-max"]

# 上游 models 端点同站还挂着图片/视频/语音等 marketplace 模型，对话桥只保留文本类：
# 命中下列子串的一律不暴露（与 catalog_filter 的 junk 规则同一意图）。
_JUNK_SUBSTR = (
    "image", "video", "tts", "asr", "audio", "embedding", "embed", "rerank",
    "ocr", "moderation", "flux", "wan", "whisper", "speech", "music",
)

app = _common.make_app("qwen2codex", BRIDGE_VERSION)

# 本地桥访问控制：key 与上游 Qwen Cloud API key 同一 env（QWEN2CODEX_KEY）。
check_bridge_auth = _common.make_auth_checker(BRIDGE_KEY)

client = _common.make_client_getter(
    **_common.client_kwargs(DEFAULT_TIMEOUT, proxy=UPSTREAM_PROXY))

# 把 Codex 侧带 qwen/ 前缀的模型名还原成上游原生模型名。
remap_model = _common.make_model_remapper(
    CATALOG_PREFIX, double_prefix="qwen-qwen", double_strip="qwen-")


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
            r = await client().get(f"{UPSTREAM_BASE}/models", headers=upstream_headers())
            if r.status_code == 200:
                up = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
                kept = [m for m in up if is_chat_model(m)]
                if kept:
                    ids = kept
            else:
                detail = f"upstream {r.status_code}"
                if r.status_code in (401, 403):
                    # The key was refused. Advertising rows here means every
                    # chat call 401s while /health and the picker stay green,
                    # so report the failure instead of the static catalog.
                    # The names in FALLBACK_MODELS are real, which is why a
                    # network/5xx failure below still serves them.
                    return JSONResponse(
                        {"error": {"message": f"qwen upstream refused the API key (HTTP {r.status_code}); "
                                              f"set QWEN_API_KEY to a real Qwen Cloud key",
                                   "type": "upstream_auth_error"}},
                        status_code=401)
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
    # 无 key / 上游不可达：静态兜底目录（带 qwen/ 前缀，slug 与 catalog 注入一致）
    data = {
        "object": "list",
        "data": [
            {"id": f"{CATALOG_PREFIX}{m}", "object": "model", "created": 0, "owned_by": "qwen-cloud"}
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
        # No key means every upstream call is a guaranteed 401; fail locally
        # with the fix instead of paying for the round trip.
        return _common.upstream_error_response(
            503, "", "qwen", "qwen_key_missing",
            message="qwen bridge has no QWEN_API_KEY; set a real Qwen Cloud key "
                    "in fleet.env and re-run bash bridges/finish.sh qwen")

    url = f"{UPSTREAM_BASE}/chat/completions"
    try:
        req = client().build_request("POST", url, json=body, headers=upstream_headers())
        resp = await client().send(req, stream=True)
    except Exception as e:
        return _common.upstream_error_response(
            502, "", "qwen", "qwen_upstream_error",
            message=f"qwen upstream unreachable: {type(e).__name__}: {e}")

    return await _common.stream_response(resp, stream=stream,
                                         upstream_name="qwen",
                                         error_type="qwen_upstream_error",
                                         error_chars=500)


def main():
    _common.serve(
        app, 8798,
        f"[qwen2codex] v{BRIDGE_VERSION} on http://%s:%s  upstream={UPSTREAM_BASE} "
        f"proxy={UPSTREAM_PROXY or 'direct'} api_key={'set' if API_KEY else 'MISSING'}")


if __name__ == "__main__":
    main()
