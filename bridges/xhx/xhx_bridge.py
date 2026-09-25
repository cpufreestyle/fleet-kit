#!/usr/bin/env python3
"""xhx2codex — 把商汤小浣熊（Raccoon / SenseTime）官方 SaaS 订阅模型暴露成标准 OpenAI 兼容 API。

学习来源（逆向自官方桌面端）：
  * /Applications/商汤小浣熊.app（Electron，app.asar 内 build/electron/main/*.js + desktop-renderer chunk）
    —— 逆向出的调用契约：
      - 登录态：~/.box-agent/config/auth.json（与桌面端共享，桌面端会随时重写/清除），
        字段 {access_token, refresh_token, office_identity, ...}
      - 鉴权：Authorization: Bearer <access_token>
      - 刷新：POST https://xiaohuanxiong.com/api/web/auth/v1/refresh
              body {"refresh_token": "<rt>"} -> {code:0, data:{access_token, refresh_token}}
              ** refresh_token 是单次轮换：每次刷新都会换发新 RT，旧 RT 立即失效（code 200822
                refresh_conflict）；刷新成功必须立即把新 token 写回 auth.json **
      - 模型目录：GET {api}/model_catalog -> data.categories[].models[]（含 context_window/max_tokens）
      - 聊天：POST {api}/chat/completions（OpenAI 兼容，SSE 流式同标准 chunk 格式）
      - api = https://xiaohuanxiong.com/api/web/llm/v2

链路：Codex → 本桥(:8793) → https://xiaohuanxiong.com/api/web/llm/v2
登录态异常时：打开「商汤小浣熊」桌面 app 登录，它会自动把新 token 同步进 ~/.box-agent/config/auth.json。

依赖：fastapi + uvicorn + httpx。用法：python3 xhx_bridge.py [--port 8793]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

BRIDGE_VERSION = "0.1.0"

WEB_BASE = os.environ.get("XHX_WEB_BASE_URL") or "https://xiaohuanxiong.com"
API_BASE = f"{WEB_BASE}/api/web/llm/v2"
REFRESH_URL = f"{WEB_BASE}/api/web/auth/v1/refresh"
MODELS_PATH = "/model_catalog"
CHAT_PATH = "/chat/completions"
CATALOG_PREFIX = "xhx/"
FALLBACK_MODELS = ["raccoon-8c4485", "sn-glm-5-3-flash", "sn-deepseek-v4-1-flash",
                   "sn-kimi-k3", "sn-glm-5-3", "sn-sensenova-6-8-flash",
                   "sn-sensenova-6-8-flash-lite", "raccoon-19b265", "raccoon-405a1c"]

BRIDGE_KEY = os.environ.get("XHX2CODEX_KEY") or ""
CALL_TIMEOUT = float(os.environ.get("XHX_CALL_TIMEOUT") or "600")


def auth_file() -> Path:
    root = os.environ.get("BOX_AGENT_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".box-agent", "config")
    return Path(root) / "auth.json"


app = FastAPI(title="xhx2codex", version=BRIDGE_VERSION)
_http: Optional[httpx.AsyncClient] = None
_lock = asyncio.Lock()
_cache: dict = {"models": {}, "ts": 0.0}   # model_id -> {name, context_window, max_tokens, ...}


def client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(CALL_TIMEOUT, connect=15),
                                  headers={"User-Agent": f"xhx2codex/{BRIDGE_VERSION}"})
    return _http


def check_bridge_auth(request: Request) -> None:
    if not BRIDGE_KEY:
        return
    if (request.headers.get("authorization") or "") != f"Bearer {BRIDGE_KEY}":
        raise HTTPException(status_code=401, detail="invalid bridge key")


# ---------------- 登录态（~/.box-agent/config/auth.json，与桌面端共享） ----------------

def load_auth() -> Optional[dict]:
    try:
        auth = json.loads(auth_file().read_text(encoding="utf-8"))
        return auth if auth.get("access_token") else None
    except Exception:
        return None


def save_auth(auth: dict) -> None:
    """原地更新 access_token/refresh_token，保留其余字段；原子写 + 0600（桌面端会校验权限）。"""
    path = auth_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".auth.json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(auth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


async def resolve_token() -> str:
    auth = load_auth()
    if auth is None:
        raise HTTPException(status_code=401,
                            detail="小浣熊未登录：打开「商汤小浣熊」桌面 app 登录（token 会自动同步到 ~/.box-agent/config/auth.json）")
    return auth["access_token"]


async def refresh_token(auth: dict) -> Optional[dict]:
    """单次轮换刷新：成功后立即写回 auth.json。返回 None 表示刷新失败（含冲突）。"""
    rt = auth.get("refresh_token") or ""
    if not rt:
        return None
    try:
        r = await client().post(REFRESH_URL, json={"refresh_token": rt},
                                headers={"Content-Type": "application/json"})
        if r.status_code != 200:
            return None
        data = (r.json() or {}).get("data") or {}
        token = data.get("access_token")
        if not token:
            return None
        merged = {**auth, "access_token": token,
                  "refresh_token": data.get("refresh_token") or rt}
        save_auth(merged)
        return merged
    except Exception as e:
        print(f"[xhx2codex] refresh failed: {e}", flush=True)
        return None


async def authed(method: str, path: str, **kw) -> httpx.Response:
    """带刷新重试的请求；refresh_conflict 时重新读盘（桌面端可能已重同步）。"""
    auth = load_auth()
    if auth is None:
        raise HTTPException(status_code=401, detail="小浣熊未登录：打开「商汤小浣熊」桌面 app 登录")
    r = await client().request(method, f"{API_BASE}{path}",
                               headers={"Authorization": f"Bearer {auth['access_token']}"}, **kw)
    if r.status_code in (401, 403):
        await r.aclose()
        fresh = await refresh_token(auth)
        if fresh is None and load_auth() and load_auth() != auth:
            fresh = load_auth()  # 桌面端换了新 token，直接用盘上的
        if fresh is None:
            raise HTTPException(status_code=401,
                                detail="小浣熊会话失效：打开「商汤小浣熊」桌面 app 重新登录")
        r = await client().request(method, f"{API_BASE}{path}",
                                   headers={"Authorization": f"Bearer {fresh['access_token']}"}, **kw)
    return r


# ---------------- 模型目录 ----------------

async def get_catalog(force: bool = False) -> dict:
    async with _lock:
        if not force and _cache["models"] and time.time() - _cache["ts"] < 300:
            return _cache["models"]
    try:
        r = await authed("GET", MODELS_PATH)
        if r.status_code == 200:
            data = (r.json() or {}).get("data") or {}
            by_id: dict = {}
            for cat in data.get("categories") or []:
                for m in cat.get("models") or []:
                    mid = m.get("name") or ""
                    if not mid or mid in by_id:
                        continue
                    p = m.get("params") or {}
                    by_id[mid] = {"id": mid,
                                  "name": m.get("description") or mid,
                                  "context_window": p.get("context_window") or 0,
                                  "max_tokens": p.get("max_tokens") or 0,
                                  "visible": bool(m.get("visible"))}
            if by_id:
                async with _lock:
                    _cache["models"] = by_id
                    _cache["ts"] = time.time()
    except Exception as e:
        print(f"[xhx2codex] catalog refresh failed: {e}", flush=True)
    async with _lock:
        return _cache["models"]


def remap_model(model: Optional[str]) -> Optional[str]:
    # 循环剥离：兼容 ocx 发现的双前缀 slug（如 xhx/xhx-sn-glm-5-3-flash）
    while model and model.startswith(CATALOG_PREFIX):
        model = model[len(CATALOG_PREFIX):]
    return model


# ---------------- 路由 ----------------

@app.get("/health")
async def health():
    info = {"ok": True, "version": BRIDGE_VERSION, "api_base": API_BASE}
    auth = load_auth()
    info["logged_in"] = bool(auth)
    try:
        r = await authed("GET", MODELS_PATH)
        info["session_alive"] = r.status_code == 200
        if r.status_code != 200:
            info["detail"] = f"model_catalog http {r.status_code}"
        else:
            await r.aclose()
    except HTTPException as e:
        info["session_alive"] = False
        info["detail"] = e.detail
    except Exception as e:
        info["session_alive"] = False
        info["detail"] = str(e)[:200]
    catalog = await get_catalog()
    info["models"] = sorted(catalog.keys()) or FALLBACK_MODELS
    return info


@app.get("/v1/models")
async def list_models(request: Request):
    check_bridge_auth(request)
    catalog = await get_catalog()
    models = sorted(catalog.keys()) or FALLBACK_MODELS
    return {"object": "list", "data": [
        {"id": f"{CATALOG_PREFIX}{m}", "object": "model", "created": 0, "owned_by": "xiaohuanxiong",
         "context_window": (catalog.get(m) or {}).get("context_window") or 0,
         "name": (catalog.get(m) or {}).get("name") or m}
        for m in models]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    check_bridge_auth(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
    model = remap_model(payload.get("model"))
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    payload = {**payload, "model": model}
    stream = bool(payload.get("stream"))

    r = await authed("POST", CHAT_PATH, json=payload)
    if r.status_code != 200:
        text = r.text[:400]
        await r.aclose()
        return JSONResponse({"error": {"message": f"xiaohuanxiong upstream {r.status_code}: {text}",
                                       "type": "xhx_upstream_error"}},
                            status_code=r.status_code)

    if stream:
        return StreamingResponse(_pump(r), media_type="text/event-stream")

    await r.aread()
    body = r.content
    await r.aclose()
    return Response(content=body, media_type="application/json")


async def _pump(resp: httpx.Response):
    try:
        async for chunk in resp.aiter_bytes():
            yield chunk
    finally:
        await resp.aclose()


def main():
    global BRIDGE_KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8793)
    args = ap.parse_args()
    print(f"[xhx2codex] v{BRIDGE_VERSION} on http://{args.host}:{args.port} "
          f"api={API_BASE} key={'set' if BRIDGE_KEY else 'OPEN'}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=True)


if __name__ == "__main__":
    main()
