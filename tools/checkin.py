#!/usr/bin/env python3
"""codex-checkin — 各 AI 订阅平台的自动签到守护。

签到覆盖整个机队（15 个节点），分三类：
  * 真签到（有端点，会真的领）
      - xhx   商汤小浣熊：POST /api/web/desktop/v1/login/points/grant（每日登录积分），
              契约逆向自官方桌面端 app.asar；data.granted=false 表示今日已发放，
              签到后拉 points/balance 记账
      - workbuddy / workbuddy-gpt  腾讯 Buddy 加油站：经桥 /ui/checkin/claim 领取，
              每账号 credit 与连签天数来自桥 /ui/checkin
  * 无签到端点（12 个节点）：qoder / codely / trae / lingxi / cline / qwen / gemini /
      catpaw / antigravity / zcode / stepfun / tokendance。已逐个核实上游没有每日签到
      接口（codely 的 LiteLLM /key/info、/user/info 实测 403），记为 na：不是失败，
      但仍读出该节点账号与积分，让 15 个节点在面板里都有带数字的一行。

每个节点的账目见 tools/node_credits.py（stdlib，可单独跑：python3 node_credits.py）。

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
# A loopback health check must never inherit HTTP_PROXY. Measured 2026-09-29:
# under a shell with HTTP_PROXY set, the bridge call failed with
# httpx.ConnectError("All connection attempts failed") while the bridge answered
# fine on 127.0.0.1 -- httpx routed the loopback request through the proxy.
# httpx mounts with a None transport disable proxy use for those hosts only;
# every other host still honours the operator's proxy settings.
# tools/node_credits.py builds its urllib openers the same way, and its own
# test drives a dead proxy against a live loopback server to prove it.
LOOPBACK_MOUNTS = {pattern: None for pattern in (
    "all://127.0.0.1", "all://localhost", "all://[::1]")}


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


# ---------------- WorkBuddy：Buddy 加油站真签到 ----------------

# The dashboard claim API is cookie-protected, but the bridge hands that cookie
# to anyone who asks for GET / (the same one the browser dashboard gets), and
# the endpoints still demand loopback + JSON + a matching Origin. Measured
# 2026-10-01 on 127.0.0.1:8787: GET /ui/checkin answered with the real
# activity and per-account state (MichaelQiu, today_checked_in=true,
# credit=100, streak_days=2), so this task can do the actual claim instead of
# only confirming the bridge is alive.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import node_credits


def _workbuddy_task(name: str) -> dict:
    state = node_credits.workbuddy_checkin(name)
    if not state.get("ok"):
        return {"ok": False, "detail": state.get("detail") or "bridge unreachable"}
    accounts = state.get("accounts") or []
    broken = [a for a in accounts if not a.get("ok")]
    if broken:
        # Not "unclaimed", just unreadable: the overseas bridge reports
        # ok=false with 状态读取失败 while its activity is unavailable.
        return {"ok": False,
                "detail": "%s: %s" % (broken[0].get("name") or name,
                                      broken[0].get("message") or "状态读取失败")}
    unclaimed = int(state.get("unclaimed_count") or 0)
    claimed = None
    if unclaimed:
        claimed = node_credits.workbuddy_claim(name)
        state = node_credits.workbuddy_checkin(name)
        accounts = state.get("accounts") or []
    activity = state.get("activity") or {}
    credits = sum(node_credits._num(a.get("credit")) or 0 for a in accounts)
    streak = max((a.get("streak_days") or 0) for a in accounts) if accounts else 0
    detail = "账号 %d 个；今日 %s；连签 %s 天；活动每日 %s credits" % (
        len(accounts),
        "已领取" if not state.get("unclaimed_count") else "仍有 %s 个待领" % state.get("unclaimed_count"),
        streak, node_credits._pretty(node_credits._num(activity.get("daily_credit"))))
    if claimed is not None:
        detail += "；本次领取 %s" % ("成功" if claimed.get("ok") else "失败")
    return {"ok": True, "granted": bool(claimed and claimed.get("ok")),
            "detail": detail, "available_points": credits,
            "credits_unit": "credits", "streak_days": streak,
            "credits_source": "bridge /ui/checkin"}


async def task_workbuddy(client: httpx.AsyncClient) -> dict:
    """Claim today's Buddy 加油站 credits for every account in the pool.

    node_credits is stdlib urllib and this daemon is async httpx, so the call
    runs in a worker thread rather than blocking the loop.
    """
    return await asyncio.to_thread(_workbuddy_task, "workbuddy")


async def task_workbuddy_gpt(client: httpx.AsyncClient) -> dict:
    """The same Buddy 加油站 on the overseas bridge (port 8788)."""
    return await asyncio.to_thread(_workbuddy_task, "workbuddy-gpt")


# ---------------- 其余节点：无每日签到端点，但仍看积分 ----------------

# Verified 2026-10-01 against each upstream's own surface: these twelve expose
# no daily check-in endpoint. docs/codex-checkin-runbook.md records the same
# conclusion for 灵犀/qoder/codely/trae, and codely (budget) / trae (points
# plan) were probed directly -- codely's LiteLLM /key/info and /user/info both
# answer 403 from nginx, so there is no balance to read there either.
#
# Each still returns the account and credits the node *does* expose, so the
# 签到 panel has a row with a real number for every node instead of silently
# dropping them. "na" marks "this node has no such endpoint", which is a
# different thing from a failed attempt and must not read as one.
NO_CHECKIN_NODES = ("qoder", "codely", "trae", "lingxi", "cline", "qwen",
                    "gemini", "catpaw", "antigravity", "zcode", "stepfun",
                    "tokendance")


def _no_checkin_task(name: str) -> dict:
    row = node_credits.read_node(name)
    return {"ok": False, "na": True,
            "detail": "%s；%s" % (node_credits.NO_CHECKIN_NOTE, row["detail"]),
            "available_points": row["credits_value"],
            "credits_unit": row["credits_unit"],
            "credits_source": row["credits_source"],
            "credits_note": row["credits_note"],
            "account": row["account"], "up": row["up"]}


def no_checkin_task(name: str):
    async def task(client: httpx.AsyncClient) -> dict:
        return await asyncio.to_thread(_no_checkin_task, name)
    task.__name__ = "task_%s" % name.replace("-", "_")
    return task


# ---------------- 任务注册表（新平台按此格式扩展） ----------------

TASKS = {
    "xhx": {"desc": "商汤小浣熊 每日登录积分", "fn": task_xhx},
    "workbuddy": {"desc": "WorkBuddy Buddy 加油站（国内）", "fn": task_workbuddy},
    "workbuddy-gpt": {"desc": "WorkBuddy Buddy 加油站（海外）", "fn": task_workbuddy_gpt},
}
for _name in NO_CHECKIN_NODES:
    TASKS[_name] = {"desc": "%s（无签到端点，仅看积分）"
                    % node_credits.VENDORS.get(_name, _name),
                    "fn": no_checkin_task(_name)}


async def run_tasks(names: list[str]) -> int:
    state = load_state()
    date = today()
    rc = 0
    async with httpx.AsyncClient(timeout=60, mounts=LOOPBACK_MOUNTS) as client:
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
            elif result.get("na"):
                # Neither a success nor a failure: the node has no endpoint to
                # call. The credits it did report are still worth keeping.
                log(f"{name} N/A: {result.get('detail')}")
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
        if s.get("na"):
            mark = "➖ 无签到端点"
        else:
            mark = "✅ 今日已签" if done else "❌ 今日未签"
        credits = s.get("available_points")
        unit = s.get("credits_unit") or ""
        print(f"- {name} ({task['desc']}): {mark}"
              f" | 上次: {s.get('at', '从未')} | {s.get('detail', '')}"
              f" | 积分: {credits if credits is not None else '-'} {unit}".rstrip())


def main() -> int:
    ap = argparse.ArgumentParser(
        description="机队签到 + 积分查看（15 个节点）")
    ap.add_argument("--run-now", action="store_true", help="立即执行签到")
    ap.add_argument("--force", action="store_true", help="忽略今日已签状态强制执行")
    ap.add_argument("--status", action="store_true", help="查看状态与积分")
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
