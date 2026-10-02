#!/usr/bin/env python3
"""zcode captcha relay — 托管阿里云 captcha 换取页，把 captchaVerifyParam 落盘给 zcode 桥。

背景：zcode.z.ai 的 zcode-plan（ZCode Start Plan / Weekend Build 计划，GLM-5.3-Flash）
对每次调用都校验阿里云 captcha（sceneId 11xygtvd，region cn，prefix no8xfe）。
本服务在 127.0.0.1:8910 提供换取页；用户在浏览器完成滑块后 param 写入
ZCODE_CAPTCHA_FILE（默认 runtime/bridges/zcode/captcha.txt），zcode 桥读取后
附带 X-Aliyun-Captcha-Verify-Param / X-Aliyun-Captcha-Verify-Region 头访问上游。

仅标准库。用法：python3 captcha-relay.py [--port 8910]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
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
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            fh.write(f"# {stamp}\n{param}\n")
        self._send(200, f"saved len={len(param)}")

    @staticmethod
    def _status_json():
        import json

        info = {"ok": True, "file": STATE_FILE, "has_param": False, "length": 0, "saved_at": None}
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
