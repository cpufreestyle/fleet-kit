#!/usr/bin/env python3
"""Keep the Codex default model on a route that actually answers.

Why this exists, in one sentence: this fleet once left model = "trae/
trae-step-5-preview" pinned in ~/.codex/config.toml after that route died
(401 -> 502), and fleet.env still carried the override that re-pinned it on
every setup -- Codex opened on a model that could not answer, and the only
way out was remembering to switch by hand.

The guarantees:

  * the live default -- the model line in ~/.codex/config.toml -- must
    answer one real chat call on its own route. A listening port, a
    /v1/models listing or a canned 200 do not count; the picker is what the
    user feels. fleet_probe.py owns that contract for the whole fleet, this
    tool owns it for the one model Codex boots on;
  * when the live default's route is dead and the default is not already
    the harbor (stepfun/step-5-preview, the StepFun official Plan API behind
    the image-cap shim), the default jumps back to the harbor and
    config.toml is re-pinned, with the evidence printed;
  * when the harbor itself is dead, the guard says so and changes nothing:
    there is nowhere to jump back to;
  * CC Switch's runtime failover (auto_failover_enabled plus the harbor
    sitting in the codex failover queue) is checked read-only, because that
    is what carries a request to the harbor when the provider the user
    selected mid-session breaks. The guard never writes CC Switch's
    database: it is a 55MB file the app keeps open while it answers, and a
    half-applied flag is worse than a reported one.

StepFun routes are probed through the image-cap shim on 15722, which is the
endpoint CC Switch forwards to. Probing Codex's gateway (15721) instead
would measure whichever provider is current there, not the default: CC
Switch rewrites a foreign model to the current provider's model before it
ever leaves the gateway, which is exactly how a dead trae default looked
healthy. Every other provider is probed on its own bridge port for the same
reason.

Nothing raises: every caller is either a setup script or a timer that has
to carry on with its next job.
"""
import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

HARBOR = "stepfun/step-5-preview"
SHIM_PORT = 15722
CC_DB = "~/.cc-switch/cc-switch.db"

# Ports and fleet.env key names per bridge; mirrors fleet_probe.py so the
# two tools never disagree about where a provider lives.
BRIDGE_PORTS = {
    "workbuddy": 8787, "workbuddy-gpt": 8788, "qoder": 8789,
    "codely": 8790, "trae": 8791, "lingxi": 8792, "xhx": 8793,
    "gemini": 8794, "catpaw": 8795, "antigravity": 8797,
    "qwen": 8798, "cline": 8799, "zcode": 8800,
}
KEY_ENV = {
    "workbuddy": "CODEBUDDY2OPENAI_KEY", "workbuddy-gpt": "CODEBUDDY2OPENAI_KEY",
    "qoder": "QODER2CODEX_KEY", "codely": "CODELY2CODEX_KEY",
    "trae": "TRAE2CODEX_KEY", "lingxi": "LINGXI2CODEX_KEY",
    "xhx": "XHX2CODEX_KEY", "gemini": "GEMINI2CODEX_KEY",
    "catpaw": "CATPAW2CODEX_KEY", "antigravity": "ANTIGRAVITY2CODEX_KEY",
    "qwen": "QWEN2CODEX_KEY", "cline": "CLINE2CODEX_KEY",
    "zcode": "ZCODE2CODEX_KEY",
}

NONCE = "E2E_OK"
BUDGETS = (60, 1024)
PROBE_PROMPT = "Reply exactly: " + NONCE
ERROR_MARKERS = ("error", "upstream", "refused", "unauthorized", "401",
                 "402", "403", "404", "429", "500", "502", "503")

# urllib otherwise hands loopback calls to the macOS system proxy and they
# leave through the tunnel: fine in a terminal, a silent hang under launchd.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_env(path):
    """fleet.env as a dict; the same KEY=VALUE shape fleet_probe reads."""
    env = {}
    if not path or not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip(chr(34)).strip(chr(39))
    return env


def read_model(config_path):
    """The model line Codex opens on, or None when the file pins none."""
    if not os.path.exists(config_path):
        return None
    with open(config_path, encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped.startswith("model = ") or stripped.startswith("model="):
                return stripped.split("=", 1)[1].strip().strip(chr(34))
    return None


def pin_model(config_path, model):
    """Rewrite the model line, inserting one after model_provider if absent.

    Same line transform setup-providers.sh applies with its inline python;
    the two must stay identical or the next setup undoes the guard's jump.
    """
    with open(config_path, encoding="utf-8") as fh:
        lines = fh.readlines()
    out, pinned = [], False
    for line in lines:
        if line.startswith("model = ") or line.startswith("model="):
            out.append('model = "%s"' % model)
            out[-1] += chr(10)
            pinned = True
        else:
            out.append(line)
    if not pinned:
        for index, line in enumerate(out):
            if line.startswith("model_provider"):
                out.insert(index + 1, 'model = "%s"' % model + chr(10))
                break
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.writelines(out)


def route_for(slug, env):
    """Where a model slug is probed: (url, key, model, detail).

    Returns (None, None, None, detail) when the slug names no route this
    tool can measure. The probe model is the part after the provider
    prefix: the StepFun Plan API and every bridge name their models bare
    (step-5-preview, hy4-preview), and sending the catalog slug upstream is
    how a healthy route gets measured as a 404. A slug with no slash
    resolves against the default provider, which is stepfun on this fleet.
    """
    if "/" in (slug or ""):
        provider, model = slug.split("/", 1)
    else:
        provider, model = "", slug or ""
    if provider in ("stepfun", ""):
        key = env.get("STEPFUN_PLAN_API_KEY", "")
        if not key:
            return None, None, None, "STEPFUN_PLAN_API_KEY is not set in fleet.env"
        return ("http://127.0.0.1:%d/v1/chat/completions" % SHIM_PORT,
                key, model, "image-cap shim on %d" % SHIM_PORT)
    if provider in BRIDGE_PORTS:
        key = env.get(KEY_ENV[provider], "")
        if not key:
            return None, None, None, "%s is not set in fleet.env" % KEY_ENV[provider]
        return ("http://127.0.0.1:%d/v1/chat/completions" % BRIDGE_PORTS[provider],
                key, model, "%s bridge on %d" % (provider, BRIDGE_PORTS[provider]))
    return None, None, None, "no local route for provider %r" % (provider or "?")


def is_harbor(slug):
    """True when the slug names the harbor, bare prefix included.

    config.toml may carry either spelling -- CC Switch's own template pins
    the bare step-5-preview, the fleet pin writes stepfun/step-5-preview --
    and both answer through the same shim, so a jump between them would be
    churn, not a rescue.
    """
    if not slug:
        return False
    if slug == HARBOR:
        return True
    return "/" not in slug and slug == HARBOR.split("/", 1)[1]


def probe(url, key, model, timeout=25.0):
    """One real chat call. Returns (ok, evidence)."""
    last = "no attempt"
    for budget in BUDGETS:
        body = json.dumps({
            "model": model,
            "max_tokens": budget,
            "messages": [{"role": "user", "content": PROBE_PROMPT}],
        }).encode()
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + (key or "PROXY_MANAGED")})
        try:
            with OPENER.open(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode()[:120]
            except Exception:
                pass
            return False, "HTTP %s %s" % (exc.code, detail)
        except Exception as exc:
            return False, "%s: %s" % (type(exc).__name__, exc)
        choices = data.get("choices") or []
        content = ((choices[0].get("message") or {}).get("content")
                   if choices else "") or ""
        text = content.strip()
        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        reasoning = details.get("reasoning_tokens") or 0
        low = text.lower()
        if NONCE in text:
            return True, text[:60]
        if len(text) >= 8 and not low.startswith(("error", "sorry", "i cannot",
                                          '"error"')):
            # A real reply that ignores "reply exactly" is still a live
            # route; only canned failures and silence are not.
            return True, text[:60]
        if any(marker in low for marker in ERROR_MARKERS) and len(text) < 200:
            return False, "upstream refused: " + text[:120]
        if not text and reasoning >= budget * 0.9:
            # Spent the whole budget reasoning; the bigger budget retries.
            last = "empty reply, reasoning ate the budget"
            continue
        last = text[:120] or "empty reply"
    return False, last


def cc_failover_state(db_path):
    """Read CC Switch's runtime failover flags. Returns (ok, lines).

    Read-only on purpose: CC Switch caches this database in memory, so a
    write without an app restart changes nothing and a write during a
    switch can corrupt the provider the user just picked. The guard reports
    drift and prints the repair instead of performing it.
    """
    path = os.path.abspath(os.path.expanduser(db_path))
    if not os.path.exists(path):
        return True, ["cc-switch db not found (%s); runtime failover unchecked"
                      % path]
    lines = []
    ok = True
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)
    except sqlite3.Error as exc:
        return True, ["cc-switch db unreadable (%s); unchecked" % exc]
    try:
        row = conn.execute(
            "select auto_failover_enabled from proxy_config"
            " where app_type='codex'").fetchone()
        if row is None:
            lines.append("cc-switch: no codex proxy_config row; unchecked")
        elif int(row[0] or 0) != 1:
            ok = False
            lines.append("cc-switch DRIFT: auto_failover_enabled=%s for codex"
                         % row[0])
            lines.append("  repair: update proxy_config set"
                         " auto_failover_enabled=1 where app_type='codex';"
                         " (this CC Switch reads the db live; an older build"
                         " needs an app restart)")
        else:
            lines.append("cc-switch: auto failover on for codex")
        row = conn.execute(
            "select in_failover_queue from providers"
            " where app_type='codex' and name='StepFun'").fetchone()
        if row is None:
            ok = False
            lines.append("cc-switch DRIFT: no StepFun provider for codex;"
                         " the failover target is gone")
        elif int(row[0] or 0) != 1:
            ok = False
            lines.append("cc-switch DRIFT: StepFun in_failover_queue=%s"
                         % row[0])
            lines.append("  repair: update providers set in_failover_queue=1"
                         " where app_type='codex' and name='StepFun';"
                         " (this CC Switch reads the db live; an older build"
                         " needs an app restart)")
        else:
            lines.append("cc-switch: StepFun sits in the codex failover queue")
    except sqlite3.Error as exc:
        return True, ["cc-switch db schema unexpected (%s); unchecked" % exc]
    finally:
        conn.close()
    return ok, lines


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Keep the Codex default model on a route that answers;"
                    " jump it back to %s when it does not." % HARBOR)
    parser.add_argument("--env-file", default=None,
                        help="fleet.env to read FLEET_DEFAULT_MODEL and keys"
                             " from (default: <kit>/../runtime/fleet.env)")
    parser.add_argument("--config", default=os.path.expanduser(
        "~/.codex/config.toml"), help="Codex config to guard")
    parser.add_argument("--cc-db", default=CC_DB,
                        help="CC Switch database to read failover state from")
    parser.add_argument("--dry-run", action="store_true",
                        help="report only; never rewrite the config")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable verdict")
    args = parser.parse_args(argv)

    env_file = args.env_file
    if not env_file:
        here = os.path.dirname(os.path.abspath(__file__))
        env_file = os.path.join(here, os.pardir, os.pardir, "runtime",
                                "fleet.env")
    env = load_env(env_file)

    override = (env.get("FLEET_DEFAULT_MODEL") or "").strip()
    live = read_model(args.config)
    report = {"harbor": HARBOR, "env_override": override or None,
             "live_default": live, "env_file": os.path.abspath(env_file)}
    lines = []
    failed = False

    if live is None:
        lines.append("no model line in %s; nothing to guard" % args.config)
        report["verdict"] = "no-default"
    else:
        url, key, model, detail = route_for(live, env)
        if url is None:
            lines.append("default %s: cannot probe (%s); left alone"
                         % (live, detail))
            report["verdict"] = "unprobeable"
            report["evidence"] = detail
        else:
            ok, evidence = probe(url, key, model)
            report["target"] = detail
            report["evidence"] = evidence
            if ok:
                lines.append("default %s answers via %s (%s)"
                             % (live, detail, evidence))
                report["verdict"] = "healthy"
            elif is_harbor(live):
                failed = True
                lines.append("HARBOR DEAD: %s does not answer via %s (%s);"
                             " nothing to jump back to"
                             % (live, detail, evidence))
                report["verdict"] = "harbor-dead"
            else:
                report["verdict"] = "jumped"
                report["jumped_from"] = live
                if args.dry_run:
                    lines.append("default %s is dead via %s (%s); would jump"
                                 " back to %s"
                                 % (live, detail, evidence, HARBOR))
                else:
                    try:
                        pin_model(args.config, HARBOR)
                    except OSError as exc:
                        failed = True
                        report["verdict"] = "jump-failed"
                        lines.append("JUMP FAILED: could not rewrite %s (%s)"
                                     % (args.config, exc))
                    else:
                        lines.append("default jumped back: %s -> %s"
                                     " (was dead via %s: %s)"
                                     % (live, HARBOR, detail, evidence))

    # A dying override is a trap for the next setup even when the live
    # default is healthy: setup re-pins whatever fleet.env names.
    if override and override != live:
        url, key, model, detail = route_for(override, env)
        if url is None:
            lines.append("fleet.env override %s cannot be probed (%s);"
                         " the next setup will pin it anyway"
                         % (override, detail))
            report["override_verdict"] = "unprobeable"
        else:
            ok, evidence = probe(url, key, model)
            report["override_verdict"] = "healthy" if ok else "dead"
            if not ok:
                lines.append("WARNING: fleet.env override %s is dead via %s"
                             " (%s); setup will pin it on the next run --"
                             " set FLEET_DEFAULT_MODEL=%s"
                             % (override, detail, evidence, HARBOR))
            else:
                lines.append("fleet.env override %s answers via %s"
                             % (override, detail))

    cc_ok, cc_lines = cc_failover_state(args.cc_db)
    lines.extend(cc_lines)
    report["cc_failover_ok"] = cc_ok
    if not cc_ok:
        failed = True

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        for line in lines:
            print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
