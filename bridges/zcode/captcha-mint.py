#!/usr/bin/env python3
"""zcode captcha minter — 自动换取一次性阿里云 captcha 票据。

背景：zcode.z.ai 的 zcode-plan 对每次 /v1/messages 调用校验阿里云 captcha，
且 captchaVerifyParam 是一次性的（用过即 3007 captcha verify failed）。人工
在 relay 页面拖滑块只能顶一两次调用，因此本脚本用真实 Chrome 走 SDK 的
无感验证（startTracelessVerification，多数情况下无需滑块）批量换取票据。

用法：
  captcha-mint.py --once                   # 换一张，打印到 stdout
  captcha-mint.py --once --write FILE      # 换一张并落盘（兼容旧 captcha.txt）
  captcha-mint.py --serve [--target 2]     # 常驻：把票据池维持在 target 张

票据池：DIR/<epoch>-<rand>.txt，内容即 param，文件名首段是换取时间戳，
供桥接器按 max_age 淘汰过期票据；桥接器用 os.remove 抢占，天然去重。

依赖：playwright + 本机 Google Chrome（channel=chrome，指纹比 headless
chromium 更接近真人，无感通过率高）。profile 持久化，设备指纹随时间变熟。
"""

from __future__ import annotations

import argparse
import os
import random
import string
import sys
import time
from pathlib import Path

RELAY = os.environ.get("ZCODE_CAPTCHA_RELAY", "http://127.0.0.1:8910/")
PROFILE = os.environ.get(
    "ZCAP_PROFILE", "/Users/a1-6/.cache/fleetkit-captcha-profile")
PAGE = os.environ.get("ZCAP_PAGE", RELAY.rstrip("/") + "/")
POOL = Path(os.environ.get(
    "ZCODE_CAPTCHA_POOL",
    str(Path(__file__).resolve().parent / "captcha_pool")))
FAIL_SHOT = "/tmp/fleet-logs/zcode-captcha-fail.png"


def log(*args) -> None:
    print(time.strftime("%H:%M:%S"), *args, flush=True)


def rand() -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits)
                   for _ in range(6))


# --------------------------------------------------------------------------
# pool helpers（与桥接器共用同一套命名约定）
# --------------------------------------------------------------------------

def pool_files(pool: Path) -> list:
    try:
        return sorted([p for p in pool.glob("*.txt")
                       if p.name.split("-")[0].isdigit()],
                      key=lambda p: p.name, reverse=True)
    except OSError:
        return []


def pool_fresh(pool: Path, max_age: float) -> list:
    now = time.time()
    out = []
    for p in pool_files(pool):
        try:
            if now - float(p.name.split("-")[0]) <= max_age:
                out.append(p)
        except ValueError:
            continue
    return out


def pool_put(pool: Path, param: str) -> Path:
    pool.mkdir(parents=True, exist_ok=True)
    tmp = pool / (".tmp-%s" % rand())
    tmp.write_text(param, encoding="utf-8")
    dst = pool / ("%d-%s.txt" % (time.time(), rand()))
    os.replace(tmp, dst)
    return dst


def pool_evict(pool: Path, max_age: float) -> int:
    n, now = 0, time.time()
    for p in pool_files(pool):
        try:
            if now - float(p.name.split("-")[0]) > max_age:
                p.unlink()
                n += 1
        except OSError:
            continue
    return n


# --------------------------------------------------------------------------
# minting
# --------------------------------------------------------------------------

def _stealth(ctx, page) -> None:
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "window.chrome=window.chrome||{runtime:{}};"
        "Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh',"
        "'en-US','en']});")


def drag_slider(page) -> bool:
    """无感失败时尽力拖一次滑块（真·拼图仍建议人工）。"""
    btn = None
    for sel in ("#aliyunCaptcha-sliding-slider", ".nc_scale .btn_slide",
                "div[id*='slider']"):
        try:
            cand = page.query_selector(sel)
            if cand and cand.is_visible():
                btn = cand
                break
        except Exception:  # noqa: BLE001
            continue
    if not btn:
        return False
    box = btn.bounding_box()
    if not box:
        return False
    x0, y0 = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x0, y0)
    page.mouse.down()
    for i in range(1, 26):
        page.mouse.move(x0 + i * 11, y0 + (2 if i % 3 == 0 else -2), steps=2)
        page.wait_for_timeout(random.randint(12, 34))
    page.mouse.up()
    return True


def mint_once(page, timeout: float = 40.0) -> str:
    """打开 relay 页，等无感验证出票（失败则拖一次滑块），返回 param。"""
    page.goto(PAGE, wait_until="domcontentloaded", timeout=45000)
    deadline, dragged = time.time() + timeout, False
    while time.time() < deadline:
        page.wait_for_timeout(500)
        try:
            param = page.evaluate("() => window.__capParam || null")
        except Exception:  # noqa: BLE001
            continue
        if param:
            return param
        if not dragged and time.time() < deadline - 12:
            dragged = True
            try:
                drag_slider(page)
            except Exception as exc:  # noqa: BLE001
                log("slider-drag-failed:", exc)
    try:
        page.screenshot(path=FAIL_SHOT)
    except Exception:  # noqa: BLE001
        pass
    raise TimeoutError("captcha not issued in %.0fs (shot=%s out=%r)"
                       % (timeout, FAIL_SHOT, page.text_content("#out")))


def open_browser(pw, headless: bool, profile: str):
    kwargs = dict(
        headless=headless,
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled",
              "--no-first-run", "--no-default-browser-check"])
    try:
        return pw.chromium.launch_persistent_context(
            profile, channel="chrome", **kwargs)
    except Exception as exc:  # noqa: BLE001
        log("chrome channel unavailable (%s) -> bundled chromium" % exc)
        return pw.chromium.launch_persistent_context(profile, **kwargs)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_once(args) -> int:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = open_browser(pw, args.headless, args.profile)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        _stealth(ctx, page)
        try:
            param = mint_once(page, args.timeout)
        finally:
            ctx.close()
    if args.write:
        dst = Path(args.write)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("%s\n%s\n"
                       % (time.strftime("# %Y-%m-%d %H:%M:%S"), param),
                       encoding="utf-8")
        log("wrote", dst)
    if args.pool:
        pool_put(POOL, param)
    print(param)
    return 0


def cmd_serve(args) -> int:
    from playwright.sync_api import sync_playwright
    pool = Path(args.pool_dir)
    pool.mkdir(parents=True, exist_ok=True)
    fails = 0
    with sync_playwright() as pw:
        ctx = open_browser(pw, args.headless, args.profile)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        _stealth(ctx, page)
        log("captcha-minter up: pool=%s target=%d max_age=%.0fs page=%s"
            % (pool, args.target, args.max_age, PAGE))
        while True:
            try:
                pool_evict(pool, args.max_age)
                have = len(pool_fresh(pool, args.max_age))
                if have >= args.target:
                    time.sleep(min(5.0, max(1.0, args.max_age / 4)))
                    fails = 0
                    continue
                param = mint_once(page, args.timeout)
                dst = pool_put(pool, param)
                log("minted %s (pool %d->%d)"
                    % (dst.name, have, len(pool_fresh(pool, args.max_age))))
                fails = 0
            except KeyboardInterrupt:
                break
            except Exception as exc:  # noqa: BLE001
                fails += 1
                log("mint-error x%d: %s" % (fails, exc))
                time.sleep(min(300, 5 * fails))
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass
                ctx = open_browser(pw, args.headless, args.profile)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                _stealth(ctx, page)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="换取一张票据")
    ap.add_argument("--serve", action="store_true", help="常驻补池")
    ap.add_argument("--write", default="", help="同时写入的 captcha.txt 路径")
    ap.add_argument("--pool", action="store_true",
                    help="--once 时也写入票据池")
    ap.add_argument("--pool-dir", default=str(POOL), help="票据池目录")
    ap.add_argument("--timeout", type=float, default=40.0)
    ap.add_argument("--target", type=int, default=2, help="池内保有张数")
    ap.add_argument("--max-age", type=float, default=600.0,
                    help="票据最大可用龄（秒），超时丢弃")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--profile", default=PROFILE)
    args = ap.parse_args()
    if args.serve:
        return cmd_serve(args)
    return cmd_once(args)


if __name__ == "__main__":
    sys.exit(main())


