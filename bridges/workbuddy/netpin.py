"""海外后端网络适配（MacPacket fake-ip / 系统代理 / uvloop）。

workbuddy-gpt 直连 www.workbuddy.ai，本机网络有三处会打坏这条直连：

  1) fake-ip DNS：该域名被解析成 198.18.x，需经公共 DoH 取真实 IP，并在
     socket.getaddrinfo 层固定，周期性刷新，让所有 httpx Client 自动生效。
  2) 系统代理：macOS“系统设置”里的 127.0.0.1:1082 对海外后端返回 503，
     强制桥内所有 httpx Client 直连（trust_env=False），DoH 查询也走直连。
  3) uvloop：uvloop 的 TLS 握手会被海外上游直接 EOF（uvicorn 默认
     loop=auto 会优先使用 uvloop），因此 core.py 对海外变体强制 asyncio。

只有海外变体（WORKBUDDY_PROVIDER=gpt）会调用 install()；国内变体不装载。
"""

from __future__ import annotations

import json
import os
import socket as _socket
import sys
import threading
import time
import urllib.request as _urllib_request
from urllib.parse import urlparse

import httpx

_PIN_DOH_TEMPLATES = (
    "https://doh.pub/dns-query?name={host}&type=A",
    "https://dns.alidns.com/dns-query?name={host}&type=A",
    "https://1.1.1.1/dns-query?name={host}&type=A",
)
_PIN_STATIC_FALLBACK = ("43.160.158.125",)

_state: dict = {
    "hosts": (),
    "user_agent": "",
    "map": {},
    "lock": threading.Lock(),
    "original_getaddrinfo": _socket.getaddrinfo,
    "refresh_seconds": int(os.environ.get("WORKBUDDY_PIN_REFRESH_SECONDS", "600")),
}


def _doh_lookup(host: str) -> str:
    """经公共 DoH 解析 A 记录，返回第一个 IPv4 地址。"""
    for template in _PIN_DOH_TEMPLATES:
        try:
            request = _urllib_request.Request(
                template.format(host=host),
                headers={
                    "accept": "application/dns-json",
                    "user-agent": _state["user_agent"],
                },
            )
            with _doh_opener.open(request, timeout=6) as response:
                payload = json.loads(response.read().decode("utf-8", "ignore"))
            for answer in payload.get("Answer") or []:
                if answer.get("type") == 1 and answer.get("data"):
                    return str(answer["data"])
        except Exception:
            continue
    return ""


def _tcp_reachable(host: str, port: int = 443, timeout: float = 4.0) -> bool:
    try:
        with _socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _resolve(host: str) -> str:
    override = os.environ.get("WORKBUDDY_PINNED_IP", "").strip()
    candidates = [override] if override else []
    candidates.append(_doh_lookup(host))
    candidates.extend(_PIN_STATIC_FALLBACK)
    for candidate in candidates:
        if candidate and _tcp_reachable(candidate):
            return candidate
    return ""


def refresh() -> None:
    """重新解析并固定各 host 的 A 记录（失败时保留上一次的值）。"""
    for host in _state["hosts"]:
        address = _resolve(host)
        if not address:
            continue
        with _state["lock"]:
            changed = _state["map"].get(host) != address
            _state["map"][host] = address
        if changed:
            sys.stderr.write(f"[dns-pin] {host} -> {address}\n")
            sys.stderr.flush()


def refresh_async() -> None:
    """后台线程刷新，用于上游报错后立刻缩短恢复时间。"""
    threading.Thread(target=refresh, daemon=True).start()


def _pinned_getaddrinfo(host, *args, **kwargs):
    if isinstance(host, bytes):
        try:
            host = host.decode()
        except Exception:
            host = None
    if isinstance(host, str):
        with _state["lock"]:
            pinned = _state["map"].get(host)
        if pinned:
            host = pinned
    return _state["original_getaddrinfo"](host, *args, **kwargs)


def _install_no_proxy() -> None:
    """让 httpx 忽略系统设置里的全局代理，避免被 127.0.0.1:1082 劫持成 503。"""
    for client_cls in (httpx.Client, httpx.AsyncClient):
        original_init = client_cls.__init__

        def _forced_direct_init(self, *args, _original=original_init, **kwargs):
            kwargs["trust_env"] = False
            _original(self, *args, **kwargs)

        client_cls.__init__ = _forced_direct_init


def _loop() -> None:
    while True:
        time.sleep(_state["refresh_seconds"])
        try:
            refresh()
        except Exception:
            pass


def install(host: str, user_agent: str) -> None:
    """为指定 host 固定 DNS 并强制直连。重复调用只生效一次。"""
    if _state["hosts"]:
        return
    _state["user_agent"] = user_agent
    _state["hosts"] = (urlparse(host).hostname or host,)

    try:
        _install_no_proxy()
    except Exception as exc:  # pragma: no cover
        sys.stderr.write(f"[dns-pin] no-proxy init failed: {exc}\n")

    _socket.getaddrinfo = _pinned_getaddrinfo

    try:
        refresh()
    except Exception as exc:  # 初始化失败时保持原有解析行为
        sys.stderr.write(f"[dns-pin] init failed: {exc}\n")

    threading.Thread(target=_loop, name="workbuddy-dns-pin", daemon=True).start()


# DoH 查询同样直连，避开系统代理拦截
_doh_opener = _urllib_request.build_opener(_urllib_request.ProxyHandler({}))
