#!/usr/bin/env python3
"""codely2codex — 把 Tuanjie AI（团结 AI，codely.tuanjie.cn）订阅模型暴露成标准 OpenAI 兼容 API。

原理（官方网关直连 + 设备码登录，凭据格式与官方 CLI 完全一致）：
  * 从官方 CLI（@unity-china/codely-cli，已逆向验证）复用官方链路：
      设备码登录:  POST https://codely.tuanjie.cn/auth/device/initiate  {provider:"unity", client_name:"codely-cli"}
                  GET  https://codely.tuanjie.cn/auth/device/poll?auth_request_token=...
                  POST https://codely.tuanjie.cn/auth/device/exchange   {authorization_code}
      刷新令牌:    POST https://codely.tuanjie.cn/auth/refresh          {refresh_token}
      换虚拟密钥:  GET  https://codely.tuanjie.cn/api/api-token/cli-api-key  (Bearer: access_token)
                  → {cli_api_key, user_id, rpm, tpm}
  * 凭据落盘 ~/.codely-cli/oauth_creds.json（与官方 CLI 同格式同路径， CLI 与桥可共享登录态）。
  * 模型网关: https://codely-litellm.tuanjie.cn/v1 —— LiteLLM，原生 OpenAI 兼容。
  * 桥只做协议转换：chat/completions ⇄ LiteLLM，Bearer 换绑为 cli_api_key，
    模型名剥掉 `codely/` 前缀；401 时自动 refresh + 重取 cli_api_key 后重试一次。

依赖：fastapi + uvicorn + httpx。用法：python3 codely_bridge.py [--port 8790]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

BRIDGE_VERSION = "0.2.0"

CODELY_SERVER = os.environ.get("CODELY_SERVER") or "https://codely.tuanjie.cn"
GATEWAY_BASE = os.environ.get("CODELY_GATEWAY") or "https://codely-litellm.tuanjie.cn/v1"
DEFAULT_TIMEOUT = float(os.environ.get("CODELY_CALL_TIMEOUT") or "300")
BRIDGE_KEY = os.environ.get("CODELY2CODEX_KEY") or ""
POLL_INTERVAL = float(os.environ.get("CODELY_DEVICE_POLL_INTERVAL") or "4")
DEVICE_TIMEOUT = float(os.environ.get("CODELY_DEVICE_TIMEOUT") or "900")

# 官方 CLI 未登录时 `--cmd "/model list"` 实测到的 8 个模型（`/health` 与无凭据降级用）
FALLBACK_MODELS = [
    "codely-core", "codely-flash", "codely-air", "codely-basic", "codely-vl",
    "DeepSeek-V4.1-Flash", "GLM-5.3-FLASH", "KIMI-K3",
]
CATALOG_PREFIX = "codely/"

# 官方 CLI 逆向出的 LiteLLM 网关签名参数（HMAC-SHA256 双层派生）：
#   inner = HMAC-SHA256(BASE_KEY, "codely-signing-v1")
#   key   = HMAC-SHA256(inner, cli_api_key)
#   sig   = base64url(HMAC-SHA256(key, "v1\n<path>\n<unix_ts>"))
#   header: X-Codely-Signature: v1.<ts>.<sig>
_SIGN_BASE_KEY = bytes.fromhex("406f00f74768ba0cb0cd30f097ec6c2bdacb89c61a38b7dd140838bbd0e98018")


def sign_gateway_headers(cli_key: str, path: str) -> dict:
    ts = str(int(time.time()))
    inner = hmac.new(_SIGN_BASE_KEY, b"codely-signing-v1", hashlib.sha256).digest()
    key = hmac.new(inner, cli_key.encode(), hashlib.sha256).digest()
    payload = f"v1\n{path}\n{ts}".encode()
    digest = base64.urlsafe_b64encode(hmac.new(key, payload, hashlib.sha256).digest()).rstrip(b"=").decode()
    return {"X-Codely-Signature": f"v1.{ts}.{digest}"}

app = FastAPI(title="codely2codex", version=BRIDGE_VERSION)

_http: Optional[httpx.AsyncClient] = None
_creds_lock = asyncio.Lock()
_device: dict = {}  # auth_request_token -> {"started": ts, "status": str}


def client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(DEFAULT_TIMEOUT, connect=15))
    return _http


def cli_home() -> Path:
    env = (os.environ.get("CODELY_CLI_HOME") or "").strip()
    if env:
        return Path(env)
    return Path.home() / ".codely-cli"


def creds_path() -> Path:
    return cli_home() / "oauth_creds.json"


def load_creds() -> dict:
    try:
        return json.loads(creds_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_creds(creds: dict) -> None:
    p = creds_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    old = {}
    try:
        old = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    old.update(creds or {})
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def check_bridge_auth(request: Request) -> None:
    if not BRIDGE_KEY:
        return
    auth = request.headers.get("authorization") or ""
    if auth != f"Bearer {BRIDGE_KEY}":
        raise HTTPException(status_code=401, detail="invalid bridge key")


def remap_model(model: Optional[str]) -> Optional[str]:
    """把 Codex 侧带 `codely/` 前缀的模型名还原成 Tuanjie 网关原生模型名。"""
    if not model:
        return model
    if model.startswith(CATALOG_PREFIX):
        return model[len(CATALOG_PREFIX):]
    if model.startswith("codely-codely"):
        return model[len("codely-"):]
    return model


# ---------------- 官方链路：设备码登录 / 刷新 / 虚拟密钥 ----------------

async def device_start(provider: str = "unity", client_name: str = "codely-cli") -> dict:
    r = await client().post(
        f"{CODELY_SERVER}/auth/device/initiate",
        json={"provider": provider, "client_name": client_name},
    )
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"device initiate failed: {r.status_code} {r.text[:200]}")
    data = r.json()
    token = data.get("auth_request_token")
    if not token or not data.get("verification_uri_complete"):
        raise HTTPException(status_code=502, detail=f"device initiate bad payload: {data}")
    _device[token] = {"started": time.time(), "status": "pending"}
    return {
        "verification_uri_complete": data.get("verification_uri_complete"),
        "verification_uri": data.get("verification_uri"),
        "user_code": data.get("user_code"),
        "provider": data.get("provider") or provider,
        "interval": data.get("interval") or POLL_INTERVAL,
    }


async def device_poll(token: str) -> dict:
    r = await client().get(
        f"{CODELY_SERVER}/auth/device/poll",
        params={"auth_request_token": token},
    )
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"device poll failed: {r.status_code} {r.text[:200]}")
    data = r.json()
    status = data.get("status")
    if token in _device:
        _device[token]["status"] = status or ""
    if status == "authorized" and data.get("authorization_code"):
        ex = await client().post(
            f"{CODELY_SERVER}/auth/device/exchange",
            json={"authorization_code": data["authorization_code"]},
        )
        if ex.status_code != 200:
            raise HTTPException(status_code=502, detail=f"device exchange failed: {ex.status_code} {ex.text[:200]}")
        tk = ex.json()
        save_creds({
            "access_token": tk.get("access_token"),
            "refresh_token": tk.get("refresh_token"),
            "token_type": tk.get("token_type") or "Bearer",
            "expires_in": tk.get("expires_in"),
            "expiry_date": int(time.time() * 1000) + int(tk.get("expires_in") or 3600) * 1000,
        })
        try:
            await fetch_cli_api_key(force=True)
            ok = True
        except Exception as e:  # 登录成功但虚拟密钥没拿到，也算登录成功（凭据已落盘）
            print(f"[codely2codex] WARN fetch cli_api_key failed: {e}", flush=True)
            ok = False
        _device.pop(token, None)
        return {"status": "authorized", "credentials_saved": True, "cli_api_key_ready": ok}
    return {"status": status}


async def refresh_access_token() -> dict:
    creds = load_creds()
    rt = creds.get("refresh_token")
    if not rt:
        raise HTTPException(status_code=401, detail="no refresh_token; re-login required")
    r = await client().post(
        f"{CODELY_SERVER}/auth/refresh",
        json={"refresh_token": rt},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    if r.status_code in (400, 401):
        raise HTTPException(status_code=401, detail="refresh token expired or invalid; re-login required")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"auth refresh failed: {r.status_code} {r.text[:200]}")
    data = r.json()
    save_creds({
        "access_token": data.get("access_token"),
        "token_type": data.get("token_type") or "Bearer",
        "expires_in": data.get("expires_in"),
        "expiry_date": int(time.time() * 1000) + int(data.get("expires_in") or 3600) * 1000,
        "refresh_token": data.get("refresh_token") or rt,
    })
    return load_creds()


async def fetch_cli_api_key(force: bool = False) -> str:
    """拿 LiteLLM 虚拟密钥（sk- 前缀）；缓存只在格式正确时复用，
    老版本 CLI 落盘的 cli_api_key 可能不是 sk- 虚拟密钥，必须重新拉。"""
    creds = load_creds()
    if not force and str(creds.get("cli_api_key") or "").startswith("sk-"):
        return creds["cli_api_key"]
    access = creds.get("access_token")
    if not access:
        raise HTTPException(status_code=401, detail="not logged in; please authorize device login")
    r = await client().get(
        f"{CODELY_SERVER}/api/api-token/cli-api-key",
        headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
    )
    if r.status_code == 401:
        raise HTTPException(status_code=401, detail="access token rejected; refresh/re-login required")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"cli-api-key failed: {r.status_code} {r.text[:200]}")
    data = r.json()
    key = data.get("cli_api_key")
    if not key:
        raise HTTPException(status_code=502, detail=f"cli_api_key missing in response: {data}")
    save_creds({
        "cli_api_key": key,
        "user_id": data.get("user_id"),
        "rpm": data.get("rpm"),
        "tpm": data.get("tpm"),
    })
    return key


async def get_gateway_key(allow_refresh: bool = True) -> str:
    try:
        return await fetch_cli_api_key()
    except HTTPException as e:
        if e.status_code != 401 or not allow_refresh:
            raise
        await refresh_access_token()
        return await fetch_cli_api_key(force=True)


# ---------------- OpenAI 兼容 API ----------------

@app.get("/health")
async def health():
    creds = load_creds()
    return {
        "ok": True,
        "version": BRIDGE_VERSION,
        "logged_in": bool(creds.get("access_token")),
        "has_cli_api_key": bool(creds.get("cli_api_key")),
        "user_id": creds.get("user_id"),
        "gateway": GATEWAY_BASE,
        "models": FALLBACK_MODELS,
    }


@app.post("/auth/device/start")
async def auth_device_start(request: Request):
    check_bridge_auth(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    return await device_start(
        provider=body.get("provider") or "unity",
        client_name=body.get("client_name") or "codely-cli",
    )


@app.get("/auth/device/check")
async def auth_device_check(request: Request, auth_request_token: str = ""):
    check_bridge_auth(request)
    if not auth_request_token:
        raise HTTPException(status_code=400, detail="auth_request_token required")
    info = _device.get(auth_request_token)
    if not info:
        return {"status": "unknown_or_expired"}
    if time.time() - info["started"] > DEVICE_TIMEOUT:
        _device.pop(auth_request_token, None)
        return {"status": "expired"}
    return await device_poll(auth_request_token)


@app.get("/v1/models")
async def list_models(request: Request):
    check_bridge_auth(request)
    try:
        key = await get_gateway_key()
        gpath = f"{GATEWAY_BASE}/models"
        r = await client().get(
            f"{GATEWAY_BASE}/models",
            headers={"Authorization": f"Bearer {key}", **sign_gateway_headers(key, "/v1/models")},
        )
        if r.status_code == 200:
            return Response(content=r.content, media_type="application/json")
        detail = r.text[:200]
    except HTTPException as e:
        detail = e.detail
    # 无凭据/降级：官方 CLI 实测的静态目录（带 codely/ 前缀）
    data = {
        "object": "list",
        "data": [
            {"id": f"{CATALOG_PREFIX}{m}", "object": "model", "created": 0, "owned_by": "tuanjie-ai"}
            for m in FALLBACK_MODELS
        ],
    }
    return JSONResponse(data, headers={"X-Codely-Models-Fallback": detail if isinstance(detail, str) else "1"})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    check_bridge_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
    body["model"] = remap_model(body.get("model"))
    stream = bool(body.get("stream"))

    key = await get_gateway_key()
    url = f"{GATEWAY_BASE}/chat/completions"

    def _hdrs(k: str) -> dict:
        return {"Authorization": f"Bearer {k}", "Content-Type": "application/json",
                "Accept": "application/json", **sign_gateway_headers(k, "/v1/chat/completions")}

    headers = _hdrs(key)

    async def once(hdrs):
        c = client()
        req = c.build_request("POST", url, json=body, headers=hdrs)
        return await c.send(req, stream=True)

    resp = await once(headers)
    if resp.status_code == 401:
        await resp.aclose()
        # 令牌失效：刷新 + 重取虚拟密钥，重试一次
        try:
            await refresh_access_token()
            key = await fetch_cli_api_key(force=True)
        except HTTPException:
            pass
        headers = _hdrs(key)
        resp = await once(headers)

    if resp.status_code != 200:
        text = (await resp.aread()).decode("utf-8", "replace")[:500]
        await resp.aclose()
        return Response(content=json.dumps({"error": {"message": f"gateway {resp.status_code}: {text}",
                                                    "type": "codely_upstream_error"}}),
                        media_type="application/json", status_code=resp.status_code)

    if stream:
        passthrough = {k: v for k, v in resp.headers.items() if k.lower() in ("content-type",)}
        sgen = StreamingResponse(_sse_pump(resp), media_type=passthrough.get("content-type", "text/event-stream"))
        return sgen
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
    ap.add_argument("--port", type=int, default=8790)
    args = ap.parse_args()
    print(f"[codely2codex] v{BRIDGE_VERSION} on http://{args.host}:{args.port}  gateway={GATEWAY_BASE}", flush=True)
    print(f"[codely2codex] creds: {creds_path()}  bridge_key={'set' if BRIDGE_KEY else 'OPEN (no key)'}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=True)


if __name__ == "__main__":
    main()
