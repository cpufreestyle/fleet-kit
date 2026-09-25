#!/usr/bin/env python3
"""lingxi2codex 登录助手 —— 复刻 @lingxi-agent/core 的 /login 流程：
1. 在 127.0.0.1:8062 起回调服务器
2. 浏览器打开 https://lingxi.regaing.com/login?client=cli&port=8062
3. 登录成功后回调 ?token=...&refresh=...&name=... → 写 ~/.LingXi/auth.json

用法：python3 login_helper.py [--timeout 600]
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

CALLBACK_PORT = 8062
WEB_BASE = os.environ.get("LINGXI_WEB_BASE_URL") or "https://lingxi.regaing.com"


def auth_file() -> Path:
    home = Path(os.environ.get("LINGXI_HOME") or (Path.home() / ".LingXi"))
    home.mkdir(parents=True, exist_ok=True)
    return home / "auth.json"


OK_HTML = "<h2>login ok, you can close this window</h2><p>lingxi2codex bridge received the token.</p>"
FAIL_HTML = "<h2>login failed, please retry</h2>"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--no-browser", action="store_true", help="only print URL, do not open browser")
    args = ap.parse_args()

    result = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            token = (q.get("token") or [""])[0]
            refresh = (q.get("refresh") or [""])[0]
            name = (q.get("name") or [""])[0]
            if token:
                result.update(token=token, refresh=refresh, name=name)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(OK_HTML.encode("utf-8"))
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(FAIL_HTML.encode("utf-8"))

    server = HTTPServer(("127.0.0.1", CALLBACK_PORT), Handler)
    url = f"{WEB_BASE}/login?client=cli&port={CALLBACK_PORT}"
    print(f"[login] please complete LingXi login in browser: {url}", flush=True)
    if not args.no_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            subprocess.run(["open", url])
    server.timeout = args.timeout
    t0 = time.time()
    while not result and time.time() - t0 < args.timeout:
        server.handle_request()
    if not result:
        print("[login] timed out waiting for callback; rerun this script to retry", flush=True)
        return 1
    payload = {"token": result["token"], "refresh": result.get("refresh") or "",
               "name": result.get("name") or "", "baseURL": WEB_BASE,
               "saved_at": int(time.time())}
    f = auth_file()
    f.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[login] success: {result.get('name') or '(account)'} -> {f}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
