#!/usr/bin/env python3
"""FleetKit status UI - a local read-mostly web dashboard for the bridge fleet.

Stdlib only (http.server / urllib.request / plistlib / subprocess), so it adds
nothing to requirements.txt and runs on a bare macOS python3.

What the page shows
  * per bridge: launchd agent state, port listener, /v1/models probe (HTTP code,
    model count, latency, model ids) and the md5 fingerprint of its key
  * opencodex proxy status (from "ocx status") plus a /healthz probe
  * today's check-in state per task (points, last success, detail)
  * a tail of every bridge log, located from the launchd plist StandardOutPath
  * two guarded actions: "check in now" and "restart bridge"

API keys are never sent to the browser; only the first 8 hex chars of the md5
are exposed, matching tools/status.sh and tools/fleet_chat_test.py.

Usage
  status_ui.py [--home DIR] [--env-file PATH] [--host ADDR] [--port N]
               [--port-base N] [--refresh SEC] [--no-browser] [--once]

Config precedence: CLI flag > environment variable > <home>/fleet.env > default.
fleet.env may be missing (live fleets often run without it); the UI then reports
keys as "unset", probes without an Authorization header and keeps working.

Security
  Binds 127.0.0.1 unless --host says otherwise. The log endpoint only accepts
  names from the built-in bridge table, so it cannot read arbitrary files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# name | label suffix | port offset | key environment variable.
# Keep in sync with tools/status.sh, bridges/finish.sh and deploy.sh.
BRIDGES = (
    ("workbuddy", "workbuddy2codex", 0, "CODEBUDDY2OPENAI_KEY"),
    ("workbuddy-gpt", "workbuddy2codex-gpt", 1, "CODEBUDDY2OPENAI_KEY"),
    ("qoder", "qoder2codex", 2, "QODER2CODEX_KEY"),
    ("codely", "codely2codex", 3, "CODELY2CODEX_KEY"),
    ("trae", "trae2codex", 4, "TRAE2CODEX_KEY"),
    ("lingxi", "lingxi2codex", 5, "LINGXI2CODEX_KEY"),
    ("xhx", "xhx2codex", 6, "XHX2CODEX_KEY"),
    ("gemini", "gemini2codex", 7, "GEMINI2CODEX_KEY"),
    ("catpaw", "catpaw2codex", 8, "CATPAW2CODEX_KEY"),
)
BRIDGE_BY_NAME = dict((item[0], item) for item in BRIDGES)

UI_PORT_OFFSET = 9
PROBE_TIMEOUT = 2.0
DEFAULT_PORT_BASE = 8787
MAX_TAIL_LINES = 200
ACTIONS_LOCK = threading.Lock()
ACTIONS = {"checkin": None, "restart": {}, "verify": None}

OCX_TTL_SECONDS = 30.0
_OCX_CACHE = {"at": 0.0, "value": None}
_OCX_LOCK = threading.Lock()

VERDICT_RANK = {"REAL": 0, "ECHO/MIRROR": 1, "CANNED/MOCK": 2, "UNCLEAR": 3,
                "AUTH_EXPIRED": 4, "UPSTREAM_DOWN": 5, "BRIDGE_DOWN": 6, "GATE": 7}
VERDICT_KIND = {"REAL": "ok", "ECHO/MIRROR": "warn", "CANNED/MOCK": "warn",
                "UNCLEAR": "warn", "AUTH_EXPIRED": "bad", "UPSTREAM_DOWN": "bad",
                "BRIDGE_DOWN": "bad", "GATE": "bad"}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def now_str():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def md5_short(value):
    if not value:
        return None
    return hashlib.md5(value.encode("utf-8")).hexdigest()[:8]


def _short(text, limit=140):
    text = (text or "").strip().replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


def run(cmd, timeout=10.0):
    """Run a command and return (returncode, combined output). Never raises."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "command not found: %s" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "timeout after %ss: %s" % (timeout, " ".join(cmd))
    except OSError as exc:
        return 1, "%s: %s" % (type(exc).__name__, exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def http_get(url, key=None, timeout=PROBE_TIMEOUT):
    """GET url, return a dict that never raises."""
    request = urllib.request.Request(url, method="GET")
    if key:
        request.add_header("Authorization", "Bearer " + key)
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"ok": 200 <= response.status < 300, "http": response.status,
                    "ms": round((time.time() - started) * 1000),
                    "body": response.read().decode("utf-8", "replace"),
                    "error": None}
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return {"ok": False, "http": exc.code, "ms": round((time.time() - started) * 1000),
                "body": body, "error": None}
    except Exception as exc:
        return {"ok": False, "http": 0, "ms": round((time.time() - started) * 1000),
                "body": "", "error": "%s: %s" % (type(exc).__name__, exc)}


def parse_env_file(path):
    """Parse a shell-style KEY=VALUE file; strips quotes and skips comments."""
    values = {}
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in (chr(34), chr(39)):
                value = value[1:-1]
            if key:
                values[key] = value
    return values


def resolve_python(keys):
    """Prefer an interpreter that can import httpx (checkin.py needs it)."""
    candidates = []
    env_python = keys.get("FLEET_PYTHON") or os.environ.get("FLEET_PYTHON")
    if env_python:
        candidates.append(env_python)
    candidates.append(sys.executable)
    found = shutil.which("python3")
    if found:
        candidates.append(found)
    for candidate in candidates:
        if not candidate or not os.path.isfile(candidate):
            continue
        code, _ = run([candidate, "-c", "import httpx"], timeout=20.0)
        if code == 0:
            return candidate
    return candidates[-1] if candidates else "python3"


# --------------------------------------------------------------------------- #
# collectors
# --------------------------------------------------------------------------- #

def launchd_info(label):
    code, out = run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)], timeout=6.0)
    if code != 0:
        return {"loaded": False, "state": "not loaded", "pid": None, "last_exit": None}
    state = None
    match = re.search(r"^\s*state = (.+?)\s*$", out, re.MULTILINE)
    if match:
        state = match.group(1)
    pid = None
    match = re.search(r"^\s*pid = (\d+)\s*$", out, re.MULTILINE)
    if match:
        pid = int(match.group(1))
    last_exit = None
    match = re.search(r"^\s*last exit code = (-?\d+)\s*$", out, re.MULTILINE)
    if match:
        last_exit = int(match.group(1))
    return {"loaded": True, "state": state or "unknown", "pid": pid, "last_exit": last_exit}


def listen_info(port):
    code, out = run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"], timeout=6.0)
    if code != 0 or not out.strip():
        return {"ok": False, "pid": None}
    pid = None
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) > 1 and parts[1].isdigit():
            pid = int(parts[1])
            break
    return {"ok": True, "pid": pid}


def probe_bridge(port, key, timeout=PROBE_TIMEOUT):
    response = http_get("http://127.0.0.1:%d/v1/models" % port, key, timeout)
    body = response.get("body") or ""
    count = 0
    models = []
    if body:
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            count = len(payload["data"])
            for item in payload["data"]:
                if isinstance(item, dict) and item.get("id"):
                    models.append(str(item["id"]))
        if not count:
            count = body.count('"id"')
    error = response.get("error")
    if error is None and not response["ok"]:
        error = _short(body) or ("HTTP %d" % response["http"])
    return {"ok": bool(response["ok"]) and count > 0, "http": response["http"],
            "count": count, "ms": response["ms"], "models": models[:12],
            "error": error}


def plist_path(launch_dir, label):
    return os.path.join(launch_dir, label + ".plist")


def log_paths_from_plist(plist, log_dir, suffix):
    """Log candidates: whatever the launchd plist declares, then common names."""
    paths = []
    if os.path.isfile(plist):
        try:
            with open(plist, "rb") as handle:
                data = plistlib.load(handle)
            for key in ("StandardOutPath", "StandardErrorPath"):
                value = data.get(key)
                if isinstance(value, str) and value and value not in paths:
                    paths.append(value)
        except Exception:
            pass
    for candidate in (os.path.join(log_dir, suffix + ".log"),
                      os.path.join(log_dir, suffix + "-bridge.log"),
                      "/tmp/%s-bridge.log" % suffix):
        if candidate not in paths:
            paths.append(candidate)
    return paths


def tail_file(path, lines=MAX_TAIL_LINES):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 65536))
            chunk = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    all_lines = chunk.splitlines()
    if len(all_lines) > lines:
        all_lines = all_lines[-lines:]
    return {"path": path, "size": size, "lines": all_lines}


def ocx_status(ttl=OCX_TTL_SECONDS):
    # ocx status costs a couple of seconds; cache it so refreshes stay cheap.
    with _OCX_LOCK:
        cached = _OCX_CACHE["value"]
        if cached is not None and (time.time() - _OCX_CACHE["at"]) < ttl:
            return cached
    code, out = run(["ocx", "status"], timeout=25.0)
    if code == 127:
        return {"available": False, "text": "ocx not installed", "healthz": None}
    text = "\n".join(out.strip().splitlines()[:40])
    healthz = None
    match = re.search(r"(https?://(?:127\.0\.0\.1|localhost):\d+/healthz)", out)
    if match:
        url = match.group(1)
        probe = http_get(url, None, 5.0)
        healthz = {"url": url, "ok": probe["ok"], "http": probe["http"],
                   "ms": probe["ms"], "error": probe["error"]}
    result = {"available": True, "text": text, "healthz": healthz}
    with _OCX_LOCK:
        _OCX_CACHE["at"] = time.time()
        _OCX_CACHE["value"] = result
    return result


def checkin_state(homes, today):
    for home in homes:
        state_file = os.path.join(home, "state.json")
        if not os.path.isfile(state_file):
            continue
        try:
            with open(state_file, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            continue
        tasks = []
        if isinstance(raw, dict):
            for name in sorted(raw):
                item = raw[name] if isinstance(raw[name], dict) else {}
                tasks.append({"name": name, "ok_today": item.get("last_success_date") == today,
                              "at": item.get("at"), "points": item.get("available_points"),
                              "detail": str(item.get("detail") or "")})
        return {"home": home, "found": True, "tasks": tasks}
    return {"home": homes[0] if homes else None, "found": False, "tasks": []}


def collect_bridge(cfg, spec):
    name, suffix, offset, keyenv = spec
    port = cfg["port_base"] + offset
    label = cfg["label_prefix"] + "." + suffix
    key = cfg["keys"].get(keyenv) or None
    paths = log_paths_from_plist(plist_path(cfg["launch_dir"], label), cfg["log_dir"], suffix)
    log = None
    for path in paths:
        log = tail_file(path, 5)
        if log:
            break
    return {"name": name, "port": port, "label": label,
            "agent": launchd_info(label), "listen": listen_info(port),
            "probe": probe_bridge(port, key),
            "key": {"env": keyenv, "md5": md5_short(key), "set": bool(key)},
            "logs": paths, "log": log}


def collect(cfg):
    started = time.time()
    today = dt.date.today().strftime("%Y-%m-%d")
    with ThreadPoolExecutor(max_workers=len(BRIDGES) + 2) as pool:
        futures = [pool.submit(collect_bridge, cfg, spec) for spec in BRIDGES]
        ocx_future = pool.submit(ocx_status)
        bridges = [future.result() for future in futures]
        ocx = ocx_future.result()
    checkin = checkin_state(cfg["checkin_candidates"], today)
    free = free_models()
    verify = verify_snapshot(cfg)

    warnings = list(cfg["warnings"])
    for bridge in bridges:
        if not bridge["key"]["set"] and bridge["probe"]["http"] in (401, 403):
            warnings.append("%s: HTTP %d 且 key 未读取到 - 登录后执行 bash %s/bridges/finish.sh %s"
                            % (bridge["name"], bridge["probe"]["http"], cfg["home"], bridge["name"]))
        if not os.path.isfile(plist_path(cfg["launch_dir"], bridge["label"])):
            warnings.append("%s: plist 缺失 %s" % (bridge["name"], bridge["label"]))

    summary = {
        "bridges": len(bridges),
        # "not running" must not count as up: compare the whole state, not a substring.
        "agent_up": sum(1 for b in bridges
                        if b["agent"]["loaded"]
                        and (b["agent"]["state"] or "").strip().lower() == "running"),
        "listening": sum(1 for b in bridges if b["listen"]["ok"]),
        "models": sum(b["probe"]["count"] for b in bridges),
        "probe_ok": sum(1 for b in bridges if b["probe"]["ok"]),
        "checkin_ok_today": sum(1 for t in checkin["tasks"] if t["ok_today"]),
        "checkin_total": len(checkin["tasks"]),
        "verify_real": len(verify["real"]),
        "verify_at": verify["generated_at"],
    }
    return {"generated_at": now_str(), "elapsed_ms": round((time.time() - started) * 1000),
            "config": cfg["public"], "summary": summary, "warnings": warnings,
"bridges": bridges, "ocx": ocx, "checkin": checkin, "free": free, "verify": verify,
            "actions": snapshot_actions()}


FREE_TTL_SECONDS = 30.0
_FREE_LOCK = threading.Lock()
_FREE_CACHE = {"at": 0.0, "value": None}


def free_models():
    """Free-model annotations from tools/free_models.py (cached, never raises)."""
    with _FREE_LOCK:
        cached = _FREE_CACHE["value"]
        if cached is not None and (time.time() - _FREE_CACHE["at"]) < FREE_TTL_SECONDS:
            return cached
    value = {"available": False, "error": "", "counts": {}, "models": [],
             "gaps": [], "live_by_provider": {}, "picker_by_provider": {},
             "catalog_total": 0, "db_updated": "?"}
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "free_models.py")
    try:
        out = subprocess.run([sys.executable, script, "--json"],
                             capture_output=True, timeout=60)
        value = json.loads(out.stdout.decode("utf-8", "ignore"))
        value["available"] = True
    except Exception as exc:
        value["error"] = str(exc)[:160]
    with _FREE_LOCK:
        _FREE_CACHE["at"] = time.time()
        _FREE_CACHE["value"] = value
    return value


def _verify_paths(cfg):
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "verify_real_calls.py")
    if not os.path.isfile(script):
        script = os.path.join(cfg.get("home", ""), "tools", "verify_real_calls.py")
    snapshot = os.path.join(cfg.get("home", os.path.expanduser("~")), "real_calls.json")
    return script, snapshot


def verify_snapshot(cfg):
    """Last real-call verification, read from a file snapshot (never raises).

    verify_real_calls.py makes genuine (metered) upstream calls and takes a
    few minutes, so the UI reads a persisted snapshot written on demand by the
    "immediate verify" action instead of probing on every refresh.
    """
    _script, path = _verify_paths(cfg)
    value = {"available": False, "error": "", "generated_at": None,
             "by_bridge": {}, "counts": {}, "real": [], "summary": ""}
    if not os.path.isfile(path):
        value["error"] = "尚未核验：点「立即核验」生成快照"
        return value
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        value["error"] = "快照不可读: %s" % exc
        return value
    by = {}
    counts = {}
    real = []
    for item in raw.get("bridges", []):
        name = item.get("name")
        by[name] = {"verdict": item.get("verdict"), "note": item.get("note"),
                    "model": item.get("model"), "code": item.get("code"),
                    "secs": item.get("secs"), "reply": item.get("reply")}
        verdict = item.get("verdict")
        counts[verdict] = counts.get(verdict, 0) + 1
        if verdict == "REAL":
            real.append(name)
    value.update({"available": True, "generated_at": raw.get("generated_at"),
                  "by_bridge": by, "counts": counts, "real": real,
                  "summary": "  ".join("%s=%d" % (k, counts[k])
                                       for k in sorted(counts, key=lambda x: VERDICT_RANK.get(x, 9)))})
    return value


def snapshot_actions():
    with ACTIONS_LOCK:
        return json.loads(json.dumps(ACTIONS, ensure_ascii=False, default=str))


COLLECT_TTL_SECONDS = 1.5
_COLLECT_LOCK = threading.Lock()
_COLLECT_CACHE = {"at": 0.0, "value": None}


def collect_cached(cfg):
    """Coalesce concurrent /api/status polls into a single probe round."""
    with _COLLECT_LOCK:
        cached = _COLLECT_CACHE["value"]
        if cached is not None and (time.time() - _COLLECT_CACHE["at"]) < COLLECT_TTL_SECONDS:
            return cached
        result = collect(cfg)
        _COLLECT_CACHE["at"] = time.time()
        _COLLECT_CACHE["value"] = result
        return result


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

def build_config(args):
    home = args.home or os.environ.get("FLEET_HOME") or os.path.join(os.path.expanduser("~"), "fleet")
    env_file = args.env_file or os.environ.get("FLEET_ENV_FILE") or os.path.join(home, "fleet.env")
    keys = {}
    env_found = os.path.isfile(env_file)
    warnings = []
    if env_found:
        try:
            keys = parse_env_file(env_file)
        except OSError as exc:
            warnings.append("fleet.env 不可读 %s (%s)" % (env_file, exc))
    else:
        warnings.append("fleet.env 不存在: %s（桥 key 未知，探测不带 Authorization 头；"
                        "其余状态仍可查看）" % env_file)

    # config layers: CLI flag > process env > fleet.env > default
    port_base = args.port_base
    if port_base is None:
        raw_port = os.environ.get("PORT_BASE") or keys.get("PORT_BASE")
        if raw_port:
            try:
                port_base = int(raw_port)
            except ValueError:
                warnings.append("PORT_BASE 不是整数: %r" % raw_port)
        if port_base is None:
            port_base = DEFAULT_PORT_BASE
    label_prefix = (args.label_prefix or os.environ.get("LABEL_PREFIX")
                    or keys.get("LABEL_PREFIX") or "com.local")
    log_dir = (args.log_dir or os.environ.get("LOG_DIR")
               or keys.get("LOG_DIR") or "/tmp/fleet-logs")
    launch_dir = (args.launch_dir or os.environ.get("FLEET_LAUNCH_DIR")
                  or keys.get("LAUNCH_DIR")
                  or os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents"))
    ui_port = args.port
    if ui_port is None:
        ui_port = port_base + UI_PORT_OFFSET

    checkin_home = (args.checkin_home or os.environ.get("CODEX_CHECKIN_HOME")
                    or os.path.join(home, "checkin"))
    checkin_candidates = [checkin_home]
    live_fallback = os.path.join(os.path.expanduser("~"), ".codex-checkin")
    if live_fallback not in checkin_candidates:
        checkin_candidates.append(live_fallback)

    checkin_script = os.path.join(home, "tools", "checkin.py")
    if not os.path.isfile(checkin_script):
        for candidate in (os.path.join(os.path.expanduser("~"), ".codex-checkin", "checkin.py"),
                          os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkin.py")):
            if os.path.isfile(candidate):
                checkin_script = candidate
                break

    return {
        "home": home, "env_file": env_file, "env_found": env_found, "keys": keys,
        "port_base": port_base, "label_prefix": label_prefix, "log_dir": log_dir,
        "launch_dir": launch_dir, "host": args.host, "port": ui_port,
        "refresh": args.refresh, "checkin_home": checkin_home,
        "checkin_candidates": checkin_candidates, "checkin_script": checkin_script,
        "python": resolve_python(keys), "warnings": warnings,
        "public": {"home": home, "env_file": env_file, "env_found": env_found,
                   "port_base": port_base, "ui_port": ui_port,
                   "label_prefix": label_prefix, "log_dir": log_dir,
                   "launch_dir": launch_dir, "checkin_home": checkin_home,
                   "python": None, "refresh": args.refresh},
    }


# --------------------------------------------------------------------------- #
# guarded actions
# --------------------------------------------------------------------------- #

def actions_checkin(cfg, force=False):
    with ACTIONS_LOCK:
        current = ACTIONS["checkin"]
        if current and current.get("running"):
            return {"ok": False, "error": "已有一个签到任务在跑", "started_at": current.get("started_at")}
        ACTIONS["checkin"] = {"running": True, "started_at": now_str(), "force": bool(force),
                              "finished_at": None, "returncode": None, "out": []}
        worker = threading.Thread(target=_checkin_worker, args=(cfg, bool(force)), daemon=True)
        worker.start()
    return {"ok": True, "started_at": ACTIONS["checkin"]["started_at"]}


def _checkin_worker(cfg, force):
    cmd = [cfg["python"], cfg["checkin_script"], "--run-now"]
    if force:
        cmd.append("--force")
    env = dict(os.environ)
    env["CODEX_CHECKIN_HOME"] = cfg["checkin_home"]
    started = ACTIONS["checkin"]["started_at"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env)
    except OSError as exc:
        with ACTIONS_LOCK:
            ACTIONS["checkin"] = {"running": False, "started_at": started, "force": force,
                                  "finished_at": now_str(), "returncode": 127,
                                  "out": ["启动失败: %s: %s" % (type(exc).__name__, exc)]}
        return
    out = []
    for line in proc.stdout:
        out.append(line.rstrip())
    proc.wait()
    with ACTIONS_LOCK:
        ACTIONS["checkin"] = {"running": False, "started_at": started, "force": force,
                              "finished_at": now_str(), "returncode": proc.returncode,
                              "out": out[-80:]}


def actions_verify(cfg):
    with ACTIONS_LOCK:
        current = ACTIONS["verify"]
        if current and current.get("running"):
            return {"ok": False, "error": "已有一个核验任务在跑",
                    "started_at": current.get("started_at")}
        ACTIONS["verify"] = {"running": True, "started_at": now_str(),
                            "finished_at": None, "returncode": None, "out": []}
        worker = threading.Thread(target=_verify_worker, args=(cfg,), daemon=True)
        worker.start()
    return {"ok": True, "started_at": ACTIONS["verify"]["started_at"]}


def _verify_worker(cfg):
    script, snapshot = _verify_paths(cfg)
    started = ACTIONS["verify"]["started_at"]
    if not os.path.isfile(script):
        with ACTIONS_LOCK:
            ACTIONS["verify"] = {"running": False, "started_at": started,
                                 "finished_at": now_str(), "returncode": 127,
                                 "out": ["verify_real_calls.py 不存在: %s" % script]}
        return
    cmd = [sys.executable, script, "--json", "--port-base", str(cfg["port_base"])]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
    except OSError as exc:
        with ACTIONS_LOCK:
            ACTIONS["verify"] = {"running": False, "started_at": started,
                                 "finished_at": now_str(), "returncode": 127,
                                 "out": ["启动失败: %s: %s" % (type(exc).__name__, exc)]}
        return
    lines = [line for line in proc.stdout]
    proc.wait()
    text = "".join(lines)
    out = []
    try:
        data = json.loads(text.strip())
        tmp = snapshot + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(tmp, snapshot)
        real = [b.get("name") for b in data.get("bridges", []) if b.get("verdict") == "REAL"]
        for b in data.get("bridges", []):
            out.append("%-14s %-26s %-5s %-13s %s" % (
                b.get("name"), str(b.get("model") or "-")[:26], str(b.get("code")),
                b.get("verdict"), str(b.get("note") or "")[:38]))
        out.append("真实可用: %s" % (", ".join(real) if real else "(无)"))
    except ValueError:
        out.append("解析核验输出失败（原始输出末尾）：")
        out.append(text.strip()[-600:])
    with ACTIONS_LOCK:
        ACTIONS["verify"] = {"running": False, "started_at": started,
                             "finished_at": now_str(), "returncode": proc.returncode,
                             "out": out[-60:]}


def actions_restart(cfg, name):
    spec = BRIDGE_BY_NAME.get(name)
    if spec is None:
        return {"ok": False, "error": "unknown bridge: %s" % name}
    label = cfg["label_prefix"] + "." + spec[1]
    target = "gui/%d/%s" % (os.getuid(), label)
    code, out = run(["launchctl", "kickstart", "-k", target], timeout=25.0)
    record = {"name": name, "label": label, "at": now_str(), "returncode": code,
              "ok": code == 0, "out": _short(out, 400)}
    with ACTIONS_LOCK:
        ACTIONS["restart"][name] = record
        recent = list(ACTIONS["restart"].items())[-6:]
        ACTIONS["restart"] = dict(recent)
    return record


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "FleetKit-ui/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[ui] %s\n" % (fmt % args))

    def _send(self, payload, content_type, code=200):
        if isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        if route in ("/", "/index.html"):
            self._send(render_page(self.server.cfg), "text/html; charset=utf-8")
        elif route == "/api/status":
            self._send(collect_cached(self.server.cfg), "application/json; charset=utf-8")
        elif route.startswith("/api/logs/"):
            name = route[len("/api/logs/"):]
            query = urllib.parse.parse_qs(parsed.query)
            self._send(self.server.log_payload(name, query), "application/json; charset=utf-8")
        elif route == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif route.startswith("/api/"):
            self._send({"ok": False, "error": "not found: %s" % route},
                       "application/json; charset=utf-8", 404)
        else:
            # stray URLs (IME typos, trailing punctuation) self-heal to the dashboard
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if route == "/api/action/checkin":
            force = (query.get("force", ["0"])[0] or "0").lower() in ("1", "true", "yes")
            self._send(actions_checkin(self.server.cfg, force), "application/json; charset=utf-8")
        elif route == "/api/action/verify-real-calls":
            self._send(actions_verify(self.server.cfg), "application/json; charset=utf-8")
        elif route.startswith("/api/action/restart/"):
            name = route[len("/api/action/restart/"):]
            self._send(actions_restart(self.server.cfg, name), "application/json; charset=utf-8")
        else:
            self._send({"ok": False, "error": "not found: %s" % route},
                       "application/json; charset=utf-8", 404)


class FleetUIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, cfg):
        self.cfg = cfg
        super().__init__(address, Handler)

    def log_payload(self, name, query):
        spec = BRIDGE_BY_NAME.get(name)
        if spec is None:
            return {"ok": False, "error": "unknown bridge: %s" % name}
        try:
            lines = int(query.get("lines", [str(MAX_TAIL_LINES)])[0])
        except (TypeError, ValueError):
            lines = MAX_TAIL_LINES
        lines = max(10, min(lines, 2000))
        label = self.cfg["label_prefix"] + "." + spec[1]
        candidates = log_paths_from_plist(plist_path(self.cfg["launch_dir"], label),
                                         self.cfg["log_dir"], spec[1])
        for path in candidates:
            found = tail_file(path, lines)
            if found:
                return {"ok": True, "name": name, "path": found["path"],
                        "size": found["size"], "lines": found["lines"],
                        "candidates": candidates}
        return {"ok": False, "name": name, "error": "no log file found",
                "candidates": candidates}


# --------------------------------------------------------------------------- #
# page
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FleetKit 状态面板</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#e6e9ef;--dim:#8b93a3;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--accent:#58a6ff;--chip:#1f2630}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Mono",Menlo,Consolas,monospace}
header{padding:12px 20px;border-bottom:1px solid var(--line);background:var(--panel);
position:sticky;top:0;z-index:5}
h1{margin:0;font-size:16px;font-weight:600}
.meta{color:var(--dim);font-size:12px;word-break:break-all}
main{padding:16px 20px;max-width:1500px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.card .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.card .v{font-size:22px;font-weight:600;margin-top:2px}
.card .s{color:var(--dim);font-size:11px}
table{width:100%;border-collapse:collapse;background:var(--panel);
border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.05em;background:#14171d}
tr:last-child td{border-bottom:none}
.pill{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;font-weight:600}
.p-ok{background:rgba(63,185,80,.15);color:var(--ok)}
.p-warn{background:rgba(210,153,34,.15);color:var(--warn)}
.p-bad{background:rgba(248,81,73,.15);color:var(--bad)}
.p-idle{background:#22262f;color:var(--dim)}
.chips{display:flex;flex-wrap:wrap;gap:4px;max-width:330px}
.chip{background:var(--chip);border:1px solid var(--line);border-radius:4px;
padding:0 5px;font-size:11px;color:#c8cfdb}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}
@media(max-width:980px){.grid2{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;margin-top:12px}
.panel h2{margin:0 0 8px;font-size:13px;color:var(--dim);
text-transform:uppercase;letter-spacing:.06em;font-weight:600}
pre{margin:0;white-space:pre-wrap;word-break:break-all;font-size:12px;color:#c8cfdb;
max-height:280px;overflow:auto}
button{background:#22262f;color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:5px 11px;font:inherit;font-size:12px;cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
button.primary{background:rgba(88,166,255,.14);border-color:rgba(88,166,255,.4);color:var(--accent)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
select{background:#14171d;color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:5px 8px;font:inherit;font-size:12px}
.warnbox{background:rgba(210,153,34,.12);border:1px solid rgba(210,153,34,.35);
color:#e3b341;border-radius:6px;padding:8px 10px;margin-bottom:12px;font-size:12px}
.dim{color:var(--dim)}
</style>
</head>
<body>
<header>
  <h1>FleetKit 状态面板</h1>
  <div class="meta" id="meta">loading...</div>
</header>
<main>
  <div id="warn"></div>
  <div class="cards" id="cards"></div>
  <table>
    <thead><tr>
      <th>桥</th><th>端口</th><th>key md5</th><th>launchd</th><th>监听</th>
      <th>/v1/models</th><th>模型</th><th>真实调用</th><th></th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="grid2">
    <div class="panel">
      <h2>opencodex</h2>
      <div class="row"><span id="ocx-pill" class="pill p-idle">...</span>
      <span class="meta" id="ocx-health"></span></div>
      <pre id="ocx-text"></pre>
    </div>
    <div class="panel">
      <h2>签到</h2>
      <div class="row">
        <button class="primary" onclick="doCheckin(false)">立即签到</button>
        <button onclick="doCheckin(true)">强制 --force</button>
        <span class="meta" id="ck-home"></span>
      </div>
      <table><thead><tr><th>任务</th><th>今日</th><th>积分</th><th>上次</th><th>明细</th></tr></thead>
      <tbody id="ck-rows"></tbody></table>
      <pre id="ck-out" style="margin-top:8px"></pre>
    </div>
  </div>
  <div class="panel">
    <h2>真实调用核验（随机运算题抗伪造，真计费 · 一轮约 3 分钟）</h2>
    <div class="row">
      <button class="primary" onclick="doVerify()">立即核验</button>
      <span class="meta" id="vf-at"></span>
      <span class="meta" id="vf-meta"></span>
    </div>
    <table><thead><tr><th>桥</th><th>模型</th><th>HTTP</th><th>判定</th><th>说明</th></tr></thead>
    <tbody id="vf-rows"></tbody></table>
    <pre id="vf-out" style="margin-top:8px"></pre>
  </div>
  <div class="panel">
    <h2>免费模型标注（官网信息，更新于 <span id="free-updated">?</span>）</h2>
    <div class="row"><span class="meta" id="free-meta"></span></div>
    <table><thead><tr><th>模型（选择器名）</th><th>免费</th><th>时段 / 说明</th><th>在选择器</th></tr></thead>
    <tbody id="free-rows"></tbody></table>
    <div class="row" style="margin-top:8px"><span class="meta" id="free-gaps"></span></div>
  </div>
  <div class="panel">
    <h2>操作输出</h2>
    <pre id="act-out"></pre>
  </div>
  <div class="panel">
    <h2>日志</h2>
    <div class="row">
      <select id="logpick" onchange="loadLog()"></select>
      <button onclick="loadLog()">刷新</button>
      <span class="meta" id="log-path"></span>
    </div>
    <pre id="log-out"></pre>
  </div>
</main>
<script>
var REFRESH = __REFRESH__;
var SNAP = null;
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function pill(kind,text){return '<span class="pill p-'+kind+'">'+esc(text)+'</span>';}
function agentCell(a){
  if(!a.loaded){return pill('bad','not loaded');}
  var st=(a.state||'').toLowerCase();
  if(st.indexOf('run')>=0){return pill('ok','running')+' <span class="dim">pid '+esc(a.pid)+'</span>';}
  var tail=(a.state||'loaded')+(a.last_exit==null?'':' · exit '+a.last_exit);
  return pill('warn',tail);
}
function probeCell(p){
  if(p.http===0){return pill('bad','unreachable')+' <span class="dim">'+esc(p.error)+'</span>';}
  if(p.ok){return pill('ok','HTTP '+p.http)+' <span class="dim">'+p.count+' models · '+p.ms+'ms</span>';}
  return pill('warn','HTTP '+p.http)+' <span class="dim">'+esc(p.error||('count '+p.count))+'</span>';
}
function render(){
  var s=SNAP; if(!s){return;}
  document.getElementById('meta').textContent =
    '生成于 '+s.generated_at+' (耗时 '+s.elapsed_ms+'ms) · home '+s.config.home+
    ' · 端口基线 '+s.config.port_base+' · 面板 '+s.config.ui_port+
    ' · fleet.env '+(s.config.env_found?'ok':'MISSING')+
    ' · 自动刷新 '+(REFRESH>0?REFRESH+'s':'关');
  document.getElementById('warn').innerHTML = s.warnings.length
    ? '<div class="warnbox">'+s.warnings.map(esc).join('<br>')+'</div>' : '';
  var sum=s.summary;
  var cards=[['桥在线',sum.agent_up+' / '+sum.bridges,'launchd loaded 且在跑'],
             ['端口监听',sum.listening+' / '+sum.bridges,'127.0.0.1 LISTEN'],
             ['模型总数',sum.models,'/v1/models 汇总'],
             ['今日签到',sum.checkin_ok_today+' / '+sum.checkin_total,'tasks ok today'],
             ['真实调用',sum.verify_real+' / '+sum.bridges,'上次 '+(sum.verify_at||'未核验')]];
  document.getElementById('cards').innerHTML = cards.map(function(c){
    return '<div class="card"><div class="k">'+esc(c[0])+'</div><div class="v">'+esc(c[1])+
           '</div><div class="s">'+esc(c[2])+'</div></div>';}).join('');
  document.getElementById('rows').innerHTML = s.bridges.map(function(b){
    var chips=(b.probe.models||[]).map(function(m){
      return '<span class="chip">'+esc(m)+'</span>';}).join('');
    return '<tr><td><b>'+esc(b.name)+'</b></td><td>'+b.port+'</td>'+
      '<td>'+(b.key.md5?pill('ok',b.key.md5):pill('idle','unset'))+
      ' <span class="dim">'+esc(b.key.env)+'</span></td>'+
      '<td>'+agentCell(b.agent)+'</td>'+
      '<td>'+(b.listen.ok?pill('ok','yes')+' <span class="dim">pid '+esc(b.listen.pid)+'</span>'
                     :pill('bad','no'))+'</td>'+
      '<td>'+probeCell(b.probe)+'</td>'+
      '<td><div class="chips">'+chips+'</div></td>'+
      '<td>'+verifyCell(b.name)+'</td>'+
      '<td><button onclick="restart('+esc(b.name)+')">重启</button></td></tr>';}).join('');
  var o=s.ocx, p=document.getElementById('ocx-pill');
  if(!o.available){p.className='pill p-bad';p.textContent='ocx 未安装';}
  else if(o.healthz&&o.healthz.ok){p.className='pill p-ok';p.textContent='proxy ok '+o.healthz.ms+'ms';}
  else{p.className='pill p-warn';p.textContent='proxy 状态未知';}
  document.getElementById('ocx-health').textContent=o.healthz?o.healthz.url:'';
  document.getElementById('ocx-text').textContent=o.text||'';
  var ck=s.checkin;
  document.getElementById('ck-home').textContent = ck.found
    ? ck.home : ('未找到 state.json（'+ck.home+'）');
  document.getElementById('ck-rows').innerHTML = ck.tasks.length
    ? ck.tasks.map(function(t){
        return '<tr><td><b>'+esc(t.name)+'</b></td>'+
          (t.ok_today?pill('ok','今天'):pill('warn','未签'))+'<td>'+esc(t.points)+'</td>'+
          '<td class="dim">'+esc(t.at)+'</td><td class="dim">'+esc(t.detail)+'</td></tr>';}).join('')
    : '<tr><td colspan="5" class="dim">暂无记录</td></tr>';
  var chk=s.actions.checkin;
  document.getElementById('ck-out').textContent = chk
    ? ((chk.running?'[running] ':'')+'checkin @ '+chk.started_at+' force='+chk.force+
       (chk.returncode==null?'':' rc='+chk.returncode)+'\n'+(chk.out||[]).join('\n'))
    : '';
  var names=Object.keys(s.actions.restart||{});
  document.getElementById('act-out').textContent = names.length
    ? names.map(function(n){var r=s.actions.restart[n];
        return r.at+'  '+r.name+'  '+(r.ok?'ok':'rc='+r.returncode)+'  '+r.out;}).join('\n')
    : '（暂无）';
  var pick=document.getElementById('logpick');
  var cur=pick.value;
  pick.innerHTML=s.bridges.map(function(b){
    return '<option value="'+esc(b.name)+'">'+esc(b.name)+' · '+b.port+'</option>';}).join('');
  if(cur && s.bridges.some(function(b){return b.name===cur;})){pick.value=cur;}
  else if(!pick.value && s.bridges.length){pick.value=s.bridges[0].name;}
  renderFree();
  renderVerify();
}
function freeKind(f){
  if(f==='free'||f==='free-window'||f==='quota'||f==='trial'){return 'ok';}
  if(f==='blocked'){return 'bad';}
  if(f==='unknown'){return 'warn';}
  return 'idle';
}
function renderFree(){
  var f=SNAP.free;
  var meta=document.getElementById('free-meta');
  var rows=document.getElementById('free-rows');
  if(!f||!f.available){
    meta.textContent='free_models.py 不可用: '+((f&&f.error)||'missing');
    rows.innerHTML='';return;}
  document.getElementById('free-updated').textContent=f.db_updated||'?';
  var live=0;for(var k in (f.live_by_provider||{})){live+=f.live_by_provider[k];}
  var pick=0;for(var k2 in (f.picker_by_provider||{})){pick+=f.picker_by_provider[k2];}
  var cnt=[];for(var c in (f.counts||{})){cnt.push(f.counts[c]+' '+c);}
  meta.textContent='live '+live+' · 在选择器 '+pick+' · catalog '+f.catalog_total+' · '+cnt.join(' · ');
  var list=(f.models||[]).filter(function(m){
    return m.in_picker||m.free==='free'||m.free==='free-window'||m.free==='quota'||m.free==='trial';});
  rows.innerHTML=list.map(function(m){
    return '<tr><td>'+esc(m.picker_name||m.picker_slug||m.model)+'</td>'+
      '<td>'+pill(freeKind(m.free),m.badge)+'</td>'+
      '<td class="dim">'+esc(m.window)+'</td>'+
      '<td>'+(m.in_picker?pill('ok','yes'):pill('bad','no'))+'</td></tr>';}).join('');
  document.getElementById('free-gaps').textContent=(f.gaps||[]).map(function(g){
    return '['+g.provider+'] '+g.reason;}).join('   |   ');
}
var VF_KIND={REAL:'ok','ECHO/MIRROR':'warn','CANNED/MOCK':'warn',UNCLEAR:'warn',
  AUTH_EXPIRED:'bad',UPSTREAM_DOWN:'bad',BRIDGE_DOWN:'bad',GATE:'bad'};
function vfKind(v){return VF_KIND[v]||'idle';}
function verifyCell(name){
  var v=(SNAP&&SNAP.verify&&SNAP.verify.by_bridge)||{};
  var r=v[name];
  if(!r||!r.verdict)return pill('idle','未核验');
  return pill(vfKind(r.verdict),r.verdict);
}
function renderVerify(){
  var v=SNAP.verify||{};
  var meta=document.getElementById('vf-meta');
  var rows=document.getElementById('vf-rows');
  if(!v.available){
    meta.textContent=v.error||'不可用';
    rows.innerHTML='';
    document.getElementById('vf-at').textContent='';
  }else{
    document.getElementById('vf-at').textContent=v.generated_at?('上次核验 '+v.generated_at):'';
    meta.textContent=(v.real.length?('REAL '+v.real.length+' 座 · '):'')+(v.summary||'');
    var names=Object.keys(v.by_bridge).sort();
    rows.innerHTML=names.length?names.map(function(n){
      var r=v.by_bridge[n];
      return '<tr><td><b>'+esc(n)+'</b></td>'
        +'<td>'+esc(r.model||'?')+'</td>'
        +'<td>'+(r.code?esc(r.code):'<span class="dim">-</span>')+'</td>'
        +'<td>'+pill(vfKind(r.verdict),r.verdict)+'</td>'
        +'<td class="dim">'+esc(r.note||'')+'</td></tr>';}).join('')
      : '<tr><td colspan="5" class="dim">暂无记录</td></tr>';
  }
  var a=SNAP.actions.verify;
  document.getElementById('vf-out').textContent=a
    ? ((a.running?'[running] ':'')+'verify @ '+a.started_at
       +(a.returncode==null?'':' rc='+a.returncode)+'\n'+(a.out||[]).join('\n'))
    : '';
}
function doVerify(){post('/api/action/verify-real-calls').then(function(){load();});}
function load(){
  fetch('/api/status').then(function(r){return r.json();}).then(function(j){
    SNAP=j; render(); loadLog();}).catch(function(e){
      document.getElementById('meta').textContent='刷新失败: '+e;});
}
function loadLog(){
  var name=document.getElementById('logpick').value;
  if(!name){return;}
  fetch('/api/logs/'+encodeURIComponent(name)+'?lines=200').then(function(r){return r.json();})
    .then(function(j){
      document.getElementById('log-path').textContent = j.ok ? (j.path+' ('+j.size+' bytes)') : '';
      document.getElementById('log-out').textContent = j.ok ? j.lines.join('\n') : (j.error||'no log');
    }).catch(function(e){document.getElementById('log-out').textContent=''+e;});
}
function post(url){return fetch(url,{method:'POST'}).then(function(r){return r.json();});}
function doCheckin(force){post('/api/action/checkin'+(force?'?force=1':'')).then(function(){load();});}
function restart(name){
  if(!window.confirm('重启桥 '+name+' ?')){return;}
  post('/api/action/restart/'+encodeURIComponent(name)).then(function(){load();});}
if(REFRESH>0){setInterval(load,REFRESH*1000);}
load();
</script>
</body>
</html>
"""


def render_page(cfg):
    return PAGE.replace("__REFRESH__", str(int(cfg["refresh"])))


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="status_ui.py",
        description="FleetKit local status dashboard (stdlib only, zero new deps)")
    parser.add_argument("--home", help="fleet root (default ~/FleetKit/runtime or $FLEET_HOME)")
    parser.add_argument("--env-file", help="fleet.env path (default <home>/fleet.env)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, help="UI port (default PORT_BASE + %d)" % UI_PORT_OFFSET)
    parser.add_argument("--port-base", type=int, help="first bridge port (default 8787)")
    parser.add_argument("--label-prefix", help="launchd label prefix (default com.local)")
    parser.add_argument("--launch-dir", help="plist directory (default ~/Library/LaunchAgents)")
    parser.add_argument("--log-dir", help="bridge log directory (default /tmp/fleet-logs)")
    parser.add_argument("--checkin-home", help="check-in state dir (default <home>/checkin)")
    parser.add_argument("--refresh", type=int, default=10, help="auto refresh seconds (0 = off)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--once", action="store_true", help="print one JSON snapshot and exit")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    cfg = build_config(args)
    if args.once:
        print(json.dumps(collect(cfg), ensure_ascii=False, indent=2))
        return 0
    if cfg["port_base"] <= cfg["port"] < cfg["port_base"] + len(BRIDGES):
        sys.stderr.write("refusing to bind %d: it collides with bridge ports %d..%d\n"
                         % (cfg["port"], cfg["port_base"], cfg["port_base"] + len(BRIDGES) - 1))
        return 2
    try:
        server = FleetUIServer((cfg["host"], cfg["port"]), cfg)
    except OSError as exc:
        sys.stderr.write("cannot bind %s:%d (%s)\n" % (cfg["host"], cfg["port"], exc))
        return 1
    url = "http://%s:%d/" % (cfg["host"], cfg["port"])
    print("FleetKit status UI  ->  %s" % url)
    print("  home      %s" % cfg["home"])
    print("  fleet.env %s" % (cfg["env_file"] + (" (found)" if cfg["env_found"] else " (MISSING)")))
    print("  bridges   %d..%d  label prefix %s" % (cfg["port_base"], cfg["port_base"] + 8, cfg["label_prefix"]))
    print("  python    %s (checkin.py)" % cfg["python"])
    if cfg["host"] not in ("127.0.0.1", "localhost", "::1"):
        print("  WARNING  bound to %s - your fleet status is exposed to the network" % cfg["host"])
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
