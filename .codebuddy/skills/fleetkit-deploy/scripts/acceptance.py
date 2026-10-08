#!/usr/bin/env python3
"""FleetKit acceptance test.

Probes every bridge in a runtime root: is the port listening, does /v1/models
answer, and - with --chat - does a minimal chat completion come back 200.
A bridge that only listens is not a working bridge, so ports alone never count
as a pass.

This is the same question tools/status.sh and tools/fleet_chat_test.py ask, but
it runs without bash and prints one row per bridge, which makes it the right
first check when Codex reports "502 Provider unreachable": that error means the
model you picked routes to a bridge whose port is dead.

Usage:
  acceptance.py --home DIR [--port-base N] [--chat] [--json] [--timeout S]

Exit codes:
  0 every bridge up   2 partial   3 nothing up
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _deploylib import git_bash

import urllib.error
import urllib.request

BRIDGES = [
    ("workbuddy", 0, "CODEBUDDY2OPENAI_KEY"),
    ("workbuddy-gpt", 1, "CODEBUDDY2OPENAI_KEY"),
    ("qoder", 2, "QODER2CODEX_KEY"),
    ("codely", 3, "CODELY2CODEX_KEY"),
    ("trae", 4, "TRAE2CODEX_KEY"),
    ("lingxi", 5, "LINGXI2CODEX_KEY"),
    ("xhx", 6, "XHX2CODEX_KEY"),
    ("gemini", 7, "GEMINI2CODEX_KEY"),
    ("catpaw", 8, "CATPAW2CODEX_KEY"),
    ("antigravity", 10, "ANTIGRAVITY2CODEX_KEY"),
    ("qwen", 11, "QWEN2CODEX_KEY"),
    ("cline", 12, "CLINE2CODEX_KEY"),
    ("zcode", 13, "ZCODE2CODEX_KEY"),
]


def finish_cmd(home, name="<name>"):
    """How to re-finalize one bridge from the default shell (PowerShell 7)."""
    script = os.path.join(home, "bridges", "finish.sh").replace("\\", "/")
    posix_home = home.replace("\\", "/")
    if sys.platform == "win32":
        return ('pwsh -NoProfile -Command "& \'%s\' -lc \\"bash \'%s\' %s --home \'%s\'\\""'
                % (git_bash(), script, name, posix_home))
    return "bash \"%s\" %s --home \"%s\"" % (script, name, home)


def real_call_check(home, timeout=1800):
    """Run tools/verify_real_calls.py and return {bridge: verdict}."""
    script = os.path.join(home, "tools", "verify_real_calls.py")
    if not os.path.isfile(script):
        return {}, "verify_real_calls.py not found in %s" % os.path.join(home, "tools")
    try:
        proc = subprocess.run([sys.executable, script, "--json"],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {}, "verify_real_calls.py timed out"
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return {}, "could not parse verify_real_calls.py output"
    verdicts = {}
    for row in payload.get("bridges", []):
        verdicts[row.get("name", "?")] = row.get("verdict", "?")
    return verdicts, ""


def read_env(path):
    values = {}
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip().lstrip("export ").strip()] = value.strip().strip('"').strip("'")
    return values


def http_json(url, payload=None, key=None, timeout=8):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer %s" % key
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        return exc.code, {"error": body}
    except Exception as exc:  # noqa: BLE001 - report, do not raise
        return 0, {"error": str(exc)}


def main():
    ap = argparse.ArgumentParser(description="FleetKit acceptance test")
    ap.add_argument("--home", required=True)
    ap.add_argument("--port-base", type=int, default=8787)
    ap.add_argument("--chat", action="store_true", help="also send a minimal chat request")
    ap.add_argument("--real", action="store_true",
                    help="also run tools/verify_real_calls.py (real upstream inference, "
                         "a few minutes and billable)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--timeout", type=float, default=8)
    args = ap.parse_args()

    home = os.path.abspath(os.path.expanduser(args.home))
    env = read_env(os.path.join(home, "fleet.env"))
    base = int(env.get("PORT_BASE", args.port_base))

    rows = []
    for name, offset, key_env in BRIDGES:
        port = base + offset
        sock = socket.socket()
        sock.settimeout(1.0)
        listening = sock.connect_ex(("127.0.0.1", port)) == 0
        sock.close()
        row = {"name": name, "port": port, "listen": listening,
               "models": 0, "chat": "-", "real": "-", "detail": ""}
        if not listening:
            row["detail"] = "port closed"
            rows.append(row)
            continue
        # Every bridge rejects an unauthenticated /v1/models with 401. Probing
        # without the fleet.env key used to paint a healthy fleet as dead, so
        # send the same key the --chat probe below uses.
        status, payload = http_json("http://127.0.0.1:%d/v1/models" % port,
                                    key=env.get(key_env), timeout=args.timeout)
        if status != 200:
            row["detail"] = "models HTTP %s" % (status or "err")
            rows.append(row)
            continue
        models = payload.get("data") if isinstance(payload, dict) else payload
        ids = [m.get("id") for m in models if isinstance(m, dict) and m.get("id")]
        row["models"] = len(ids)
        if not ids:
            row["detail"] = "empty model list"
            rows.append(row)
            continue
        if args.chat:
            key = env.get(key_env) or "local"
            status, payload = http_json(
                "http://127.0.0.1:%d/v1/chat/completions" % port,
                {"model": ids[0], "messages": [{"role": "user", "content": "hi"}],
                 "max_tokens": 16},
                key=key, timeout=max(args.timeout, 30))
            row["chat"] = str(status)
            if status != 200:
                row["detail"] = (payload.get("error") or "")[:80]
            else:
                row["detail"] = "ok"
        else:
            row["detail"] = "ok"
        rows.append(row)

    verdicts, real_err = {}, ""
    if args.real:
        verdicts, real_err = real_call_check(home)
        for r in rows:
            r["real"] = verdicts.get(r["name"], "-")

    up = [r for r in rows if r["listen"] and r["models"] > 0
          and (not args.chat or r["chat"] == "200")
          and (not args.real or r["real"] == "REAL")]
    if args.json:
        print(json.dumps({"home": home, "port_base": base, "chat": args.chat,
                          "real": args.real, "verdicts": verdicts,
                          "up": len(up), "total": len(rows), "rows": rows},
                         indent=2, ensure_ascii=False))
    else:
        print("FleetKit acceptance  home=%s  ports=%d..%d" % (home, base, base + 13))
        header = ("%-14s %-6s %-7s %-8s %-6s %-13s %s"
                  % ("BRIDGE", "PORT", "LISTEN", "MODELS", "CHAT", "REAL", "NOTE"))
        print(header)
        for r in rows:
            print("%-14s %-6s %-7s %-8s %-6s %-13s %s" % (
                r["name"], r["port"], "up" if r["listen"] else "DOWN",
                r["models"], r["chat"], r["real"], r["detail"]))
        print()
        if real_err:
            print("[warn] real-call check: %s" % real_err)
        print("up %d/%d" % (len(up), len(rows)))
        down = [r["name"] for r in rows if r not in up]
        if down:
            print("down: %s" % ", ".join(down))
            print("next: %s" % finish_cmd(home))
    if not up:
        return 3
    return 0 if len(up) == len(rows) else 2


if __name__ == "__main__":
    sys.exit(main())
