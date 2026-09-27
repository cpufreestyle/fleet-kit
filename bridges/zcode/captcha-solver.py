#!/usr/bin/env python3
"""zcode captcha solver -- 自动过 zcode 上游的阿里云拼图滑块，把票据写进 captcha.txt。

背景：zcode.z.ai 的 zcode-plan 每次都校验阿里云滑块（sceneId 11xygtvd / region cn /
prefix no8xfe），票据过期后上游返回 code 3007 "captcha verify failed"。之前只能人工
在 captcha-relay(:8910) 上拖一次；本脚本用 Playwright 驱动真实浏览器自动完成：
识别缺口 -> 人手轨迹拖动 -> success 回调 POST 给 relay 落盘 -> 脚本再兜底写一次。

依赖：playwright / pillow / numpy（都在 runtime/.venv），浏览器优先用系统 Google Chrome。

用法：
    python3 captcha-solver.py                 # 有头，最多 4 次尝试
    python3 captcha-solver.py --dry-run       # 只识别缺口打印坐标，不拖动
    python3 captcha-solver.py --headless --attempts 8
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import io
import json
import os
import random
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

RELAY = os.environ.get("ZCODE_CAPTCHA_RELAY", "http://127.0.0.1:8910/")
STATE_FILE = Path(os.environ.get("ZCODE_CAPTCHA_FILE")
                  or Path(__file__).resolve().parent / "captcha.txt")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

POPUP = "#aliyunCaptcha-window-popup"
BACK_IMG = "#aliyunCaptcha-img"
PUZZLE_IMG = "#aliyunCaptcha-puzzle"
SLIDER = "#aliyunCaptcha-sliding-slider"
TRACK = "#aliyunCaptcha-sliding-body"
REFRESH = "#aliyunCaptcha-btn-refresh"
REOPEN = "#zcode-aliyun-captcha-button"


def log(*args):
    print("[%s]" % _dt.datetime.now().strftime("%H:%M:%S"), *args, flush=True)


# ---------------------------------------------------------------- 缺口识别
def _ring(mask: np.ndarray) -> np.ndarray:
    """拼图块的外轮廓环，用来和背景缺口的边缘做模板匹配。"""
    p = np.pad(mask, 3, mode="constant", constant_values=False)
    dil = np.zeros_like(p, dtype=bool)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            dil |= np.roll(np.roll(p, dy, axis=0), dx, axis=1)
    er = np.ones_like(p, dtype=bool)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            er &= np.roll(np.roll(p, dy, axis=0), dx, axis=1)
    dil = dil[3:-3, 3:-3]
    er = er[3:-3, 3:-3]
    return (mask & ~er) | (dil & ~mask)


def find_gap(back_bytes: bytes, shadow_bytes: bytes) -> dict:
    """返回背景图上缺口左边缘 x（原图像素）及诊断信息。"""
    gray = np.asarray(Image.open(io.BytesIO(back_bytes)).convert("L"),
                      dtype=np.float32)
    h, w = gray.shape
    gy, gx = np.gradient(gray)
    edge = np.abs(gx) + np.abs(gy)

    rgba = np.asarray(Image.open(io.BytesIO(shadow_bytes)).convert("RGBA"))
    ys, xs = np.nonzero(rgba[..., 3] > 40)
    if len(ys) < 30:
        raise RuntimeError("shadow.png 里找不到拼图块")
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    mask = rgba[y0:y1, x0:x1, 3] > 40
    ring = _ring(mask)
    inner = mask & ~ring
    piece_w = x1 - x0
    ph, pw = ring.shape

    best, best_x, scores = -1.0, None, {}
    for x in range(piece_w + 1, min(w - piece_w, w) + 1):
        if y0 + ph > h:
            break
        win_e = edge[y0:y0 + ph, x:x + pw]
        win_g = gray[y0:y0 + ph, x:x + pw]
        edge_score = float(win_e[ring].mean()) if ring.any() else 0.0
        dark_score = 0.0
        if inner.any() and win_g.size:
            dark_score = max(0.0, float(np.median(win_g)) - float(win_g[inner].mean()))
        score = edge_score + 0.6 * dark_score
        scores[x] = round(score, 3)
        if score > best:
            best, best_x = score, x

    top = sorted(scores.items(), key=lambda kv: -kv[1])[:6]
    return {"gap_x": best_x, "piece_w": piece_w, "piece_x0": x0, "piece_y": (y0, y1),
            "img_wh": (w, h), "score": round(best, 3), "top": top}


# ---------------------------------------------------------------- 取图 / 落盘
def fetch_image(page, url: str) -> bytes:
    """优先页面内 fetch（顺带复用浏览器会话），失败退回 urllib。"""
    try:
        b64 = page.evaluate(
            """async (u) => {
              const r = await fetch(u, {cache: 'no-store'});
              if (!r.ok) throw new Error('http ' + r.status);
              const b = new Uint8Array(await r.arrayBuffer());
              let s = '';
              for (let i = 0; i < b.length; i += 8192)
                s += String.fromCharCode.apply(null, b.subarray(i, i + 8192));
              return btoa(s);
            }""", url)
        raw = base64.b64decode(b64)
        if raw:
            return raw
    except Exception as exc:  # noqa: BLE001
        log("  页内取图失败:", str(exc)[:100])
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                              "Referer": RELAY, "Accept": "image/*"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read()


def relay_target_file() -> Path:
    """relay 的 /status 会报出它真正写入的文件，兜底落盘必须落在同一个。"""
    try:
        with urllib.request.urlopen(RELAY.rstrip("/") + "/status", timeout=6) as resp:
            found = json.loads(resp.read().decode("utf-8")).get("file")
        if found:
            return Path(found)
    except Exception as exc:  # noqa: BLE001
        log("  relay /status 不可用:", str(exc)[:80])
    return STATE_FILE


def write_captcha(param: str) -> Path:
    target = relay_target_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tmp.write_text("# %s\n%s\n" % (stamp, param.strip()), encoding="utf-8")
    tmp.replace(target)
    return target


# ---------------------------------------------------------------- 人手轨迹
def _path(x_from: float, x_to: float, y: float, ease: str = "out") -> list:
    dist = x_to - x_from
    steps = random.randint(22, 38)
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        eased = (1.0 - (1.0 - t) ** 3) if ease == "out" else (t * t * (3 - 2 * t))
        pts.append((x_from + dist * eased + random.uniform(-0.5, 0.5),
                    y + random.uniform(-1.8, 1.8)))
    pts.append((x_to, y + random.uniform(-0.6, 0.6)))
    return pts


def _move(mouse, pts, dt=(0.004, 0.019)):
    last = None
    for (x, y) in pts:
        mouse.move(x, y, steps=1)
        last = (x, y)
        time.sleep(random.uniform(*dt))
    return last


def warmup(mouse, box) -> None:
    """先在滑块附近随意晃几下，做出“人在操作”的鼠标轨迹。"""
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    for _ in range(random.randint(6, 12)):
        mouse.move(cx + random.uniform(-140, 160), cy + random.uniform(-70, 55),
                   steps=random.randint(1, 4))
        time.sleep(random.uniform(0.01, 0.05))
    mouse.move(cx, cy, steps=random.randint(4, 8))
    time.sleep(random.uniform(0.12, 0.3))


def drag_to(page, target_left: float) -> float:
    """按住把手，闭环把拼图块 left 对准 target_left（CSS px），返回最终 left。"""
    box = page.locator(SLIDER).bounding_box()
    if not box:
        raise RuntimeError("找不到把手 " + SLIDER)
    sx, sy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    warmup(page.mouse, box)
    page.mouse.down()
    time.sleep(random.uniform(0.1, 0.2))

    read = lambda: float(page.evaluate(  # noqa: E731
        "() => parseFloat(document.querySelector('%s').style.left) || 0" % PUZZLE_IMG))

    cx, cy = _move(page.mouse, _path(sx, sx + target_left * 0.72, sy))
    err = target_left - read()
    for _ in range(14):
        err = target_left - read()
        if abs(err) <= 0.9:
            break
        cx, cy = _move(page.mouse, _path(cx, cx + err, sy, ease="mid"),
                       dt=(0.008, 0.03))
    # 轻微过冲再回拉，更像真人
    if abs(err) < 4:
        over = random.uniform(2.5, 6.0)
        cx, cy = _move(page.mouse, _path(cx, cx + over, sy))
        cx, cy = _move(page.mouse, _path(cx, cx - over, sy), dt=(0.01, 0.035))
        time.sleep(random.uniform(0.08, 0.2))
    page.mouse.up()
    final = read()


# ---------------------------------------------------------------- 页面驱动
STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}, loadTimes: function(){}};
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
"""

GEOM_JS = """() => {
  const back = document.querySelector('#aliyunCaptcha-img');
  const pz = document.querySelector('#aliyunCaptcha-puzzle');
  if (!back || !pz) return null;
  const bb = back.getBoundingClientRect();
  return {src: back.src, pzSrc: pz.src, natW: back.naturalWidth,
          dispW: bb.width || back.naturalWidth, pzLeft: parseFloat(pz.style.left) || 0,
          complete: back.complete && back.naturalWidth > 0};
}"""


def popup_visible(page) -> bool:
    return page.evaluate(
        """() => {
          const el = document.getElementById('aliyunCaptcha-window-popup');
          if (el) {
            const s = getComputedStyle(el), r = el.getBoundingClientRect();
            if (s.display !== 'none' && s.visibility !== 'hidden' && r.width > 20)
              return true;
          }
          const sl = document.getElementById('aliyunCaptcha-sliding-slider');
          return !!sl && !!sl.getBoundingClientRect().width;
        }""")


def wait_state(page, timeout: float) -> str:
    """轮询：拿到票据 -> passed，滑块出现 -> popup，否则 timeout。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.evaluate("() => !!window.__capParam"):
                return "passed"
            if popup_visible(page):
                return "popup"
        except Exception:  # noqa: BLE001  导航中
            pass
        time.sleep(0.2)
    return "timeout"


def collect(page) -> bool:
    """确认窗口里的票据落盘；relay 没写成功就自己写一次。"""
    param = page.evaluate("() => window.__capParam") or ""
    if not param:
        return False
    if page.evaluate("() => document.title === 'CAP-OK'"):
        log("  票据已由 relay 落盘 len=%d" % len(param))
    else:
        log("  relay 未确认，兜底写入 %s len=%d" % (write_captcha(param).name, len(param)))
    return True


def solve_attempt(page, attempt: int, dry: bool) -> bool:
    state = wait_state(page, 18)
    log("attempt %d: state=%s" % (attempt, state))
    if state == "passed":
        log("  traceless 直接通过")
        return collect(page)
    if state != "popup":
        try:
            page.locator(REOPEN).click(timeout=2000)
        except Exception:  # noqa: BLE001
            pass
        if wait_state(page, 8) != "popup":
            log("  滑块没出现:", page.evaluate(
                "() => (document.getElementById('out')||{}).textContent"))
            return False

    page.wait_for_function(
        "() => { const g = (%s)(); return g && g.complete; }" % GEOM_JS, timeout=15000)
    geom = page.evaluate("(" + GEOM_JS + ")()")
    scale = geom["natW"] / geom["dispW"] if geom["dispW"] else 1.0
    info = find_gap(fetch_image(page, geom["src"]), fetch_image(page, geom["pzSrc"]))
    target_left = (info["gap_x"] - info["piece_x0"]) / scale - geom["pzLeft"]
    log("  img=%s scale=%.3f gap_x=%s piece_w=%d top=%s"
        % (info["img_wh"], scale, info["gap_x"], info["piece_w"], info["top"][:4]))
    log("  -> 目标 strip_left=%.1f (需拖动 %.1f)" % (target_left + geom["pzLeft"], target_left))
    if dry:
        return False
    if target_left < 6:
        log("  缺口位置异常，本轮放弃")
        return False
    drag_to(page, target_left)

    deadline = time.time() + 15
    while time.time() < deadline:
        if page.evaluate("() => !!window.__capParam"):
            return collect(page)
        time.sleep(0.3)
    log("  验证未通过:", page.evaluate(
        "() => ((document.getElementById('aliyunCaptcha-sliding-body')||{}).className)"
        " + ' | ' + ((document.getElementById('aliyunCaptcha-sliding-text')||{}).textContent)"))
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay", default=RELAY)
    ap.add_argument("--attempts", type=int, default=4)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只识别缺口，不拖动")
    ap.add_argument("--slowmo", type=int, default=0)
    ap.add_argument("--profile", default="/tmp/zcode-captcha-profile")
    ap.add_argument("--channel", default="auto",
                    help="chrome=系统 Chrome，chromium=playwright 自带，auto=优先系统")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    channel = None
    if args.channel == "chrome" or (args.channel == "auto"
                                    and Path("/Applications/Google Chrome.app").exists()):
        channel = "chrome"

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            args.profile, headless=args.headless, slow_mo=args.slowmo,
            channel=channel, viewport={"width": 1280, "height": 820},
            locale="zh-CN", timezone_id="Asia/Shanghai",
            args=["--disable-blink-features=AutomationControlled",
                  "--no-first-run", "--no-default-browser-check",
                  "--password-store=basic"])
        ctx.add_init_script(STEALTH)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ok = False
        try:
            for attempt in range(1, args.attempts + 1):
                page.goto(args.relay, wait_until="domcontentloaded", timeout=45000)
                try:
                    ok = solve_attempt(page, attempt, args.dry_run)
                except Exception:  # noqa: BLE001
                    import traceback
                    log("  attempt 异常:\n" + traceback.format_exc(limit=4)[-900:])
                if ok or args.dry_run:
                    break
                try:
                    page.locator(REFRESH).click(timeout=2500)
                    page.wait_for_timeout(1200)
                except Exception:  # noqa: BLE001
                    pass
            if not ok and not args.dry_run:
                try:
                    page.screenshot(path="/tmp/zcode-captcha-fail.png")
                    log("失败现场: /tmp/zcode-captcha-fail.png")
                except Exception:  # noqa: BLE001
                    pass
        finally:
            ctx.close()

    out = {"ok": bool(ok), "relay": args.relay}
    if ok:
        try:
            lines = relay_target_file().read_text(encoding="utf-8").splitlines()
            out["saved_at"] = lines[0].lstrip("# ").strip()
            out["length"] = len(lines[1].strip()) if len(lines) > 1 else 0
        except OSError:
            out["ok"] = False
    print(json.dumps(out, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())




