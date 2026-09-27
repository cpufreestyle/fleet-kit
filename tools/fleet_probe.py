#!/usr/bin/env python3
"""Measure which fleet bridges actually answer a real request.

Writes a reachability snapshot consumed by catalog_sort.py:

  {"reachable": [...], "unreachable": [...],
   "measured_at": "2026-09-27T17:10:00+08:00",
   "evidence": {"workbuddy": "glm-5.2 -> E2E_OK", ...}}

A bridge counts as reachable only when a real chat call returns the expected
nonce echo. A listening port, a /v1/models listing, or a fast canned 200 all
count as NOT reachable, because the picker is what the user actually feels.

Usage:
  fleet_probe.py            write ~/.codex/fleet-reach.json
  fleet_probe.py --stdout   print JSON instead of writing
"""
import argparse
import datetime
import json
import os
import re
import urllib.error
import urllib.request

PORTS = {
    "workbuddy": 8787, "workbuddy-gpt": 8788, "qoder": 8789,
    "codely": 8790, "trae": 8791, "lingxi": 8792,
    "xhx": 8793, "gemini": 8794, "catpaw": 8795,
    "antigravity": 8797, "qwen": 8798, "cline": 8799,
}

# local token each bridge checks, as found in its launchd plist
KEY_ENV = {
    "workbuddy": "CODEBUDDY2OPENAI_KEY",
    "workbuddy-gpt": "CODEBUDDY2OPENAI_KEY",
    "qoder": "QODER2CODEX_KEY", "codely": "CODELY2CODEX_KEY",
    "trae": "TRAE2CODEX_KEY", "lingxi": "LINGXI2CODEX_KEY",
    "xhx": "XHX2CODEX_KEY", "gemini": "GEMINI2CODEX_KEY",
    "catpaw": "CATPAW2CODEX_KEY", "antigravity": "ANTIGRAVITY2CODEX_KEY",
    "qwen": "QWEN2CODEX_KEY", "cline": "CLINE2CODEX_KEY",
}

NONCE = "E2E_OK"
SKIP_RE = ("image", "tts", "embed", "ocr", "vision", "vl")


def load_env(path):
    env = {}
    if not path or not os.path.exists(path):
        return env
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip(chr(34)).strip(chr(39))
    return env


def list_models(port, headers):
    url = "http://127.0.0.1:%d/v1/models" % port
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode())
    return [m.get("id") for m in data.get("data", []) if m.get("id")]


def try_call(port, headers, model, timeout=20.0):
    url = "http://127.0.0.1:%d/v1/chat/completions" % port
    body = json.dumps({
        "model": model,
        "max_tokens": 60,
        "messages": [{"role": "user",
                     "content": "Reply exactly: " + NONCE}],
    }).encode()
    req = urllib.request.Request(url, data=body, headers=headers,
                                method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    choices = data.get("choices") or []
    content = (choices[0].get("message") or {}).get("content") or ""
    return NONCE in content, content[:60]


SKIP_EXACT = ("cline-free/",)


def probe(name, port, key, tries=3, timeout=20.0):
    headers = {"Authorization": "Bearer " + key} if key else {}
    try:
        models = list_models(port, headers)
    except Exception as exc:
        return False, "list failed: %s" % str(exc)[:60]
    if not models:
        return False, "no models advertised"
    candidates = [m for m in models
                  if not any(t in m.lower() for t in SKIP_RE)][:tries]
    # a model hitting a rate cap is not proof the bridge is down: keep going
    # until one model answers or the list runs out.
    candidates = [m for m in candidates if not m.startswith(SKIP_EXACT)] or candidates
    if not candidates:
        return False, "only image/tts models"
    last = ""
    for model in candidates:
        try:
            ok, content = try_call(port, headers, model, timeout)
            if ok:
                return True, "%s -> %s" % (model, content)
            last = "%s: no echo" % model
        except urllib.error.HTTPError as exc:
            last = "%s: HTTP %s" % (model, exc.code)
        except Exception as exc:
            last = "%s: %s" % (model, str(exc)[:40])
    return False, last or "no chat model to try"


def plist_port(name):
    home = os.path.expanduser("~")
    for suffix in ("", "-gpt", "2codex"):
        path = os.path.join(home, "Library", "LaunchAgents",
                            "com.local.%s%s.plist" % (name, suffix))
        if not os.path.exists(path):
            continue
        text = open(path, encoding="utf-8").read()
        m = re.search(r"--port[=\s]+(\d{4,5})", text)
        if m:
            return int(m.group(1))
    return None


def write_json(path, snap):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False, indent=1)
    json.load(open(tmp, encoding="utf-8"))
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=os.environ.get(
        "FLEET_ENV_FILE",
        os.path.expanduser("~/AI Shared/repo/FleetKit/runtime/fleet.env")))
    ap.add_argument("--out", default=os.environ.get(
        "FLEET_REACH_FILE",
        os.path.expanduser("~/.codex/fleet-reach.json")))
    ap.add_argument("--kit-reach", default=None)
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--chunk", type=int, default=4,
                    help="bridges per subprocess run (0 = all in one)")
    ap.add_argument("--only", default="",
                    help="comma-separated bridge names to probe")
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--call-timeout", type=float, default=20.0)
    ap.add_argument("--tries", type=int, default=6)
    args = ap.parse_args()

    env = load_env(args.env)
    reachable, unreachable, evidence, ports = [], [], {}, {}
    names = [n.strip() for n in args.only.split(",") if n.strip()]
    for name in (names or sorted(PORTS)):
        port = plist_port(name) or PORTS[name]
        ports[name] = port
        key = env.get(KEY_ENV.get(name, ""), "")
        ok, why = probe(name, port, key, tries=max(1, args.tries),
                        timeout=args.call_timeout)
        evidence[name] = why
        (reachable if ok else unreachable).append(name)
        print(("UP  " if ok else "DOWN"), name, port, why, flush=True)

    tz = datetime.datetime.now().astimezone().tzinfo
    snap = {
        "reachable": sorted(reachable),
        "unreachable": sorted(unreachable),
        "measured_at": datetime.datetime.now(tz).isoformat(
            timespec="seconds"),
        "ports": ports,
        "evidence": evidence,
    }

    if args.stdout:
        print(json.dumps(snap, ensure_ascii=False, indent=1))
        return 0

    if args.merge and os.path.exists(args.out):
        try:
            prev = json.load(open(args.out, encoding="utf-8"))
            ev = dict(prev.get("evidence") or {})
            ev.update(snap["evidence"])
            pr = dict(prev.get("ports") or {})
            pr.update(snap["ports"])
            good = sorted(set(prev.get("reachable") or []) |
                            set(snap["reachable"]))
            bad = sorted((set(prev.get("unreachable") or []) |
                           set(snap["unreachable"])) - set(good))
            snap = dict(snap, reachable=good, unreachable=bad,
                       evidence=ev, ports=pr)
            print("merged with", args.out)
        except Exception as exc:
            print("merge skipped:", exc)
    write_json(args.out, snap)
    print("wrote", args.out)
    if args.kit_reach:
        write_json(args.kit_reach, snap)
        print("wrote", args.kit_reach)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
