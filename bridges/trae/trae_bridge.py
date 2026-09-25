#!/usr/bin/env python3
"""trae2codex — 把本机 Trae（国内版 CN / TRAE SOLO CN / 国际版）SOLO 模型暴露成标准 OpenAI 兼容 API。

学习来源（开源项目）：
  * npm: dsh-connect-trae@2.3.0（MIT, github.com/dingminhua/dsh-connect-trae）
    —— 「Connect locally signed-in Trae models to DeepSeek Harness」。
    本桥复用了它的全部逆向结论：
      - storage.json 的 iCubeAuthInfo://icube.cloudide 解密算法
        （AES-128-CBC，双层 SHA512 派生，SALT_A^SALT_B / SALT_C^SALT_D）
      - 设备码之外的刷新契约 POST /cloudide/api/v3/trae/oauth/ExchangeToken
      - 模型目录 POST {gateway}/api/ide/v1/get_detail_param
      - 聊天通道 POST {gateway}/api/agent/v3/llm_utils_chat（SOLO 通道）
      - Trae 命名 SSE 事件 → OpenAI chat.completion.chunk 的转换逻辑

链路：Codex → 本桥(:8791) → https://trae-api-cn.mchost.guru（CN）或 coresg-normal.trae.ai（国际）
登录态：直接读官方 IDE 落盘的 storage.json；过期时用 refresh token 换新 token，
        换到的凭据缓存到 ~/.trae2codex/creds.json（不覆盖 IDE 文件）。

依赖：fastapi + uvicorn + httpx + cryptography。用法：python3 trae_bridge.py [--port 8791]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
import uvicorn
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

BRIDGE_VERSION = "0.1.0"

# ---------------- 常量（来自 dsh-connect-trae 逆向，已验证可解密本机 storage.json） ----------------

_SALT_A = bytes.fromhex("52096ad53036a538bf40a39e81f3d7fb7ce339829b2fff87348e4344c4dee9cb547b9432a6c2233dee4c950b42fac34e082ea16628d924b2765ba2496d8bd125")
_SALT_B = bytes.fromhex("1fdda8338807c731b11210592780ec5f60517fa919b54a0d2de57a9f93c99cefa0e03b4dae2af5b0c8ebbb3c83539961172b047eba77d626e169146355210c7d")
_SALT_C = bytes.fromhex("bfc0d8fa7af6dc611ffe621b084847b0876360127f65cb68d366bf7d2548969c33e5792311998db16e839680acfffe06128c373eecf98740870c75045995a8d1")
_SALT_D = bytes.fromhex("f6cc1ae8e846816ddf92a9f217f1699132c4a52afe780336f4cfd15535068a6aaf941fccbabaa5b6578e310a276e1a9a5638ad7d1240c6e163635352bf864caa")

GATEWAYS = {
    "cn": {"chat": "https://trae-api-cn.mchost.guru", "remote": "https://solo.trae.cn/api/remote/v1", "pay": "https://api.trae.cn"},
    "ai": {"chat": "https://coresg-normal.trae.ai", "remote": "https://coresg-normal.trae.ai/api/remote/v1", "pay": "https://growsg-normal.trae.ai"},
}
REFRESH_PATH = "/cloudide/api/v3/trae/oauth/ExchangeToken"
CLIENT_ID = "ono9krqynydwx5"
AUTH_STORAGE_KEY = "iCubeAuthInfo://icube.cloudide"
DC_PREFIX = "iCubeAuthInfo://icube-dc:"
VERSION_CODE_FALLBACK = "20260716"
APP_NAMES = ["Trae CN", "TRAE SOLO CN", "Trae", "TRAE SOLO"]
CHAT_PATH = "/api/agent/v3/llm_utils_chat"
DETAIL_PATH = "/api/ide/v1/get_detail_param"
DIRECTORY_FUNCTIONS = ["solo_work_remote", "solo_work_lite"]
DEFAULT_FUNCTION = "solo_work_lite"
# get_detail_param 目录里的内部 agent / 自定义模型槽位，不是可直接聊的模型
EXCLUDED_CONFIG_IDS = {"file_search_agent", "explore_sub_agent_v2", "browser_use_subagent", "summary"}
FALLBACK_MODELS = ["DeepSeek-V4-Flash-Official", "DeepSeek-V4-Pro-Official", "GLM-5.3", "GLM-5.2",
                   "Kimi-K3", "MiniMax-M3", "Qwen3.8-Max", "Doubao-Seed-2.1-Pro"]
CATALOG_PREFIX = "trae/"

BRIDGE_KEY = os.environ.get("TRAE2CODEX_KEY") or ""
CALL_TIMEOUT = float(os.environ.get("TRAE_CALL_TIMEOUT") or "300")
CREDS_DIR = Path(os.environ.get("TRAE2CODEX_HOME") or (Path.home() / ".trae2codex"))
CREDS_FILE = CREDS_DIR / "creds.json"

app = FastAPI(title="trae2codex", version=BRIDGE_VERSION)
_http: Optional[httpx.AsyncClient] = None
_state_lock = asyncio.Lock()
_catalog: dict = {"models": {}, "ts": 0.0}   # model_id -> {"function": fn, "name": str}


def client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(CALL_TIMEOUT, connect=15),
                                  headers={"User-Agent": "Mozilla/5.0"})
    return _http


def check_bridge_auth(request: Request) -> None:
    if not BRIDGE_KEY:
        return
    if (request.headers.get("authorization") or "") != f"Bearer {BRIDGE_KEY}":
        raise HTTPException(status_code=401, detail="invalid bridge key")


# ---------------- storage.json 解密（dsh-connect-trae 算法，逐字段对齐） ----------------

def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def decrypt_trae_storage_value(encoded: str) -> str:
    buf = base64.b64decode(encoded)
    if len(buf) <= 102:
        raise ValueError("Trae auth ciphertext is too short")
    header = buf[:6]
    if header == bytes([116, 99, 5, 16, 0, 0]):
        salt = _xor(_SALT_A, _SALT_B)
    elif header == bytes([18, 57, 32, 32, 2, 3]):
        salt = _xor(_SALT_C, _SALT_D)
    else:
        raise ValueError("unsupported Trae auth encryption header")
    random = buf[6:38]
    encrypted = buf[38:]
    first = hashlib.sha512(random).digest()
    derived = hashlib.sha512(first + salt).digest()
    dec = Cipher(algorithms.AES(derived[:16]), modes.CBC(derived[16:32])).decryptor()
    plain = dec.update(encrypted) + dec.finalize()
    # Node createDecipheriv 默认剥 PKCS#7 填充；python 不剥，手动对齐
    if plain and 1 <= plain[-1] <= 16 and plain[-plain[-1]:] == bytes([plain[-1]]) * plain[-1]:
        plain = plain[:-plain[-1]]
    if len(plain) < 64:
        raise ValueError("Trae auth plaintext is too short")
    if hashlib.sha512(plain[64:]).digest() != plain[:64]:
        raise ValueError("Trae auth integrity check failed")
    return plain[64:].decode("utf-8")


def parse_trae_auth_value(value: str) -> dict:
    trimmed = value.strip()
    if trimmed == "":
        raise ValueError("Trae auth value is empty")
    if not trimmed.startswith("{"):
        trimmed = decrypt_trae_storage_value(trimmed)
    return json.loads(trimmed)


# ---------------- 凭据与身份 ----------------

def storage_candidates() -> list[dict]:
    out = []
    base = Path.home() / "Library" / "Application Support"
    for name in APP_NAMES:
        out.append({"edition": name, "path": base / name / "User" / "globalStorage" / "storage.json", "source": "desktop"})
    out.append({"edition": "cli-cn", "path": Path.home() / ".trae-cn" / "trae-jwt-token", "source": "cli"})
    out.append({"edition": "cli", "path": Path.home() / ".trae" / "trae-jwt-token", "source": "cli"})
    return [c for c in out if c["path"].exists()]


def _edition_region(edition: str, auth: dict) -> str:
    ur = auth.get("userRegion")
    raw = ur.get("region") if isinstance(ur, dict) else ur
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low == "cn":
            return "cn"
        if low in ("sg", "ai"):
            return "ai"
    host = (auth.get("host") or "").lower()
    if "trae.ai" in host:
        return "ai"
    return "cn"


def read_desktop_auth(candidate: dict) -> Optional[dict]:
    try:
        storage = json.loads(candidate["path"].read_text(encoding="utf-8"))
    except Exception:
        return None
    value = storage.get(AUTH_STORAGE_KEY)
    if not isinstance(value, str):
        return None
    try:
        auth = parse_trae_auth_value(value)
    except Exception:
        return None
    if not auth.get("token"):
        return None
    machine_id = storage.get("telemetry.machineId") or ""
    device_id = ""
    for key in storage:
        if key.startswith(DC_PREFIX):
            device_id = key[len(DC_PREFIX):]
            break
    app_version = ""
    product = candidate["path"].parents[3] / "Resources" / "app" / "product.json"
    # macOS: <App>.app/Contents/Resources/app/product.json —— storage.json 在
    # User/globalStorage/ 下，向上 4 级是 App 根目录（.../<App>.app/User/globalStorage）
    try:
        p = candidate["path"]
        app_root = p.parents[3]  # .../<App>.app
        product = app_root / "Contents" / "Resources" / "app" / "product.json"
        app_version = json.loads(product.read_text(encoding="utf-8")).get("appVersion") or ""
    except Exception:
        app_version = ""
    build_version = storage.get("iCubeLastVersion") or ""
    return {
        "edition": candidate["edition"],
        "source": candidate["source"],
        "access_token": auth.get("token") or "",
        "refresh_token": auth.get("refreshToken") or "",
        "user_id": str(auth.get("userId") or ""),
        "host": auth.get("host") or GATEWAYS["cn"]["pay"],
        "account": (auth.get("account") or {}).get("username") or "",
        "region": _edition_region(candidate["edition"], auth),
        "expires_at_ms": _parse_time(auth.get("expiredAt")),
        "refresh_expires_at_ms": _parse_time(auth.get("refreshExpiredAt")),
        "machine_id": machine_id,
        "device_id": device_id,
        "app_version": app_version,
        "build_version": build_version,
    }


def _parse_time(v) -> float:
    if isinstance(v, (int, float)) and v > 0:
        return float(v) * 1000 if v < 1e11 else float(v)
    if isinstance(v, str) and v.strip():
        try:
            import datetime
            return datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000
        except Exception:
            return 0.0
    return 0.0


def load_store() -> dict:
    try:
        return json.loads(CREDS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_store(store: dict) -> None:
    CREDS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CREDS_DIR.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CREDS_FILE)


def identity_of(cred: dict) -> dict:
    return {"machineId": cred.get("machine_id", ""), "deviceId": cred.get("device_id", ""),
            "appVersion": cred.get("app_version", ""), "buildVersion": cred.get("build_version", ""),
            "platform": "darwin"}


async def resolve_credential(allow_refresh: bool = True) -> dict:
    """桥缓存优先；没有再读官方 IDE storage；过期则刷新。"""
    store = load_store()
    cred = store.get("credential")
    if cred and cred.get("access_token"):
        if cred.get("expires_at_ms", 0) - 300_000 > time.time() * 1000:
            return cred
        if allow_refresh and cred.get("refresh_token"):
            try:
                return await refresh_credential(cred)
            except Exception as e:
                print(f"[trae2codex] store refresh failed: {e}", flush=True)
    for cand in storage_candidates():
        if cand["source"] != "desktop":
            continue
        c = read_desktop_auth(cand)
        if not c:
            continue
        if c["access_token"] and (not c["expires_at_ms"] or c["expires_at_ms"] - 300_000 > time.time() * 1000):
            save_store({"credential": c, "source": "desktop", "updated": int(time.time())})
            return c
        if allow_refresh and c.get("refresh_token"):
            try:
                fresh = await refresh_credential(c)
                save_store({"credential": fresh, "source": "desktop", "updated": int(time.time())})
                return fresh
            except Exception as e:
                print(f"[trae2codex] refresh from {cand['edition']} failed: {e}", flush=True)
        # token 过期且刷新失败也先返回，让请求以 401 暴露真实状态
        return c
    raise HTTPException(status_code=401, detail="Trae login state not found; 请在 Trae CN IDE 中登录")


async def refresh_credential(cred: dict) -> dict:
    body = {"ClientID": CLIENT_ID, "ClientSecret": "-",
            "RefreshToken": cred.get("refresh_token") or "", "UserID": cred.get("user_id") or ""}
    r = await client().post(f"{cred.get('host') or GATEWAYS['cn']['pay']}{REFRESH_PATH}", json=body)
    if r.status_code != 200:
        raise RuntimeError(f"ExchangeToken http {r.status_code}: {r.text[:200]}")
    result = (r.json() or {}).get("Result") or {}
    token = result.get("Token") or ""
    if not token:
        raise RuntimeError("ExchangeToken returned no token")
    return {**cred,
            "access_token": token,
            "refresh_token": result.get("RefreshToken") or cred.get("refresh_token"),
            "expires_at_ms": _parse_ms(result.get("TokenExpireAt")),
            "refresh_expires_at_ms": _parse_ms(result.get("RefreshExpireAt")) or cred.get("refresh_expires_at_ms", 0)}


def _parse_ms(v) -> float:
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    if isinstance(v, str) and v.strip():
        try:
            import datetime
            return datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000
        except Exception:
            return 0.0
    return 0.0


# ---------------- Trae 请求构造 ----------------

def trae_headers(cred: dict, ident: dict, accept: str = "text/event-stream") -> dict:
    request_id = str(uuid.uuid4())
    trace_id = request_id.replace("-", "")[:32]
    vc = cred.get("build_version") or ""
    version_code = vc if vc.isdigit() else VERSION_CODE_FALLBACK
    h = {
        "Authorization": f"Cloud-IDE-JWT {cred['access_token']}",
        "X-Ide-Token": cred["access_token"],
        "X-Cloudide-Token": cred["access_token"],
        "x-plugin-channel": "icube-ai",
        "User-Agent": f"Trae/{cred.get('app_version') or cred.get('build_version') or '3.3.93'}",
        "x-app-id": "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8",
        "x-machine-id": ident.get("machineId", ""),
        "x-device-id": ident.get("deviceId", ""),
        "x-device-type": "mac",
        "x-os-version": "mac",
        "x-app-version-code": version_code,
        "x-ide-version-code": version_code,
        "x-ide-version-type": "stable",
        "x-custom-trace-id": trace_id,
        "x-flow-traceparent": f"04-{trace_id}-{trace_id[:16]}-01",
        "request-traffic-type": "prod",
        "x-uid": cred.get("user_id", ""),
        "x-request-id": request_id,
        "x-trae-request-id": request_id,
        "Content-Type": "application/json",
        "Accept": accept,
    }
    if cred.get("app_version"):
        h["x-app-version"] = cred["app_version"]
        h["x-ide-version"] = cred["app_version"]
    return h


def gateway(cred: dict) -> str:
    return GATEWAYS[cred.get("region", "cn")]["chat"]


async def fetch_directory(cred: dict) -> dict:
    """POST get_detail_param，合并 solo_work_remote + solo_work_lite 的模型（先到先得）。"""
    ident = identity_of(cred)
    by_id: dict = {}
    for fn in DIRECTORY_FUNCTIONS:
        try:
            r = await client().post(f"{gateway(cred)}{DETAIL_PATH}",
                                    headers=trae_headers(cred, ident, "application/json"),
                                    json={"function": fn, "config_names": None, "need_prompt": False,
                                          "current_config_info": None, "poly_prompt": True,
                                          "mode_type": None, "agent_type": None})
        except Exception:
            continue
        if r.status_code != 200:
            continue
        for cfg in (r.json() or {}).get("config_info_list") or []:
            mid = cfg.get("config_name") or ""
            if not mid or mid in by_id:
                continue
            if mid.startswith("custom_model") or mid in EXCLUDED_CONFIG_IDS:
                continue
            name = ((cfg.get("display_config") or {}).get("display_name") or "")
            if not name or name == "-":
                continue
            details = cfg.get("model_detail_list") or []
            detail = details[0] if details and isinstance(details[0], dict) else {}
            ctx = detail.get("prompt_max_tokens") or (cfg.get("context_window_tokens") or {}).get("dev") or 0
            by_id[mid] = {"id": mid, "function": fn, "name": name,
                          "context_window": int(ctx) if isinstance(ctx, (int, float)) else 0}
    return by_id  # 两个 function 求并集，先到先得（对齐 dsh-connect-trae collectModels）


async def get_catalog(force: bool = False) -> dict:
    async with _state_lock:
        if not force and _catalog["models"] and time.time() - _catalog["ts"] < 300:
            return _catalog["models"]
    try:
        cred = await resolve_credential()
        by_id = await fetch_directory(cred)
        if by_id:
            async with _state_lock:
                _catalog["models"] = by_id
                _catalog["ts"] = time.time()
    except Exception as e:
        print(f"[trae2codex] catalog refresh failed: {e}", flush=True)
    async with _state_lock:
        return _catalog["models"]


def remap_model(model: Optional[str]) -> Optional[str]:
    # 循环剥离：兼容 ocx 发现的双前缀 slug（如 trae/trae-glm-5.2）
    while model and model.startswith(CATALOG_PREFIX):
        model = model[len(CATALOG_PREFIX):]
    return model


def build_chat_body(payload: dict, model: str, fn: str) -> dict:
    messages = []
    for m in payload.get("messages") or []:
        role = m.get("role") or "user"
        if role == "developer":
            role = "system"
        content = m.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        msg = {"role": role, "content": content}
        if role == "tool" and not m.get("tool_call_id"):
            raise HTTPException(status_code=400, detail="tool message requires tool_call_id")
        if role == "assistant" and isinstance(m.get("tool_calls"), list):
            msg["tool_calls"] = [{**tc, "function_call": tc.get("function")} if isinstance(tc, dict) else tc
                                 for tc in m["tool_calls"]]
        messages.append(msg)
    tools = None
    if isinstance(payload.get("tools"), list):
        tools = []
        for t in payload["tools"]:
            fn_obj = (t or {}).get("function") or {}
            tools.append({**t, "function": {**fn_obj,
                            "parameters": json.dumps(fn_obj.get("parameters") or {}, ensure_ascii=False)}})
    body = {
        "messages": messages,
        "model": model,
        "config_name": model,
        "function": fn,
        "stream": True,
    }
    if tools:
        body["tools"] = tools
    if isinstance(payload.get("reasoning_effort"), str):
        body["reasoning_effort"] = payload["reasoning_effort"]
    if isinstance(payload.get("max_tokens"), int):
        body["max_tokens"] = payload["max_tokens"]
    if isinstance(payload.get("temperature"), (int, float)):
        body["temperature"] = payload["temperature"]
    return body


# ---------------- Trae 命名 SSE → OpenAI chunks ----------------

def decode_trae_event(event_name: Optional[str], data: str):
    """返回 (kind, delta_dict | usage_dict | finish_reason)。对齐 bridgeTraeSoloStream 的行为。"""
    if data == "[DONE]":
        return "done", "stop"
    try:
        rec = json.loads(data)
    except Exception:
        return "unknown", None
    if not isinstance(rec, dict):
        return "unknown", None
    if event_name == "request_wait_in_queue":
        return "queue", None
    if event_name == "progress_notice":
        return "progress", None
    if event_name == "token_usage":
        usage = {}
        for src, dst in (("prompt_tokens", "prompt_tokens"), ("completion_tokens", "completion_tokens"),
                         ("total_tokens", "total_tokens"), ("reasoning_tokens", "reasoning_tokens")):
            if isinstance(rec.get(src), (int, float)):
                usage[dst] = rec[src]
        if not usage:
            return "usage", None
        details = {}
        if isinstance(rec.get("cache_read_input_tokens"), (int, float)):
            details["cached_tokens"] = rec["cache_read_input_tokens"]
        if details:
            usage["prompt_tokens_details"] = details
        return "usage", usage
    if event_name == "done" or (isinstance(rec.get("finish_reason"), str) and "response" not in rec):
        return "done", rec.get("finish_reason") or "stop"
    if event_name == "output" or "response" in rec or "reasoning_content" in rec:
        delta = {}
        if isinstance(rec.get("response"), str) and rec["response"] != "":
            delta["content"] = rec["response"]
        if isinstance(rec.get("reasoning_content"), str) and rec["reasoning_content"] != "":
            delta["reasoning_content"] = rec["reasoning_content"]
        tcs = rec.get("tool_calls")
        if isinstance(tcs, list) and tcs:
            norm = []
            for i, tc in enumerate(tcs):
                if not isinstance(tc, dict):
                    continue
                f = tc.get("function_call") or tc.get("function") or {}
                item = {"index": tc.get("index", i)}
                if isinstance(tc.get("id"), str):
                    item["id"] = tc["id"]
                if tc.get("type") == "function":
                    item["type"] = "function"
                if f:
                    item["function"] = {k: f[k] for k in ("name", "arguments") if k in f}
                norm.append(item)
            if norm:
                delta["tool_calls"] = norm
        if not delta:
            return "noop", None
        return "delta", delta
    if event_name == "error" or (isinstance(rec.get("code"), (int, float)) and rec["code"] >= 4000):
        return "error", rec.get("message") or f"Trae upstream error code {rec.get('code')}"
    return "unknown", None


def sse_events(text: str):
    """按 SSE 规范切事件：空行分段，聚合 data: 行，取 event: 名。"""
    events = []
    cur_name, cur_data = None, []
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if line == "":
            if cur_data:
                events.append((cur_name, "\n".join(cur_data)))
            cur_name, cur_data = None, []
            continue
        if line.startswith("event:"):
            cur_name = line[6:].strip()
        elif line.startswith("data:"):
            cur_data.append(line[5:].lstrip(" "))
    if cur_data:
        events.append((cur_name, "\n".join(cur_data)))
    return events


# ---------------- API ----------------

async def session_alive() -> tuple[bool, str]:
    """轻量探测服务端会话是否真的有效：允许刷新后打一次 get_detail_param。"""
    try:
        cred = await resolve_credential()
    except HTTPException as e:
        return False, str(e.detail)
    except Exception as e:
        return False, str(e)[:200]
    try:
        r = await client().post(f"{gateway(cred)}{DETAIL_PATH}",
                                headers=trae_headers(cred, identity_of(cred), "application/json"),
                                json={"function": DEFAULT_FUNCTION, "config_names": None, "need_prompt": False,
                                      "current_config_info": None, "poly_prompt": True,
                                      "mode_type": None, "agent_type": None})
        if r.status_code == 200:
            return True, "ok"
        return False, f"http {r.status_code}: {r.text[:160]}"
    except Exception as e:
        return False, str(e)[:200]


@app.get("/health")
async def health():
    info = {"ok": True, "version": BRIDGE_VERSION, "gateways": GATEWAYS}
    try:
        cred = await resolve_credential(allow_refresh=False)
        info.update({
            "logged_in": bool(cred.get("access_token")),
            "account": cred.get("account") or cred.get("user_id"),
            "region": cred.get("region"),
            "edition": cred.get("edition"),
            "token_expired": bool(cred.get("expires_at_ms") and cred["expires_at_ms"] < time.time() * 1000),
            "expires_at_ms": cred.get("expires_at_ms"),
        })
    except HTTPException as e:
        info.update({"logged_in": False, "detail": e.detail})
    except Exception as e:
        info.update({"logged_in": False, "detail": str(e)[:200]})
    alive, detail = await session_alive()
    info["session_alive"] = alive
    if not alive:
        info["session_detail"] = detail
    info["cached_models"] = sorted((await get_catalog()).keys())
    return info


@app.get("/v1/models")
async def list_models(request: Request):
    check_bridge_auth(request)
    catalog = await get_catalog()
    if catalog:
        data = [{"id": f"{CATALOG_PREFIX}{mid}", "object": "model", "created": 0, "owned_by": "trae",
                 "context_window": (catalog[mid] or {}).get("context_window") or 0,
                 "name": (catalog[mid] or {}).get("name") or mid}
                for mid in catalog]
    else:
        data = [{"id": f"{CATALOG_PREFIX}{m}", "object": "model", "created": 0, "owned_by": "trae"}
                for m in FALLBACK_MODELS]
    return {"object": "list", "data": data}


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

    catalog = await get_catalog()
    entry = catalog.get(model) or {}
    fn = entry.get("function") or DEFAULT_FUNCTION
    body = build_chat_body(payload, model, fn)
    stream = bool(payload.get("stream"))
    cred = await resolve_credential()
    ident = identity_of(cred)
    url = f"{gateway(cred)}{CHAT_PATH}"

    async def attempt(c: dict):
        return await client().send(
            client().build_request("POST", url, json=body, headers=trae_headers(c, ident)),
            stream=True)

    resp = await attempt(cred)
    if resp.status_code in (401, 403):
        await resp.aclose()
        try:
            cred = await refresh_credential(cred)
            save_store({"credential": cred, "source": "store-refresh", "updated": int(time.time())})
        except Exception as e:
            print(f"[trae2codex] refresh before retry failed: {e}", flush=True)
        resp = await attempt(cred)

    if resp.status_code != 200:
        text = (await resp.aread()).decode("utf-8", "replace")[:400]
        await resp.aclose()
        return JSONResponse({"error": {"message": f"trae upstream {resp.status_code}: {text}",
                                       "type": "trae_upstream_error"}},
                            status_code=resp.status_code)

    if stream:
        return StreamingResponse(_sse_pump(resp, model), media_type="text/event-stream")

    # 非流式：聚合 SSE
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    content_parts, reasoning_parts, tool_calls = [], [], []
    usage, finish_reason, err = None, "stop", None
    buf, leftover = "", b""
    async for chunk in resp.aiter_bytes():
        leftover += chunk
        while b"\n" in leftover:
            line, leftover = leftover.split(b"\n", 1)
            buf += line.decode("utf-8", "replace") + "\n"
    await resp.aclose()
    for name, data in sse_events(buf):
        kind, val = decode_trae_event(name, data)
        if kind == "delta":
            if isinstance(val, dict):
                if "content" in val:
                    content_parts.append(val["content"])
                if "reasoning_content" in val:
                    reasoning_parts.append(val["reasoning_content"])
                if "tool_calls" in val:
                    tool_calls.extend(val["tool_calls"])
        elif kind == "usage" and val:
            usage = val
        elif kind == "done":
            finish_reason = val
        elif kind == "error":
            err = val
    if err:
        return JSONResponse({"error": {"message": str(err), "type": "trae_upstream_error"}}, status_code=502)
    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [{**tc, "type": tc.get("type") or "function"} for tc in tool_calls]
    out = {"id": chat_id, "object": "chat.completion", "created": created, "model": model,
           "choices": [{"index": 0, "message": message, "finish_reason":
                        "tool_calls" if tool_calls else finish_reason}],
           }
    if usage:
        out["usage"] = usage
    return out


async def _sse_pump(resp: httpx.Response, model: str):
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def pack(delta, finish=None, usage=None):
        obj = {"id": chat_id, "object": "chat.completion.chunk", "created": created, "model": model,
               "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage:
            obj["usage"] = usage
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()

    try:
        buf, leftover = "", b""
        usage, saw_tools, finished = None, False, False
        async for chunk in resp.aiter_bytes():
            leftover += chunk
            while b"\n" in leftover:
                line, leftover = leftover.split(b"\n", 1)
                buf += line.decode("utf-8", "replace") + "\n"
                if line.strip() == b"":
                    for name, data in sse_events(buf):
                        kind, val = decode_trae_event(name, data)
                        if kind == "delta" and isinstance(val, dict):
                            if "tool_calls" in val:
                                saw_tools = True
                            yield pack(val)
                        elif kind == "usage" and val:
                            usage = val
                        elif kind == "done":
                            if not finished:
                                finished = True
                                yield pack({}, "tool_calls" if saw_tools else (val or "stop"), usage)
                        elif kind == "error":
                            yield pack({}, "stop")
                            err = {"error": {"message": str(val), "type": "trae_upstream_error"}}
                            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode()
                            return
                    buf = ""
        if not finished:
            yield pack({}, "tool_calls" if saw_tools else "stop", usage)
        yield b"data: [DONE]\n\n"
    finally:
        await resp.aclose()


def main():
    global BRIDGE_KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8791)
    args = ap.parse_args()
    print(f"[trae2codex] v{BRIDGE_VERSION} on http://{args.host}:{args.port} key={'set' if BRIDGE_KEY else 'OPEN'}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=True)


if __name__ == "__main__":
    main()
