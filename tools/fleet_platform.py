"""FleetKit platform abstraction (python side).

Cross-platform answers to the questions every tool asks:

* where do services live?   -> service_dir()
* which port is a bridge on?-> service_port()   (launchd plist / .cmd wrapper / fleet.env)
* which key does it use?    -> service_keys()   (launchd plist / .cmd wrapper / fleet.env)
* is it running?            -> service_status() (launchctl / schtasks / pgrep)

macOS keeps reading the plists it always read; Windows and Linux read the
wrapper files install.sh writes, so no code path depends on ~/Library.
"""
import glob
import os
import re
import subprocess

PORT_BASE = 8787
PORT_OFFSETS = {
    "workbuddy": 0, "workbuddy-gpt": 1, "qoder": 2, "codely": 3, "trae": 4,
    "lingxi": 5, "xhx": 6, "gemini": 7, "catpaw": 8, "antigravity": 10,
    "qwen": 11, "cline": 12, "zcode": 13,
    "kimi-code": 15, "minimax": 16,
}
# label suffixes as installed: com.local.<name>2codex
LABEL_SUFFIX = {
    "workbuddy": "workbuddy2codex", "workbuddy-gpt": "workbuddy2codex-gpt",
    "qoder": "qoder2codex", "codely": "codely2codex", "trae": "trae2codex",
    "lingxi": "lingxi2codex", "xhx": "xhx2codex", "gemini": "gemini2codex",
    "catpaw": "catpaw2codex", "antigravity": "antigravity2codex",
    "qwen": "qwen2codex", "cline": "cline2codex", "zcode": "zcode2codex",
    "kimi-code": "kimi2codex", "minimax": "minimax2codex",
}


def os_name():
    """macos / linux / windows (FLEET_OS overrides)."""
    forced = os.environ.get("FLEET_OS")
    if forced:
        return forced
    import platform
    p = platform.system().lower()
    if p == "darwin":
        return "macos"
    if p.startswith("cygwin") or p.startswith("mingw") or p == "windows":
        return "windows"
    if os.environ.get("OSTYPE", "").lower().startswith("msys"):
        return "windows"
    return "linux" if p == "linux" else "unknown"


def is_macos():
    return os_name() == "macos"


def is_windows():
    return os_name() == "windows"


def service_dir():
    env = os.environ.get("FLEET_SERVICE_DIR")
    if env:
        return env
    if is_macos():
        return os.path.expanduser("~/Library/LaunchAgents")
    if is_windows():
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if not base:
            base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "FleetKit", "services")
    return os.path.join(os.environ.get("XDG_DATA_HOME",
                                       os.path.expanduser("~/.local/share")),
                        "FleetKit", "services")


def log_dir():
    env = os.environ.get("FLEET_LOG_DIR")
    if env:
        return env
    if is_windows():
        base = os.environ.get("TEMP") or os.environ.get("TMP") or os.environ.get("LOCALAPPDATA") or "/tmp"
        return os.path.join(base, "fleet-logs")
    return os.path.join(os.environ.get("TMPDIR", "/tmp"), "fleet-logs")


def label_prefix():
    return os.environ.get("FLEET_LABEL_PREFIX", "com.local")


def fleet_env_path():
    return os.environ.get(
        "FLEET_ENV_FILE",
        os.path.expanduser("~/AI Shared/repo/FleetKit/runtime/fleet.env"))


def load_env(path=None):
    """Minimal KEY=VALUE (or export KEY=VALUE) reader with quote stripping."""
    out = {}
    path = path or fleet_env_path()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def _service_files():
    """[(label, path)] for every installed fleet service."""
    sdir = service_dir()
    if not os.path.isdir(sdir):
        return []
    if is_macos():
        return [(os.path.basename(p)[: -len(".plist")], p)
                for p in glob.glob(os.path.join(sdir, "*.plist"))]
    if is_windows():
        return [(os.path.basename(p)[: -len(".cmd")], p)
                for p in glob.glob(os.path.join(sdir, "*.cmd"))]
    return [(os.path.basename(p)[: -len(".sh")], p)
            for p in glob.glob(os.path.join(sdir, "*.sh"))]


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def service_ports():
    """{bridge_name: port} from installed services, else PORT_BASE+offset."""
    out = {}
    prefix = label_prefix() + "."
    for label, path in _service_files():
        if not label.startswith(prefix):
            continue
        suffix = label[len(prefix):]
        text = _read(path)
        if is_windows():
            m = re.search(r"--port[=\s]+(\d{3,5})", text)
        else:
            m = re.search(r"--port[=\s]+(\d{3,5})", text) or \
                re.search(r"<string>--port</string>\s*<string>(\d{3,5})</string>", text)
        if not m:
            continue
        for name, lsuffix in LABEL_SUFFIX.items():
            if lsuffix == suffix:
                out[name] = int(m.group(1))
    for name, offset in PORT_OFFSETS.items():
        out.setdefault(name, PORT_BASE + offset)
    return out


def service_port(name):
    return service_ports().get(name)


def service_envs():
    """{label: {ENV_NAME: value}} for every installed service.

    Reads launchd plists on macOS and the generated wrapper (set "K=V" on
    Windows, export K=V on Linux) elsewhere, so no backend needs ~/Library.
    """
    out = {}
    prefix = label_prefix() + "."
    for label, path in _service_files():
        if not label.startswith(prefix):
            continue
        text = _read(path)
        pairs = {}
        if is_macos():
            pairs = dict(re.findall(r"<key>([A-Z0-9_]+)</key>\s*<string>([^<]*)</string>", text))
        else:
            pairs = dict(re.findall(r'set "([A-Z0-9_]+)=([^"]*)"', text))
            if not pairs:
                pairs = dict(re.findall(r'^export ([A-Z0-9_]+)=(.*)$', text, re.MULTILINE))
        if pairs:
            # wrapper files quote values for the shell ('x' or "x"); strip them
            out[label] = {k: _unquote(v) for k, v in pairs.items()}
    return out


def _unquote(value):
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def service_keys():
    """{label: api key} for every *2codex* service (first KEY/TOKEN found)."""
    out = {}
    for label, pairs in service_envs().items():
        if "2codex" not in label:
            continue
        for k, v in pairs.items():
            if ("KEY" in k or "TOKEN" in k) and v:
                out[label] = v
                break
    return out


def service_key(label, keyenv):
    """One KEY=VALUE out of one service definition, or ""."""
    return service_envs().get(label, {}).get(keyenv, "")


def _run(cmd, timeout=8.0):
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=timeout)
        return out.returncode, out.stdout.decode("utf-8", "replace")
    except Exception as exc:
        return 1, str(exc)


def service_status(label):
    """running / ready / missing"""
    if is_macos():
        sdir = service_dir()
        if not os.path.exists(os.path.join(sdir, label + ".plist")):
            return "missing"
        code, out = _run(["launchctl", "print",
                          "gui/%d/%s" % (os.getuid(), label)])
        if code == 0 and "state = running" in out:
            return "running"
        return "ready" if code == 0 else "missing"
    if is_windows():
        st = "schtasks"
        for candidate in ("schtasks", "schtasks.exe"):
            code, out = _run([candidate, "//Query", "//TN", label, "//V", "//FO", "LIST"])
            if code == 0:
                st = candidate
                break
        else:
            return "missing"
        return "running" if re.search(r"(?im)^\s*Status:\s*Running", out) else "ready"
    sdir = service_dir()
    if not os.path.exists(os.path.join(sdir, label + ".sh")):
        return "missing"
    code, _ = _run(["pgrep", "-f", os.path.join(sdir, label + ".sh")])
    return "running" if code == 0 else "ready"


def service_restart(label):
    if is_macos():
        _run(["launchctl", "kickstart", "-k",
              "gui/%d/%s" % (os.getuid(), label)], timeout=25.0)
        return
    if is_windows():
        for candidate in ("schtasks", "schtasks.exe"):
            _run([candidate, "//End", "//TN", label], timeout=10.0)
            if _run([candidate, "//Run", "//TN", label], timeout=15.0)[0] == 0:
                return
        return
    sdir = service_dir()
    _run(["pkill", "-f", os.path.join(sdir, label + ".sh")])
    _run(["setsid", "nohup", "bash", os.path.join(sdir, label + ".sh")], timeout=2.0)


def port_open(port, host="127.0.0.1", timeout=0.5):
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def venv_python(root):
    if is_windows():
        cand = os.path.join(root, ".venv", "Scripts", "python.exe")
        if os.path.exists(cand):
            return cand
        return "python"
    for sub in ("bin/python", "bin/python3"):
        cand = os.path.join(root, ".venv", sub)
        if os.path.exists(cand):
            return cand
    return "python3"
