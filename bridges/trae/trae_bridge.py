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
账号池：本机每套 Trae edition 是一个独立账号，额度互不影响。单个账号配额
        耗尽（Your requests have exceeded the quota）后自动冷却并顺延到下
        一个账号；流式请求在流头窥探阶段完成换号，不把半截响写给客户端。

依赖：fastapi + uvicorn + httpx + cryptography。用法：python3 trae_bridge.py [--port 8791]
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
import _platform
import _common
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

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

client = _common.make_client_getter(**_common.client_kwargs(
    CALL_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"}))

app = _common.make_app("trae2codex", BRIDGE_VERSION)
_state_lock = asyncio.Lock()
_catalog: dict = {"models": {}, "ts": 0.0}   # model_id -> {"function": fn, "name": str}


check_bridge_auth = _common.make_auth_checker(BRIDGE_KEY)


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

def app_support_root() -> Path:
    """应用数据根目录：macOS 是 ~/Library/Application Support。

    app_support_dirs("Trae") 给的是 <root>/Trae，而各版本真实目录是
    <root>/<版本名>，例如
    ~/Library/Application Support/Trae CN/User/globalStorage/storage.json。
    在中间多拼一级 "Trae/" 会让全部桌面候选被 exists() 过滤掉，
    桥只剩凭据缓存兜底，缓存一失效就报「登录态未找到」。
    """
    return Path(_platform.app_support_dirs("Trae")[0]).parent


def storage_candidates() -> list[dict]:
    # macOS  : ~/Library/Application Support/<edition>/User/globalStorage/storage.json
    # Windows: %APPDATA%\<edition>\User\globalStorage\storage.json
    # Some installs nest the IDE folder under a vendor folder (.../Trae/Trae CN),
    # so both roots are probed. Different version dirs can share one app data
    # root, so the IDE dir is unioned with its parent to catch nested layouts.
    out = []
    roots = [Path(app_support_root())]
    for base in _platform.app_data_containers():
        q = Path(base)
        if q not in roots:
            roots.append(q)
    for base in roots:
        for name in APP_NAMES:
            out.append({"edition": name,
                        "path": base / name / "User" / "globalStorage" / "storage.json",
                        "source": "desktop"})
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
    # storage.json 在用户数据目录里，不在 .app 包内，版本号得去别处找：
    # 优先 Applications 下的同名 .app 包，再退回用户数据目录的祖先。
    try:
        p = candidate["path"]
        roots = [Path("/Applications") / (candidate["edition"] + ".app"), p.parents[3]]
        for product in [r / "Contents" / "Resources" / "app" / "product.json" for r in roots]:
            if not product.exists():
                continue
            app_version = json.loads(product.read_text(encoding="utf-8")).get("appVersion") or ""
            if app_version:
                break
    except Exception:
        app_version = ""
    if not app_version:
        # Windows: storage.json lives outside any .app bundle, so the loop above
        # finds nothing; read the version from the real IDE install directory.
        app_version = _ide_app_version(candidate["edition"], candidate["path"])

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


def _ide_app_version(edition: str, storage_path: Path) -> str:
    r"""从 IDE 安装目录读 appVersion（resources/app/product.json）。

    Windows 的 Trae 装在 <盘>:\Programs\<edition>\ 或
    %LOCALAPPDATA%\Programs\<edition>\ 下；macOS 则把 storage.json 放进 .app 内部。
    """
    roots = []
    try:
        roots.append(storage_path.parents[3])
    except IndexError:
        pass
    for env_key in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = os.environ.get(env_key)
        if base:
            roots.append(Path(base) / "Programs" / edition)
            roots.append(Path(base) / edition)
    for code in range(ord("A"), ord("Z") + 1):
        letter = chr(code)
        if not os.path.isdir(letter + ":\\"):
            continue
        roots.append(Path(letter + ":\\Programs\\" + edition))
        roots.append(Path(letter + ":\\Program Files\\" + edition))
    for root in roots:
        try:
            product = root / "resources" / "app" / "product.json"
            if product.is_file():
                return (json.loads(product.read_text(encoding="utf-8")) or {}).get("appVersion") or ""
        except Exception:
            continue
    return ""


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
            "platform": _platform.short_platform()}


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


# ---------------- 凭证池与配额冷却 ----------------
# 2026-10-02 实测：这台 Mac 装了三套 Trae（Trae CN / TRAE SOLO CN / 国际版
# Trae）。CN 账号（用户6781982309）22 个模型全部配额耗尽，国际版账号
# （Q Micheal）额度独立但 coresg-normal.trae.ai 被 TLS 墙挡死。修复前
# resolve_credential 缓存优先、钉死单账号：CN 配额耗尽后所有模型统一报
# quota，池里其他账号永远不会被轮到。现在按活的优先排全部账号，quota
# 错误触发冷却 + 顺延，网络异常也不再 500 而是换号。
QUOTA_COOLDOWN = float(os.environ.get("TRAE_QUOTA_COOLDOWN") or "600")
_quota_dead: dict = {}


def cred_ident(cred: dict) -> str:
    return f"{cred.get('region') or 'cn'}:{cred.get('account') or cred.get('user_id') or ''}"


def is_quota_error(text: str) -> bool:
    low = (text or "").lower()
    return ("quota" in low or "exceeded" in low or "rate limit" in low
            or "too many requests" in low)


def mark_quota_dead(cred: dict) -> None:
    _quota_dead[cred_ident(cred)] = time.time() + QUOTA_COOLDOWN


def is_quota_dead(cred: dict) -> bool:
    return _quota_dead.get(cred_ident(cred), 0.0) > time.time()


def region_first(pool: list, want_region: str) -> list:
    """同区凭证排前：国际模型打国际网关，别拿 CN 模型 id 去挨 param invalid。"""
    if not want_region:
        return pool
    same = [c for c in pool if (c.get("region") or "cn") == want_region]
    rest = [c for c in pool if (c.get("region") or "cn") != want_region]
    return same + rest


async def credential_pool(allow_refresh: bool = True) -> list:
    """全部可用凭证：缓存 + 各桌面 edition，按 (账号, 区域) 去重。

    活的排前、冷却中的垫底，冷却账号只在家底死绝时兜底。
    """
    creds: list = []
    seen: set = set()

    async def offer(cred):
        if cred and cred.get("access_token") and cred_ident(cred) not in seen:
            seen.add(cred_ident(cred))
            creds.append(cred)

    cached = load_store().get("credential")
    if cached and cached.get("access_token"):
        if cached.get("expires_at_ms", 0) - 300_000 > time.time() * 1000:
            await offer(cached)
        elif allow_refresh and cached.get("refresh_token"):
            try:
                await offer(await refresh_credential(cached))
            except Exception as e:
                print(f"[trae2codex] store refresh failed: {e}", flush=True)
    for cand in storage_candidates():
        c = read_desktop_auth(cand)
        if not c:
            continue
        if c["access_token"] and (not c["expires_at_ms"] or c["expires_at_ms"] - 300_000 > time.time() * 1000):
            await offer(c)
        elif allow_refresh and c.get("refresh_token"):
            try:
                await offer(await refresh_credential(c))
            except Exception as e:
                print(f"[trae2codex] refresh from {cand['edition']} failed: {e}", flush=True)
    alive = [c for c in creds if not is_quota_dead(c)]
    return alive + [c for c in creds if is_quota_dead(c)]


async def _peek_quota(ait) -> tuple:
    """窥探 SSE 流头：quota error 返回 (b"", msg)，否则返回 (已读字节, None)。

    Trae 的配额错误是 HTTP 200 流里的 error 事件，等非流式聚合完才换号未免
    太晚，流式请求那时已经开始向客户端写字节，换不了号。窥探阶段只缓冲不产
    出，决策点必然落在首个 delta / error / done 事件上。
    """
    buf = b""
    try:
        async for chunk in ait:
            buf += chunk
            for name, data in sse_events(buf.decode("utf-8", "replace")):
                kind, val = decode_trae_event(name, data)
                if kind == "error":
                    msg = str(val)
                    return (b"", msg) if is_quota_error(msg) else (buf, None)
                if kind in ("delta", "done"):
                    return buf, None
        return buf, None
    except Exception:
        return buf, None


class _ReplayResponse:
    """把窥探阶段读走的字节接回流里，对 _sse_pump 伪装成 httpx.Response。"""

    def __init__(self, resp, prefix: bytes, ait):
        self._resp, self._prefix, self._ait = resp, prefix, ait

    async def aiter_bytes(self):
        if self._prefix:
            yield self._prefix
        async for chunk in self._ait:
            yield chunk

    async def aclose(self):
        await self._resp.aclose()


# ---------------- Trae 请求构造 ----------------

def trae_headers(cred: dict, ident: dict, accept: str = "text/event-stream") -> dict:
    request_id = str(uuid.uuid4())
    trace_id = request_id.replace("-", "")[:32]
    # HTTP 头只能走 ASCII：IDE 落盘的 userId 一般是数字，但 account.username 可能是
    # 中文昵称，混进 x-uid 会让 httpx 抛 UnicodeEncodeError，请求根本发不出去。
    uid = "".join(ch for ch in str(cred.get("user_id") or "") if " " <= ch <= "~")
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
        "x-uid": uid,
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
    # 逐凭证合并目录：本机 Trae CN 与国际版各暴露自己的模型集，过去只用
    # resolve_credential() 的单一缓存凭证（CN），国际版模型即使可达也进不了
    # 目录，聊天时拿 CN 模型 id 打国际网关只会得到 param invalid。合并后每个
    # model 带 region 标签，chat_completions 按标签优先路由到同区凭证。
    try:
        pool = await credential_pool()
        by_id: dict = {}
        for cred in pool:
            region = cred.get("region") or "cn"
            try:
                part = await fetch_directory(cred)
            except Exception as e:
                print(f"[trae2codex] directory {cred_ident(cred)} failed: {e}", flush=True)
                continue
            for mid, entry in (part or {}).items():
                if mid in by_id:
                    continue
                tagged = dict(entry)
                tagged["region"] = region
                by_id[mid] = tagged
        if by_id:
            async with _state_lock:
                _catalog["models"] = by_id
                _catalog["ts"] = time.time()
    except Exception as e:
        print(f"[trae2codex] catalog refresh failed: {e}", flush=True)
    async with _state_lock:
        return _catalog["models"]


# 循环剥离：兼容 ocx 发现的双前缀 slug（如 trae/trae-glm-5.2）
remap_model = _common.make_prefix_stripper(CATALOG_PREFIX)


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
    try:
        pool = await credential_pool(allow_refresh=False)
    except Exception:
        pool = []
    info["accounts"] = [{"account": c.get("account") or c.get("user_id"),
                         "region": c.get("region"), "edition": c.get("edition"),
                         "quota_dead": is_quota_dead(c)} for c in pool]
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
    pool = await credential_pool()
    if not pool:
        raise HTTPException(status_code=401,
                            detail="Trae login state not found; 请在 Trae CN IDE 中登录")
    want_region = entry.get("region")
    if want_region:
        pool = region_first(pool, want_region)
    last_err = ""
    notes: list = []
    for cred in pool:
        ident = identity_of(cred)
        url = f"{gateway(cred)}{CHAT_PATH}"

        async def attempt(c=cred, u=url):
            return await client().send(
                client().build_request("POST", u, json=body, headers=trae_headers(c, ident)),
                stream=True)

        try:
            resp = await attempt()
        except Exception as e:
            last_err = f"trae upstream unreachable: {type(e).__name__}: {e}"
            reason = ("凭证非法" if isinstance(e, UnicodeEncodeError) else "网络不可达")
            notes.append(f"{cred_ident(cred)} {reason}({type(e).__name__})")
            print(f"[trae2codex] {cred_ident(cred)} unreachable: {last_err}", flush=True)
            continue
        if resp.status_code in (401, 403):
            await resp.aclose()
            try:
                cred = await refresh_credential(cred)
                save_store({"credential": cred, "source": "store-refresh", "updated": int(time.time())})
            except Exception as e:
                print(f"[trae2codex] refresh before retry failed: {e}", flush=True)
            try:
                resp = await attempt(c=cred)
            except Exception as e:
                last_err = f"trae upstream unreachable: {type(e).__name__}: {e}"
                reason = ("凭证非法" if isinstance(e, UnicodeEncodeError)
                          else "网络不可达")
                notes.append(f"{cred_ident(cred)} {reason}({type(e).__name__})")
                continue

        if resp.status_code != 200:
            text = (await resp.aread()).decode("utf-8", "replace")[:400]
            await resp.aclose()
            last_err = f"trae upstream {resp.status_code}: {text}"
            if is_quota_error(text):
                mark_quota_dead(cred)
                notes.append(f"{cred_ident(cred)} 配额耗尽(冷却 {QUOTA_COOLDOWN:.0f}s)")
                print(f"[trae2codex] quota dead on {cred_ident(cred)}; "
                      f"cooling {QUOTA_COOLDOWN:.0f}s, {len(pool) - 1} credential(s) left", flush=True)
                continue
            if resp.status_code in (400, 422):
                # 请求体本身的问题：换号没有意义，原样带上游状态码返回。
                return JSONResponse({"error": {"message": last_err,
                                               "type": "trae_upstream_error"}},
                                    status_code=resp.status_code)
            notes.append(f"{cred_ident(cred)} 上游 {resp.status_code}")
            return JSONResponse({"error": {"message": last_err,
                                           "type": "trae_upstream_error"}},
                                status_code=resp.status_code)

        # Trae 用 HTTP 200 + SSE error 事件报配额耗尽，非流式聚合后才看得到。
        # 先窥探流头：quota 就冷却换号，否则把已读字节接回去继续走原链路。
        ait = resp.aiter_bytes()
        prefix, quota_msg = await _peek_quota(ait)
        if quota_msg:
            await resp.aclose()
            mark_quota_dead(cred)
            last_err = f"trae quota: {quota_msg}"
            notes.append(f"{cred_ident(cred)} 配额耗尽(冷却 {QUOTA_COOLDOWN:.0f}s)")
            print(f"[trae2codex] quota dead on {cred_ident(cred)} (stream head); "
                  f"cooling {QUOTA_COOLDOWN:.0f}s", flush=True)
            continue
        resp = _ReplayResponse(resp, prefix, ait)
        break
    else:
        detail = "；".join(notes) if notes else ""
        suffix = f"：{detail}" if detail else ""
        return JSONResponse({"error": {"message": (last_err or "trae: no usable credential") + suffix,
                                       "type": "trae_upstream_error"}},
                            status_code=502)

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
    _common.serve(
        app, 8791,
        f"[trae2codex] v{BRIDGE_VERSION} on http://%s:%s "
        f"key={'set' if BRIDGE_KEY else 'OPEN'}")


if __name__ == "__main__":
    main()
