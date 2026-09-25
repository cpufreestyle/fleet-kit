#!/usr/bin/env python3
"""codex-checkin — 各 AI 订阅平台的自动签到守护。

当前已实现的签到任务：
  * xhx        商汤小浣熊：POST /api/web/desktop/v1/login/points/grant（每日登录积分）
               契约逆向自官方桌面端 app.asar（build/desktop-renderer chunk）：
               头 X-Client-Platform: desktop-macos / X-Client-Version: v<版本>，Bearer 鉴权；
               响应 data.granted=false 表示今日已发放（桌面端启动时也会调，天然幂等）。

用法：
  python3 checkin.py --run-now [task ...]   # 立即签到（可指定任务，默认全部）
  python3 checkin.py --status               # 查看各任务上次结果与积分余额
  python3 checkin.py --daemon               # 常驻模式（ LaunchAgent 用，每日 09:00 由 plist 触发 ）

状态：~/.codex-checkin/state.json（每任务上次成功日期/结果/余额）
日志：~/.codex-checkin/checkin.log
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx

HOME = Path(os.environ.get("CODEX_CHECKIN_HOME") or (Path.home() / ".codex-checkin"))
STATE_FILE = HOME / "state.json"
LOG_FILE = HOME / "checkin.log"
CST = timezone(timedelta(hours=8))  # 国内平台按北京时间记“今日”


def log(msg: str) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = HOME / ".state.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def today() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def jwt_exp(token: str) -> float:
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        return float(json.loads(base64.urlsafe_b64decode(p)).get("exp") or 0)
    except Exception:
        return 0.0


# ---------------- 小浣熊（SenseTime Raccoon）每日登录积分 ----------------

XHX_WEB = os.environ.get("XHX_WEB_BASE_URL") or "https://xiaohuanxiong.com"
XHX_VERSION = os.environ.get("XHX_CLIENT_VERSION") or "v1.0.28"


def xhx_auth_file() -> Path:
    root = os.environ.get("BOX_AGENT_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".box-agent", "config")
    return Path(root) / "auth.json"


def xhx_load_auth() -> Optional[dict]:
    try:
        auth = json.loads(xhx_auth_file().read_text(encoding="utf-8"))
        return auth if auth.get("access_token") else None
    except Exception:
        return None


def xhx_save_auth(auth: dict) -> None:
    path = xhx_auth_file()
    tmp = path.with_name(f".auth.json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(auth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


async def xhx_token(client: httpx.AsyncClient) -> str:
    """取可用 access_token：过期则刷新（单次轮换，必须立即落盘）。"""
    auth = xhx_load_auth()
    if auth is None:
        raise RuntimeError("小浣熊未登录（~/.box-agent/config/auth.json 不存在），请打开「商汤小浣熊」桌面 app 登录")
    if jwt_exp(auth["access_token"]) > time.time() + 60:
        return auth["access_token"]
    r = await client.post(f"{XHX_WEB}/api/web/auth/v1/refresh",
                          json={"refresh_token": auth.get("refresh_token") or ""})
    data = (r.json() or {}).get("data") or {}
    token = data.get("access_token")
    if token:
        xhx_save_auth({**auth, "access_token": token,
                       "refresh_token": data.get("refresh_token") or auth.get("refresh_token")})
        return token
    # 刷新失败：桌面端可能已重同步，重读盘上的
    fresh = xhx_load_auth()
    if fresh and jwt_exp(fresh["access_token"]) > time.time() + 60:
        return fresh["access_token"]
    raise RuntimeError(f"小浣熊 token 刷新失败（http {r.status_code}）")


async def xhx_balance(client: httpx.AsyncClient, token: str) -> dict:
    r = await client.get(f"{XHX_WEB}/api/web/points/v1/balance",
                         headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 200:
        return (r.json() or {}).get("data") or {}
    return {}


async def task_xhx(client: httpx.AsyncClient) -> dict:
    token = await xhx_token(client)
    headers = {"Authorization": f"Bearer {token}",
               "X-Client-Platform": os.environ.get("XHX_CLIENT_PLATFORM") or "desktop-macos",
               "X-Client-Version": XHX_VERSION}
    r = await client.post(f"{XHX_WEB}/api/web/desktop/v1/login/points/grant", headers=headers)
    if r.status_code != 200:
        return {"ok": False, "detail": f"grant http {r.status_code}: {r.text[:120]}"}
    data = (r.json() or {}).get("data") or {}
    granted = bool(data.get("granted"))
    popup = data.get("popup")
    bal = await xhx_balance(client, token)
    return {"ok": True, "granted": granted,
            "detail": ("今日积分已发放" if granted else "今日已发放过（桌面端启动时已领或非首登）")
                      + (f"，popup={json.dumps(popup, ensure_ascii=False)[:120]}" if popup else ""),
            "available_points": bal.get("available_points"),
            "daily_points": bal.get("daily_points"),
            "reward_points": bal.get("reward_points")}


# ---------------- 任务注册表（新平台按此格式扩展） ----------------

TASKS = {
    "xhx": {"desc": "商汤小浣熊 每日登录积分", "fn": task_xhx},
}


async def run_tasks(names: list[str]) -> int:
    state = load_state()
    date = today()
    rc = 0
    async with httpx.AsyncClient(timeout=60) as client:
        for name in names:
            task = TASKS[name]
            prev = (state.get(name) or {})
            if prev.get("last_success_date") == date and "--force" not in sys.argv:
                log(f"{name} ({task['desc']}) 今日已成功，跳过")
                continue
            try:
                result = await task["fn"](client)
            except Exception as e:
                result = {"ok": False, "detail": str(e)[:200]}
            result["at"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
            state[name] = {**prev, **result}
            if result.get("ok"):
                state[name]["last_success_date"] = date
                state[name]["last_balance"] = result.get("available_points")
                log(f"{name} OK: {result.get('detail')} 余额={result.get('available_points')}")
            else:
                rc = 1
                log(f"{name} FAIL: {result.get('detail')}")
    save_state(state)
    return rc


def show_status() -> None:
    state = load_state()
    print(f"今日（{today()}）状态：")
    for name, task in TASKS.items():
        s = state.get(name) or {}
        done = s.get("last_success_date") == today()
        print(f"- {name} ({task['desc']}): {'✅ 今日已签' if done else '❌ 今日未签'}"
              f" | 上次: {s.get('at', '从未')} | {s.get('detail', '')}"
              f" | 余额: {s.get('available_points', '-')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-now", action="store_true", help="立即执行签到")
    ap.add_argument("--force", action="store_true", help="忽略今日已签状态强制执行")
    ap.add_argument("--status", action="store_true", help="查看状态")
    ap.add_argument("--daemon", action="store_true", help="守护模式（由 LaunchAgent 每日触发）")
    ap.add_argument("tasks", nargs="*", help="指定任务名（默认全部）")
    args = ap.parse_args()

    names = args.tasks or list(TASKS.keys())
    unknown = [n for n in names if n not in TASKS]
    if unknown:
        print(f"未知任务: {unknown}；可用: {list(TASKS.keys())}")
        return 2

    if args.status:
        show_status()
        return 0
    if args.run_now or args.daemon:
        return asyncio.run(run_tasks(names))
    show_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
