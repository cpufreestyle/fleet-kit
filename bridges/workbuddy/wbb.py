#!/usr/bin/env python3
"""wbb - cross-platform management command for the WorkBuddy2Codex Bridge.

Works on any machine with Python 3.10+:
  wbb install                create .venv, install deps, write user config
  wbb start                  start the bridge (detached, hidden on Windows)
  wbb stop                   stop the bridge
  wbb status                 show running status
  wbb restart                restart the bridge

The bridge location is resolved from the user config file (~/.wbb/config.json),
written by `wbb install`. Nothing here is hard-coded to a specific machine.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


CONFIG_DIR = Path.home() / ".wbb"
CONFIG_PATH = CONFIG_DIR / "config.json"
BIN_DIR = CONFIG_DIR / "bin"
PORT = 8787
HEALTH_PATH = "/health"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        # npm-installed scenario: the package dir itself is the bridge.
        # Fall back to this file's directory so `wbb start` works right away.
        return {
            "bridge_dir": str(Path(__file__).resolve().parent),
            "port": PORT,
        }
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log(f"[ERROR] cannot read config {CONFIG_PATH}: {exc}")
        sys.exit(1)


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def install_shim(bridge_dir: Path) -> Path:
    """Generate a self-contained shim in ~/.wbb/bin with the venv python baked in.

    The generated command points directly at this repo's venv interpreter, so it
    works on any machine regardless of whether `python` is on PATH.
    """
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    py = venv_python(bridge_dir)
    if not py.is_file():
        log(f"[ERROR] venv python not found: {py}")
        sys.exit(1)
    if is_windows():
        dst = BIN_DIR / "wbb.cmd"
        content = (
            "@echo off\r\n"
            f"\"{py}\" \"{bridge_dir / 'wbb.py'}\" %*\r\n"
            "exit /b %errorlevel%\r\n"
        )
    else:
        dst = BIN_DIR / "wbb"
        content = (
            "#!/usr/bin/env sh\n"
            f"exec \"{py}\" \"{bridge_dir / 'wbb.py'}\" \"$@\"\n"
        )
    dst.write_text(content, encoding="utf-8")
    if not is_windows():
        dst.chmod(0o755)
    log(f"[OK] command installed: {dst}")
    return dst


def ensure_in_user_path(bin_dir: Path) -> None:
    """Add bin_dir to the user PATH (Windows user env / POSIX shell profile)."""
    if is_windows():
        # Use PowerShell's user-level environment API (handles REG_EXPAND_SZ and
        # broadcasts WM_SETTINGCHANGE itself). Best-effort: a policy/AV block must
        # not fail the whole install.
        ps = (
            "try { "
            f"$p=[Environment]::GetEnvironmentVariable('Path','User'); "
            f"if ($p -notlike '*{bin_dir}*') {{ "
            f"[Environment]::SetEnvironmentVariable('Path', ($p.TrimEnd(';')+';{bin_dir}'), 'User') "
            "} } catch { exit 1 }"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, timeout=30,
            )
        except Exception:
            pass
    else:
        # POSIX: append export to ~/.profile / ~/.zshrc if not present
        line = f'export PATH="{bin_dir}:$PATH"'
        for rc in (Path.home() / ".profile", Path.home() / ".zshrc"):
            if not rc.is_file():
                continue
            text = rc.read_text(encoding="utf-8", errors="replace")
            if line not in text:
                with rc.open("a", encoding="utf-8") as fh:
                    fh.write("\n# wbb\n" + line + "\n")


def remove_from_user_path(bin_dir: Path) -> None:
    """Remove bin_dir from the user PATH and any shell profile line we added."""
    if is_windows():
        ps = (
            "try { "
            f"$p=[Environment]::GetEnvironmentVariable('Path','User'); "
            f"if ($p) {{ "
            f"$parts=$p.Split(';') | Where-Object {{ $_ -and $_ -ne '{bin_dir}' }}; "
            "[Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'User') "
            "} } catch { exit 1 }"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, timeout=30,
            )
        except Exception:
            pass
    else:
        line = f'export PATH="{bin_dir}:$PATH"'
        for rc in (Path.home() / ".profile", Path.home() / ".zshrc"):
            if not rc.is_file():
                continue
            try:
                text = rc.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if line in text:
                text = text.replace(f"\n# wbb\n{line}", "").replace(line + "\n", "")
                try:
                    rc.write_text(text, encoding="utf-8")
                except OSError:
                    pass


def is_windows() -> bool:
    return sys.platform == "win32"


def venv_python(bridge_dir: Path) -> Path:
    if is_windows():
        return bridge_dir / ".venv" / "Scripts" / "python.exe"
    return bridge_dir / ".venv" / "bin" / "python"


def bridge_dir_from_args(cfg: dict, args) -> Path:
    raw = getattr(args, "bridge_dir", None) or cfg.get("bridge_dir")
    if not raw:
        log("[ERROR] bridge dir is not configured; run 'wbb install'.")
        sys.exit(1)
    return Path(raw).expanduser().resolve()


def port_from_config(cfg: dict) -> int:
    try:
        return int(cfg.get("port") or PORT)
    except (TypeError, ValueError):
        return PORT


def http_ok(url: str, timeout: float = 3.0) -> bool:
    """Lightweight health probe without third-party deps."""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def find_listener_pid(port: int) -> int | None:
    """Return the PID listening on port, or None. Windows + POSIX."""
    if is_windows():
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except Exception:
            return None
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].startswith("TCP"):
                local = parts[1]
                state = parts[3] if len(parts) > 3 else ""
                if state.upper() == "LISTENING" and local.endswith(f":{port}"):
                    try:
                        return int(parts[4])
                    except ValueError:
                        return None
        return None
    # POSIX: lsof or ss
    for cmd in (
        ["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
        ["ss", "-tlnp", f"sport = :{port}"],
    ):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except Exception:
            continue
        if out.strip():
            # lsof prints PIDs on their own lines; ss needs pid= parsing
            for token in out.replace("pid=", " ").split():
                if token.isdigit():
                    return int(token)
    return None


def process_commandline(pid: int) -> str:
    """Return the command line of a process (best effort), or ''."""
    try:
        if is_windows():
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=15,
            ).stdout.strip()
            return out
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except Exception:
        return ""


def bridge_pid(port: int) -> int | None:
    """PID listening on port, only if it looks like our bridge."""
    pid = find_listener_pid(port)
    if pid is None:
        return None
    cli = process_commandline(pid).lower()
    if "converter.py" in cli or "workbuddy2codex" in cli:
        return pid
    return None


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_install(args) -> int:
    bridge_dir = Path(args.bridge_dir).expanduser().resolve()
    if not (bridge_dir / "converter.py").is_file():
        log(f"[ERROR] converter.py not found under {bridge_dir}")
        log("        pass the directory that contains converter.py via --bridge-dir.")
        return 1

    py = venv_python(bridge_dir)
    if not py.is_file():
        log(f"[..] creating virtualenv at {py.parent}")
        subprocess.run([sys.executable, "-m", "venv", str(bridge_dir / ".venv")], check=True)

    log("[..] upgrading pip")
    subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip"], check=True)

    deps = [
        "fastapi",
        "uvicorn[standard]",
        "httpx",
        "python-multipart",
        "opencv-python-headless",
        "numpy",
    ]
    log("[..] installing dependencies")
    subprocess.run([str(py), "-m", "pip", "install", *deps], check=True)

    port = args.port or PORT
    cfg = {"bridge_dir": str(bridge_dir), "port": port}
    save_config(cfg)

    shim = install_shim(bridge_dir)
    ensure_in_user_path(BIN_DIR)

    log(f"[OK] installed. Config written to {CONFIG_PATH}")
    log(f"     bridge dir : {bridge_dir}")
    log(f"     port       : {port}")
    log(f"     command    : {shim}")
    log("")
    log("  Next: open a NEW terminal so PATH picks up the command, then run:")
    log("        wbb start")
    return 0


def cmd_start(args) -> int:
    cfg = load_config()
    bridge_dir = bridge_dir_from_args(cfg, args)
    port = port_from_config(cfg)
    if bridge_pid(port):
        log(f"[INFO] Bridge already running on port {port}")
        return 0

    py = venv_python(bridge_dir)
    if not py.is_file():
        log(f"[ERROR] venv python not found: {py}")
        log("        run 'wbb install' first.")
        return 1

    logdir = bridge_dir / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / "bridge.log"

    cmd = [
        str(py),
        str(bridge_dir / "converter.py"),
        "--host", "127.0.0.1",
        "--port", str(port),
    ]
    if is_windows():
        kwargs = {
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
            "stdout": open(logfile, "ab"),
            "stderr": subprocess.STDOUT,
        }
    else:
        kwargs = {
            "start_new_session": True,
            "stdout": open(logfile, "ab"),
            "stderr": subprocess.STDOUT,
        }

    log(f"[..] starting Bridge (log: {logfile})")
    subprocess.Popen(cmd, cwd=str(bridge_dir), **kwargs)

    deadline = time.time() + 30
    while time.time() < deadline:
        if bridge_pid(port):
            log(f"[OK] Bridge started - http://127.0.0.1:{port}/")
            return 0
        time.sleep(2)

    log(f"[WARN] port {port} not listening after 30s, check log: {logfile}")
    return 1


def cmd_stop(args) -> int:
    cfg = load_config()
    port = port_from_config(cfg)
    pid = bridge_pid(port)
    if pid is None:
        log("[INFO] Bridge not running, nothing to stop")
        return 0
    log(f"[..] stopping Bridge PID {pid}")
    try:
        if is_windows():
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=15)
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception as exc:
        log(f"[WARN] stop failed: {exc}")
        return 1
    time.sleep(1)
    if bridge_pid(port):
        log("[WARN] process still running")
        return 1
    log("[OK] Bridge stopped")
    return 0


def cmd_status(args) -> int:
    cfg = load_config()
    port = port_from_config(cfg)
    pid = bridge_pid(port)
    if pid:
        log(f"[OK] Bridge is running - http://127.0.0.1:{port}/ - PID {pid}")
    else:
        log(f"[OFF] Bridge is not running - nothing listens on port {port}")
    return 0


def cmd_restart(args) -> int:
    code = cmd_stop(args)
    time.sleep(2)
    if code != 0:
        return code
    return cmd_start(args)


def cmd_uninstall(args) -> int:
    code = cmd_stop(args)
    remove_from_user_path(BIN_DIR)
    # Delay the directory removal so the currently-running shim (inside
    # ~/.wbb/bin) can exit cleanly before its own file disappears.
    try:
        if is_windows():
            subprocess.Popen(
                ["cmd", "/c", "timeout", "/t", "2", "/nobreak", ">nul",
                 "&", "rmdir", "/s", "/q", str(CONFIG_DIR)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            subprocess.Popen(
                ["sh", "-c", f"sleep 2; rm -rf -- '{CONFIG_DIR}'"],
                start_new_session=True,
            )
        log(f"[OK] scheduled removal of {CONFIG_DIR} (in 2s)")
    except Exception as exc:
        log(f"[WARN] could not schedule removal of {CONFIG_DIR}: {exc}")
    log("[OK] removed wbb from user PATH / shell profile")
    log("      new terminals will no longer see the 'wbb' command")
    if code != 0:
        return code
    return 0


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="wbb",
        description="WorkBuddy2Codex Bridge management command (cross-platform).",
    )
    sub = parser.add_subparsers(dest="command")

    p_install = sub.add_parser("install", help="create venv, install deps, write config")
    p_install.add_argument("--bridge-dir", required=True,
                           help="absolute path containing converter.py")
    p_install.add_argument("--port", type=int, default=PORT)

    for name in ("start", "stop", "status", "restart", "uninstall"):
        sub.add_parser(name)

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return 0

    handlers = {
        "install": cmd_install,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "restart": cmd_restart,
        "uninstall": cmd_uninstall,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
