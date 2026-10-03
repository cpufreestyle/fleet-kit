#!/usr/bin/env python3
"""Read the quota a coding-plan account has left (Kimi Code, MiniMax).

Kimi Code publishes the coding-plan windows at GET {base}/v1/usages -- the
same endpoint CC Switch queries for its Claude provider -- so one GET
answers "how much is left" without spending anything. The key is taken
either as a bearer token or in the Anthropic-style x-api-key header, so a
refusal under one shape is retried under the other.

An account with no coding plan answers {} there, which is not an answer
anyone can act on, so the plan is also read from GET {base}/v1/me and, when
that shows nothing to spend, one short chat call is made to surface the
reason the platform gives. Measured 2026-10-02 on an account whose Kimi Code
access had lapsed: usages {} , /v1/me user_level_name "Free" and goods_version
0, and the chat call coming back 403 access_terminated_error naming the plan
page to renew at.

MiniMax publishes no balance endpoint. What it does answer is a real chat
call: a one-token request both proves the key still works and moves the
counter, which is the only quota signal the platform hands out. The raw
reply is printed so the number can be read straight off the console when
the body carries one.

Probed 2026-10-02: on api.minimaxi.com and api.minimax.io, /v1/chat/completions
answers 401 authorized_error while /v1/usage, /v1/usages, /v1/credits,
/v1/quota and /v1/account all answer 404, so there is no balance route to
query. On api.kimi.com, /coding/v1/usages answers 401
invalid_authentication_error while its sibling /coding/v1/usage answers 404,
which is what pins that path as the right one.

Where a key comes from, in order: --key, then KIMI_CODING_API_KEY or
MINIMAX_API_KEY, then the Kimi desktop app's own key file (see
kimi_app_key). That last one is why "plan_credits.py kimi" works with no
arguments on a machine that has used Kimi Code once; --no-app-key turns it
off.

Usage:
  plan_credits.py kimi    [--key KEY] [--base URL] [--no-app-key]
  plan_credits.py minimax [--key KEY] [--base URL] [--model ID]
  plan_credits.py all                    # every plan, keys from the
                                         # environment or the app's file

Environment: KIMI_CODING_API_KEY, MINIMAX_API_KEY.

Exit codes: 0 the platform answered, 2 the platform refused the key (401/403
-- a dead key, the one answer worth acting on), 1 transport or shape
failure, 3 no key supplied, 4 the platform answered and the key works but
the plan it would bill is not active.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

KIMI_BASE = "https://api.kimi.com/coding"
MINIMAX_BASE = "https://api.minimaxi.com"
# A MiniMax key is minted against one region, so an international key reads as
# 401 against the CN host and the other way round. Both are tried before the
# key is called dead; --base pins one host and skips the retry.
MINIMAX_INTL_BASE = "https://api.minimax.io"
MINIMAX_MODEL = "MiniMax-M2"
# MiniMax's Token Plan quota. Documented in their own FAQ
# (platform.minimaxi.com/docs/token-plan/faq.md, "如何查看 Token Plan 用量",
# method two) as GET https://www.minimax.cn/v1/token_plan/remains. The key it
# wants is a subscription Key, which is a different credential from a normal
# pay-as-you-go API key and cannot be substituted for one.
MINIMAX_REMAINS_HOSTS = ("https://www.minimax.cn", "https://www.minimaxi.com")
KIMI_MODEL = "kimi-for-coding"

# The Kimi desktop app keeps the coding key it minted under its own user data.
# Read, never written: this is the user's own file on their own machine, and
# reading it is what spares them pasting a key that is already there.
KIMI_APP_KEY_FILES = (
    ("darwin", "Library/Application Support/kimi-desktop/daimon-share/daimon/kimi-code-key.json"),
    ("win32", "AppData/Roaming/kimi-desktop/daimon-share/daimon/kimi-code-key.json"),
    ("linux", ".config/kimi-desktop/daimon-share/daimon/kimi-code-key.json"),
)

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


def _as_json(text):
    """A parsed body, or the head of the text when it is not JSON."""
    try:
        return json.loads(text)
    except ValueError:
        return text[:400]


def app_key_from(root):
    """(key, where) from a Kimi app user-data root, or (None, why)."""
    for platform, rel in KIMI_APP_KEY_FILES:
        if not sys.platform.startswith(platform):
            continue
        path = os.path.join(root, *rel.split("/"))
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        keys = data.get("keys") if isinstance(data, dict) else None
        if isinstance(keys, list) and keys and isinstance(keys[0], dict):
            key = keys[0].get("apiKey")
            if key:
                return key, path
    return None, "no kimi-code key in the Kimi desktop app's storage"


def kimi_app_key(home=None):
    """(key, where) from this machine's Kimi desktop app, or (None, why)."""
    return app_key_from(home or os.path.expanduser("~"))


def _kimi_usages(key, root):
    """(status, out) for GET {base}/v1/usages, both auth shapes tried."""
    url = root + "/v1/usages"
    outcome = (None, {"platform": "kimi-code", "endpoint": url})
    for header, value in (("Authorization", "Bearer " + key),
                          ("x-api-key", key)):
        status, text = fetch(url, {header: value,
                                     "Accept": "application/json"})
        out = {"platform": "kimi-code", "endpoint": url, "http": status,
               "auth": header, "body": _as_json(text)}
        if status == 200:
            return status, out
        outcome = (status, out)
    return outcome


def _kimi_plan(key, root):
    """The plan /v1/me reports, or None when it will not say."""
    status, text = fetch(root + "/v1/me",
                         {"Authorization": "Bearer " + key,
                          "Accept": "application/json"})
    if status != 200:
        return None
    body = _as_json(text)
    if not isinstance(body, dict):
        return None
    return {"user_level": body.get("user_level"),
            "user_level_name": body.get("user_level_name"),
            "goods_version": body.get("goods_version"),
            "status": body.get("status")}


def _kimi_reason(key, root):
    """Why a plan that reports nothing answers nothing, from one chat call.

    A lapsed plan comes back 403 access_terminated_error naming the page to
    renew at, which is the sentence the user actually needs. Only spent a
    sliver, and only when there was nothing to report in the first place.
    """
    status, text = fetch(
        root + "/v1/chat/completions",
        {"Authorization": "Bearer " + key},
        method="POST",
        body={"model": KIMI_MODEL, "max_tokens": 16,
              "messages": [{"role": "user", "content": "hi"}]})
    body = _as_json(text)
    probe = {"endpoint": root + "/v1/chat/completions", "http": status,
             "body": body}
    terminated = status == 403 and isinstance(body, dict) and (
        (body.get("error") or {}).get("type") == "access_terminated_error")
    return ("terminated" if terminated else "unknown"), probe


def kimi(key, base=KIMI_BASE, spend=True):
    """Kimi Code coding-plan windows, plus the plan the key actually holds.

    An account with no plan answers {} from /v1/usages, so the plan is read
    from /v1/me and the reason is read from one chat call. spend=False skips
    that call for anyone who would rather not spend even a sliver.
    """
    root = base.rstrip("/")
    status, out = _kimi_usages(key, root)
    out["plan"] = _kimi_plan(key, root)
    windows = out.get("body")
    if spend and status == 200 and not (isinstance(windows, dict) and windows):
        out["subscription"], out["reason"] = _kimi_reason(key, root)
    return status, out


def minimax_remains(key, hosts=MINIMAX_REMAINS_HOSTS):
    """Token Plan quota, straight from GET {base}/v1/token_plan/remains.

    This is the endpoint MiniMax documents for exactly this question, found
    in their own FAQ on 2026-10-03. Probing the open-platform hosts for
    /v1/usage and friends finds nothing, which is why it was missed for
    three days: the route lives on the www host, not the api host.

    It wants a subscription Key. A normal pay-as-you-go API key is a
    different credential and reads as a login failure here, so the answer
    is returned as-is rather than retried or reinterpreted.
    """
    outcome = (None, {})
    for host in hosts:
        url = host.rstrip("/") + "/v1/token_plan/remains"
        status, text = fetch(url, {"Authorization": "Bearer " + key,
                                   "Content-Type": "application/json"})
        out = {"platform": "minimax", "endpoint": url,
               "token_plan_remains": True, "http": status,
               "body": _as_json(text)}
        if status == 200:
            body = out["body"]
            if isinstance(body, dict) and body.get("base_resp", {}).get(
                    "status_code", 0) == 0:
                out["answered"] = True
                return status, out
        outcome = (status, out)
    return outcome


def minimax(key, base=None, model=MINIMAX_MODEL, bases=None):
    """Token Plan quota first, then a one-token chat call as the fallback.

    A 401 is the only answer that means "this key is dead", and it is also
    what the wrong region answers, so the other region is tried before that
    verdict is written down. bases pins the host list.
    """
    hosts = [base] if base else list(bases or (MINIMAX_BASE, MINIMAX_INTL_BASE))
    outcome = (None, {})
    for host in hosts:
        url = host.rstrip("/") + "/v1/chat/completions"
        status, text = fetch(
            url,
            {"Authorization": "Bearer " + key},
            method="POST",
            body={"model": model, "max_tokens": 1,
                  "messages": [{"role": "user", "content": "hi"}]})
        out = {"platform": "minimax", "endpoint": url, "model": model,
               "http": status, "body": _as_json(text)}
        if status not in (401, 403):
            return status, out
        outcome = (status, out)
    return outcome


def report(name, status, out):
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if out.get("subscription") == "terminated":
        renew = ""
        body = ((out.get("reason") or {}).get("body") or {})
        if isinstance(body, dict):
            renew = (body.get("error") or {}).get("message") or ""
        print("the key works but this plan is not active -- %s" % renew,
              file=sys.stderr)
        print("renew the plan, then this reports real windows again",
              file=sys.stderr)
        return 4
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
    ap.add_argument("--no-spend", action="store_true",
                    help="skip the chat call that explains an empty plan")
    ap.add_argument("--no-app-key", action="store_true",
                    help="do not fall back to the Kimi desktop app's key file")
    args = ap.parse_args(argv)
    plans = ["kimi", "minimax"] if args.plan == "all" else [args.plan]
    worst = 0
    for name in plans:
        env = "KIMI_CODING_API_KEY" if name == "kimi" else "MINIMAX_API_KEY"
        key = args.key or os.environ.get(env)
        source = "environment"
        if not key and name == "kimi" and not args.no_app_key:
            key, where = kimi_app_key()
            source = where
        if not key:
            print("no key for %s (pass --key or set %s)" % (name, env),
                  file=sys.stderr)
            if name == "minimax":
                print("and the key it wants is a subscription Key, not a"
                      " pay-as-you-go API key -- they are separate"
                      " credentials and one cannot stand in for the other",
                      file=sys.stderr)
            else:
                print("the Kimi desktop app can supply one: it keeps the"
                      " coding key in its own user data", file=sys.stderr)
            worst = worst or 3
            continue
        if source != "environment":
            print("kimi key from %s" % source)
        base = args.base or (KIMI_BASE if name == "kimi" else None)
        if name == "kimi":
            status, out = kimi(key, base, spend=not args.no_spend)
        else:
            status, out = minimax_remains(key)
            if not out.get("answered"):
                # a pay-as-you-go key is not a subscription Key, so the
                # documented route cannot read it; fall back to the
                # one-token call that at least proves the key is alive
                status, out = minimax(key, base, args.model)
        worst = worst or report(name, status, out)
    return worst


if __name__ == "__main__":
    sys.exit(main())

