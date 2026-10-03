#!/usr/bin/env python3
"""zcode captcha relay — 托管阿里云 captcha 换取页，把 captchaVerifyParam 落盘给 zcode 桥。

背景：zcode.z.ai 的 zcode-plan（ZCode Start Plan / Weekend Build 计划，GLM-5.3-Flash）
对每次调用都校验阿里云 captcha（sceneId 11xygtvd，region cn，prefix no8xfe）。
本服务在 127.0.0.1:8910 提供换取页；每完成一次验证就往票池写一张一次性票据
（ZCODE_CAPTCHA_POOL，默认与 captcha.txt 同级的 captcha_pool，一个文件一张票，桥凭
claim-by-delete 领取），zcode 桥取票后附带 X-Aliyun-Captcha-Verify-Param /
X-Aliyun-Captcha-Verify-Region 头访问上游。换票是手动的、可连续多张：调用从池里
静默取票，池干了才回来换，不会再每调用跳一次验证。

仅标准库。用法：python3 captcha-relay.py [--port 8910]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import random
import string
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# One escaped exception used to reset the connection (http.server has no
# exception stage); _basehttp is the stdlib-only shared guard in bridges/,
# reached through the same bootstrap the bridges use.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
import _basehttp

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "captcha", "index.html")
STATE_FILE = os.environ.get("ZCODE_CAPTCHA_FILE") or os.path.join(HERE, "captcha.txt")

# The ticket pool the bridge spends from (claim-by-delete, one file per
# ticket). The relay banks into the pool rather than overwriting
# captcha.txt on every save: a param is single-use upstream, and the same
# param sitting in two places is two claims on one use -- the second comes
# back as 3007. The pool file name leads with the mint time, which is what
# the bridge's max-age gate evicts on.
# Next to STATE_FILE, not HERE: the deployed relay runs the kit copy while
# ZCODE_CAPTCHA_FILE points into the runtime tree, and the bridge derives its
# own pool from that same file. HERE-relative would bank into the kit tree,
# where no bridge ever looks.
POOL_DIR = os.environ.get("ZCODE_CAPTCHA_POOL") or os.path.join(
    os.path.dirname(STATE_FILE), "captcha_pool")


def _rand(n: int = 6) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits)
                   for _ in range(n))


# A ticket is spendable only inside a short window (the bridge spends one under
# 600s of age, the CLI route under 900s); 900s is the outer lifetime. The
# names lead with the mint epoch, which is what both gates read.
MAX_AGE = float(os.environ.get("ZCODE_CAPTCHA_MAX_AGE") or "900")
FRESH_AGE = float(os.environ.get("ZCODE_CAPTCHA_MAX_FRESH") or "600")


def _pool_files() -> list:
    out = []
    try:
        names = os.listdir(POOL_DIR)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".txt"):
            continue
        try:
            out.append((name, float(name.split("-")[0])))
        except ValueError:
            continue
    return out


def _pool_fresh() -> int:
    now = time.time()
    return sum(1 for _name, epoch in _pool_files()
               if now - epoch <= FRESH_AGE)


def _pool_evict_stale() -> int:
    now = time.time()
    gone = 0
    for name, epoch in _pool_files():
        if now - epoch > MAX_AGE:
            try:
                os.unlink(os.path.join(POOL_DIR, name))
                gone += 1
            except OSError:
                continue
    return gone


class Handler(BaseHTTPRequestHandler):
    server_version = "zcode-captcha-relay/1.0"

    def log_message(self, fmt, *args):  # 保留到 stderr -> /tmp/fleet-logs/zcode-captcha.log
        print("%s - %s" % (self.log_date_time_string(), fmt % args), flush=True)

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # ZCode.app 的渲染进程是 file:// 源，app-ticket.py 要把 App 自己弹出的
        # 验证码票据 POST 回来，必须允许跨源。
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(PAGE, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError as exc:
                self._send(500, f"page missing: {exc}")
        elif path == "/healthz":
            self._send(200, "ok")
        elif path == "/status":
            self._send(200, self._status_json(), "application/json")
        else:
            self._send(404, "not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/save":
            self._send(404, "not found")
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        data = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        param = (data.get("p") or [""])[0].strip()
        if len(param) < 32:
            self._send(400, "param too short")
            return
        os.makedirs(POOL_DIR, exist_ok=True)
        # atomic publish: a half-written file would be spent as a refusal
        tmp = os.path.join(POOL_DIR, ".tmp-%d" % os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(param)
        dst = os.path.join(POOL_DIR, "%d-%s.txt" % (time.time(), _rand()))
        os.replace(tmp, dst)
        _pool_evict_stale()   # a stale ticket in the count is a lie
        self._send(200, f"saved len={len(param)} pool={_pool_fresh()}")

    @staticmethod
    def _status_json():
        import json

        newest = 0.0
        if os.path.isdir(POOL_DIR):
            for name in os.listdir(POOL_DIR):
                if not name.endswith(".txt"):
                    continue
                try:
                    newest = max(newest, os.path.getmtime(
                        os.path.join(POOL_DIR, name)))
                except OSError:
                    continue
        info = {"ok": True, "pool": POOL_DIR, "tickets": _pool_fresh(),
                "stale": len(_pool_files()) - _pool_fresh(),
                "newest_age": (round(time.time() - newest, 1)
                               if newest else None),
                "file": STATE_FILE, "has_param": False, "length": 0,
                "saved_at": None}
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            param = ""
            for line in lines:
                if line.startswith("#"):
                    info["saved_at"] = line.lstrip("# ").strip()
                elif line.strip():
                    param = line.strip()
            info["has_param"] = bool(param)
            info["length"] = len(param)
        except OSError:
            pass
        return json.dumps(info, ensure_ascii=False)


Handler = _basehttp.install_basehttp_guard(Handler)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8910)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.serve_forever()


if __name__ == "__main__":
    main()
