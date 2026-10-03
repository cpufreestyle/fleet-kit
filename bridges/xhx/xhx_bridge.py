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
                refresh_conflict）；刷新成功必须立即把新 token 写回文件 **
      - 模型目录：GET {api}/model_catalog -> data.categories[].models[]（含 context_window/max_tokens）
      - 聊天：POST {api}/chat/completions（OpenAI 兼容，SSE 流式同标准 chunk 格式）
      - 积分余额：GET {web}/api/web/points/v1/balance（只读）
      - api = https://xiaohuanxiong.com/api/web/llm/v2

账号池（bridges/xhx/account_pool.py）：
  桌面端 refresh_token 单次轮换会与桥互相顶号，官方 auth.json 一旦被清空，全节点就报
  「小浣熊未登录」。桥因此只在本地 auths/ 目录持有账号副本（import_current 从官方登录
  一次性拷贝），绝不写官方目录；请求按池内候选逐个尝试，遇 401/积分不足自动切下一个
  账号并给失败账号记冷却。每账号剩余积分缓存在池状态里，/health 与面板按需刷新。

链路：Codex → 本桥(:8793) → https://xiaohuanxiong.com/api/web/llm/v2
账号管理：POST /admin/pool/import（从桌面 app 导入当前登录）、
          POST /admin/pool/{ref}/primary、DELETE /admin/pool/{ref}、
          POST /admin/pool/points（强制刷新积分）。

依赖：fastapi + uvicorn + httpx。用法：python3 xhx_bridge.py [--port 8793]
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
# the usage ledger ships beside this bridge
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import _common
import usage_ledger

# The raccoon pool ships as a sibling file and is loaded by explicit path
# under a private module name: the bare name "account_pool" also belongs to
# the workbuddy bridge, so a process that loads that bridge first would
# otherwise leave its pool in sys.modules and silently answer with the
# wrong account_pool.
_pool_spec = importlib.util.spec_from_file_location(
    "xhx_account_pool",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_pool.py"))
_pool_module = importlib.util.module_from_spec(_pool_spec)
# dataclasses resolves bare-identifier annotations (KW_ONLY, ClassVar)
# through sys.modules, so the module must be registered before it executes.
sys.modules[_pool_spec.name] = _pool_module
_pool_spec.loader.exec_module(_pool_module)
AccountPool = _pool_module.AccountPool

BRIDGE_VERSION = "0.2.1"

WEB_BASE = os.environ.get("XHX_WEB_BASE_URL") or "https://xiaohuanxiong.com"
API_BASE = f"{WEB_BASE}/api/web/llm/v2"
REFRESH_URL = f"{WEB_BASE}/api/web/auth/v1/refresh"
POINTS_URL = f"{WEB_BASE}/api/web/points/v1/balance"
MODELS_PATH = "/model_catalog"
CHAT_PATH = "/chat/completions"
CATALOG_PREFIX = "xhx/"
FALLBACK_MODELS = ["raccoon-8c4485", "sn-glm-5-3-flash", "sn-deepseek-v4-1-flash",
                   "sn-kimi-k3", "sn-glm-5-3", "sn-sensenova-6-8-flash",
                   "sn-sensenova-6-8-flash-lite", "raccoon-19b265", "raccoon-405a1c"]

BRIDGE_KEY = os.environ.get("XHX2CODEX_KEY") or ""
CALL_TIMEOUT = float(os.environ.get("XHX_CALL_TIMEOUT") or "600")
BASE_DIR = Path(__file__).resolve().parent


def auth_file() -> Path:
    root = os.environ.get("BOX_AGENT_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".box-agent", "config")
    return Path(root) / "auth.json"


def auth_pool_dir() -> Path:
    return Path(os.environ.get("XHX_AUTH_POOL_DIR") or BASE_DIR / "auths")


client = _common.make_client_getter(**_common.client_kwargs(
    CALL_TIMEOUT, headers={"User-Agent": f"xhx2codex/{BRIDGE_VERSION}"}))

app = _common.make_app("xhx2codex", BRIDGE_VERSION)
_lock = asyncio.Lock()
_cache: dict = {"models": {}, "ts": 0.0}   # model_id -> {name, context_window, max_tokens, ...}


check_bridge_auth = _common.make_auth_checker(BRIDGE_KEY)


# ---------------- 账号池 ----------------

POOL = AccountPool(
    auth_pool_dir(),
    client,
    lambda: auth_file() if auth_file().is_file() else None,
    forbidden_dirs=[auth_file().parent],
)


# ---------------- 登录态（池内副本，官方目录只读导入） ----------------

def load_auth() -> Optional[dict]:
    """官方桌面端登录态，只用于 import_current，不参与请求。"""
    try:
        auth = json.loads(auth_file().read_text(encoding="utf-8"))
        return auth if auth.get("access_token") else None
    except Exception:
        return None


def _account_failure(status: int, raw: bytes) -> tuple[str, int] | None:
    """导致账号不可用的上游错误 -> (原因, 冷却秒数)；None 表示原样透传给客户端。"""
    text = raw.decode("utf-8", "replace").lower()
    # 积分不足必须排在 401 之前：上游把余额不足也包在错误体里一起返回。
    if status == 402 or "insufficient_points" in text or "积分" in text or "余额" in text \
            or "insufficient balance" in text:
        return "账号积分不足", 3600
    if status in {401, 403} or "refresh_conflict" in text or "200822" in text \
            or ("token" in text and ("expired" in text or "invalid" in text)):
        return "登录态失效，等待重新认证或刷新", 600
    if status == 429 or "rate limit" in text or "too many requests" in text or "限流" in text:
        return "账号触发限流", 60
    if status >= 500:
        return f"上游 HTTP {status}", 30
    return None


async def _headers_for_candidate(candidate) -> Optional[dict]:
    """取一个账号的请求头；必要时先做单次轮换刷新。返回 None 表示该账号出局。"""
    try:
        return await candidate.manager.ensure_headers()
    except Exception as exc:
        POOL.mark_failure(candidate.ref, f"凭据异常 {str(exc)[:80]}", 600)
        return None


async def upstream_request(method: str, path: str, **kw) -> httpx.Response:
    """按账号池顺序发一次请求；账号级失败自动换号，最后一个错误向上抛。

    返回的 response 可能是非 200（没有账号级失败、需原样透传时），调用方照旧处理。
    """
    candidates = POOL.candidates()
    if not candidates:
        raise HTTPException(status_code=503, detail={"error": {
            "message": "小浣熊账号池全部处于冷却状态", "type": "xhx_pool_unavailable"}})
    last_status, last_raw = 502, b"all xhx accounts failed"
    for candidate in candidates:
        headers = await _headers_for_candidate(candidate)
        if headers is None:
            continue
        for attempt in (0, 1):
            try:
                r = await client().request(method, f"{API_BASE}{path}", headers=headers, **kw)
            except httpx.HTTPError as exc:
                last_status, last_raw = 502, str(exc).encode("utf-8", "replace")
                POOL.mark_failure(candidate.ref, "上游网络错误", 30)
                break
            if r.status_code in (401, 403) and attempt == 0:
                await r.aclose()
                if await candidate.manager.refresh():
                    headers = candidate.manager.headers()
                    continue
                POOL.mark_failure(candidate.ref, "登录态失效，等待重新认证或刷新", 600)
                break
            if r.status_code == 200:
                POOL.mark_success(candidate.ref)
                return r
            body = await r.aread()
            await r.aclose()
            last_status, last_raw = r.status_code, body
            failure = _account_failure(r.status_code, body)
            if failure:
                POOL.mark_failure(candidate.ref, failure[0], failure[1])
                break
            return r
    raise HTTPException(status_code=last_status, detail={"error": {
        "message": f"xiaohuanxiong upstream {last_status}: {last_raw.decode('utf-8', 'replace')[:400]}",
        "type": "xhx_upstream_error"}})


# ---------------- 模型目录 ----------------

async def get_catalog(force: bool = False) -> dict:
    async with _lock:
        if not force and _cache["models"] and time.time() - _cache["ts"] < 300:
            return _cache["models"]
    try:
        r = await upstream_request("GET", MODELS_PATH)
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
        await r.aclose()
    except Exception as e:
        print(f"[xhx2codex] catalog refresh failed: {e}", flush=True)
    async with _lock:
        return _cache["models"]


# 循环剥离：兼容 ocx 发现的双前缀 slug（如 xhx/xhx-sn-glm-5-3-flash）
remap_model = _common.make_prefix_stripper(CATALOG_PREFIX)


# ---------------- SSE 透传（带首字节前换号） ----------------

def _err_event(msg: bytes, status: int) -> bytes:
    chunk = {"error": {"message": msg.decode("utf-8", "replace")[:500],
                       "type": "xhx_upstream_error", "code": status}}
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


def _sse_error_event(payload: bytes) -> tuple[int, bytes] | None:
    """首个 SSE 事件里的 error 对象 -> (status, 原始事件)。"""
    for line in payload.splitlines():
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        try:
            obj = json.loads(line[5:].strip())
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        error = obj.get("error") if isinstance(obj, dict) else None
        if not error:
            continue
        code = error.get("code") if isinstance(error, dict) else None
        status = code if isinstance(code, int) and 400 <= code <= 599 else 502
        return status, payload
    return None


async def _stream_with_pool(path: str, payload: dict):
    """转发 SSE；只有在首个事件释放给客户端之前才允许换账号。

    已经开始向客户端吐字节之后再重发会重复输出，因此换号只发生在：刷新失败、
    传输错误、非 200 响应、以及首字节前的 SSE error 事件。
    """
    candidates = POOL.candidates()
    if not candidates:
        yield _err_event(b"xhx account pool is cooling down", 503)
        return
    last_status, last_error = 502, b"all xhx accounts failed"
    completed = False
    for candidate in candidates:
        headers = await _headers_for_candidate(candidate)
        if headers is None:
            continue
        started = False
        switch = False
        try:
            async with client().stream("POST", f"{API_BASE}{path}", headers=headers, json=payload) as response:
                if response.status_code != 200:
                    error = await response.aread()
                    await response.aclose()
                    last_status, last_error = response.status_code, error
                    failure = _account_failure(response.status_code, error)
                    if failure:
                        POOL.mark_failure(candidate.ref, failure[0], failure[1])
                        switch = True
                    else:
                        yield _err_event(error, response.status_code)
                        return
                else:
                    pending = bytearray()
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        if not started:
                            pending.extend(chunk)
                            if b"\n\n" not in pending and b"\r\n\r\n" not in pending \
                                    and len(pending) < 131_072:
                                continue
                            first = bytes(pending)
                            stream_error = _sse_error_event(first)
                            if stream_error:
                                last_status, last_error = stream_error
                                failure = _account_failure(last_status, last_error) \
                                    or ("上游 SSE 错误", 30)
                                POOL.mark_failure(candidate.ref, failure[0], failure[1])
                                switch = True
                                break
                            POOL.mark_success(candidate.ref)
                            started = True
                            pending.clear()
                            chunk = first
                        yield chunk
                    if switch:
                        pass
                    elif started:
                        completed = True
                    elif pending:
                        stream_error = _sse_error_event(bytes(pending))
                        if stream_error:
                            last_status, last_error = stream_error
                            failure = _account_failure(last_status, last_error) or ("上游 SSE 错误", 30)
                            POOL.mark_failure(candidate.ref, failure[0], failure[1])
                        else:
                            POOL.mark_success(candidate.ref)
                            started = True
                            yield bytes(pending)
                            completed = True
                    else:
                        POOL.mark_failure(candidate.ref, "上游返回空流", 30)
                        last_status, last_error = 502, b"empty upstream SSE response"
        except httpx.HTTPError as exc:
            if started:
                yield _err_event(str(exc).encode("utf-8", "replace"), 502)
                return
            POOL.mark_failure(candidate.ref, "上游网络错误", 30)
            last_status, last_error = 502, str(exc).encode("utf-8", "replace")
            switch = True
        if completed:
            break
        if not switch:
            break
    if not completed:
        yield _err_event(last_error, last_status)


# ---------------- 路由 ----------------

@app.get("/health")
async def health():
    info = {"ok": True, "version": BRIDGE_VERSION, "api_base": API_BASE}
    accounts = POOL.status()
    primary = next((item for item in accounts if item["primary"]), None)
    active = next((item for item in accounts if item["active"]), None)
    info["logged_in"] = bool(accounts)
    info["account"] = (primary or active or {}).get("name") or ""
    info["session_alive"] = None
    if accounts:
        try:
            r = await upstream_request("GET", MODELS_PATH)
            info["session_alive"] = r.status_code == 200
            if r.status_code != 200:
                info["detail"] = f"model_catalog http {r.status_code}"
            await r.aclose()
        except HTTPException as e:
            info["session_alive"] = False
            info["detail"] = e.detail if isinstance(e.detail, str) else "账号池全部冷却"
        except Exception as e:
            info["session_alive"] = False
            info["detail"] = str(e)[:200]
    catalog = await get_catalog()
    info["models"] = sorted(catalog.keys()) or FALLBACK_MODELS
    if POOL.points_stale():
        asyncio.ensure_future(POOL.refresh_points())
    info["account_pool"] = POOL.summary()
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

    if stream:
        return StreamingResponse(_metered(_stream_with_pool(CHAT_PATH, payload), model),
                                 media_type="text/event-stream")

    r = await upstream_request("POST", CHAT_PATH, json=payload)
    if r.status_code != 200:
        text = r.text[:400]
        await r.aclose()
        return JSONResponse({"error": {"message": f"xiaohuanxiong upstream {r.status_code}: {text}",
                                       "type": "xhx_upstream_error"}},
                            status_code=r.status_code)

    await r.aread()
    body = r.content
    await r.aclose()
    usage = None
    try:
        usage = (json.loads(body) or {}).get("usage")
    except ValueError:
        pass
    usage_ledger.record(model, usage, stream=False)
    return Response(content=body, media_type="application/json")


# ---------------- 账号池管理 ----------------

@app.post("/admin/pool/import")
async def admin_import(request: Request):
    """把桌面 app 当前登录拷进池（幂等：同账号刷新 token，新账号追加）。"""
    check_bridge_auth(request)
    try:
        account = POOL.import_current()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "account": account, "account_pool": POOL.summary()}


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


async def _metered(resp, model):
    """Relay the SSE body, then count the call.

    llm/v2 bills nothing, so the local ledger is the only usage record this
    fleet has -- see bridges/xhx/usage_ledger.py. A stream only carries
    usage when the client asked for it (stream_options.include_usage), so
    a call without it still counts, with zero tokens.
    """
    started = time.time()
    seen = ""
    usage = None
    try:
        async for chunk in resp:
            if usage is None and chunk:
                seen += chunk.decode("utf-8", "ignore")
                usage = _usage_from_sse(seen)
                if len(seen) > 65536:
                    seen = seen[-8192:]
            yield chunk
    finally:
        usage_ledger.record(model, usage, stream=True,
                            seconds=time.time() - started)


def _usage_from_sse(buffer):
    """The usage object of the last complete SSE event that carries one."""
    found = None
    for event in buffer.split("\n\n"):
        for line in event.split("\n"):
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            if isinstance(data, dict) and isinstance(data.get("usage"), dict):
                found = data["usage"]
    return found


def main():
    _common.serve(
        app, 8793,
        f"[xhx2codex] v{BRIDGE_VERSION} on http://%s:%s "
        f"api={API_BASE} pool={POOL.summary()['count']} key={'set' if BRIDGE_KEY else 'OPEN'}")


if __name__ == "__main__":
    main()
