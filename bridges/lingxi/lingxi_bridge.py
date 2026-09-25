#!/usr/bin/env python3
"""lingxi2codex — 把灵犀（LingXi）官方 SaaS 订阅模型暴露成标准 OpenAI 兼容 API，供 Codex 反代理使用。

学习来源（开源项目）：
  * npm: @lingxi-agent/core@0.9.6（灵犀 CLI，作者 LING YUN，lingxi-ai/lingxi）
    —— README「灵犀官方 SaaS」一节 + dist/index.cjs 逆向出的调用契约：
      - 登录态：~/.LingXi/auth.json（与桌面端共享），字段 {token, refresh, name, baseURL}
      - API 基座：{baseURL}/api/v1（默认 https://lingxi.regaing.com/api/v1，OpenAI 兼容）
      - 鉴权：Authorization: Bearer <token>
      - 刷新：POST {api}/auth/refresh {refresh_token} -> {access_token, refresh_token}
      - 探活：GET {api}/users/me
      - 模型：POST/GET {api}/models、{api}/chat/completions（envelope: {"error": "unauthorized"}）
      - 官方默认模型：deepseek-v4-flash（快）/ deepseek-v4-pro（思考）

链路：Codex → 本桥(:8792) → https://lingxi.regaing.com/api/v1
登录：浏览器打开 https://lingxi.regaing.com/login?client=cli&port=8062 完成登录后，
      回调 127.0.0.1:8062/?token=...&refresh=...&name=... 由本目录 login_helper.py 接收并写 auth.json。

依赖：fastapi + uvicorn + httpx。用法：python3 lingxi_bridge.py [--port 8792]
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

DEFAULT_API_BASE = "https://lingxi.regaing.com"
CHAT_PATH = "/chat/completions"
MODELS_PATH = "/models"
ME_PATH = "/users/me"
REFRESH_PATH = "/auth/refresh"
CATALOG_PREFIX = "lingxi/"
FALLBACK_MODELS = ["deepseek-v4-flash", "deepseek-v4-pro"]

BRIDGE_KEY = os.environ.get("LINGXI2CODEX_KEY") or ""
CALL_TIMEOUT = float(os.environ.get("LINGXI_CALL_TIMEOUT") or "300")


def lingxi_home() -> Path:
    return Path(os.environ.get("LINGXI_HOME") or (Path.home() / ".LingXi"))


def auth_file() -> Path:
    return lingxi_home() / "auth.json"


def saas_api_base() -> str:
    """解析 SaaS API 基座：环境变量 > ~/.LingXi/config.json 的 saas.apiBaseURL > 默认。"""
    env = os.environ.get("LINGXI_API_BASE_URL")
    if env:
        return env.rstrip("/")
    try:
        cfg = json.loads((lingxi_home() / "config.json").read_text(encoding="utf-8"))
        base = (cfg.get("saas") or {}).get("apiBaseURL")
        if base:
            return str(base).rstrip("/")
    except Exception:
        pass
    return DEFAULT_API_BASE


def api_base() -> str:
    return f"{saas_api_base()}/api/v1"


app = FastAPI(title="lingxi2codex", version=BRIDGE_VERSION)
_http: Optional[httpx.AsyncClient] = None
_lock = asyncio.Lock()
_cache: dict = {"models": [], "ts": 0.0, "account": ""}


def client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(CALL_TIMEOUT, connect=15),
                                  headers={"User-Agent": "lingxi2codex/%s" % BRIDGE_VERSION})
    return _http


def check_bridge_auth(request: Request) -> None:
    if not BRIDGE_KEY:
        return
    if (request.headers.get("authorization") or "") != f"Bearer {BRIDGE_KEY}":
        raise HTTPException(status_code=401, detail="invalid bridge key")


# ---------------- 登录态（灵犀官方 auth.json，不覆盖 IDE/CLI 语义，只在其失效时刷新并回写） ----------------

def load_auth() -> Optional[dict]:
    try:
        auth = json.loads(auth_file().read_text(encoding="utf-8"))
        return auth if auth.get("token") else None
    except Exception:
        return None


def save_auth(auth: dict) -> dict:
    home = lingxi_home()
    home.mkdir(parents=True, exist_ok=True)
    prev = load_auth() or {}
    merged = {**prev, **auth}
    tmp = home / ".auth.json.tmp"
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(auth_file())
    return merged


def me_headers(auth: dict) -> dict:
    return {"Authorization": f"Bearer {auth['token']}", "Accept": "application/json"}


async def fetch_me(auth: dict) -> dict:
    r = await client().get(f"{api_base()}{ME_PATH}", headers=me_headers(auth))
    if r.status_code != 200:
        return {}
    try:
        return r.json() or {}
    except Exception:
        return {}


async def refresh_auth(auth: dict) -> Optional[dict]:
    """POST {api}/auth/refresh {refresh_token} -> {access_token, refresh_token}"""
    refresh = auth.get("refresh") or ""
    if not refresh:
        return None
    r = await client().post(f"{api_base()}{REFRESH_PATH}",
                            json={"refresh_token": refresh},
                            headers={"Content-Type": "application/json"})
    if r.status_code != 200:
        return None
    data = r.json() or {}
    token = data.get("access_token")
    if not token:
        return None
    merged = save_auth({"token": token, "refresh": data.get("refresh_token") or refresh})
    return merged


async def resolve_auth() -> dict:
    auth = load_auth()
    if auth is None:
        raise HTTPException(status_code=401,
                            detail="灵犀未登录：运行 login_helper.py 打开浏览器登录（账号登录态写入 ~/.LingXi/auth.json）")
    return auth


async def authed(method: str, path: str, auth: dict, **kw) -> httpx.Response:
    """带 401 自动刷新重试的请求。"""
    r = await client().request(method, f"{api_base()}{path}", headers=me_headers(auth), **kw)
    if r.status_code == 401:
        try:
            fresh = await refresh_auth(auth)
        except Exception:
            fresh = None
        if fresh:
            auth = fresh
            r = await client().request(method, f"{api_base()}{path}", headers=me_headers(auth), **kw)
    return r


# ---------------- 模型目录 ----------------

async def get_models(force: bool = False) -> list[str]:
    async with _lock:
        if not force and _cache["models"] and time.time() - _cache["ts"] < 300:
            return _cache["models"]
    try:
        auth = await resolve_auth()
        r = await authed("GET", MODELS_PATH, auth)
        if r.status_code == 200:
            data = r.json() or {}
            items = data.get("data") if isinstance(data, dict) else data
            ids = []
            for m in items or []:
                if isinstance(m, dict) and m.get("id"):
                    ids.append(str(m["id"]))
                elif isinstance(m, str):
                    ids.append(m)
            if ids:
                async with _lock:
                    _cache["models"] = ids
                    _cache["ts"] = time.time()
    except Exception as e:
        print(f"[lingxi2codex] models refresh failed: {e}", flush=True)
    async with _lock:
        return _cache["models"]


def remap_model(model: Optional[str]) -> Optional[str]:
    # 循环剥离：兼容 ocx 发现的双前缀 slug（如 lingxi/lingxi-deepseek-flash）
    while model and model.startswith(CATALOG_PREFIX):
        model = model[len(CATALOG_PREFIX):]
    return model


# ---------------- 路由 ----------------

@app.get("/health")
async def health():
    info = {"ok": True, "version": BRIDGE_VERSION, "api_base": api_base()}
    auth = load_auth()
    info["logged_in"] = bool(auth)
    if auth:
        info["account"] = auth.get("name") or ""
        try:
            me = await fetch_me(auth)
            if not me:
                fresh = await refresh_auth(auth)
                if fresh:
                    me = await fetch_me(fresh)
            if me:
                info["account"] = me.get("name") or me.get("username") or auth.get("name") or ""
                info["plan"] = me.get("plan") or me.get("subscription") or ""
                info["session_alive"] = True
            else:
                info["session_alive"] = False
                info["detail"] = "users/me 401 且刷新失败，需重新登录"
        except Exception as e:
            info["session_alive"] = False
            info["detail"] = str(e)[:200]
    else:
        info["session_alive"] = False
        info["detail"] = "auth.json 不存在，未登录"
    info["models"] = await get_models() or FALLBACK_MODELS
    return info


@app.get("/v1/models")
async def list_models(request: Request):
    check_bridge_auth(request)
    models = await get_models() or FALLBACK_MODELS
    return {"object": "list", "data": [
        {"id": f"{CATALOG_PREFIX}{m}", "object": "model", "created": 0, "owned_by": "lingxi"}
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

    auth = await resolve_auth()
    r = await authed("POST", CHAT_PATH, auth, json=payload)
    if r.status_code == 401:
        text = r.text[:200]
        await r.aclose()
        return JSONResponse({"error": {"message": f"lingxi auth failed: {text}",
                                       "type": "lingxi_auth_error"}}, status_code=401)
    if r.status_code != 200:
        text = r.text[:400]
        await r.aclose()
        return JSONResponse({"error": {"message": f"lingxi upstream {r.status_code}: {text}",
                                       "type": "lingxi_upstream_error"}},
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
    ap.add_argument("--port", type=int, default=8792)
    args = ap.parse_args()
    print(f"[lingxi2codex] v{BRIDGE_VERSION} on http://{args.host}:{args.port} "
          f"api={api_base()} key={'set' if BRIDGE_KEY else 'OPEN'}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=True)


if __name__ == "__main__":
    main()
