#!/usr/bin/env python3
"""Pin one model as the Codex default and fall back to it when the picked model breaks.

Why: the fleet has ~14 upstreams and any of them can go down mid-day (login
expired, quota, VPN, provider outage). When that happens Codex keeps sending to
the dead model and every turn fails. This guard makes `stepfun/step-5-preview`
the anchor:

  * `~/.codex/config.toml` is (re-)pinned to FLEET_DEFAULT_MODEL, so every new
    session starts there;
  * if the pinned model is *not* the default, it is probed with one minimal
    request through the opencodex proxy; on failure the file is rewritten back
    to the default, i.e. "其他模型出问题 → 跳回 Step 5 preview".

Probing only happens while a non-default model is selected, so the steady state
costs nothing.

    python3 tools/default_model_guard.py --once          # one check (cron/timer)
    python3 tools/default_model_guard.py --daemon --interval 120
    python3 tools/default_model_guard.py --once --dry-run
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "stepfun/step-5-preview"
PROXY_BASE = "http://127.0.0.1:10100"
PROBE_TIMEOUT = 45.0
MODEL_RE = re.compile(r'^(\s*model\s*=\s*)(["\'])(.*?)\2', re.MULTILINE)


def env_file() -> Path:
    for cand in (os.environ.get("FLEET_HOME"), Path.home() / "FleetKit" / "runtime"):
        if cand and (Path(cand) / "fleet.env").is_file():
            return Path(cand) / "fleet.env"
    return Path.home() / "FleetKit" / "runtime" / "fleet.env"


def read_env(path: Path) -> dict:
    out = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip() or line.strip().startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def log(log_file: Path | None, msg: str) -> None:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def codex_toml() -> Path:
    env = os.environ.get("CODEX_HOME")
    base = Path(env) if env else Path.home() / ".codex"
    return base / "config.toml"


def read_pinned(toml: Path) -> str | None:
    try:
        text = toml.read_text(encoding="utf-8")
    except OSError:
        return None
    m = MODEL_RE.search(text)
    return m.group(3) if m else None


def write_pinned(toml: Path, model: str, dry_run: bool) -> None:
    text = toml.read_text(encoding="utf-8")
    new_text, n = MODEL_RE.subn(lambda m: f'{m.group(1)}"{model}"', text, count=1)
    if n == 0:
        new_text = text.rstrip() + f'\nmodel = "{model}"\n'
    if dry_run:
        return
    tmp = toml.with_suffix(toml.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(toml)


def probe(base: str, model: str, timeout: float) -> tuple[bool, str]:
    """One minimal non-streaming request; 200 is the only success signal."""
    body = json.dumps({
        "model": model,
        "input": "ping",
        "stream": False,
        "max_output_tokens": 16,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/v1/responses",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer sk-local"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = ""
        return False, f"HTTP {exc.code} {detail}"
    except Exception as exc:  # network / timeout
        return False, f"{type(exc).__name__}: {exc}"


def run_once(default: str, base: str, timeout: float, dry_run: bool, log_file: Path | None) -> int:
    toml = codex_toml()
    pinned = read_pinned(toml)
    if pinned is None:
        log(log_file, f"no model= line in {toml}; pinning {default}")
        if not dry_run:
            write_pinned(toml, default, dry_run)
        return 0
    if pinned == default:
        return 0
    ok, detail = probe(base, pinned, timeout)
    if ok:
        log(log_file, f"selected model {pinned} is healthy ({detail}); leaving it pinned")
        return 0
    log(log_file, f"selected model {pinned} failed ({detail}); falling back to {default}")
    if not dry_run:
        write_pinned(toml, default, dry_run)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=float, default=120.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--default", default=None, help="override the anchor model")
    ap.add_argument("--base", default=None, help="override the proxy base url")
    args = ap.parse_args()

    env = read_env(env_file())
    default = args.default or env.get("FLEET_DEFAULT_MODEL") or DEFAULT_MODEL
    base = (args.base or env.get("FLEET_PROXY_BASE") or PROXY_BASE).rstrip("/")
    home = Path(env.get("FLEET_HOME") or (Path.home() / "FleetKit" / "runtime"))
    log_file = home / "logs" / "default-model-guard.log"

    if args.daemon:
        log(log_file, f"daemon start: anchor={default} interval={args.interval}s")
        while True:
            try:
                run_once(default, base, PROBE_TIMEOUT, args.dry_run, log_file)
            except Exception as exc:
                log(log_file, f"guard iteration failed: {type(exc).__name__}: {exc}")
            time.sleep(max(15.0, args.interval))
    return run_once(default, base, PROBE_TIMEOUT, args.dry_run, log_file)


if __name__ == "__main__":
    sys.exit(main())
