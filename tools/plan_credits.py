#!/usr/bin/env python3
"""Read the quota a coding-plan account has left (Kimi Code, MiniMax).

Kimi Code publishes the coding-plan windows at GET {base}/v1/usages -- the
same endpoint CC Switch queries for its Claude provider -- so one GET
answers "how much is left" without spending anything.

MiniMax publishes no balance endpoint. What it does answer is a real chat
call: a one-token request both proves the key still works and moves the
counter, which is the only quota signal the platform hands out. The raw
reply is printed so the number can be read straight off the console when
the body carries one.

Usage:
  plan_credits.py kimi    --key KEY [--base URL] [--json]
  plan_credits.py minimax --key KEY [--base URL] [--model ID] [--json]
  plan_credits.py all                    # keys from the environment

Environment: KIMI_CODING_API_KEY, MINIMAX_API_KEY.

Exit codes: 0 the platform answered, 2 the platform refused the key
(401/403 -- a dead key, the one answer worth acting on), 1 transport or
shape failure, 3 no key supplied.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

KIMI_BASE = "https://api.kimi.com/coding"
MINIMAX_BASE = "https://api.minimaxi.com"
MINIMAX_MODEL = "MiniMax-M2"

# loopback and vendor calls must not leave through a system proxy; the same
# rule the anthropic gateway follows
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(url, headers=None, method="GET", body=None, timeout=25):
    """(status, text) for one HTTP call; raises nothing."""
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def kimi(key, base=KIMI_BASE):
    """Kimi Code coding-plan windows, straight from /v1/usages."""
    url = base.rstrip("/") + "/v1/usages"
    status, text = fetch(url, {"Authorization": "Bearer " + key,
                               "Accept": "application/json"})
    out = {"platform": "kimi-code", "endpoint": url, "http": status}
    try:
        out["body"] = json.loads(text)
    except ValueError:
        out["body"] = text[:400]
    return status, out


def minimax(key, base=MINIMAX_BASE, model=MINIMAX_MODEL):
    """One one-token MiniMax chat call: proves the key, spends a sliver."""
    url = base.rstrip("/") + "/v1/chat/completions"
    status, text = fetch(
        url,
        {"Authorization": "Bearer " + key},
        method="POST",
        body={"model": model, "max_tokens": 1,
              "messages": [{"role": "user", "content": "hi"}]})
    out = {"platform": "minimax", "endpoint": url, "model": model,
           "http": status}
    try:
        out["body"] = json.loads(text)
    except ValueError:
        out["body"] = text[:400]
    return status, out


def report(name, status, out):
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if status in (401, 403):
        print("refused: this key is dead or revoked -- get a fresh one",
              file=sys.stderr)
        return 2
    if status is None or status >= 500:
        print("the platform did not answer cleanly", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("plan", choices=["kimi", "minimax", "all"])
    ap.add_argument("--key", default=None)
    ap.add_argument("--base", default=None)
    ap.add_argument("--model", default=MINIMAX_MODEL)
    args = ap.parse_args(argv)
    plans = ["kimi", "minimax"] if args.plan == "all" else [args.plan]
    worst = 0
    for name in plans:
        env = "KIMI_CODING_API_KEY" if name == "kimi" else "MINIMAX_API_KEY"
        key = args.key or os.environ.get(env)
        if not key:
            print("no key for %s (pass --key or set %s)" % (name, env),
                  file=sys.stderr)
            worst = worst or 3
            continue
        base = args.base or (KIMI_BASE if name == "kimi" else MINIMAX_BASE)
        if name == "kimi":
            status, out = kimi(key, base)
        else:
            status, out = minimax(key, base, args.model)
        worst = worst or report(name, status, out)
    return worst


if __name__ == "__main__":
    sys.exit(main())
