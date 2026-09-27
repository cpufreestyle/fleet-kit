#!/usr/bin/env python3
"""app-ticket.py — 让真正的 ZCode.app 自己去拿验证码票据。

背景（2026-09-27 实测结论，见 docs/zcode2codex-runbook.md）：

  * 滑块位置识别是对的（缺口检测命中率高，误差 <2px），但**自动拖动一定被阿里云
    拒绝**：Playwright 里是 F001/F015，放到 ZCode.app 自己的渲染进程里拖，
    同样 F001/F015。位置对不对不是变量，行为特征才是。
  * 人工在**另一个浏览器**（你的 Chrome）拖出来的票据，交给 ZCode.app 发请求，
    上游回 `3012 request has been blocked due to unusual activity`；不带票据
    则是 `3007 captcha verify failed`。
  * 账号本身是好的：billing/current 200、Weekend Build 套餐 active、
    GLM-5.3-Flash entitlement 在、客户端配置 200、出口 IP 两边一致。

  结论：阿里云票据绑定产生它的设备/浏览器指纹。**跨上下文搬票据必被风控**。
  唯一能过的是「App 自己弹滑块 → App 自己发请求」，所以这里不去伪造票据，
  而是在 App 的渲染进程里挂钩 initAliyunCaptcha，把 App 自己拿到的票据
  镜像给本地 relay，桥接器再消费这一张。

用法：
    python3 app-ticket.py arm         # 挂钩 ZCode.app（需 --remote-debugging-port）
    python3 app-ticket.py status      # 看 App 渲染进程里挂钩/票据状态
    python3 app-ticket.py unarm       # 还原
    python3 app-ticket.py send "hi"   # 用 App 自己刚拿到的票据发一条（调试用）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RELAY = os.environ.get("ZCODE_CAPTCHA_RELAY", "http://127.0.0.1:8910")
CDP = os.environ.get("ZCODE_CDP", "http://127.0.0.1:9444")
SNIFF_JS = HERE / "js" / "sniff-ticket.js"
UPSTREAM = ("https://zcode.z.ai/api/v1/zcode-plan/anthropic"
            "/v1/messages")


def log(*a):
    print("[%s]" % time.strftime("%H:%M:%S"), *a, flush=True)


# 还原用：把 initAliyunCaptcha 换回原始实现
UNARM_JS = """() => {
  if (window.__fkOrigInit) {
    window.initAliyunCaptcha = window.__fkOrigInit;
    delete window.__fkOrigInit;
  }
  window.__fkSniffInstalled = false;
  return {unarmed: true, installed: !!window.__fkSniffInstalled};
}"""


def relay_status() -> dict:
    with urllib.request.urlopen(RELAY.rstrip("/") + "/status", timeout=6) as r:
        return json.loads(r.read().decode("utf-8"))


def connect(playwright):
    browser = playwright.chromium.connect_over_cdp(CDP, timeout=20000)
    pages = [p for c in browser.contexts for p in c.pages]
    if not pages:
        raise RuntimeError("no renderer page; launch ZCode.app with "
                           "--remote-debugging-port=%s"
                           % CDP.rsplit(":", 1)[-1])
    return browser, pages[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["arm", "status", "unarm", "send"])
    ap.add_argument("prompt", nargs="?", default="Reply exactly: E2E_OK")
    ap.add_argument("--model", default="GLM-5.3-Flash")
    ap.add_argument("--wait", type=float, default=0.0,
                    help="send: seconds to wait for a fresh ticket")
    args = ap.parse_args()

    try:
        import zcode_bridge as zb  # noqa: F401  (同目录，解密凭证)
    except Exception as exc:  # noqa: BLE001
        log("cannot import zcode_bridge:", exc)
        return 1

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser, page = connect(pw)
        except Exception as exc:  # noqa: BLE001
            log("CDP connect failed:", str(exc)[:200])
            log("hint: open -a ZCode --args --remote-debugging-port=%s"
                % CDP.rsplit(":", 1)[-1])
            return 1

        try:
            if args.cmd == "arm":
                # 存一份原始实现，供 unarm 还原
                page.evaluate(
                    "() => { if (!window.__fkOrigInit) "
                    "window.__fkOrigInit = window.initAliyunCaptcha; "
                    "return !!window.__fkOrigInit; }")
                res = page.evaluate(SNIFF_JS.read_text(encoding="utf-8"))
                log("sniff:", res, "| url:", page.url[:70])
                log("now send a message in ZCode and solve its slider; "
                    "the ticket lands in the relay automatically")

            elif args.cmd == "unarm":
                log("unarm:", json.dumps(page.evaluate(UNARM_JS),
                                         ensure_ascii=False))

            elif args.cmd == "status":
                out = page.evaluate(
                    """() => ({
                      installed: !!window.__fkSniffInstalled,
                      tickets: (window.__fkTickets || []).slice(-5),
                      url: location.href
                    })""")
                out["relay"] = relay_status()
                log(json.dumps(out, ensure_ascii=False, indent=1))

            elif args.cmd == "send":
                st = relay_status()
                log("relay ticket: saved_at=%s len=%s"
                    % (st.get("saved_at"), st.get("length")))
                res = page.evaluate(SEND_JS, {
                    "url": UPSTREAM, "model": args.model, "prompt": args.prompt,
                })
                log(json.dumps(res, ensure_ascii=False, indent=1)[:1200])
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
    return 0


SEND_JS = """async (arg) => {
  const out = {};
  const r0 = await fetch(arg.url.replace(/\\/v1\\/messages$/, '') +
    '/v1/messages', {method: 'OPTIONS'}).catch(() => null);
  out.preflight = r0 ? r0.status : 'n/a';
  return out;
}"""


if __name__ == "__main__":
    sys.exit(main())
