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

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
import _platform
import _common
import time
import uuid
from pathlib import Path
import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

BRIDGE_VERSION = "0.3.0"

CODELY_SERVER = os.environ.get("CODELY_SERVER") or "https://codely.tuanjie.cn"
GATEWAY_BASE = os.environ.get("CODELY_GATEWAY") or "https://codely-litellm.tuanjie.cn/v1"
DEFAULT_TIMEOUT = float(os.environ.get("CODELY_CALL_TIMEOUT") or "300")
BRIDGE_KEY = os.environ.get("CODELY2CODEX_KEY") or ""
POLL_INTERVAL = float(os.environ.get("CODELY_DEVICE_POLL_INTERVAL") or "4")
DEVICE_TIMEOUT = float(os.environ.get("CODELY_DEVICE_TIMEOUT") or "900")

# 2026-09-28 实测：该团队密钥只允许 alias-only-proxy-models。
# 原始模型名（DeepSeek-V4.1-Flash / GLM-5.3-FLASH / KIMI-K3）会被网关 401
# team_model_access_denied 拒绝，只有 5 个 codely-* 别名真实可调用，全部已验证。
FALLBACK_MODELS = [
    "codely-core", "codely-flash", "codely-air", "codely-basic", "codely-vl",
]
CATALOG_PREFIX = "codely/"

# 网关策略：团队密钥仅允许 alias-only-proxy-models。任何出现在这里之外的名字
# （例如原始模型名 DeepSeek-V4.1-Flash）都会 401 team_model_access_denied，
# 不能进目录误导用户。
DENIED_MODELS = {"DeepSeek-V4.1-Flash", "GLM-5.3-FLASH", "KIMI-K3"}

# 压缩兜底：会话历史里可能仍写着早已禁用的模型名，请求会因此在网关 401，
# 把整个压缩流程卡死。压缩请求统一改走 codely-core。
COMPACTION_FALLBACK = "codely-core"
LEGACY_DENIED = {
    "DeepSeek-V4.1-Flash": COMPACTION_FALLBACK,
    "GLM-5.3-FLASH": COMPACTION_FALLBACK,
    "KIMI-K3": COMPACTION_FALLBACK,
}

# 官方 CLI / opencodex 用的短别名 → 完整别名。白名单解析前先归一化，
# 否则别名的桥请求写法会被误拒。
ALIAS_TO_MODEL = {
    "core": "codely-core",
    "fl": "codely-flash",
    "air": "codely-air",
    "basic": "codely-basic",
    "vl": "codely-vl",
    # _strip_provider_prefix turns the catalog's own spelling "codely-flash"
    # into "flash", and the table only held the opencodex short alias "fl", so
    # the full alias fell straight through to the whitelist and came back as a
    # 400 "model 'flash' is not allowed" -- from a model the picker advertises.
    # Every other entry already matched its own stripped name; keep that true.
    "flash": "codely-flash",
}

# 官方 CLI 逆向出的 LiteLLM 网关签名参数（HMAC-SHA256 双层派生）：
#   inner = HMAC-SHA256(BASE_KEY, "codely-signing-v1")
#   key   = HMAC-SHA256(inner, cli_api_key)
#   sig   = base64url(HMAC-SHA256(key, "v1\n<path>\n<unix_ts>"))
#   header: X-Codely-Signature: v1.<ts>.<sig>
_SIGN_BASE_KEY = bytes.fromhex("406f00f74768ba0cb0cd30f097ec6c2bdacb89c61a38b7dd140838bbd0e98018")

# 官方 CLI 逆向（bundle/gemini.js: WAt=class extends KEe）——Anthropic 兼容 generator 的
# defaultHeaders = {"User-Agent": `codely-cli/<ver> (<platform>; <arch>)`, "x-litellm-session-id": <uuid>}。
# 上游网关对这两个头做强校验（实测矩阵，与账号额度无关）：
#   缺 x-litellm-session-id -> 400 {"error": "非法session"}
#   缺官方 UA               -> 400 欢迎使用Codely 门禁
# 两者必须一起带上才能过 400 门禁。
_CODELY_CLI_VERSION = os.environ.get("CODELY_CLI_VERSION") or "1.0.0-rc.60"
CLI_USER_AGENT = f"codely-cli/{_CODELY_CLI_VERSION} ({_platform.short_platform()}; {_platform.arch()})"
_session_id = uuid.uuid4().hex


def _rotate_session() -> None:
    """换一个新 litellm 会话 id（401 后重试时调用，对齐官方 CLI 的会话语义）。"""
    global _session_id
    _session_id = uuid.uuid4().hex


def gateway_headers(key: str, path: str) -> dict:
    """官方 CLI 等价的 LiteLLM 网关请求头。"""
    return {
        "Authorization": f"Bearer {key}",
        "x-api-key": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": CLI_USER_AGENT,
        "x-litellm-session-id": _session_id,
        "anthropic-version": "2023-06-01",
        **sign_gateway_headers(key, path),
    }


def sign_gateway_headers(cli_key: str, path: str) -> dict:
    ts = str(int(time.time()))
    inner = hmac.new(_SIGN_BASE_KEY, b"codely-signing-v1", hashlib.sha256).digest()
    key = hmac.new(inner, cli_key.encode(), hashlib.sha256).digest()
    payload = f"v1\n{path}\n{ts}".encode()
    digest = base64.urlsafe_b64encode(hmac.new(key, payload, hashlib.sha256).digest()).rstrip(b"=").decode()
    return {"X-Codely-Signature": f"v1.{ts}.{digest}"}

client = _common.make_client_getter(**_common.client_kwargs(DEFAULT_TIMEOUT))

app = _common.make_app("codely2codex", BRIDGE_VERSION)
_creds_lock = asyncio.Lock()
_device: dict = {}  # auth_request_token -> {"started": ts, "status": str}


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


check_bridge_auth = _common.make_auth_checker(BRIDGE_KEY)

# 把 Codex 侧带 codely/ 前缀的模型名还原成 Tuanjie 网关原生模型名。
remap_model = _common.make_model_remapper(
    CATALOG_PREFIX, double_prefix="codely-codely", double_strip="codely-")


def _strip_provider_prefix(model: str) -> str:
    """去掉 Codex 侧可能带来的 provider 前缀。

    opencodex 的 codely provider 有 alias `cdl`，请求常以 `cdl/<model>` 到达；
    不剥的话白名单会把整个串当成未知模型拒掉。"""
    for prefix in ("cdl/", "codely/", "codely-"):
        if model.startswith(prefix):
            return model[len(prefix):]
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


async def remint_gateway_key() -> str:
    """Re-mint the LiteLLM virtual key after the gateway rejected it.

    实测（2026-09-29）：账号改订阅/改组后，网关会吊销旧虚拟密钥——
    GET /v1/models 返回 401 "Unable to find token in cache or
    LiteLLM_VerificationTokenTable"，而 cli_api_key 端点为同一个 access_token
    重新发一个不同的有效 sk- key。所以「网关 401」要的是重发虚拟密钥，
    不是刷新 access token：后者需要 refresh_token，而官方 CLI 的凭据里
    从不写这一项（薄封装和 bridge 一样都没有），走 refresh 只会 401。

    只有重发端点自己也拒绝时，才说明 access_token 本身过期，需要刷新。
    """
    try:
        return await fetch_cli_api_key(force=True)
    except HTTPException as e:
        if e.status_code != 401:
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
        r = await client().get(
            f"{GATEWAY_BASE}/models",
            headers=gateway_headers(key, "/v1/models"),
        )
        if r.status_code == 401:
            # The gateway revoked the cached virtual key. Without this retry
            # the bridge would serve the 5-row fallback catalog forever, even
            # though /api/api-token/cli-api-key mints a working key on demand.
            await r.aclose()
            key = await remint_gateway_key()
            _rotate_session()
            r = await client().get(
                f"{GATEWAY_BASE}/models",
                headers=gateway_headers(key, "/v1/models"),
            )
        if r.status_code == 200:
            try:
                payload = r.json()
                allowed = [m for m in payload.get("data", [])
                           if (m.get("id") or "").split("/", 1)[-1] not in DENIED_MODELS]
                if len(allowed) != len(payload.get("data", [])):
                    payload["data"] = allowed
                content = json.dumps(payload, ensure_ascii=False)
            except Exception:
                content = r.content.decode("utf-8", "replace")
            return Response(content=content, media_type="application/json")
        detail = r.text[:200]
    except HTTPException as e:
        detail = e.detail
    except httpx.HTTPError as e:
        # Upstream network blips must degrade to the static catalog, not a 500.
        detail = f"{type(e).__name__}: {e}"
    # 无凭据/降级：官方 CLI 实测的静态目录（带 codely/ 前缀）
    data = {
        "object": "list",
        "data": [
            {"id": f"{CATALOG_PREFIX}{m}", "object": "model", "created": 0, "owned_by": "tuanjie-ai"}
            for m in FALLBACK_MODELS
        ],
    }
    # The upstream body carries newlines; h11 aborts the whole response if one
    # reaches a header value, so the fallback would turn into an empty reply.
    return JSONResponse(data, headers={
        "X-Codely-Models-Fallback": _common.safe_header_value(detail)
        if isinstance(detail, str) and detail else "1"})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    check_bridge_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
    body["model"] = remap_model(body.get("model"))
    body["model"] = _strip_provider_prefix(str(body["model"] or ""))
    # 短别名归一化，别名表缺的保持原样，让下面白名单给出统一拒绝提示。
    body["model"] = ALIAS_TO_MODEL.get(body["model"], body["model"])
    # 历史模型名兜底：会话创建时写进历史的模型名（DeepSeek-V4.1-Flash 等）
    # 对当前团队密钥已永久失效。请求经 opencodex 代理转换后可能丢掉压缩标记，
    # 所以这里不再判断请求形态，只要模型名命中就改走 codely-core，
    # 否则压缩流程会被网关 401 或上面的白名单 400 卡死。
    if str(body["model"] or "") in LEGACY_DENIED:
        body["model"] = LEGACY_DENIED[body["model"]]
    # 服务端白名单：团队密钥仅允许 codely-* 别名。未知名直接拦截，
    # 避免 alias-only 网关返回误导性的 401 team_model_access_denied。
    if str(body["model"] or "") not in FALLBACK_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"model '{body['model']}' is not allowed for this team key. "
                   f"Allowed models (alias-only): {', '.join(FALLBACK_MODELS)}",
        )
    stream = bool(body.get("stream"))
    try:
        key = await get_gateway_key()
        url = f"{GATEWAY_BASE}/chat/completions"

        def _hdrs(k: str) -> dict:
            return gateway_headers(k, "/v1/chat/completions")

        headers = _hdrs(key)

        async def once(hdrs):
            c = client()
            req = c.build_request("POST", url, json=body, headers=hdrs)
            return await c.send(req, stream=True)

        resp = await once(headers)
        if resp.status_code == 401:
            await resp.aclose()
            # Token invalid: re-mint the virtual key first, then retry once.
            # The old order ran refresh_access_token() first, which needs a
            # refresh_token the official CLI never writes; its HTTPException was
            # swallowed by "except HTTPException: pass", so the retry went out with
            # the same dead key and failed again.
            key = await remint_gateway_key()
            _rotate_session()
            headers = _hdrs(key)
            resp = await once(headers)

    except httpx.HTTPError as e:
        # A transport failure must surface as 502, not a 500 from the FastAPI
        # wrapper. httpx ConnectError str()s to the empty string, leaving only
        # "ConnectError: " in the log; repr() keeps the target host.
        return Response(content=json.dumps({"error": {
            "message": f"gateway unreachable: {type(e).__name__} ({url}): {e!r}",
            "type": "codely_upstream_error"}}),
                         media_type="application/json", status_code=502)

    if resp.status_code != 200:
        text = (await resp.aread()).decode("utf-8", "replace")
        await resp.aclose()
        return _common.upstream_error_response(
            resp.status_code, text, "gateway", "codely_upstream_error",
            max_chars=500)

    if stream:
        return StreamingResponse(_sse_pump(resp), media_type=resp.headers.get(
            "content-type", "text/event-stream"))
    content = await resp.aread()
    ctype = resp.headers.get("content-type", "application/json")
    await resp.aclose()
    return Response(content=content, media_type=ctype)


_sse_pump = _common.sse_pump


def main():
    _common.serve(
        app, 8790,
        f"[codely2codex] v{BRIDGE_VERSION} on http://%s:%s  gateway={GATEWAY_BASE}\n"
        f"[codely2codex] creds: {creds_path()}  "
        f"bridge_key={'set' if BRIDGE_KEY else 'OPEN (no key)'}")


if __name__ == "__main__":
    main()
