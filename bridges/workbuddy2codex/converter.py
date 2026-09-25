#!/usr/bin/env python3
"""
codebuddy2openai — 把 CodeBuddy / WorkBuddy 的订阅暴露成标准 OpenAI 兼容 API。

原理（直连后端，原生 function calling）：
  - 读取本机已登录的 CodeBuddy 桌面端凭据（auth 文件里的 token / uid / enterpriseId）。
  - 直接转发到 CodeBuddy 后端 `https://copilot.tencent.com/v2/chat/completions`。
    该后端本身就是标准 OpenAI chat/completions 协议（含原生 tools / tool_calls / SSE 流式）。
  - 转换器只做两件事：①注入鉴权 header（Authorization / X-User-Id 等）
    ②在本地 /v1/* 与后端 /v2/* 之间做路径映射与透传。
  - token 过期时自动调 `/v2/plugin/auth/token/refresh` 刷新，并回写 auth 文件。

跨平台：自动定位 auth 目录（macOS / Windows / Linux）。
依赖：fastapi + uvicorn + httpx（pip install fastapi "uvicorn[standard]" httpx）。

用法：
  python3 converter.py                       # 默认 127.0.0.1:8787
  python3 converter.py --port 9000
  python3 converter.py --api-key mysecret    # 启用客户端鉴权
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hmac
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
import uvicorn

from account_pool import AccountPool, harden_private_path
from dashboard import DASHBOARD_HTML
from workbuddy_account_service import WorkBuddyAccountService
from workbuddy_checkin import WorkBuddyCheckinService

try:
    from desensitize import desensitize_body
except ImportError:  # 模块缺失时降级为不脱敏
    def desensitize_body(body, roles=("system",)):
        return body

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
VERSION_PATH = BASE_DIR / "VERSION"


def _load_version() -> str:
    try:
        version = VERSION_PATH.read_text(encoding="utf-8").strip()
        return version or "0.0.0+unknown"
    except OSError:
        return "0.0.0+unknown"


BRIDGE_VERSION = _load_version()
BACKEND = "https://www.workbuddy.ai"
# ---------------------------------------------------------------------------
# 海外后端网络适配（MacPacket fake-ip / 系统代理 / uvloop）：
#  1) fake-ip DNS：www.workbuddy.ai 被解析成 198.18.x，经 DoH 取真实 IP 并
#     在 socket.getaddrinfo 层固定，所有 httpx Client 自动生效，周期性刷新。
#  2) 系统代理：macOS"系统设置"里的 127.0.0.1:1082 对海外后端返回 503，
#     强制桥内所有 httpx Client 直连（trust_env=False），DoH 查询也走直连。
#  3) uvloop：uvloop 的 TLS 握手会被海外上游直接 EOF（uvicorn 默认 loop=auto
#     会优先使用 uvloop），因此入口处强制 asyncio 事件循环。
# ---------------------------------------------------------------------------
import socket as _socket
import urllib.request as _urllib_request

_PIN_HOSTS: tuple[str, ...] = (urlparse(BACKEND).hostname or "www.workbuddy.ai",)
_PIN_DOH_TEMPLATES = (
    "https://doh.pub/dns-query?name={host}&type=A",
    "https://dns.alidns.com/dns-query?name={host}&type=A",
    "https://1.1.1.1/dns-query?name={host}&type=A",
)
_PIN_STATIC_FALLBACK = ("43.160.158.125",)
_PIN_REFRESH_SECONDS = int(os.environ.get("WORKBUDDY_PIN_REFRESH_SECONDS", "600"))
_pin_lock = threading.Lock()
_pin_map: dict[str, str] = {}
_pin_original_getaddrinfo = _socket.getaddrinfo


def _pin_doh_lookup(host: str) -> str:
    """经公共 DoH 解析 A 记录，返回第一个 IPv4 地址。"""
    for template in _PIN_DOH_TEMPLATES:
        try:
            request = _urllib_request.Request(
                template.format(host=host),
                headers={"accept": "application/dns-json", "user-agent": USER_AGENT},
            )
            with _doh_opener.open(request, timeout=6) as response:
                payload = json.loads(response.read().decode("utf-8", "ignore"))
            for answer in payload.get("Answer") or []:
                if answer.get("type") == 1 and answer.get("data"):
                    return str(answer["data"])
        except Exception:
            continue
    return ""


def _pin_tcp_reachable(host: str, port: int = 443, timeout: float = 4.0) -> bool:
    try:
        with _socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _pin_resolve(host: str) -> str:
    override = os.environ.get("WORKBUDDY_PINNED_IP", "").strip()
    candidates = [override] if override else []
    candidates.append(_pin_doh_lookup(host))
    candidates.extend(_PIN_STATIC_FALLBACK)
    for candidate in candidates:
        if candidate and _pin_tcp_reachable(candidate):
            return candidate
    return ""


def _pin_refresh() -> None:
    for host in _PIN_HOSTS:
        address = _pin_resolve(host)
        if not address:
            continue
        with _pin_lock:
            changed = _pin_map.get(host) != address
            _pin_map[host] = address
        if changed:
            sys.stderr.write(f"[dns-pin] {host} -> {address}\n")
            sys.stderr.flush()


def _pinned_getaddrinfo(host, *args, **kwargs):
    if isinstance(host, bytes):
        try:
            host = host.decode()
        except Exception:
            host = None
    if isinstance(host, str):
        with _pin_lock:
            pinned = _pin_map.get(host)
        if pinned:
            host = pinned
    return _pin_original_getaddrinfo(host, *args, **kwargs)


_socket.getaddrinfo = _pinned_getaddrinfo


def _pin_install_no_proxy() -> None:
    """macOS 上 httpx/urllib 会把"系统设置"里的 HTTP 代理（127.0.0.1:1082）
    当作全局代理，而该代理访问海外后端返回 503。桥本身是直连后端，这里强制
    所有 httpx Client 直连（trust_env=False），避免再被系统代理劫持。"""
    for client_cls in (httpx.Client, httpx.AsyncClient):
        original_init = client_cls.__init__

        def _forced_direct_init(self, *args, _original=original_init, **kwargs):
            kwargs["trust_env"] = False
            _original(self, *args, **kwargs)

        client_cls.__init__ = _forced_direct_init


try:
    _pin_install_no_proxy()
except Exception as _exc:  # pragma: no cover
    sys.stderr.write(f"[dns-pin] no-proxy init failed: {_exc}\n")

# DoH 查询同样直连，避开系统代理拦截
_doh_opener = _urllib_request.build_opener(_urllib_request.ProxyHandler({}))

try:
    _pin_refresh()
except Exception as _exc:  # 初始化失败时保持原有解析行为
    sys.stderr.write(f"[dns-pin] init failed: {_exc}\n")


def _pin_loop() -> None:
    while True:
        time.sleep(_PIN_REFRESH_SECONDS)
        try:
            _pin_refresh()
        except Exception:
            pass


threading.Thread(target=_pin_loop, name="workbuddy-dns-pin", daemon=True).start()

DEFAULT_DOMAIN = "www.codebuddy.cn"
USER_AGENT = f"workbuddy2codex/{BRIDGE_VERSION}"
# 海外版后端强制请求首条为 system prompt，缺失时注入该占位
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
AUTH_POOL_DIR = Path(os.environ.get("WORKBUDDY_AUTH_POOL_DIR", str(BASE_DIR / "auths")))
DASHBOARD_SESSION = secrets.token_urlsafe(32)

# 腾讯上游内容审核对 Codex 的 skills 列表（几十个工具描述）会概率性误判为
# “敏感内容”。转发给 WorkBuddy 上游前剥离 <skills_instructions> 块，可大幅
# 降低触发概率（实测剥离后 4/4 通过，未剥离约 80% 拦截）。
_SKILLS_BLOCK_RE = re.compile(r"<skills_instructions>.*?</skills_instructions>", re.S)
_SKILLS_HEADER_RE = re.compile(r"## Skills\s*", re.S)


def _strip_skills_from_text(text: str) -> str:
    """Remove Codex skills blocks that trigger Tencent content moderation."""
    text = _SKILLS_BLOCK_RE.sub("", text)
    text = _SKILLS_HEADER_RE.sub("", text)
    return text


def _strip_skills_from_messages(messages: list) -> list:
    """Apply skills stripping to system/developer messages before upstream."""
    out = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        role = m.get("role")
        if role not in {"system", "developer"}:
            out.append(m)
            continue
        m2 = dict(m)
        c = m2.get("content")
        if isinstance(c, str):
            m2["content"] = _strip_skills_from_text(c)
        elif isinstance(c, list):
            newc = []
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") in {"input_text", "text"}:
                    newc.append({**blk, "text": _strip_skills_from_text(str(blk.get("text") or ""))})
                else:
                    newc.append(blk)
            m2["content"] = newc
        out.append(m2)
    return out


# --- external API gateways (multiple vendors, parallel to WorkBuddy account pool) ---
GATEWAYS_CONFIG_PATH = BASE_DIR / "auths" / "gateways.json"


def _read_gateways_raw() -> list:
    """Read raw gateway list from disk (or [] on any error)."""
    try:
        raw = json.loads(GATEWAYS_CONFIG_PATH.read_text(encoding="utf-8"))
        items = raw.get("gateways") if isinstance(raw, dict) else raw
        return items if isinstance(items, list) else []
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def _write_gateways_raw(items: list) -> None:
    """Atomically write the gateway list (non-secret fields only)."""
    GATEWAYS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"gateways": items}
    tmp = GATEWAYS_CONFIG_PATH.with_suffix(GATEWAYS_CONFIG_PATH.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, GATEWAYS_CONFIG_PATH)


def _public_gateway(gw: dict) -> dict:
    """Non-secret projection for the dashboard (never includes api_key_env value)."""
    return {
        "id": gw.get("id"),
        "name": gw.get("name"),
        "prefix": gw.get("prefix"),
        "base_url": gw.get("base_url"),
        "home_url": gw.get("home_url"),
        "models": gw.get("models"),
        "image_models": gw.get("image_models"),
        "configured": gw.get("configured"),
        "protocol": gw.get("protocol") or "auto",
        "enabled": bool(gw.get("enabled", True)),
        "key_source": "env" if os.environ.get(gw.get("api_key_env") or "", "").strip() else ("file" if gw.get("api_key") else None),
    }


def _load_gateways() -> list:
    """Load external API-key gateways from auths/gateways.json (array form)."""
    try:
        raw = json.loads(GATEWAYS_CONFIG_PATH.read_text(encoding="utf-8"))
        items = raw.get("gateways") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            gw_id = str(item.get("id") or "").strip()
            models_raw = item.get("models")
            models = [str(m).strip() for m in models_raw if str(m).strip()] if isinstance(models_raw, list) else []
            image_raw = item.get("image_models")
            image_models = [str(m).strip() for m in image_raw if str(m).strip()] if isinstance(image_raw, list) else []
            base_url = str(item.get("base_url") or "").strip()
            if not gw_id or (not models and not image_models) or not base_url:
                continue
            key_env = str(item.get("api_key_env") or f"{gw_id.upper()}_STORE_KEY").strip()
            stored_key = str(item.get("api_key") or "").strip()
            protocol = str(item.get("protocol") or "auto")
            # 默认开关：显式 enabled 优先；否则 responses 协议默认关闭、chat/auto 默认开启
            enabled_default = protocol != "responses"
            enabled = bool(item.get("enabled", enabled_default))
            result.append({
                "id": gw_id,
                "name": str(item.get("name") or gw_id),
                "prefix": str(item.get("prefix") or gw_id).upper(),
                "api_key_env": key_env,
                "base_url": base_url.rstrip("/"),
                "home_url": str(item.get("home_url") or "").strip(),
                "models": models,
                "image_models": image_models,
                "api_key": stored_key,
                "protocol": protocol,
                "enabled": enabled,
                "configured": bool(os.environ.get(key_env, "").strip() or stored_key),
            })
        return result
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


# ---------------------------------------------------------------------------
# 平台相关：定位 auth 目录
# ---------------------------------------------------------------------------

def auth_dirs() -> list[Path]:
    home = Path.home()
    plat = sys.platform
    if plat == "darwin":
        return [home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    if plat == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return [local / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return [xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth"]


def find_auth_file() -> Path | None:
    candidates: list[Path] = []
    for d in auth_dirs():
        if d.is_dir():
            candidates.extend(d.glob("*.info"))
    try:
        ai_pool = [p for p in candidates if p.name.endswith("-ai.info")]
        pool = ai_pool or candidates
        return max(pool, key=lambda path: path.stat().st_mtime) if pool else None
    except OSError:
        return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Auth 凭据管理（读 + 自动刷新 + 回写）
# ---------------------------------------------------------------------------

class CredentialManager:
    """从 auth 文件读取凭据；token 临近过期时自动刷新并回写。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._mtime: float = 0.0

    def _read_raw(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_if_stale(self):
        """若文件 mtime 变了（外部刷新过），重新加载缓存。"""
        try:
            mt = self.path.stat().st_mtime
        except OSError as exc:
            self._cached = None
            self._mtime = 0.0
            raise RuntimeError(f"凭据文件不存在或不可读：{self.path}") from exc
        if self._cached is None or mt != self._mtime:
            self._cached = self._read_raw()
            self._mtime = mt

    def _session(self) -> dict:
        self._load_if_stale()
        if self._cached is None:
            raise RuntimeError(f"无法读取 auth 文件：{self.path}")
        return self._cached

    def _is_expired(self) -> bool:
        s = self._session()
        expires_at = (s.get("auth") or {}).get("expiresAt") or 0
        # 提前 60s 判定过期
        return time.time() * 1000 >= (expires_at - 60_000)

    def _refresh(self):
        """调后端刷新 token，写回 auth 文件与缓存。"""
        s = self._session()
        auth = s.get("auth") or {}
        headers = self._build_headers_from(auth, s.get("account") or {})
        headers["X-Refresh-Token"] = auth.get("refreshToken", "")
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = f"{BACKEND}/v2/plugin/auth/token/refresh"
        try:
            with httpx.Client(timeout=15) as c:
                r = c.post(url, headers=headers, json={})
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"刷新 token 网络失败：{e}")
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(f"刷新 token 失败：{data.get('msg', data)}")
        new_auth = data["data"]
        # 继承部分字段
        new_auth["domain"] = new_auth.get("domain") or auth.get("domain")
        new_auth["lastRefreshTime"] = int(time.time() * 1000)
        # 计算 expiresAt（若后端没直接给）
        if not new_auth.get("expiresAt") and new_auth.get("expiresIn"):
            new_auth["expiresAt"] = int(time.time() * 1000) + new_auth["expiresIn"] * 1000
        if not new_auth.get("refreshExpiresAt") and new_auth.get("refreshExpiresIn"):
            new_auth["refreshExpiresAt"] = int(time.time() * 1000) + new_auth["refreshExpiresIn"] * 1000
        s["auth"] = new_auth
        # 原子写回
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        harden_private_path(tmp)
        os.replace(tmp, self.path)
        harden_private_path(self.path)
        self._cached = s
        self._mtime = self.path.stat().st_mtime

    def _build_headers_from(self, auth: dict, account: dict) -> dict:
        domain = auth.get("domain") or DEFAULT_DOMAIN
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {auth.get('accessToken','')}",
            "X-User-Id": account.get("uid", ""),
            "X-Enterprise-Id": account.get("enterpriseId", ""),
            "X-Tenant-Id": account.get("enterpriseId", ""),
            "X-Domain": domain,
            "User-Agent": USER_AGENT,
        }
        return h

    def get_headers(self) -> dict:
        """返回带最新 token 的后端请求 header；必要时先刷新。"""
        with self._lock:
            if self._is_expired():
                self._refresh()
            s = self._session()
            return self._build_headers_from(s.get("auth") or {}, s.get("account") or {})

    def summary(self) -> dict:
        s = self._session()
        auth = s.get("auth") or {}
        acct = s.get("account") or {}
        exp = auth.get("expiresAt", 0)
        return {
            "uid": acct.get("uid"),
            "nickname": acct.get("nickname"),
            "enterpriseName": acct.get("enterpriseName"),
            "token_expires_at": exp,
            "token_expired": self._is_expired(),
        }

    def session_snapshot(self) -> dict:
        """Return an in-memory copy for trusted local account services."""
        with self._lock:
            self._load_if_stale()
            if self._cached is None:
                raise RuntimeError(f"无法读取 auth 文件：{self.path}")
            return json.loads(json.dumps(self._cached))


# ---------------------------------------------------------------------------
# 模型列表
# ---------------------------------------------------------------------------

DEFAULT_MODELS = [
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-5.5", "gpt-5.4", "gpt-5.3-codex",
    "hy4-preview", "hy3",
    "gemini-3.5-flash", "glm-5.3", "glm-5.2", "kimi-k3", "kimi-k2.6",
]

DEFAULT_ROUTE_MODEL = "gpt-5.3-codex"

# WorkBuddy 模型识图能力(基于 2026-08-07 128x128 纯色图实测)
# yes=可靠识图  weak=能收图但识别不可靠  no=基本不识别(会回复未收到图片/空)
MODEL_VISION_CAPABILITY = {
    "hy3": "yes",
    "hy4-preview": "yes",
    "gpt-6-astra": "yes", "gpt-5.6-sol": "yes", "gpt-5.6-terra": "yes",
    "gpt-5.6-luna": "yes", "gpt-5.5": "yes", "gpt-5.4": "yes", "gpt-5.3-codex": "yes",
    "gemini-3.5-flash": "yes", "glm-5.3": "yes", "kimi-k3": "yes", "kimi-k2.6": "yes",
    "glm-5v-turbo": "yes",
    "glm-5.2": "weak",
    "glm-5.1": "no",
    "kimi-k3-1": "yes",
    "kimi-k2.7": "yes",
    "kimi-k2.6": "yes",
    "minimax-m3": "weak",
    "deepseek-v4-pro": "yes",
    "deepseek-v4-flash": "weak",
    "auto": "yes",
    "hunyuan-image-v3.0-art": "no",  # 生图模型,不识别图片
}
SETTINGS_PATH = BASE_DIR / "bridge-settings.json"
LOGO_PATH = BASE_DIR / "assets" / "bridge-logo.png"
WORKBUDDY_LOCAL_STORAGE = Path(os.environ.get(
    "WORKBUDDY_LOCAL_STORAGE",
    str(Path.home() / ".workbuddy" / "local_storage"),
))
OPENCODEX_BASE_URL = os.environ.get("OPENCODEX_BASE_URL", "http://127.0.0.1:10100").rstrip("/")
OPENCODEX_ADMIN_TOKEN_PATH = Path(os.environ.get(
    "OPENCODEX_ADMIN_TOKEN_FILE",
    str(Path.home() / ".opencodex" / "admin-api-token"),
))
_SETTINGS_LOCK = threading.Lock()
_SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _promotion_timezone(name: object):
    if not isinstance(name, str) or name == "Asia/Shanghai":
        return _SHANGHAI_TZ
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return _SHANGHAI_TZ


def _display_credits(value: object) -> str | None:
    """Normalize WorkBuddy's x0.79 notation to the UI's 0.79x notation."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text[:1].lower() == "x":
        return f"{text[1:].strip()}x"
    return text


def _parse_hhmm(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        hour_text, minute_text = value.strip().split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _promotion_active(promotion: dict, now: datetime | None = None) -> bool:
    """Apply the same validity and daily-window rules as WorkBuddy's model UI."""
    if promotion.get("enabled") is False:
        return False
    schedule = promotion.get("schedule")
    if not isinstance(schedule, dict):
        return True
    zone = _promotion_timezone(schedule.get("timezone"))
    current = (now or datetime.now(zone)).astimezone(zone)

    def parse_iso(value: object) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=zone)
            return parsed.astimezone(zone)
        except ValueError:
            return None

    valid_from = parse_iso(schedule.get("validFrom"))
    valid_until = parse_iso(schedule.get("validUntil"))
    if valid_from and current < valid_from:
        return False
    if valid_until and current >= valid_until:
        return False

    daily = schedule.get("daily")
    if not isinstance(daily, list) or not daily:
        return True
    minute = current.hour * 60 + current.minute
    for window in daily:
        if not isinstance(window, dict):
            continue
        start = _parse_hhmm(window.get("start"))
        end = _parse_hhmm(window.get("end"))
        if start is None or end is None:
            continue
        if start == end:
            return True
        if start < end and start <= minute < end:
            return True
        if start > end and (minute >= start or minute < end):
            return True
    return False


def _workbuddy_cache_file() -> Path | None:
    """Return the newest product-config cache without modifying WorkBuddy files."""
    if not WORKBUDDY_LOCAL_STORAGE.is_dir():
        return None
    try:
        candidates = {
            *WORKBUDDY_LOCAL_STORAGE.glob("entry_*.info"),
            *WORKBUDDY_LOCAL_STORAGE.glob("wb_entry_*.info"),
        }
        for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                root = json.loads(path.read_text(encoding="utf-8"))
                envelope = root[0] if isinstance(root, list) and root else root
                data = envelope.get("data") if isinstance(envelope, dict) else None
                if isinstance(data, dict) and isinstance(data.get("models"), list):
                    return path
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
    except OSError:
        return None
    return None


def _load_workbuddy_model_metadata() -> tuple[dict[str, dict], dict]:
    """Read only display-safe model metadata from WorkBuddy's own local cache."""
    cache_file = _workbuddy_cache_file()
    if cache_file is None:
        return {}, {"available": False}
    try:
        root = json.loads(cache_file.read_text(encoding="utf-8"))
        envelope = root[0] if isinstance(root, list) and root else root
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(data, dict):
            raise ValueError("cache payload missing data")
        raw_models = data.get("models")
        global_promotions = data.get("modelPromotions")
        if not isinstance(raw_models, list):
            raise ValueError("cache payload missing models")
        if not isinstance(global_promotions, list):
            global_promotions = []

        result: dict[str, dict] = {}
        now = datetime.now(_SHANGHAI_TZ)
        for raw_model in raw_models:
            if not isinstance(raw_model, dict):
                continue
            model_id = raw_model.get("id")
            if model_id not in DEFAULT_MODELS:
                continue

            badges: list[dict] = []
            seen_badges: set[str] = set()
            tags = raw_model.get("tags")
            if isinstance(tags, list):
                for tag in tags:
                    if not isinstance(tag, str) or not tag.startswith("badge:"):
                        continue
                    _, label, *color = tag.split(":")
                    if label and label not in seen_badges:
                        badges.append({
                            "label": label,
                            "color": color[0] if color else None,
                            "active": True,
                            "source": "model",
                        })
                        seen_badges.add(label)

            promotions: list[dict] = []
            for promotion in global_promotions:
                if isinstance(promotion, dict) and model_id in (promotion.get("modelIds") or []):
                    promotions.append(promotion)
            local_promotions = raw_model.get("promotions")
            if isinstance(local_promotions, list):
                promotions.extend(item for item in local_promotions if isinstance(item, dict))

            enabled = [item for item in promotions if item.get("enabled") is not False]
            active_items = [item for item in enabled if _promotion_active(item, now)]
            candidates = active_items or enabled
            primary = max(candidates, key=lambda item: item.get("priority") or 0) if candidates else None
            promotion_display = None
            if primary:
                is_active = _promotion_active(primary, now)
                badge = primary.get("badge") if isinstance(primary.get("badge"), dict) else {}
                badge_label = badge.get("label")
                show_badge = badge.get("display") == "always" or is_active
                if isinstance(badge_label, str) and badge_label and show_badge and badge_label not in seen_badges:
                    badges.append({
                        "label": badge_label,
                        "color": badge.get("color"),
                        "active": is_active,
                        "source": "promotion",
                    })
                    seen_badges.add(badge_label)
                discount = primary.get("discount") if isinstance(primary.get("discount"), dict) else {}
                hover = primary.get("hover") if isinstance(primary.get("hover"), dict) else {}
                promotion_display = {
                    "id": primary.get("id"),
                    "active": is_active,
                    "kind": primary.get("kind"),
                    "label": badge_label,
                    "discounted_credits": _display_credits(discount.get("discountedCredits")) if is_active else None,
                    "note": hover.get("textZh") if is_active else None,
                }

            result[model_id] = {
                "name": raw_model.get("name") or model_id,
                "credits": _display_credits(raw_model.get("credits")),
                "credits_dynamic": model_id == "auto" or raw_model.get("credits") is None,
                "description": raw_model.get("descriptionZh") or raw_model.get("description"),
                "badges": badges,
                "promotion": promotion_display,
                "vision": MODEL_VISION_CAPABILITY.get(model_id, "yes" if raw_model.get("supportsImages") else "no"),
                "image_generation": model_id == "hunyuan-image-v3.0-art",
            }
        return result, {
            "available": True,
            "source": "WorkBuddy 本机缓存",
            "updated_at": datetime.fromtimestamp(cache_file.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}, {"available": False}


def _load_route_model() -> str:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        model = data.get("route_model")
        if model in DEFAULT_MODELS:
            return model
    except (OSError, ValueError, TypeError):
        pass
    return DEFAULT_ROUTE_MODEL


def _save_route_model(model: str):
    data = {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data["route_model"] = model
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = SETTINGS_PATH.with_suffix(SETTINGS_PATH.suffix + ".tmp")
    with _SETTINGS_LOCK:
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, SETTINGS_PATH)


DEFAULT_IMAGE_ROUTE_MODEL = "hunyuan-image-v3.0-art"


def _load_image_route_model() -> str:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        model = str(data.get("image_route_model") or "").strip()
        if model:
            return model
    except (OSError, ValueError, TypeError):
        pass
    return DEFAULT_IMAGE_ROUTE_MODEL


def _save_image_route_model(model: str):
    data = {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data["image_route_model"] = model
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = SETTINGS_PATH.with_suffix(SETTINGS_PATH.suffix + ".tmp")
    with _SETTINGS_LOCK:
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, SETTINGS_PATH)


def _resolve_route_model(requested_model: str) -> str:
    model = str(requested_model or "").strip()
    if model.lower().startswith("workbuddy/"):
        model = model.split("/", 1)[1]
    if model in DEFAULT_MODELS:
        return model
    return CONFIG.get("route_model") or DEFAULT_ROUTE_MODEL


class OpenCodexSyncError(RuntimeError):
    pass


def _opencodex_headers() -> dict[str, str]:
    try:
        token = OPENCODEX_ADMIN_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise OpenCodexSyncError("OpenCodex 管理令牌不可用") from exc
    if not token:
        raise OpenCodexSyncError("OpenCodex 管理令牌为空")
    return {"x-opencodex-api-key": token}


async def _fetch_workbuddy_provider(client: httpx.AsyncClient, headers: dict[str, str]) -> dict:
    try:
        response = await client.get(f"{OPENCODEX_BASE_URL}/api/providers", headers=headers)
        response.raise_for_status()
        providers = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OpenCodexSyncError("无法读取 OpenCodex Provider") from exc
    if not isinstance(providers, list):
        raise OpenCodexSyncError("OpenCodex Provider 响应格式无效")
    provider = next(
        (item for item in providers if isinstance(item, dict) and item.get("name") == "workbuddy"),
        None,
    )
    if provider is None:
        raise OpenCodexSyncError("OpenCodex 中未找到 workbuddy Provider")
    return provider


async def _set_opencodex_default_model(model: str) -> str | None:
    headers = _opencodex_headers()
    async with httpx.AsyncClient(timeout=10) as client:
        before = await _fetch_workbuddy_provider(client, headers)
        previous = before.get("defaultModel")
        if previous != model:
            try:
                response = await client.patch(
                    f"{OPENCODEX_BASE_URL}/api/providers",
                    params={"name": "workbuddy"},
                    headers=headers,
                    json={"defaultModel": model},
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise OpenCodexSyncError("无法更新 OpenCodex 默认模型") from exc
        after = await _fetch_workbuddy_provider(client, headers)
        if after.get("defaultModel") != model:
            raise OpenCodexSyncError("OpenCodex 默认模型更新后校验失败")
        return previous if isinstance(previous, str) else None

# 后端请求体里出现过的额外字段（透传时若客户端给了就保留）
PASSTHROUGH_BODY_KEYS = {
    "model", "messages", "tools", "tool_choice", "temperature",
    "max_tokens", "max_completion_tokens", "top_p", "stream",
    "stream_options", "stop", "presence_penalty", "frequency_penalty",
    "n", "response_format", "seed", "user", "reasoning_effort",
    "verbosity", "reasoning_summary",
}

# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="WorkBuddy2Codex", version=BRIDGE_VERSION)
CONFIG: dict = {"api_key": "", "gateways": _load_gateways(), "pool": None, "account_service": None, "checkin_service": None, "log_path": None,
                 "desensitize": False,
                 "route_model": _load_route_model(),
                 "image_route_model": _load_image_route_model()}  # pool: AccountPool | None


# ---------------------------------------------------------------------------
# 日志（写文件）
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()


def _log(msg: str):
    """写一行日志到 CONFIG['log_path'] 指定的文件（追加，带时间戳）。未设置则丢弃。"""
    path = CONFIG.get("log_path")
    if not path:
        return
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with _LOG_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass  # 日志失败不应影响主流程




def _truncate(s: str, n: int = 80) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _check_auth(authorization: Optional[str], x_api_key: Optional[str]):
    key = CONFIG["api_key"]
    if not key:
        return
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and x_api_key:
        token = x_api_key
    if token != key:
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key", "type": "auth_error"}})


def _is_loopback_host(host: str) -> bool:
    value = str(host or "").strip().strip("[]").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _pool() -> AccountPool:
    pool = CONFIG.get("pool")
    if not isinstance(pool, AccountPool) or pool.summary()["count"] == 0:
        raise HTTPException(status_code=503, detail={"error": {
            "message": "WorkBuddy 本地账号池为空，请先在 Dashboard 导入当前账号",
            "type": "auth_error",
        }})
    return pool


def _account_service() -> WorkBuddyAccountService:
    service = CONFIG.get("account_service")
    if not isinstance(service, WorkBuddyAccountService):
        raise HTTPException(status_code=503, detail="WorkBuddy 账号服务尚未初始化")
    return service


def _auto_checkin_background() -> None:
    """Best-effort auto-claim for any account that has not claimed today."""
    import threading

    def _run() -> None:
        try:
            import asyncio
            svc = CONFIG.get("checkin_service")
            if isinstance(svc, WorkBuddyCheckinService):
                result = asyncio.new_event_loop().run_until_complete(svc.claim_all())
                _log(f"[checkin] auto-claim: {result.get('claimed_accounts')} accounts, +{result.get('claimed_total')} credits")
        except Exception as exc:
            _log(f"[checkin] auto-claim skipped: {exc}")

    threading.Thread(target=_run, daemon=True).start()


def _checkin_service() -> WorkBuddyCheckinService:
    service = CONFIG.get("checkin_service")
    if not isinstance(service, WorkBuddyCheckinService):
        raise HTTPException(status_code=503, detail="Buddy checkin service not initialized")
    return service


def _check_dashboard_management(request: Request) -> None:
    client_host = request.client.host if request.client else ""
    try:
        if not ipaddress.ip_address(client_host).is_loopback:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=403, detail="账号管理仅允许本机访问")
    cookie = request.cookies.get("workbuddy_bridge_session", "")
    if not cookie or not hmac.compare_digest(cookie, DASHBOARD_SESSION):
        raise HTTPException(status_code=401, detail="请刷新 Dashboard 后重试")
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("application/json"):
        raise HTTPException(status_code=415, detail="请求必须使用 application/json")
    origin = request.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != request.headers.get("host", "").lower():
            raise HTTPException(status_code=403, detail="拒绝跨站账号管理请求")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    response = HTMLResponse(content=DASHBOARD_HTML.replace("__BRIDGE_VERSION__", BRIDGE_VERSION))
    response.headers.update({
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-src https://copilot.tencent.com "
            "https://codebuddy.cn https://www.codebuddy.cn; frame-ancestors 'none'"
        ),
    })
    response.set_cookie(
        "workbuddy_bridge_session", DASHBOARD_SESSION,
        httponly=True, samesite="strict", secure=False, path="/",
    )
    return response


@app.get("/assets/bridge-logo.png", include_in_schema=False)
def dashboard_logo():
    if not LOGO_PATH.is_file():
        raise HTTPException(status_code=404, detail="Logo not found")
    return FileResponse(LOGO_PATH, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


def _aggregate_image_models() -> list[dict]:
    """汇总所有可用生图模型: WorkBuddy 官方 + 已启用且已配置网关的 image_models(带厂商前缀)。"""
    items = [{"id": DEFAULT_IMAGE_ROUTE_MODEL, "owner": "WorkBuddy"}]
    for gw in CONFIG.get("gateways") or []:
        if not gw.get("enabled", True) or not gw.get("configured"):
            continue
        prefix = (gw.get("prefix") or "").strip()
        for m in gw.get("image_models") or []:
            items.append({"id": f"{prefix}/{m}", "owner": gw.get("name") or gw.get("id")})
    return items


@app.get("/ui/status", include_in_schema=False)
async def dashboard_status():
    """Return a minimal, non-sensitive status summary for the local dashboard."""
    model_metadata, model_metadata_status = _load_workbuddy_model_metadata()
    model_display_order = list(model_metadata)
    model_display_order.extend(model for model in DEFAULT_MODELS if model not in model_metadata)
    pool = CONFIG.get("pool")
    account_service = CONFIG.get("account_service")
    if isinstance(pool, AccountPool) and isinstance(account_service, WorkBuddyAccountService):
        pool_summary = await account_service.enrich_pool()
    elif isinstance(pool, AccountPool):
        pool_summary = pool.summary()
    else:
        pool_summary = {
            "accounts": [], "count": 0, "ready": 0, "cooling": 0,
            "auth_dir": str(AUTH_POOL_DIR.resolve()),
            "quota": {"ok": False, "balance": 0, "total": 0, "used": 0},
        }
    credential_readable = pool_summary["count"] > 0
    credential_expired = credential_readable and pool_summary["ready"] == 0
    opencodex_connected = False
    opencodex_default_model: str | None = None
    try:
        headers = _opencodex_headers()
        async with httpx.AsyncClient(timeout=5) as client:
            provider = await _fetch_workbuddy_provider(client, headers)
        opencodex_connected = True
        value = provider.get("defaultModel")
        opencodex_default_model = value if isinstance(value, str) else None
    except OpenCodexSyncError:
        pass
    route_model = CONFIG["route_model"]
    checkin_summary = {}
    checkin_service = CONFIG.get("checkin_service")
    if isinstance(checkin_service, WorkBuddyCheckinService):
        try:
            checkin_summary = await checkin_service.status()
        except Exception:
            checkin_summary = {"ok": False, "available": False, "accounts": [], "any_unclaimed": False}
    return {
        "status": "ok",
        "version": BRIDGE_VERSION,
        "mode": "direct-proxy",
        "python": sys.version.split()[0],
        "credential_configured": pool_summary["count"] > 0,
        "credential_readable": credential_readable,
        "credential_expired": credential_expired,
        "credential_ready": pool_summary["ready"] > 0,
        "account_pool": pool_summary,
        "model_count": len(DEFAULT_MODELS),
        "hy3_available": "hy3" in DEFAULT_MODELS,
        "models": DEFAULT_MODELS,
        "model_display_order": model_display_order,
        "model_metadata": model_metadata,
        "model_metadata_status": model_metadata_status,
        "route_model": route_model,
        "image_route_model": CONFIG.get("image_route_model") or DEFAULT_IMAGE_ROUTE_MODEL,
        "image_models": _aggregate_image_models(),
        "routing_mode": "adaptive",
        "opencodex_connected": opencodex_connected,
        "opencodex_default_model": opencodex_default_model,
        "opencodex_in_sync": opencodex_connected and opencodex_default_model == route_model,
        "checkin": checkin_summary,
        "gateways": [_public_gateway(gw) for gw in CONFIG.get("gateways") or []],
    }


@app.post("/ui/route", include_in_schema=False)
async def dashboard_route(request: Request):
    _check_dashboard_management(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求必须是 JSON")
    model = payload.get("model") if isinstance(payload, dict) else None
    if model not in DEFAULT_MODELS:
        raise HTTPException(status_code=400, detail="不支持的模型")
    try:
        previous_opencodex_model = await _set_opencodex_default_model(model)
    except OpenCodexSyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    try:
        _save_route_model(model)
    except OSError:
        if previous_opencodex_model and previous_opencodex_model != model:
            try:
                await _set_opencodex_default_model(previous_opencodex_model)
            except OpenCodexSyncError:
                pass
        raise HTTPException(status_code=500, detail="无法保存路由设置")
    CONFIG["route_model"] = model
    return {
        "status": "ok",
        "route_model": model,
        "routing_mode": "adaptive",
        "opencodex_default_model": model,
        "opencodex_in_sync": True,
    }


@app.post("/ui/image-route", include_in_schema=False)
async def dashboard_image_route(request: Request):
    _check_dashboard_management(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求必须是 JSON")
    model = payload.get("model") if isinstance(payload, dict) else None
    known = {item["id"] for item in _aggregate_image_models()}
    if model not in known:
        raise HTTPException(status_code=400, detail="不支持的生图模型")
    try:
        _save_image_route_model(model)
    except OSError:
        raise HTTPException(status_code=500, detail="无法保存生图路由设置")
    CONFIG["image_route_model"] = model
    return {"status": "ok", "image_route_model": model, "image_models": _aggregate_image_models()}


@app.post("/ui/accounts/login/start", include_in_schema=False)
async def dashboard_login_start(request: Request):
    _check_dashboard_management(request)
    try:
        return await _account_service().start_login()
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/ui/accounts/login/status", include_in_schema=False)
async def dashboard_login_status(request: Request):
    _check_dashboard_management(request)
    payload = await request.json()
    login_id = str(payload.get("login_id") or "") if isinstance(payload, dict) else ""
    if not login_id:
        raise HTTPException(status_code=400, detail="缺少登录会话标识")
    try:
        return await _account_service().login_status(login_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="登录会话不存在或已结束")


@app.post("/ui/accounts/login/cancel", include_in_schema=False)
async def dashboard_login_cancel(request: Request):
    _check_dashboard_management(request)
    payload = await request.json()
    login_id = str(payload.get("login_id") or "") if isinstance(payload, dict) else ""
    if login_id:
        await _account_service().cancel_login(login_id)
    return {"status": "ok"}


@app.post("/ui/accounts/import-current", include_in_schema=False)
async def dashboard_import_current(request: Request):
    _check_dashboard_management(request)
    pool = CONFIG.get("pool")
    if not isinstance(pool, AccountPool):
        raise HTTPException(status_code=503, detail="账号池尚未初始化")
    try:
        account = pool.import_current()
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    service = CONFIG.get("account_service")
    if isinstance(service, WorkBuddyAccountService):
        service.clear_quotas()
    return {"status": "ok", "account": account, "account_pool": pool.summary()}
    _auto_checkin_background()


@app.post("/ui/accounts/primary", include_in_schema=False)
async def dashboard_set_primary(request: Request):
    _check_dashboard_management(request)
    payload = await request.json()
    ref = str(payload.get("ref") or "") if isinstance(payload, dict) else ""
    pool = CONFIG.get("pool")
    if not isinstance(pool, AccountPool):
        raise HTTPException(status_code=503, detail="账号池尚未初始化")
    try:
        pool.set_primary(ref)
    except KeyError:
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"status": "ok", "account_pool": pool.summary()}


@app.post("/ui/accounts/remove", include_in_schema=False)
async def dashboard_remove_account(request: Request):
    _check_dashboard_management(request)
    payload = await request.json()
    ref = str(payload.get("ref") or "") if isinstance(payload, dict) else ""
    pool = CONFIG.get("pool")
    if not isinstance(pool, AccountPool):
        raise HTTPException(status_code=503, detail="账号池尚未初始化")
    try:
        pool.remove(ref)
    except KeyError:
        raise HTTPException(status_code=404, detail="账号不存在")
    service = CONFIG.get("account_service")
    if isinstance(service, WorkBuddyAccountService):
        service.clear_quotas()
    return {"status": "ok", "account_pool": pool.summary()}
    _auto_checkin_background()


@app.post("/ui/accounts/refresh", include_in_schema=False)
async def dashboard_refresh_accounts(request: Request):
    _check_dashboard_management(request)
    pool = CONFIG.get("pool")
    if not isinstance(pool, AccountPool):
        raise HTTPException(status_code=503, detail="账号池尚未初始化")
    pool.reload()
    service = CONFIG.get("account_service")
    if isinstance(service, WorkBuddyAccountService):
        service.clear_quotas()
    return {"status": "ok", "account_pool": pool.summary()}


@app.post("/ui/accounts/open-folder", include_in_schema=False)
async def dashboard_open_auth_folder(request: Request):
    _check_dashboard_management(request)
    pool = CONFIG.get("pool")
    if not isinstance(pool, AccountPool):
        raise HTTPException(status_code=503, detail="账号池尚未初始化")
    folder = str(pool.auth_dir)
    try:
        if sys.platform == "win32":
            os.startfile(folder)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", folder])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", folder])
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"无法打开本地 auths 目录：{exc}")
    return {"status": "ok"}


@app.get("/ui/checkin", include_in_schema=False)
async def dashboard_checkin_status(request: Request):
    _check_dashboard_management(request)
    service = CONFIG.get("checkin_service")
    if not isinstance(service, WorkBuddyCheckinService):
        raise HTTPException(status_code=503, detail="Buddy 鍔犳娊绔欏～链嶅姟灏氭湭鍒濆鍖?")
    try:
        return await service.status()
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/ui/checkin/claim", include_in_schema=False)
async def dashboard_checkin_claim(request: Request):
    _check_dashboard_management(request)
    service = CONFIG.get("checkin_service")
    if not isinstance(service, WorkBuddyCheckinService):
        raise HTTPException(status_code=503, detail="Buddy 鍔犳娊绔欏～链嶅姟灏氭湭鍒濆鍖?")
    payload = await request.json()
    ref = str(payload.get("ref") or "") if isinstance(payload, dict) else ""
    try:
        if ref:
            return {"status": "ok", "result": await service.claim_one(ref)}
        return {"status": "ok", **await service.claim_all()}
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/ui/gateways/discover", include_in_schema=False)
async def dashboard_gateway_discover(request: Request):
    _check_dashboard_management(request)
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad json: {e}")
    base_url = str(payload.get("base_url") or "").strip().rstrip("/")
    api_key = str(payload.get("api_key") or "").strip()
    if not base_url:
        raise HTTPException(status_code=400, detail="缺少接口地址")
    if not api_key:
        raise HTTPException(status_code=400, detail="缺少 API Key")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="接口地址必须是以 http/https 开头的有效 URL")
    # 允许回环地址(本机网关)或公网网关;禁止内网段探测以外的情况由 httpx 自行处理
    url = f"{base_url}/models"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"无法连接网关: {exc}")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"网关返回 HTTP {response.status_code}: {response.text[:200]}")
    try:
        data = response.json()
    except Exception:
        raise HTTPException(status_code=502, detail="网关返回的不是 JSON")
    raw_models = (data.get("data") or []) if isinstance(data, dict) else []
    models = []
    image_models = []
    # 部分网关(如火山 Ark)的 /models 响应不带 supported_endpoint_types，
    # 需要用命名关键词兜底识别生图模型(seedream/seededit/t2i/i2i 等)
    IMAGE_GEN_KEYWORDS = ("seedream", "seededit", "t2i", "i2i", "image")
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid:
            continue
        endpoints = item.get("supported_endpoint_types") or item.get("endpoint_types") or []
        low_mid = mid.lower()
        if "image-generation" in endpoints or any(k in low_mid for k in IMAGE_GEN_KEYWORDS):
            image_models.append(mid)
        else:
            models.append(mid)
    if not models and not image_models:
        raise HTTPException(status_code=502, detail="网关返回的模型列表为空")

    # 协议探测: 依次尝试多个模型(部分网关如火山 Ark 有"仅部分模型支持 coding plan"的限制),
    # 任一模型返回 200 即认为该协议可用
    probe_model = models[0] if models else (image_models[0] if image_models else None)
    protocols = {"chat": False, "responses": False}
    if probe_model:
        probe_headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        # 优先探测常见的"对话/编码"模型，避免首个模型恰好不支持该套餐(如 Ark coding plan 的老 doubao 模型)
        PROBE_PRIORITY = ("deepseek", "glm", "coding", "kimi", "qwen",
                          "seed-2", "seed-1-6", "seed-1-8", "thinking",
                          "minimax", "gpt", "hy3", "auto")

        def _probe_key(m: object) -> int:
            low = str(m).lower()
            for i, kw in enumerate(PROBE_PRIORITY):
                if kw in low:
                    return i
            return len(PROBE_PRIORITY)

        probe_candidates = sorted((models or image_models or [probe_model]), key=_probe_key)[:12]
        for candidate in probe_candidates:
            if protocols["chat"]:
                break
            try:
                async with httpx.AsyncClient(timeout=20, follow_redirects=False) as probe_client:
                    chat_probe = await probe_client.post(f"{base_url}/chat/completions",
                                                         headers=probe_headers,
                                                         json={"model": candidate, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1})
                if chat_probe.status_code == 200:
                    protocols["chat"] = True
            except Exception:
                pass
        for candidate in probe_candidates:
            if protocols["responses"]:
                break
            try:
                async with httpx.AsyncClient(timeout=20, follow_redirects=False) as probe_client:
                    resp_probe = await probe_client.post(f"{base_url}/responses",
                                                         headers=probe_headers,
                                                         json={"model": candidate, "input": "hi", "max_output_tokens": 1})
                if resp_probe.status_code == 200:
                    protocols["responses"] = True
            except Exception:
                pass

    return {"status": "ok", "models": models, "image_models": image_models, "protocols": protocols}


@app.post("/ui/gateways/save", include_in_schema=False)
async def dashboard_gateway_save(request: Request):
    _check_dashboard_management(request)
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad json: {e}")
    gw_id = str(payload.get("id") or "").strip().lower()
    name = str(payload.get("name") or "").strip()
    base_url = str(payload.get("base_url") or "").strip().rstrip("/")
    prefix = str(payload.get("prefix") or "").strip().upper()
    home_url = str(payload.get("home_url") or "").strip()
    models = payload.get("models") or []
    image_models = payload.get("image_models") or []
    api_key = str(payload.get("api_key") or "").strip()
    protocol = str(payload.get("protocol") or "auto").strip().lower()
    if protocol not in {"auto", "chat", "responses"}:
        protocol = "auto"
    api_key_env = str(payload.get("api_key_env") or "").strip()
    if not gw_id or not name or not base_url or not prefix:
        raise HTTPException(status_code=400, detail="id/name/base_url/prefix 均必填")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="接口地址必须是有效 URL")
    clean_models = [str(m).strip() for m in models if str(m).strip()]
    clean_images = [str(m).strip() for m in image_models if str(m).strip()]
    if not clean_models and not clean_images:
        raise HTTPException(status_code=400, detail="至少需要一个模型")
    if not api_key_env:
        # 用前缀生成环境变量名(ASCII 安全),避免中文 id 拼出无法设置的变量名
        env_seed = "".join(ch for ch in prefix.upper() if ch.isalnum())
        api_key_env = f"{env_seed or 'GW'}_STORE_KEY"
    items = _read_gateways_raw()
    # 同 id 更新,同 base_url 视为同一厂商也更新
    replaced = False
    for i, item in enumerate(items):
        if str(item.get("id") or "").lower() == gw_id or str(item.get("base_url") or "").rstrip("/") == base_url:
            merged_key = api_key or str(item.get("api_key") or "").strip()
            items[i] = {
                "id": gw_id,
                "name": name,
                "api_key_env": api_key_env,
                "api_key": merged_key,
                "base_url": base_url,
                "home_url": home_url,
                "prefix": prefix,
                "protocol": protocol,
                "models": clean_models,
                "image_models": clean_images,
                "enabled": bool(item.get("enabled", True)),
            }
            replaced = True
            break
    if not replaced:
        items.append({
            "id": gw_id,
            "name": name,
            "api_key_env": api_key_env,
            "api_key": api_key,
            "base_url": base_url,
            "home_url": home_url,
            "prefix": prefix,
            "protocol": protocol,
            "models": clean_models,
            "image_models": clean_images,
            "enabled": bool(payload.get("enabled", True)),
        })
    _write_gateways_raw(items)
    CONFIG["gateways"] = _load_gateways()
    return {"status": "ok", "gateways": [_public_gateway(g) for g in CONFIG["gateways"]]}


@app.post("/ui/gateways/remove", include_in_schema=False)
async def dashboard_gateway_remove(request: Request):
    _check_dashboard_management(request)
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad json: {e}")
    gw_id = str(payload.get("id") or "").strip().lower()
    if not gw_id:
        raise HTTPException(status_code=400, detail="缺少网关 id")
    items = _read_gateways_raw()
    kept = [i for i in items if str(i.get("id") or "").lower() != gw_id]
    if len(kept) == len(items):
        raise HTTPException(status_code=404, detail="网关不存在")
    _write_gateways_raw(kept)
    CONFIG["gateways"] = _load_gateways()
    return {"status": "ok", "gateways": [_public_gateway(g) for g in CONFIG["gateways"]]}


@app.post("/ui/gateways/toggle", include_in_schema=False)
async def dashboard_gateway_toggle(request: Request):
    """Enable/disable a third-party gateway from the dashboard."""
    _check_dashboard_management(request)
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad json: {e}")
    gw_id = str(payload.get("id") or "").strip().lower()
    enabled = bool(payload.get("enabled", True))
    if not gw_id:
        raise HTTPException(status_code=400, detail="id 必填")
    items = _read_gateways_raw()
    found = False
    for item in items:
        if str(item.get("id") or "").strip().lower() == gw_id:
            item["enabled"] = enabled
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail="网关不存在")
    _write_gateways_raw(items)
    CONFIG["gateways"] = _load_gateways()
    return {"status": "ok", "gateways": [_public_gateway(g) for g in CONFIG["gateways"]]}


@app.post("/ui/gateways/key", include_in_schema=False)
async def dashboard_gateway_key(request: Request):
    """Return the key for one gateway so the user can verify it in the edit form.

    Works for every gateway: file-stored keys are read from auths config, env-backed
    keys are read from the environment variable. Protected by the same dashboard
    management session auth; never exposed via /ui/status polling.
    """
    _check_dashboard_management(request)
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad json: {e}")
    gw_id = str(payload.get("id") or "").strip().lower()
    if not gw_id:
        raise HTTPException(status_code=400, detail="缺少网关 id")
    for gw in CONFIG.get("gateways") or []:
        if str(gw.get("id") or "").lower() == gw_id:
            stored = str(gw.get("api_key") or "").strip()
            if stored:
                return {"status": "ok", "api_key": stored, "key_source": "file"}
            env_val = os.environ.get(gw.get("api_key_env") or "", "").strip()
            if env_val:
                return {"status": "ok", "api_key": env_val, "key_source": "env"}
            return {"status": "ok", "api_key": "", "key_source": "none"}
    raise HTTPException(status_code=404, detail="网关不存在")


@app.get("/health")
def health(authorization: Optional[str] = Header(default=None),
           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    pool = CONFIG.get("pool")
    pool_summary = pool.summary() if isinstance(pool, AccountPool) else {"count": 0, "ready": 0}
    info: dict = {"status": "ok", "version": BRIDGE_VERSION,
                  "platform": sys.platform, "python": sys.version.split()[0],
                  "credential_configured": pool_summary["count"] > 0,
                  "account_count": pool_summary["count"],
                  "ready_accounts": pool_summary["ready"],
                  "mode": "direct-proxy (native function calling)"}
    info["credential_readable"] = pool_summary["count"] > 0
    info["credential_expired"] = pool_summary["count"] > 0 and pool_summary["ready"] == 0
    return info


@app.get("/v1/models")
def list_models(authorization: Optional[str] = Header(default=None),
                x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    data = [{"id": m, "object": "model", "created": 1700000000, "owned_by": "codebuddy"}
            for m in DEFAULT_MODELS]
    for gw in CONFIG.get("gateways") or []:
        if not gw.get("enabled", True):
            continue
        prefix = gw.get("prefix") or "GW"
        for m in gw.get("models") or []:
            data.append({"id": f"{prefix}/{m}", "object": "model", "created": 1700000000,
                         "owned_by": gw.get("id")})
        for m in gw.get("image_models") or []:
            data.append({"id": f"{prefix}/{m}", "object": "model", "created": 1700000000,
                         "owned_by": gw.get("id"), "supported_endpoint_types": ["image-generation", "openai"]})
            # 同时暴露无前缀别名（如 gpt-image-2），便于 codex-image2-skill 等客户端直接使用默认模型名
            data.append({"id": m, "object": "model", "created": 1700000000,
                         "owned_by": gw.get("id"), "supported_endpoint_types": ["image-generation", "openai"]})
    return {"object": "list", "data": data}


def _match_image_gateway(payload: dict):
    """Find the gateway owning the requested image model (prefix match, then plain image-model name)."""
    raw_model = str(payload.get("model") or "").strip()
    low_model = raw_model.lower()
    for gw in CONFIG.get("gateways") or []:
        if not gw.get("enabled", True):
            continue
        prefix = (gw.get("prefix") or "").lower()
        if not prefix:
            continue
        if low_model.startswith(prefix + "/"):
            if not gw.get("configured"):
                raise HTTPException(status_code=502, detail={"error": {
                    "message": f"gateway {gw.get('id')} key not set", "type": "gateway_not_configured"}})
            return gw
    # 无前缀别名（如 gpt-image-2 -> apiget 的 image_models 精确匹配）
    for gw in CONFIG.get("gateways") or []:
        if not gw.get("enabled", True):
            continue
        for m in gw.get("image_models") or []:
            if str(m).strip().lower() == low_model:
                if not gw.get("configured"):
                    raise HTTPException(status_code=502, detail={"error": {
                        "message": f"gateway {gw.get('id')} key not set", "type": "gateway_not_configured"}})
                return gw
    raise HTTPException(status_code=404, detail={"error": {
        "message": "no gateway matches this image model", "type": "invalid_request_error"}})


def _responses_input_to_messages(resp_input):
    """Translate a Responses `input` (string | list) to chat messages."""
    if isinstance(resp_input, str):
        return [{"role": "user", "content": resp_input}]
    messages = []
    if not isinstance(resp_input, list):
        return [{"role": "user", "content": str(resp_input or "")}]
    for item in resp_input:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            role = item.get("role") or "user"
            content = item.get("content")
            if isinstance(content, list):
                text_parts = []
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "input_text":
                        text_parts.append(str(blk.get("text") or ""))
                content = "".join(text_parts)
            messages.append({"role": role, "content": str(content or "")})
        elif item_type == "function_call_output":
            messages.append({"role": "tool", "tool_call_id": str(item.get("call_id") or ""),
                             "content": json.dumps(item.get("output"), ensure_ascii=False) if not isinstance(item.get("output"), str) else str(item.get("output") or "")})
        elif item_type == "function_call":
            messages.append({"role": "assistant", "content": None, "tool_calls": [{
                "id": str(item.get("call_id") or "call_0"),
                "type": "function",
                "function": {"name": str(item.get("name") or ""),
                             "arguments": json.dumps(item.get("arguments"), ensure_ascii=False) if not isinstance(item.get("arguments"), str) else str(item.get("arguments") or "{}")},
            }]})
    return messages


def _chat_tools_to_responses(tools):
    """Normalize tools array (chat-style -> responses-style function tools)."""
    result = []
    if not isinstance(tools, list):
        return result
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        ttype = tool.get("type")
        if ttype == "function":
            fn = tool.get("function") or {}
            result.append({"type": "function", "name": fn.get("name") or tool.get("name") or "",
                           "description": fn.get("description") or tool.get("description") or "",
                           "parameters": fn.get("parameters") or tool.get("parameters") or {}})
        elif ttype == "custom":
            result.append(tool)
    return result


def _chat_message_to_responses_item(msg, index):
    """Translate a chat assistant message into a Responses output item."""
    role = msg.get("role") or "assistant"
    content = msg.get("content")
    if isinstance(content, list):
        text_parts = [str(b.get("text") or "") for b in content if isinstance(b, dict)]
        content = "".join(text_parts)
    item = {
        "id": f"msg_{os.urandom(8).hex()}",
        "type": "message",
        "status": "completed",
        "role": role,
        "content": [{"type": "output_text", "text": str(content or ""), "annotations": []}],
    }
    tcs = msg.get("tool_calls") or []
    calls = []
    for tc in tcs:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        calls.append({
            "id": tc.get("id") or f"call_{os.urandom(6).hex()}",
            "call_id": tc.get("id") or f"call_{os.urandom(6).hex()}",
            "type": "function_call",
            "name": fn.get("name") or "",
            "arguments": fn.get("arguments") or "",
            "status": "completed",
        })
    output = [item] + calls
    return output


def _chat_to_responses(payload, chat_result):
    """Translate a non-streaming chat completion into a Responses object."""
    choices = chat_result.get("choices") or [{}]
    choice = choices[0] if choices else {}
    msg = choice.get("message") or {}
    output = _chat_message_to_responses_items(msg)
    usage = chat_result.get("usage") or {}
    return {
        "id": f"resp_{os.urandom(12).hex()}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": chat_result.get("model") or payload.get("model") or "",
        "output": output,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


def _chat_message_to_responses_items(msg):
    items = _chat_message_to_responses_item(msg, 0)
    return items


async def _iter_sse_data_lines(chunks):
    """跨 chunk 行缓冲：把上游 bytes 流按 `data: ` 行切出完整 payload。

    httpx 的分块传输 / gzip 解压不保证 SSE 事件落在同一个 chunk 边界内，
    逐 chunk splitlines 会把被切开的 `[DONE]` 或 delta 事件丢掉，导致
    客户端永远收不到 response.completed（"stream closed before
    response.completed"）。这里累积字节、按换行切完整行，剩余部分留到
    下一个 chunk。
    """
    buf = b""
    async for chunk in chunks:
        if not isinstance(chunk, bytes):
            chunk = str(chunk).encode("utf-8", "replace")
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            yield line[5:].strip().decode("utf-8", "replace")
        # 防御：异常大的单行不应无限累积
        if len(buf) > 512 * 1024:
            buf = b""
    if buf:
        line = buf.strip()
        if line.startswith(b"data:"):
            yield line[5:].strip().decode("utf-8", "replace")


def _sse(data: dict, event: str) -> bytes:
    """Format a Responses SSE event: `event: <type>` + `data: <json>`."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


async def _chat_sse_to_responses(chunks, model_name: str):
    """Consume chat SSE data lines and emit a standards-compliant Responses event stream.

    OpenAI 客户端（OpenCodex / openai SDK）期望完整事件序列：
    response.created → response.in_progress → response.output_item.added →
    response.content_part.added → response.output_text.delta … →
    response.output_text.done → response.content_part.done →
    response.output_item.done → response.completed。仅发 delta+completed 的
    最小流会被部分客户端判定为不完整（"stream closed before
    response.completed"）。
    """
    resp_id = f"resp_{os.urandom(12).hex()}"
    item_id = f"msg_{os.urandom(8).hex()}"
    created = int(time.time())
    text_parts: list[str] = []
    usage: dict = {}
    sent_head = False

    def normalized_usage():
        """chat usage (prompt_tokens/...) -> responses usage (input_tokens/...).

        OpenCodex 的 ResponseCompleted 解析器按 responses 协议严格取
        input_tokens/output_tokens/total_tokens，缺字段即报
        "missing field `input_tokens`" 并判定流失败。
        """
        return {
            "input_tokens": usage.get("prompt_tokens", 0),
            "input_tokens_details": {
                "cached_tokens": usage.get("prompt_cache_hit_tokens", 0),
            },
            "output_tokens": usage.get("completion_tokens", 0),
            "output_tokens_details": {
                "reasoning_tokens": usage.get("completion_thinking_tokens", 0),
            },
            "total_tokens": usage.get("total_tokens", 0),
        }

    def resp_obj(status: str):
        return {
            "id": resp_id,
            "object": "response",
            "created_at": created,
            "status": status,
            "model": model_name,
            "output": [],
            "usage": normalized_usage(),
        }

    yield _sse({"type": "response.created", "response": resp_obj("in_progress")}, "response.created")
    yield _sse({"type": "response.in_progress", "response": resp_obj("in_progress")}, "response.in_progress")

    async for data in chunks:
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        if chunk.get("error"):
            yield _sse(chunk, "error")
            continue
        if isinstance(chunk.get("usage"), dict):
            usage.update(chunk["usage"])
        delta = ((chunk.get("choices") or [{}])[0]).get("delta") or {}
        text = delta.get("content")
        if text:
            if not sent_head:
                item = {"id": item_id, "type": "message", "status": "in_progress",
                        "role": "assistant", "content": []}
                yield _sse({"type": "response.output_item.added", "output_index": 0, "item": item}, "response.output_item.added")
                part = {"type": "output_text", "text": "", "annotations": []}
                yield _sse({"type": "response.content_part.added", "item_id": item_id, "output_index": 0,
                            "content_index": 0, "part": part}, "response.content_part.added")
                sent_head = True
            text_parts.append(text)
            yield _sse({"type": "response.output_text.delta", "item_id": item_id, "output_index": 0,
                        "content_index": 0, "delta": text}, "response.output_text.delta")
        for tc in delta.get("tool_calls") or []:
            fn = tc.get("function") or {}
            name = fn.get("name")
            args = fn.get("arguments") or ""
            if name:
                fc_item = {"id": item_id, "type": "function_call", "status": "in_progress",
                           "call_id": item_id, "name": name, "arguments": ""}
                yield _sse({"type": "response.output_item.added", "output_index": 0, "item": fc_item}, "response.output_item.added")
                yield _sse({"type": "response.function_call_arguments.delta", "item_id": item_id,
                            "output_index": 0, "delta": args, "name": name}, "response.function_call_arguments.delta")
            elif args:
                yield _sse({"type": "response.function_call_arguments.delta", "item_id": item_id,
                            "output_index": 0, "delta": args}, "response.function_call_arguments.delta")

    if sent_head:
        full_text = "".join(text_parts)
        part = {"type": "output_text", "text": full_text, "annotations": []}
        yield _sse({"type": "response.output_text.done", "item_id": item_id, "output_index": 0,
                    "content_index": 0, "text": full_text}, "response.output_text.done")
        yield _sse({"type": "response.content_part.done", "item_id": item_id, "output_index": 0,
                    "content_index": 0, "part": part}, "response.content_part.done")
        item = {"id": item_id, "type": "message", "status": "completed",
                "role": "assistant", "content": [part]}
        yield _sse({"type": "response.output_item.done", "output_index": 0, "item": item}, "response.output_item.done")
        yield _sse({"type": "response.completed", "response": {**resp_obj("completed"), "output": [item]}},
                   "response.completed")
    else:
        # 无任何输出（上游空回复）也补发 completed，避免客户端挂起
        yield _sse({"type": "response.completed", "response": resp_obj("completed")}, "response.completed")
    yield b"data: [DONE]\n\n"


async def _responses_proxy(payload: dict):
    """Handle POST /v1/responses: native pass-through for responses-capable gateways,
    else translate Responses -> chat -> Responses."""
    raw_model = str(payload.get("model") or "")
    # 找到对应网关
    gw = None
    for g in CONFIG.get("gateways") or []:
        if not g.get("enabled", True):
            continue
        prefix = (g.get("prefix") or "").lower()
        if prefix and raw_model.lower().startswith(prefix + "/"):
            gw = g
            break
    # 非网关模型 -> WorkBuddy 账号池(经 chat 转换回 responses)
    if gw is None:
        return await _responses_via_workbuddy(payload, raw_model)

    api_key = os.environ.get(gw.get("api_key_env") or "", "").strip() or str(gw.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=502, detail={"error": {
            "message": f"gateway {gw.get('id')} key not set", "type": "gateway_not_configured"}})
    model = raw_model.split("/", 1)[1] if "/" in raw_model else raw_model
    client_wants_stream = bool(payload.get("stream"))
    rid = os.urandom(4).hex()

    # 1) 原生 responses 直通: protocol=responses 强制直通; protocol=auto 先探测; protocol=chat 跳过
    base = str(gw.get("base_url") or "").rstrip("/")
    protocol = str(gw.get("protocol") or "auto").lower()
    use_native = False
    if protocol == "responses":
        use_native = True
    elif protocol == "auto":
        responses_url = f"{base}/responses"
        native_headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"}
        native_body = dict(payload)
        native_body["model"] = model
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                probe = await client.post(responses_url, headers=native_headers, json=native_body)
            use_native = probe.status_code == 200
            if use_native:
                responses_url = responses_url
                native_headers = native_headers
                native_body = native_body
                _log(f"[{rid}] \u25b6 RESPONSES native(auto) {gw.get('id')} | {model}")
        except Exception as exc:
            _log(f"[{rid}] \u2717 RESPONSES native probe failed for {gw.get('id')}: {exc}")
            use_native = False
    if use_native:
        responses_url = f"{base}/responses"
        native_headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"}
        native_body = dict(payload)
        native_body["model"] = model
        # 透传流式
        if client_wants_stream:
            async def _native_gen():
                async with httpx.AsyncClient(timeout=300) as c2:
                    async with c2.stream("POST", responses_url, headers=native_headers, json=native_body) as resp2:
                        if resp2.status_code != 200:
                            raw = await resp2.aread()
                            yield f"data: {json.dumps(_safe_err_raw(raw, resp2.status_code), ensure_ascii=False)}\n\n".encode("utf-8")
                            yield b"data: [DONE]\n\n"
                            return
                        async for chunk in resp2.aiter_raw():
                            yield chunk
            return StreamingResponse(_native_gen(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        async with httpx.AsyncClient(timeout=60) as client:
            probe = await client.post(responses_url, headers=native_headers, json=native_body)
        return JSONResponse(content=probe.json())

    # 2) 原生不可用 -> 翻译成 chat
    _log(f"[{rid}] \u25b6 RESPONSES translate {gw.get('id')} | {model} | stream={client_wants_stream}")
    chat_body = {
        "model": model,
        "messages": _responses_input_to_messages(payload.get("input")),
        "stream": True,
    }
    if payload.get("tools"):
        chat_body["tools"] = [{"type": "function", "function": t} for t in _chat_tools_to_responses(payload["tools"])]
    if payload.get("tool_choice") is not None:
        chat_body["tool_choice"] = payload["tool_choice"]
    if payload.get("max_output_tokens"):
        chat_body["max_tokens"] = payload["max_output_tokens"]
    if payload.get("temperature") is not None:
        chat_body["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        chat_body["top_p"] = payload["top_p"]
    if payload.get("stream_options") is None:
        chat_body["stream_options"] = {"include_usage": True}

    chat_headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"}
    chat_url = f"{base}/chat/completions"

    if client_wants_stream:
        async def _translated_stream():
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream("POST", chat_url, headers=chat_headers, json=chat_body) as response:
                    if response.status_code != 200:
                        raw = await response.aread()
                        yield f"data: {json.dumps(_safe_err_raw(raw, response.status_code), ensure_ascii=False)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                        return
                    # 转发 chat SSE 并转换为完整 Responses 事件流
                    async for evt in _chat_sse_to_responses(
                            _iter_sse_data_lines(response.aiter_bytes()), model):
                        yield evt
        return StreamingResponse(_translated_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # 非流式: 调 chat 聚合,转 responses
    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream("POST", chat_url, headers=chat_headers, json=chat_body) as response:
            if response.status_code != 200:
                raw = await response.aread()
                raise HTTPException(status_code=response.status_code, detail=_safe_err_raw(raw, response.status_code))
            chat_result = await _collect_stream(response)
    return JSONResponse(content=_chat_to_responses(payload, chat_result))


async def _responses_via_workbuddy(payload: dict, raw_model: str):
    """Translate a Responses request to the WorkBuddy account pool (chat) and back."""
    pool = _pool()
    client_wants_stream = bool(payload.get("stream"))
    rid = os.urandom(4).hex()
    requested_model = str(raw_model or "")
    if requested_model.lower().startswith("workbuddy/"):
        requested_model = requested_model.split("/", 1)[1]
    upstream_model = _resolve_route_model(requested_model)
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body["model"] = upstream_model
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
    # Responses 输入 -> chat messages
    if not body.get("messages"):
        body["messages"] = _responses_input_to_messages(payload.get("input"))
    # 可选：剥离 skills 段（默认关闭；如遇腾讯上游内容审核误判可设
    # WORKBUDDY_STRIP_SKILLS=1 开启）
    if os.environ.get("WORKBUDDY_STRIP_SKILLS", "0") != "0":
        body["messages"] = _strip_skills_from_messages(body.get("messages") or [])
    # Responses tools -> chat tools
    if payload.get("tools") and "tools" not in body:
        body["tools"] = [{"type": "function", "function": t} for t in _chat_tools_to_responses(payload["tools"])]
    if payload.get("max_output_tokens") and "max_tokens" not in body:
        body["max_tokens"] = payload["max_output_tokens"]
    if payload.get("temperature") is not None and "temperature" not in body:
        body["temperature"] = payload["temperature"]
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()
    # 调试：完整 dump 请求 messages（WORKBUDDY_DUMP_REQUESTS=1 时启用）
    if os.environ.get("WORKBUDDY_DUMP_REQUESTS"):
        try:
            dump_dir = Path(os.environ.get("WORKBUDDY_DUMP_DIR", "logs/dumps"))
            dump_dir.mkdir(parents=True, exist_ok=True)
            dump_path = dump_dir / f"resp-dump-{int(time.time() * 1000)}-{rid}.json"
            dump_path.write_text(
                json.dumps({"model": upstream_model, "messages": body.get("messages")},
                           ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
    # 请求摘要（脱敏）：记录消息数与字符量，便于诊断上游内容审核
    _input = body.get("messages") or []
    _snips = []
    for _m in _input[:8]:
        if not isinstance(_m, dict):
            continue
        _role = _m.get("role") or "?"
        _c = str(_m.get("content") or "")
        _snips.append(f"{_role}:{_c[:60].replace(chr(10), ' ')}")
    _log(f"[{rid}] \u25b6 RESPONSES via WorkBuddy pool | {upstream_model} | stream={client_wants_stream} "
         f"| msgs={len(_input)} tools={len(body.get('tools') or [])} "
         f"in_chars={sum(len(str(m.get('content') or '')) for m in _input if isinstance(m, dict))} "
         f"| {' || '.join(_snips)}")

    if client_wants_stream:
        async def _wb_responses_stream():
            # _stream_upstream 产出 chat SSE；转换为完整 Responses 事件流
            async for evt in _chat_sse_to_responses(
                    _iter_sse_data_lines(_stream_upstream(url, pool, body, upstream_model, t0, rid)),
                    upstream_model):
                yield evt
        return StreamingResponse(_wb_responses_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    collected = await _collect_with_pool(url, pool, body, upstream_model, rid)
    _log_finish(upstream_model, t0, collected, rid)
    return JSONResponse(content=_chat_to_responses(payload, collected))


@app.post("/v1/responses")
async def responses_completions(request: Request,
                                authorization: Optional[str] = Header(default=None),
                                x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})
    return await _responses_proxy(payload)


async def _workbuddy_image_generations(payload: dict):
    """Generate an image via the WorkBuddy account pool (hunyuan-image-v3.0-art).

    Calls copilot.tencent.com/v2/images/generations with the account-pool auth,
    downloads the returned image URL and re-serves it as OpenAI-style b64_json.
    """
    pool = _pool()
    raw_model = str(payload.get("model") or "")
    if raw_model.lower().startswith("workbuddy/"):
        raw_model = raw_model.split("/", 1)[1]
    model = raw_model or "hunyuan-image-v3.0-art"
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail={"error": {"message": "prompt is required", "type": "invalid_request_error"}})
    # 默认关闭显式水印（腾讯官方 LogoAdd 参数：0=不添加"图片由 AI 生成"水印）。
    # 客户端若显式传 watermark=true，则保留水印。
    logo_add = 0 if not bool(payload.get("watermark")) else 1
    body = {
        "model": model,
        "prompt": prompt,
        "n": int(payload.get("n") or 1),
        "size": str(payload.get("size") or "1024x1024"),
        "LogoAdd": logo_add,
    }
    # 图生图：客户端可传 image（base64 列表，最多 3 张参考图）与 input_fidelity（风格贴合度）
    images = payload.get("image")
    if images is not None:
        if isinstance(images, str):
            images = [images]
        if isinstance(images, list):
            images = [str(i) for i in images if str(i).strip()]
        if images:
            body["image"] = images[:3]
    if payload.get("input_fidelity") is not None:
        body["input_fidelity"] = payload["input_fidelity"]
    # 实验性透传：客户端可传 footnote（自定义水印文字）与 watermark（bool）
    if payload.get("footnote") is not None:
        body["footnote"] = str(payload["footnote"])
    if payload.get("watermark") is not None:
        body["watermark"] = bool(payload["watermark"])
    url = f"{BACKEND}/v2/images/generations"
    rid = os.urandom(4).hex()
    img_n = len(body.get("image") or [])
    _log(f"[{rid}] \u25b6 WORKBUDDY IMAGE {model} | prompt_len={len(prompt)} | ref_images={img_n}")
    candidates = pool.candidates()
    if not candidates:
        raise HTTPException(status_code=503, detail={"error": {"message": "WorkBuddy 账号池全部冷却", "type": "account_pool_unavailable"}})
    last_status = 502
    last_raw = b"all WorkBuddy accounts failed"
    for candidate in candidates:
        headers = await _headers_for_candidate(pool, candidate, rid)
        if headers is None:
            continue
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(url, headers=headers, json=body)
            if response.status_code == 200:
                data = response.json()
                images = ((data.get("data") or {}).get("data")) or []
                if not images or not isinstance(images[0], dict) or not images[0].get("url"):
                    raise HTTPException(status_code=502, detail={"error": {"message": "上游未返回图片", "type": "upstream_error"}})
                image_url = images[0]["url"]
                # 下载图片转 b64_json
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client2:
                    img_resp = await client2.get(image_url)
                if img_resp.status_code != 200:
                    raise HTTPException(status_code=502, detail={"error": {"message": "图片下载失败", "type": "upstream_error"}})
                import base64
                content = img_resp.content
                if logo_add == 0:
                    cleaned = _remove_workbuddy_watermark(content)
                    if cleaned is not content:
                        content = cleaned
                        _log(f"[{rid}] \u2714 WORKBUDDY IMAGE OK (watermark removed) | {len(content)} bytes")
                    else:
                        _log(f"[{rid}] \u2714 WORKBUDDY IMAGE OK (watermark removal skipped) | {len(content)} bytes")
                else:
                    _log(f"[{rid}] \u2714 WORKBUDDY IMAGE OK (watermark kept) | {len(content)} bytes")
                b64 = base64.b64encode(content).decode("ascii")
                return JSONResponse(content={
                    "created": int(time.time()),
                    "data": [{"b64_json": b64}],
                })
            raw = await response.aread()
            last_status, last_raw = response.status_code, raw
            _log(f"[{rid}] \u2717 WORKBUDDY IMAGE HTTP {response.status_code} | account={candidate.ref} | {raw.decode('utf-8','replace')[:200]}")
            failure = _account_failure(response.status_code, raw)
            if failure:
                pool.mark_failure(candidate.ref, failure[0], failure[1])
                continue
            raise HTTPException(status_code=response.status_code, detail=_safe_err_raw(raw, response.status_code))
        except HTTPException:
            raise
        except Exception as exc:
            _log(f"[{rid}] \u2717 WORKBUDDY IMAGE error: {exc}")
            pool.mark_failure(candidate.ref, str(exc)[:120], 30)
    raise HTTPException(status_code=last_status, detail=_safe_err_raw(last_raw, last_status))


WM_TEMPLATE_PATH = BASE_DIR / "assets" / "workbuddy_wm_template.png"
# 模板基于 1024×1024 捕获：整张模板 116×56，右下角锚点距边 8px。
# 实测水印随图片短边等比缩放：scale = min(w, h) / 1024。
WM_TPL_REF_W, WM_TPL_REF_H = 116, 56
WM_TPL_REF_ANCHOR = 8  # 模板右下角距图右/下边缘的像素数（1024 基线）
# 额外精确模板（按实际交付尺寸捕获，避免缩放插值误差）：
# 内容框即模板本体，右下角锚点同样为 8px。
WM_TEMPLATE_BY_SIZE = {
    (832, 1216): BASE_DIR / "assets" / "workbuddy_wm_832x1216.png",
    (1216, 832): BASE_DIR / "assets" / "workbuddy_wm_1216x832.png",
}


def _load_workbuddy_wm_template(w: int, h: int):
    """Load the best-matching RGBA watermark template for a delivered image size.

    Returns (overlay, alpha, tw, th, anchor) or None. Exact-size templates are
    preferred; otherwise the 1024×1024 reference template is scaled to the
    image's short side.
    """
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    path = WM_TEMPLATE_BY_SIZE.get((w, h)) or WM_TEMPLATE_PATH
    if not path.is_file():
        return None
    try:
        tpl = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if tpl is None or tpl.shape[2] != 4:
            return None
        alpha = tpl[:, :, 3].astype(np.float32) / 255.0
        overlay = tpl[:, :, 0:3].astype(np.float32)
        th, tw = alpha.shape
        if (w, h) in WM_TEMPLATE_BY_SIZE:
            return overlay, alpha, tw, th, WM_TPL_REF_ANCHOR
        # 通用路径：按短边缩放 1024 参考模板
        scale = min(w, h) / 1024.0
        tw2 = max(1, int(round(WM_TPL_REF_W * scale)))
        th2 = max(1, int(round(WM_TPL_REF_H * scale)))
        anchor = max(1, int(round(WM_TPL_REF_ANCHOR * scale)))
        overlay2 = cv2.resize(overlay, (tw2, th2), interpolation=cv2.INTER_LINEAR)
        alpha2 = cv2.resize(alpha, (tw2, th2), interpolation=cv2.INTER_LINEAR)
        return overlay2, alpha2, tw2, th2, anchor
    except Exception:
        return None


def _reverse_blend_region(arr, overlay, alpha, x0, y0, opaque_cutoff=0.95):
    """Reverse alpha blending on the watermark region.

    original = (observed - alpha * overlay) / (1 - alpha)
    Pixels with alpha >= opaque_cutoff carry no recoverable original -> mask.
    Returns (out, inpaint_mask).
    """
    import numpy as np
    h, w = arr.shape[:2]
    th, tw = alpha.shape
    out = arr.copy()
    mask = np.zeros((h, w), dtype=np.uint8)
    for r in range(th):
        y = y0 + r
        if y < 0 or y >= h:
            continue
        for c in range(tw):
            x = x0 + c
            if x < 0 or x >= w:
                continue
            a = alpha[r, c]
            if a < 0.02:
                continue
            if a >= opaque_cutoff:
                mask[y, x] = 255
                continue
            inv = 1.0 / (1.0 - a)
            for ch in range(3):
                v = (arr[y, x, ch] - a * overlay[r, c, ch]) * inv
                out[y, x, ch] = max(0, min(255, round(v)))
    return out, mask


def _watermark_energy(region, alpha):
    """Residual 'watermark-likeness' after reverse blend.

    When alignment is exact, the semi-transparent text edge vanishes and local
    gradient energy inside the ink pixels drops to the same level as the rest
    of the region. A misaligned template leaves (or creates) high-contrast
    edges, which raises this metric.
    """
    import cv2
    import numpy as np
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx * gx + gy * gy)
    ink = alpha > 0.05
    if ink.sum() == 0:
        return 0.0
    return float(mag[ink].mean())


def _remove_workbuddy_watermark(content: bytes) -> bytes:
    """去掉 WorkBuddy 服务端强制添加的右下角“AI生成 WORKBUDDY”水印。

    该接口没有官方去水印参数（客户端同样无法关闭）。水印是图片生成后由
    服务端以标准 alpha 混合叠加的半透明白字（实测 α≈0.52），因此下载后
    用捕获的模板做反向 alpha 混合，逐像素还原水印下的原图，无需 inpaint。
    模板按图片短边等比缩放，并对预测位置做 ±2px 对齐搜索。

    若模板缺失或处理失败，回退到旧的固定区域 inpaint（区域可用环境变量
    WORKBUDDY_WM_REGION 调整，默认 0.55,0.88,1.0,1.0）。
    """
    debug_dir = None
    debug_on = os.environ.get("WORKBUDDY_DEBUG_IMAGES")
    if debug_on:
        debug_dir = Path(os.environ.get("WORKBUDDY_DEBUG_DIR", "logs/debug"))
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            debug_dir = None
    if debug_dir is not None:
        try:
            (debug_dir / f"wm-original-{int(time.time() * 1000)}.png").write_bytes(content)
        except OSError:
            pass
    try:
        import cv2
        import numpy as np
    except Exception:
        return content
    try:
        arr = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return content
        h, w = arr.shape[:2]
        tpl = _load_workbuddy_wm_template(w, h)
        if tpl is not None:
            overlay, alpha, tpl_w, tpl_h, anchor = tpl
            base_x0 = w - anchor - tpl_w
            base_y0 = h - anchor - tpl_h
            # ±2px 对齐搜索：选反解后水印区域梯度能量最小的偏移
            best = None
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    x0, y0 = base_x0 + dx, base_y0 + dy
                    if x0 < 0 or y0 < 0 or x0 + tpl_w > w or y0 + tpl_h > h:
                        continue
                    out, mask = _reverse_blend_region(arr, overlay, alpha, x0, y0)
                    if mask.sum() > 0:
                        out = cv2.inpaint(out, mask, 3, cv2.INPAINT_TELEA)
                    region = out[y0:y0 + tpl_h, x0:x0 + tpl_w]
                    energy = _watermark_energy(region, alpha)
                    if best is None or energy < best[0]:
                        best = (energy, x0, y0, out)
            if best is not None:
                result = best[3]
            else:
                return content
        else:
            # 模板缺失：回退到旧的固定比例区域 inpaint
            try:
                x1, y1, x2, y2 = (float(v) for v in os.environ.get(
                    "WORKBUDDY_WM_REGION", "0.55,0.88,1.0,1.0").split(","))
            except ValueError:
                x1, y1, x2, y2 = 0.55, 0.88, 1.0, 1.0
            x1i = max(0, min(w - 1, int(w * x1)))
            y1i = max(0, min(h - 1, int(h * y1)))
            x2i = max(x1i + 1, min(w, int(w * x2)))
            y2i = max(y1i + 1, min(h, int(h * y2)))
            mask = np.zeros((h, w), dtype=np.uint8)
            mask[y1i:y2i, x1i:x2i] = 255
            mask = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=2)
            result = cv2.inpaint(arr, mask, 5, cv2.INPAINT_TELEA)
        ok, buf = cv2.imencode(".png", result)
        if not ok:
            return content
        if debug_dir is not None:
            try:
                (debug_dir / f"wm-processed-{int(time.time() * 1000)}.png").write_bytes(buf.tobytes())
            except OSError:
                pass
        return buf.tobytes()
    except Exception:
        return content


async def _apiget_chat(apiget: dict, payload: dict):
    """Forward an apiget/* request to the apiget gateway (chat/completions)."""
    api_key = os.environ.get(apiget.get("api_key_env") or "", "").strip() or str(apiget.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=502, detail={"error": {
            "message": f"gateway {apiget.get('id')} key not set", "type": "gateway_not_configured"}})
    raw_model = str(payload.get("model") or "")
    model = raw_model.split("/", 1)[1] if "/" in raw_model else raw_model
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body["model"] = model
    # 与 WorkBuddy 路径一致：始终以 stream=True 调上游，非流式由聚合器转回 JSON
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
    url = f"{apiget['base_url']}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    client_wants_stream = bool(payload.get("stream"))
    rid = os.urandom(4).hex()
    _log(f"[{rid}] \u25b6 APIGET REQUEST {model} | stream={client_wants_stream}")

    if client_wants_stream:
        async def _gen():
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream("POST", url, headers=headers, json=body) as response:
                    if response.status_code != 200:
                        raw = await response.aread()
                        err = _safe_err_raw(raw, response.status_code)
                        yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                        return
                    async for chunk in response.aiter_raw():
                        yield chunk

        return StreamingResponse(_gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    t0 = time.time()
    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code != 200:
                raw = await response.aread()
                raise HTTPException(status_code=response.status_code,
                                    detail=_safe_err_raw(raw, response.status_code))
            collected = await _collect_stream(response)
    _log_finish(model, t0, collected, rid)
    return JSONResponse(content=collected)


async def _apiget_image(apiget: dict, payload: dict, endpoint: str, files: dict | None = None):
    """Forward an image request (generations/edits) to the apiget gateway.

    ``files`` (multipart, edits only) maps field name -> (filename, bytes, media_type).
    When present the upstream is called with multipart/form-data; otherwise JSON.
    """
    api_key = os.environ.get(apiget.get("api_key_env") or "", "").strip() or str(apiget.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=502, detail={"error": {
            "message": f"gateway {apiget.get('id')} key not set", "type": "gateway_not_configured"}})
    raw_model = str(payload.get("model") or "")
    model = raw_model.split("/", 1)[1] if "/" in raw_model else raw_model
    body = dict(payload)
    body["model"] = model
    url = f"{apiget['base_url']}/images/{endpoint}"
    rid = os.urandom(4).hex()
    _log(f"[{rid}] \u25b6 APIGET IMAGE {model} | {endpoint} | prompt_len={len(str(payload.get('prompt') or ''))}"
         + (f" | multipart_files={sorted(files)}" if files else ""))
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            if files:
                auth_headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
                form_data = {}
                for k, v in body.items():
                    if k in files:
                        continue
                    if isinstance(v, (list, dict)):
                        form_data[k] = json.dumps(v, ensure_ascii=False)
                    else:
                        form_data[k] = str(v)
                response = await client.post(url, headers=auth_headers, data=form_data, files=files)
            else:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
                response = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        _log(f"[{rid}] \u2717 APIGET IMAGE network error: {exc}")
        raise HTTPException(status_code=502, detail={"error": {
            "message": f"apiget image upstream failed: {exc}", "type": "upstream_error"}})
    if response.status_code != 200:
        raw = await response.aread()
        _log(f"[{rid}] \u2717 APIGET IMAGE HTTP {response.status_code}: {raw.decode('utf-8','replace')[:200]}")
        raise HTTPException(status_code=response.status_code,
                            detail=_safe_err_raw(raw, response.status_code))
    return JSONResponse(content=response.json())


@app.post("/v1/images/generations")
async def images_generations(request: Request,
                             authorization: Optional[str] = Header(default=None),
                             x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})
    raw_model = str(payload.get("model") or "")
    if not raw_model.strip() or raw_model.strip().lower() in ("auto", "default"):
        raw_model = CONFIG.get("image_route_model") or DEFAULT_IMAGE_ROUTE_MODEL
        payload = dict(payload)
        payload["model"] = raw_model
    if raw_model.lower().startswith("workbuddy/") or raw_model == "hunyuan-image-v3.0-art":
        return await _workbuddy_image_generations(payload)
    gw = _match_image_gateway(payload)
    return await _apiget_image(gw, payload, "generations")


@app.post("/v1/images/edits")
async def images_edits(request: Request,
                       authorization: Optional[str] = Header(default=None),
                       x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    content_type = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in content_type:
        try:
            form = await request.form()
        except Exception as e:
            raise HTTPException(status_code=400, detail={"error": {"message": f"bad form: {e}", "type": "invalid_request_error"}})
        payload = {}
        files = {}
        for field_name in form:
            field = form[field_name]
            if hasattr(field, "read"):  # UploadFile
                data = await field.read()
                fname = str(field.filename or "image.png")
                ctype = str(field.content_type or "application/octet-stream")
                files[field_name] = (fname, data, ctype)
            else:
                payload[str(field_name)] = str(field)
        if "image" not in files and "image_url" not in payload:
            raise HTTPException(status_code=400, detail={"error": {
                "message": "image file (multipart) or image_url is required", "type": "invalid_request_error"}})
    else:
        try:
            payload = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})
        files = None
    raw_model = str(payload.get("model") or "")
    if not raw_model.strip() or raw_model.strip().lower() in ("auto", "default"):
        raw_model = CONFIG.get("image_route_model") or DEFAULT_IMAGE_ROUTE_MODEL
        payload = dict(payload)
        payload["model"] = raw_model
    if raw_model.lower().startswith("workbuddy/") or raw_model == "hunyuan-image-v3.0-art":
        # OpenAI edits 协议 -> WorkBuddy generations(image 参考图参数)，复用官方账号池
        if files:
            import base64 as _b64
            images = []
            for field_name, (orig_name, fbytes, ctype) in files.items():
                if field_name == "image":
                    images.append(_b64.b64encode(fbytes).decode("ascii"))
            if images:
                payload["image"] = images
        elif "image_url" in payload:
            payload["image"] = str(payload["image_url"])
        return await _workbuddy_image_generations(payload)
    gw = _match_image_gateway(payload)
    return await _apiget_image(gw, payload, "edits", files=files)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request,
                           authorization: Optional[str] = Header(default=None),
                           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    pool = _pool()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    requested_model = str(payload.get("model") or "")
    for gw in CONFIG.get("gateways") or []:
        prefix = (gw.get("prefix") or "").lower()
        if (gw.get("enabled", True) and gw.get("configured") and prefix
                and requested_model.lower().startswith(prefix + "/")):
            return await _apiget_chat(gw, payload)

    # 构造后端 body：只透传已知的合法字段
    client_wants_stream = bool(payload.get("stream"))
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    requested_model = str(payload.get("model") or "")
    upstream_model = _resolve_route_model(requested_model)
    body["model"] = upstream_model
    # 海外版后端（www.workbuddy.ai）的安全策略要求首条消息必须是 system prompt，
    # 否则直接 400 "first message is not system prompt"。客户端（Codex）
    # 常常以 user 开头，这里在缺失时补一条占位 system。
    _msgs = body.get("messages")
    if isinstance(_msgs, list) and _msgs and not (
        isinstance(_msgs[0], dict)
        and str(_msgs[0].get("role") or "").lower() in ("system", "developer")
    ):
        body["messages"] = [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}] + list(_msgs)

    # 后端只支持流式：始终以 stream=True 调后端，非流式由转换器聚合
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}

    # 可选：脱敏。缓解客户端合规模板（如 ZCode 的 system 声明）被后端误判为敏感词。
    # 只对 system 角色消息里的"合规声明高频词"插入零宽空格，不改用户输入。
    if CONFIG.get("desensitize"):
        body = desensitize_body(body, roles=("system",))

    # 可选：剥离 Codex skills 段（默认关闭；如遇腾讯上游内容审核误判可设
    # WORKBUDDY_STRIP_SKILLS=1 开启）
    if os.environ.get("WORKBUDDY_STRIP_SKILLS", "0") != "0" and body.get("messages"):
        body["messages"] = _strip_skills_from_messages(body.get("messages"))

    # 日志：请求摘要
    model_name = upstream_model
    tool_names = [t.get("function", {}).get("name") for t in (payload.get("tools") or [])
                  if isinstance(t, dict)]
    last_user = _last_user_text(messages)
    rid = os.urandom(4).hex()
    route_note = f" | route={requested_model}->{upstream_model}" if requested_model != upstream_model else ""
    _log(f"[{rid}] ▶ REQUEST {model_name} | stream={client_wants_stream} | msgs={len(messages)}{route_note}"
         + (f" | tools={tool_names}" if tool_names else "")
         + (f" | last_user={_truncate(last_user, 60)!r}" if last_user else ""))
    # 完整请求体（发往后端的实际内容；若启用脱敏，这里已是脱敏后）
    _log(f"[{rid}] ── REQUEST BODY (发往后端) ──\n{json.dumps(body, ensure_ascii=False, indent=2)}")

    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_upstream(url, pool, body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：后端只支持流式，这里把后端 SSE 聚合成单个 chat.completion 响应
    collected = await _collect_with_pool(url, pool, body, model_name, rid)
    _log_finish(model_name, t0, collected, rid)
    return JSONResponse(content=collected)


def _last_user_text(messages: list) -> str:
    """取最后一条 user 消息的文本，用于日志预览。"""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    return str(blk.get("text", ""))
            return ""
        return str(content)
    return ""


def _log_finish(model_name: str, t0: float, result: dict, rid: str = ""):
    """记录一次完成的请求：耗时 / finish_reason / usage / 工具调用 / 审核拦截 + 完整响应。"""
    elapsed = time.time() - t0
    prefix = f"[{rid}] " if rid else ""
    choice = (result.get("choices") or [{}])[0]
    finish = choice.get("finish_reason")
    msg = choice.get("message") or {}
    tcs = msg.get("tool_calls") or []
    usage = result.get("usage") or {}
    tag = ""
    if finish == "content-filter":
        tag = " ⚠️内容审核拦截"
    tc_names = [t.get("function", {}).get("name") for t in tcs]
    _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | finish={finish}{tag}"
         + (f" | tool_calls={tc_names}" if tc_names else "")
         + f" | tokens={usage.get('total_tokens', '?')}")
    # 完整响应体
    _log(f"{prefix}── RESPONSE BODY ──\n{json.dumps(result, ensure_ascii=False, indent=2)}")


class UpstreamSSEError(RuntimeError):
    def __init__(self, status: int, raw: bytes):
        super().__init__(raw.decode("utf-8", "replace")[:500])
        self.status = status
        self.raw = raw


async def _collect_stream(response: httpx.Response) -> dict:
    """消费后端的 OpenAI SSE 流，聚合成单个非流式 chat.completion 对象。

    合并所有 chunk 的 delta（content / tool_calls），并取 usage / finish_reason。
    """
    content_parts: list[str] = []
    # tool_calls: index -> {id, name, arguments(分片拼接)}
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None
    saw_choice = False
    saw_done = False

    async for line in response.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            saw_done = True
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if chunk.get("error"):
            error = chunk["error"]
            code = error.get("code") if isinstance(error, dict) else None
            status = code if isinstance(code, int) and 400 <= code <= 599 else 502
            raise UpstreamSSEError(status, json.dumps(chunk, ensure_ascii=False).encode("utf-8"))
        model = chunk.get("model") or model
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            saw_choice = True
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    if not saw_choice or (not saw_done and not finish_reason):
        raise UpstreamSSEError(502, b"empty or incomplete upstream SSE response")

    tcs = None
    if tool_calls:
        tcs = [
            {"id": v["id"], "type": "function",
             "function": {"name": v["name"], "arguments": v["arguments"]}}
            for _, v in sorted(tool_calls.items())
        ]
        finish_reason = finish_reason or "tool_calls"

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if tcs:
        message["tool_calls"] = tcs
    return {
        "id": "chatcmpl-" + os.urandom(12).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "unknown",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _safe_err_raw(raw: bytes, status: int) -> dict:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"error": {"message": raw.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status}}


def _account_failure(status: int, raw: bytes) -> tuple[str, int] | None:
    text = raw.decode("utf-8", "replace").lower()
    if status in {401, 403} or "token" in text and ("expired" in text or "invalid" in text):
        return "登录态失效，等待重新认证或刷新", 600
    if status == 429 or "rate limit" in text or "too many requests" in text:
        return "账号触发限流", 60
    if status == 402 or any(marker in text for marker in ("credit", "balance", "quota", "积分", "余额")):
        return "账号额度暂不可用", 3600
    if status >= 500:
        return f"上游 HTTP {status}", 30
    return None


async def _headers_for_candidate(pool: AccountPool, candidate, rid: str) -> dict | None:
    try:
        return await asyncio.to_thread(candidate.manager.get_headers)
    except Exception as exc:
        pool.mark_failure(candidate.ref, "凭据刷新失败", 600)
        _log(f"[{rid}] 账号 {candidate.ref} 凭据刷新失败：{_truncate(exc, 160)}")
        return None


async def _collect_with_pool(url: str, pool: AccountPool, body: dict,
                             model_name: str, rid: str) -> dict:
    candidates = pool.candidates()
    if not candidates:
        raise HTTPException(status_code=503, detail={"error": {
            "message": "WorkBuddy 账号池当前全部处于冷却状态",
            "type": "account_pool_unavailable",
        }})
    last_status = 502
    last_raw = b"all WorkBuddy accounts failed"
    for candidate in candidates:
        headers = await _headers_for_candidate(pool, candidate, rid)
        if headers is None:
            continue
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream("POST", url, headers=headers, json=body) as response:
                    if response.status_code != 200:
                        raw = await response.aread()
                        last_status, last_raw = response.status_code, raw
                        failure = _account_failure(response.status_code, raw)
                        _log(f"[{rid}] ✗ HTTP {response.status_code} | account={candidate.ref} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
                        if failure:
                            pool.mark_failure(candidate.ref, failure[0], failure[1])
                            continue
                        raise HTTPException(status_code=response.status_code,
                                            detail=_safe_err_raw(raw, response.status_code))
                    try:
                        collected = await _collect_stream(response)
                    except UpstreamSSEError as exc:
                        last_status, last_raw = exc.status, exc.raw
                        failure = _account_failure(exc.status, exc.raw)
                        if failure is None and exc.status >= 500:
                            failure = ("上游 SSE 响应无效", 30)
                        if failure:
                            pool.mark_failure(candidate.ref, failure[0], failure[1])
                            continue
                        raise HTTPException(status_code=exc.status,
                                            detail=_safe_err_raw(exc.raw, exc.status))
                    pool.mark_success(candidate.ref)
                    return collected
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            # 上游 IP 可能已轮换：立即在后台重新解析固定 IP，缩短恢复时间
            threading.Thread(target=_pin_refresh, daemon=True).start()
            last_status, last_raw = 502, str(exc).encode("utf-8", "replace")
            pool.mark_failure(candidate.ref, "上游网络错误", 30)
            _log(f"[{rid}] ✗ 网络错误 | account={candidate.ref} | {model_name} | {exc}")
    raise HTTPException(status_code=last_status, detail=_safe_err_raw(last_raw, last_status))


def _sse_error_event(payload: bytes) -> tuple[int, bytes] | None:
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
        return status, json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return None


async def _stream_upstream(url: str, pool: AccountPool, body: dict,
                           model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """Forward SSE and switch accounts only before the first response chunk.

    Retrying after bytes have reached the client would duplicate partial output,
    so account failover is deliberately limited to refresh errors, transport
    errors, non-200 responses, and SSE error events before the first event is
    released to the client.
    """
    finish_reason = None
    tool_names: list[str] = []
    usage: dict = {}
    saw_filter = False
    buf = b""
    capture_limit = 1_048_576 if CONFIG.get("log_path") else 0
    raw_capture = bytearray()
    prefix = f"[{rid}] " if rid else ""

    def _feed(chunk: bytes):
        nonlocal finish_reason, saw_filter, buf
        # 行缓冲解析：把累计的 chunk 按 data: 行切出来统计
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("usage"):
                usage.update(obj["usage"])
            for ch in obj.get("choices") or []:
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
                for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                    nm = (tc.get("function") or {}).get("name")
                    if nm:
                        tool_names.append(nm)
            # 内容审核拦截常以 content-filter 或特殊中文文案返回
            try:
                text_repr = data.decode("utf-8", "replace")
            except Exception:
                text_repr = ""
            if "content-filter" in text_repr or "敏感" in text_repr or "审核" in text_repr:
                saw_filter = True
        if len(buf) > 65_536:
            buf = buf[-65_536:]

    candidates = pool.candidates()
    if not candidates:
        yield _err_event(b"all WorkBuddy accounts are cooling down", 503)
        return
    last_status = 502
    last_error = b"all WorkBuddy accounts failed"
    completed = False
    for candidate in candidates:
        headers = await _headers_for_candidate(pool, candidate, rid)
        if headers is None:
            continue
        started = False
        retry_candidate = False
        pending = bytearray()
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", url, headers=headers, json=body) as response:
                    if response.status_code != 200:
                        error = await response.aread()
                        last_status, last_error = response.status_code, error
                        failure = _account_failure(response.status_code, error)
                        _log(f"{prefix}✗ HTTP {response.status_code} | account={candidate.ref} | {model_name} | {_truncate(error.decode('utf-8','replace'),200)}")
                        if failure:
                            pool.mark_failure(candidate.ref, failure[0], failure[1])
                            continue
                        yield _err_event(error, response.status_code)
                        return
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        if not started:
                            pending.extend(chunk)
                            event_complete = b"\n\n" in pending or b"\r\n\r\n" in pending
                            if not event_complete and len(pending) < 131_072:
                                continue
                            first_payload = bytes(pending)
                            stream_error = _sse_error_event(first_payload)
                            if stream_error:
                                last_status, last_error = stream_error
                                failure = _account_failure(last_status, last_error)
                                if failure is None and last_status >= 500:
                                    failure = ("上游 SSE 错误", 30)
                                if failure:
                                    pool.mark_failure(candidate.ref, failure[0], failure[1])
                                    retry_candidate = True
                                    break
                                yield _err_event(last_error, last_status)
                                return
                            pool.mark_success(candidate.ref)
                            started = True
                            pending.clear()
                            chunk = first_payload
                        if capture_limit and len(raw_capture) < capture_limit:
                            remaining = capture_limit - len(raw_capture)
                            raw_capture.extend(chunk[:remaining])
                        _feed(chunk)
                        yield chunk
                    if retry_candidate:
                        continue
                    if not started and pending:
                        final_payload = bytes(pending)
                        stream_error = _sse_error_event(final_payload)
                        if stream_error:
                            last_status, last_error = stream_error
                            failure = _account_failure(last_status, last_error) or ("上游 SSE 错误", 30)
                            pool.mark_failure(candidate.ref, failure[0], failure[1])
                            continue
                        pool.mark_success(candidate.ref)
                        started = True
                        _feed(final_payload)
                        yield final_payload
                    elif not started:
                        pool.mark_failure(candidate.ref, "上游返回空流", 30)
                        last_status, last_error = 502, b"empty upstream SSE response"
                        continue
                    completed = True
                    break
        except httpx.HTTPError as exc:
            last_status, last_error = 502, str(exc).encode("utf-8", "replace")
            if started:
                _log(f"{prefix}✗ 流中断 | account={candidate.ref} | {model_name} | {exc}")
                yield _err_event(last_error, 502)
                return
            threading.Thread(target=_pin_refresh, daemon=True).start()
            pool.mark_failure(candidate.ref, "上游网络错误", 30)
            _log(f"{prefix}✗ 网络错误 | account={candidate.ref} | {model_name} | {exc}")

    if not completed:
        yield _err_event(last_error, last_status)
        return

    # 流结束：输出完成日志
    elapsed = time.time() - t0 if t0 else 0
    tag = " ⚠️内容审核拦截" if (saw_filter or finish_reason == "content-filter") else ""
    _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | stream finish={finish_reason}{tag}"
         + (f" | tool_calls={tool_names}" if tool_names else "")
         + f" | tokens={usage.get('total_tokens', '?')}")
    if raw_capture:
        suffix = "\n[truncated at 1 MiB]" if len(raw_capture) >= capture_limit else ""
        _log(f"{prefix}── RESPONSE RAW SSE ──\n{bytes(raw_capture).decode('utf-8','replace')}{suffix}")


def _safe_err(r: httpx.Response) -> dict:
    try:
        return {"error": r.json()}
    except Exception:
        return {"error": {"message": r.text[:500], "type": "upstream_error", "code": r.status_code}}


def _err_event(msg: bytes, status: int) -> bytes:
    # 以 OpenAI SSE 错误 chunk 形式返回
    import json as _json, time as _time
    chunk = {
        "error": {"message": msg.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status},
    }
    return f"data: {_json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def preflight(pool: AccountPool | None = None) -> bool:
    summary = pool.summary() if isinstance(pool, AccountPool) else {
        "count": 0, "ready": 0, "auth_dir": str(AUTH_POOL_DIR.resolve()), "accounts": [],
    }
    sys.stderr.write("==== 预检 ====\n")
    sys.stderr.write(f"平台      : {sys.platform}\n")
    sys.stderr.write(f"Python    : {sys.version.split()[0]}\n")
    sys.stderr.write(f"后端      : {BACKEND} (直连，原生 function calling)\n")
    sys.stderr.write(f"本地账号池: {summary['auth_dir']}\n")
    sys.stderr.write(f"账号数量  : {summary['count']}（可用 {summary['ready']}）\n")
    ok = summary["count"] > 0
    if not ok:
        sys.stderr.write("\n[警告] 本地账号池为空。请先登录 WorkBuddy，再从 Dashboard 导入当前账号。\n")
    sys.stderr.write("================\n")
    return ok


def main():
    ap = argparse.ArgumentParser(description="CodeBuddy -> OpenAI 兼容转换器（直连后端）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--api-key", default=os.environ.get("CODEBUDDY2OPENAI_KEY", ""),
                    help="可选：要求客户端携带的 API key（默认不校验）")
    ap.add_argument("--auth-dir", default=str(AUTH_POOL_DIR),
                    help="Bridge 独立账号池目录（默认 ./auths，不修改官方 WorkBuddy 登录目录）")
    ap.add_argument("--log", default=None, metavar="PATH",
                    help="开启日志并写到该文件（如 --log converter.log 或 --log /tmp/cb.log）。"
                         "不传则不记日志。")
    ap.add_argument("--desensitize", action="store_true",
                    help="启用脱敏：对 system 消息里的合规模板敏感词（DoS/exploit/credential 等）"
                         "插入零宽空格，缓解被后端内容审核误拦。默认关闭。")
    ap.add_argument("--skip-check", action="store_true", help="跳过启动预检")
    args = ap.parse_args()

    if not _is_loopback_host(args.host):
        ap.error("出于凭据安全考虑，WorkBuddy2Codex 只允许监听 localhost/127.0.0.1/::1")

    CONFIG["api_key"] = args.api_key
    CONFIG["desensitize"] = args.desensitize
    # --log 直接指定文件路径即开启；不传则不记
    CONFIG["log_path"] = args.log if args.log else os.environ.get("CODEBUDDY2OPENAI_LOG")
    pool = AccountPool(
        Path(args.auth_dir),
        manager_factory=CredentialManager,
        official_finder=find_auth_file,
        auto_import=True,
        forbidden_dirs=auth_dirs(),
    )
    CONFIG["pool"] = pool
    CONFIG["account_service"] = WorkBuddyAccountService(pool, BRIDGE_VERSION)
    CONFIG["checkin_service"] = WorkBuddyCheckinService(pool)

    # Buddy 鍔犳娊绔欏～: 鍚姩鏃朵竴娆¤嚟璐﹀彿姹犲凡锷ㄦ€佹晳鍙栧綋澶╀粖棰濓紙宸叉互棰濇棤浣滃亣锛?
    def _auto_checkin_startup() -> None:
        try:
            import asyncio
            svc = CONFIG.get("checkin_service")
            if isinstance(svc, WorkBuddyCheckinService):
                result = asyncio.new_event_loop().run_until_complete(svc.claim_all())
                _log(f"[checkin] startup auto-claim: {result.get('claimed_accounts')} accounts, +{result.get('claimed_total')} credits")
        except Exception as exc:
            _log(f"[checkin] startup auto-claim skipped: {exc}")

    _auto_checkin_startup()


    if not args.skip_check:
        preflight(pool)

    sys.stderr.write(f"\n✅ WorkBuddy2Codex v{BRIDGE_VERSION}\n")
    sys.stderr.write(f"   监听 http://{args.host}:{args.port}（直连后端，原生 function calling）\n")
    sys.stderr.write("   GET  /v1/models\n")
    sys.stderr.write("   POST /v1/chat/completions   (原生 tools/tool_calls，支持流式)\n")
    sys.stderr.write("   GET  /health\n")
    if args.api_key:
        sys.stderr.write("   鉴权已启用（API key 已设置）\n")
    if CONFIG["log_path"]:
        sys.stderr.write(f"   日志      : {CONFIG['log_path']}\n")
    if args.desensitize:
        sys.stderr.write("   脱敏      : 已启用（system 合规词零宽处理）\n")
    sys.stderr.write(f"   账号池    : {pool.auth_dir}\n")
    sys.stderr.write("按 Ctrl+C 退出。\n\n")

    # 启动时写一条标记
    _log(f"==== converter 启动 ====")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", loop="asyncio")


if __name__ == "__main__":
    main()
