#!/usr/bin/env python3
"""Measure which fleet bridges actually answer a real request.

Writes a reachability snapshot consumed by catalog_sort.py:

  {"reachable": [...], "unreachable": [...],
   "measured_at": "2026-09-27T17:10:00+08:00",
   "skipped": {"zcode": "upstream requires per-call Aliyun captcha"},
   "evidence": {"workbuddy": "glm-5.2 -> E2E_OK", ...}}

A bridge counts as reachable only when a real chat call returns the expected
nonce echo. A listening port, a /v1/models listing, or a fast canned 200 all
count as NOT reachable, because the picker is what the user actually feels.
skipped lists bridges the sweep does not call on purpose, so a strict sorter
can tell them apart from a bridge a truncated run never reached.

Usage:
  fleet_probe.py            write ~/.codex/fleet-reach.json
  fleet_probe.py --stdout   print JSON instead of writing
"""
import argparse
import datetime
import json
import os
import sys
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PORTS = {
    "workbuddy": 8787, "workbuddy-gpt": 8788, "qoder": 8789,
    "codely": 8790, "trae": 8791, "lingxi": 8792,
    "xhx": 8793, "gemini": 8794, "catpaw": 8795,
    "antigravity": 8797, "qwen": 8798, "cline": 8799,
    "zcode": 8800,
}

# Providers that do not own a local bridge: ocx forwards them straight to the
# vendor, so they are probed through the gateway on GATEWAY_PORT instead.
GATEWAY = {
    "stepfun": "stepfun",
    "tokendance": "tokendance",
}
GATEWAY_PORT = 10100

# local token each bridge checks, as found in its launchd plist
KEY_ENV = {
    "workbuddy": "CODEBUDDY2OPENAI_KEY",
    "workbuddy-gpt": "CODEBUDDY2OPENAI_KEY",
    "qoder": "QODER2CODEX_KEY", "codely": "CODELY2CODEX_KEY",
    "trae": "TRAE2CODEX_KEY", "lingxi": "LINGXI2CODEX_KEY",
    "xhx": "XHX2CODEX_KEY", "gemini": "GEMINI2CODEX_KEY",
    "catpaw": "CATPAW2CODEX_KEY", "antigravity": "ANTIGRAVITY2CODEX_KEY",
    "qwen": "QWEN2CODEX_KEY", "cline": "CLINE2CODEX_KEY",
    "zcode": "ZCODE2CODEX_KEY",
}

NONCE = "E2E_OK"
SKIP_RE = ("image", "tts", "embed", "ocr", "vision", "vl")

# Bridges whose upstream requires an interactive/anti-bot challenge on every
# real call. Probing them from the launchd reachability timer burns captcha
# tickets and repeatedly jumps the user to the verification page, without
# ever improving the picker (the challenge alone marks them unreachable).
# Add explicit --only <name> to probe one of these deliberately.
NO_PROBE = {
    "zcode": "upstream requires per-call Aliyun captcha; probe is opt-in",
}

# Every bridge and the ocx gateway listen on 127.0.0.1, but urllib picks up
# the macOS system proxy, so a local call went out to the tunnel and back:
# fine in a terminal, and a silent hang under launchd. Never proxy 127.0.0.1.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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
    with OPENER.open(req, timeout=8) as resp:
        data = json.loads(resp.read().decode())
    return [m.get("id") for m in data.get("data", []) if m.get("id")]


def try_call(port, headers, model, timeout=20.0, budget=60):
    url = "http://127.0.0.1:%d/v1/chat/completions" % port
    body = json.dumps({
        "model": model,
        "max_tokens": budget,
        "messages": [{"role": "user",
                     "content": "Reply exactly: " + NONCE}],
    }).encode()
    req = urllib.request.Request(url, data=body, headers=headers,
                                method="POST")
    with OPENER.open(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    choices = data.get("choices") or []
    content = (choices[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    # Echoing the nonce is the strongest signal, but a model that answers with
    # a real reply and simply does not follow "reply exactly" (qoder Qwen3.8-
    # Flash greets instead) is still reachable. What must not count as a pass:
    # an empty body, a canned error surfaced as text, or a stub.
    text = content.strip()
    if NONCE in text:
        return True, text[:60]
    if len(text) >= 8 and not text.lower().startswith(("error", "sorry, i can",
                                                     "i cannot", "\"error\"")):
        return True, text[:60]
    if not text:
        # finish_reason=length with an empty reply means the model spent the
        # whole budget on reasoning; the caller retries with a bigger one.
        details = (usage.get("completion_tokens_details") or {})
        reasoning = details.get("reasoning_tokens") or 0
        spent = usage.get("completion_tokens") or 0
        if (choices[0].get("finish_reason") == "length"
                and max(reasoning, spent) >= budget * 0.9):
            return None, text[:60]
    return False, text[:60]


SKIP_EXACT = ("cline-free/",)
# hy4 and friends reason before they speak, so a 60-token budget ends
# with content=null and finish_reason=length: a dead-looking pass that
# would sink a working model. Escalate the budget before judging it.
BUDGETS = (60, 1024)


def probe_gateway(name, model_prefix, timeout=20.0, tries=3):
    """Probe a provider that ocx forwards straight to its vendor.

    Retries and the same lenient content check as probe(): a vendor model
    that answers a real reply while ignoring "reply exactly" is still
    reachable, and a single attempt would flap the ordering.
    """
    url = "http://127.0.0.1:%d/v1/models" % GATEWAY_PORT
    headers = {"Authorization": "Bearer PROXY_MANAGED"}
    data = None
    for _list_attempt in range(3):
        try:
            with OPENER.open(urllib.request.Request(url, headers=headers),
                                        timeout=8) as resp:
                data = json.loads(resp.read().decode())
            break
        except Exception:
            continue
    if data is None:
        return False, "gateway list failed", None
    ids = [m.get("id") for m in data.get("data", [])
            if (m.get("id") or "").startswith(model_prefix + "/")]
    if not ids:
        return False, "no %s models in gateway" % model_prefix, None
    last = ""
    for model in ids[:tries]:
        for budget in BUDGETS:
            for attempt in range(3 if budget == BUDGETS[0] else 1):
                body = json.dumps({
                    "model": model,
                    "max_tokens": budget,
                    "messages": [{"role": "user",
                                 "content": "Reply exactly: " + NONCE}],
                }).encode()
                req = urllib.request.Request(
                    "http://127.0.0.1:%d/v1/chat/completions" % GATEWAY_PORT,
                    data=body,
                    headers={**headers, "Content-Type": "application/json"},
                    method="POST")
                try:
                    with OPENER.open(req, timeout=timeout) as resp:
                        d2 = json.loads(resp.read().decode())
                    content = (d2.get("choices") or [{}])[0].get(
                        "message", {}).get("content", "")
                    text = content.strip()
                    usage = d2.get("usage") or {}
                    details = (usage.get("completion_tokens_details") or {})
                    spent = usage.get("completion_tokens") or 0
                    reasoning = details.get("reasoning_tokens") or 0
                    if NONCE in text:
                        return True, "%s -> %s" % (model, text[:40]), model
                    if len(text) >= 8 and not text.lower().startswith(
                            ("error", "sorry, i can", "i cannot", "\"error\"")):
                        return True, "%s -> %s" % (model, text[:40]), model
                    empty_thinking = (
                        not text
                        and (d2.get("choices") or [{}])[0].get(
                            "finish_reason") == "length"
                        and max(reasoning, spent) >= budget * 0.9)
                    if empty_thinking:
                        last = "%s: empty at %d tokens" % (model, budget)
                        break
                    last = "%s: no echo (attempt %d)" % (model, attempt + 1)
                except urllib.error.HTTPError as exc:
                    return False, "%s: HTTP %s" % (model, exc.code), None
                except Exception as exc:
                    last = "%s: %s" % (model, str(exc)[:40])
                    break
    return False, last or "no %s models answered" % model_prefix, None


def probe(name, port, key, tries=3, timeout=20.0):  # -> (ok, why, model)
    headers = {"Authorization": "Bearer " + key} if key else {}
    try:
        models = list_models(port, headers)
    except Exception as exc:
        return False, "list failed: %s" % str(exc)[:60], None
    if not models:
        return False, "no models advertised", None
    candidates = [m for m in models
                  if not any(t in m.lower() for t in SKIP_RE)][:tries]
    # a model hitting a rate cap is not proof the bridge is down: keep going
    # until one model answers or the list runs out.
    candidates = [m for m in candidates if not m.startswith(SKIP_EXACT)] or candidates
    if not candidates:
        return False, "only image/tts models", None
    last = ""
    attempts = 3
    for model in candidates:
        for budget in BUDGETS:
            for _attempt in range(attempts if budget == BUDGETS[0] else 1):
                try:
                    ok, content = try_call(port, headers, model, timeout,
                                           budget=budget)
                    if ok:
                        return True, "%s -> %s" % (model, content), model
                    if ok is None:
                        # reasoning consumed the budget and said nothing: try
                        # the next budget before calling this model dead
                        last = "%s: empty at %d tokens" % (model, budget)
                        break
                    last = "%s: no echo (attempt %d)" % (model, _attempt + 1)
                except urllib.error.HTTPError as exc:
                    last = "%s: HTTP %s" % (model, exc.code)
                    break
                except Exception as exc:
                    last = "%s: %s" % (model, str(exc)[:40])
                    break
    return False, last or "no chat model to try", None


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def plist_port(name):
    """Port a bridge actually listens on.

    macOS keeps reading the launchd plist it always read; Windows and Linux
    read the wrapper install.sh wrote. Neither backend has ~/Library, so this
    goes through fleet_platform.service_ports(), which also falls back to
    PORT_BASE + offset when no service is installed yet.
    """
    try:
        from fleet_platform import service_port
    except Exception:
        return None
    try:
        return service_port(name)
    except Exception:
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
    ap.add_argument("--sort-after", action="store_true",
                    help="re-sort the catalog once the snapshot is written")
    ap.add_argument("--call-timeout", type=float, default=45.0,
                    help="per-attempt timeout; slow models must not look dead")
    ap.add_argument("--tries", type=int, default=6)
    args = ap.parse_args()

    env = load_env(args.env)
    reachable, unreachable, evidence, ports = [], [], {}, {}
    verified_models = {}
    skipped = {}
    names = [n.strip() for n in args.only.split(",") if n.strip()]
    every = sorted(set(PORTS) | set(GATEWAY))
    for name in (names or every):
        if name in NO_PROBE and not names:
            print("SKIP", name, NO_PROBE[name], flush=True)
            # Record the skip in the snapshot. A bridge the sweep deliberately
            # does not call (zcode needs a per-call captcha) is otherwise
            # indistinguishable from a truncated run's missing bridge, and
            # catalog_sort.py --strict-coverage refuses every sort on it.
            skipped[name] = NO_PROBE[name]
            continue
        port = plist_port(name) or PORTS.get(name) or GATEWAY_PORT
        ports[name] = port
        if name in GATEWAY:
            ok, why, model = probe_gateway(name, name, timeout=args.call_timeout)
        else:
            key = env.get(KEY_ENV.get(name, ""), "")
            ok, why, model = probe(name, port, key, tries=max(1, args.tries),
                                  timeout=args.call_timeout)
        evidence[name] = why
        (reachable if ok else unreachable).append(name)
        verified_models[name] = model
        print(("UP  " if ok else "DOWN"), name, port, why, flush=True)

    tz = datetime.datetime.now().astimezone().tzinfo
    snap = {
        "reachable": sorted(reachable),
        "unreachable": sorted(unreachable),
        "skipped": skipped,
        "measured_at": datetime.datetime.now(tz).isoformat(
            timespec="seconds"),
        "ports": ports,
        "evidence": evidence,
    }
    # which exact model answered, so the sorter can float proven-good rows up
    verified = {}
    for name, model in verified_models.items():
        if model:
            verified[name] = model
    snap["verified_models"] = verified

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
            sk = dict(prev.get("skipped") or {})
            sk.update(snap.get("skipped") or {})
            for name in set(good) | set(bad):
                sk.pop(name, None)
            snap = dict(snap, reachable=good, unreachable=bad,
                       evidence=ev, ports=pr, skipped=sk)
            print("merged with", args.out)
        except Exception as exc:
            print("merge skipped:", exc)
    write_json(args.out, snap)
    print("wrote", args.out)
    if args.kit_reach:
        write_json(args.kit_reach, snap)
        print("wrote", args.kit_reach)
    # A probe that only refreshes the snapshot leaves the picker stale until
    # the next ocx sync, so fold the reorder into the same run.
    if args.sort_after:
        sorter = Path(__file__).resolve().parent / "catalog_sort.py"
        if sorter.exists():
            try:
                subprocess.run([sys.executable, str(sorter),
                                "--reach", args.out],
                               capture_output=True, text=True,
                               timeout=120)
                print("catalog re-sorted")
            except Exception as exc:
                print("re-sort failed:", exc)
    if args.kit_reach:
        write_json(args.kit_reach, snap)
        print("wrote", args.kit_reach)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
