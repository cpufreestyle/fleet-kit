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

import argparse
import asyncio
import json
import os
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

BRIDGE_VERSION = "1.0.0"

UPSTREAM_BASE = (os.environ.get("QWEN_UPSTREAM") or "https://maas.qwencloudapi.com/compatible-mode/v1").rstrip("/")
BRIDGE_KEY = os.environ.get("QWEN2CODEX_KEY") or ""
API_KEY = os.environ.get("QWEN_API_KEY") or BRIDGE_KEY
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

app = FastAPI(title="qwen2codex", version=BRIDGE_VERSION)

_http: Optional[httpx.AsyncClient] = None


def _client_kwargs() -> dict:
    kw = dict(timeout=httpx.Timeout(DEFAULT_TIMEOUT, connect=15))
    if UPSTREAM_PROXY:
        kw["proxy"] = UPSTREAM_PROXY
    return kw


def client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(**_client_kwargs())
    return _http


def check_bridge_auth(request: Request) -> None:
    """本地桥访问控制：key 与上游 Qwen Cloud API key 同一 env（QWEN2CODEX_KEY）。"""
    if not BRIDGE_KEY:
        return
    auth = request.headers.get("authorization") or ""
    if auth != f"Bearer {BRIDGE_KEY}":
        raise HTTPException(status_code=401, detail="invalid bridge key")


def remap_model(model: Optional[str]) -> Optional[str]:
    """把 Codex 侧带 qwen/ 前缀的模型名还原成上游原生模型名。"""
    if not model:
        return model
    if model.startswith(CATALOG_PREFIX):
        return model[len(CATALOG_PREFIX):]
    if model.startswith("qwen-qwen"):
        return model[len("qwen-"):]
    return model


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
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
    # 无 key / 上游失败：静态兜底目录（带 qwen/ 前缀，slug 与 catalog 注入一致）
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

    url = f"{UPSTREAM_BASE}/chat/completions"
    try:
        req = client().build_request("POST", url, json=body, headers=upstream_headers())
        resp = await client().send(req, stream=True)
    except Exception as e:
        return Response(
            content=json.dumps({"error": {"message": f"qwen upstream unreachable: {type(e).__name__}: {e}",
                                          "type": "qwen_upstream_error"}}),
            media_type="application/json", status_code=502,
        )

    if resp.status_code != 200:
        err = (await resp.aread()).decode("utf-8", "replace")[:500]
        await resp.aclose()
        return Response(content=json.dumps({"error": {"message": f"qwen upstream {resp.status_code}: {err}",
                                                     "type": "qwen_upstream_error"}}),
                        media_type="application/json", status_code=resp.status_code)

    if stream:
        ctype = resp.headers.get("content-type", "text/event-stream")
        return StreamingResponse(_sse_pump(resp), media_type=ctype)
    content = await resp.aread()
    ctype = resp.headers.get("content-type", "application/json")
    await resp.aclose()
    return Response(content=content, media_type=ctype)


async def _sse_pump(resp: httpx.Response):
    try:
        async for chunk in resp.aiter_raw():
            if chunk:
                yield chunk
    finally:
        await resp.aclose()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8798)
    args = ap.parse_args()
    print(f"[qwen2codex] v{BRIDGE_VERSION} on http://{args.host}:{args.port}  upstream={UPSTREAM_BASE} "
          f"proxy={UPSTREAM_PROXY or 'direct'} api_key={'set' if API_KEY else 'MISSING'}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=True)


if __name__ == "__main__":
    main()
