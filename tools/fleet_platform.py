"""FleetKit platform abstraction (python side).

Cross-platform answers to the questions every tool asks:

* where do services live?   -> service_dir()
* which port is a bridge on?-> service_port()   (launchd plist / .cmd wrapper / fleet.env)
* which key does it use?    -> service_keys()   (launchd plist / .cmd wrapper / fleet.env)
* is it running?            -> service_status() (launchctl / schtasks / ps)

macOS keeps reading the plists it always read; Windows and Linux read the
wrapper files install.sh writes, so no code path depends on ~/Library.
"""
import glob
import os
import re
import shutil
import subprocess


def _native(path):
    """/c/Users/... -> C:/Users/... when an msys path reaches a native Python."""
    if os.name != "nt" or not path:
        return path
    m = re.match(r"^/([A-Za-z])/(.*)$", path.replace("\\", "/"))
    if m:
        return "%s:/%s" % (m.group(1).upper(), m.group(2))
    return path


PORT_BASE = 8787
PORT_OFFSETS = {
    "workbuddy": 0, "workbuddy-gpt": 1, "qoder": 2, "codely": 3, "trae": 4,
    "lingxi": 5, "xhx": 6, "gemini": 7, "catpaw": 8, "antigravity": 10,
    "qwen": 11, "cline": 12, "zcode": 13,
}
# label suffixes as installed: com.local.<name>2codex
LABEL_SUFFIX = {
    "workbuddy": "workbuddy2codex", "workbuddy-gpt": "workbuddy2codex-gpt",
    "qoder": "qoder2codex", "codely": "codely2codex", "trae": "trae2codex",
    "lingxi": "lingxi2codex", "xhx": "xhx2codex", "gemini": "gemini2codex",
    "catpaw": "catpaw2codex", "antigravity": "antigravity2codex",
    "qwen": "qwen2codex", "cline": "cline2codex", "zcode": "zcode2codex",
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


def backend():
    """The service backend this home was installed with.

    An env var wins, then the value install.sh recorded in fleet.env, and
    only a home with no record falls back to host detection. Host answers
    (venv layout, path spelling) keep using os_name().
    """
    forced = os.environ.get("FLEET_OS")
    if forced:
        return forced
    recorded = load_env().get("FLEET_OS")
    if recorded:
        return recorded
    return os_name()


def is_macos():
    return os_name() == "macos"


def is_windows():
    return os_name() == "windows"


def service_dir():
    env = os.environ.get("FLEET_SERVICE_DIR")
    if env:
        return env
    # The installer records where it put the wrappers in fleet.env.  That is
    # authoritative: a home installed with one FLEET_OS backend stays readable
    # under another, so no re-derivation from the host can replace it.
    recorded = load_env().get("LAUNCH_DIR")
    if recorded:
        return _native(recorded)
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
    recorded = load_env().get("LOG_DIR")
    if recorded:
        return _native(recorded)
    if is_windows():
        base = os.environ.get("TEMP") or os.environ.get("TMP") or os.environ.get("LOCALAPPDATA") or "/tmp"
        return os.path.join(base, "fleet-logs")
    return os.path.join(os.environ.get("TMPDIR", "/tmp"), "fleet-logs")


def label_prefix():
    return os.environ.get("FLEET_LABEL_PREFIX", "com.local")


def fleet_env_path():
    home = os.environ.get("FLEET_HOME") or os.path.join(
        os.path.expanduser("~"), "FleetKit", "runtime")
    return os.environ.get("FLEET_ENV_FILE",
                          os.path.join(_native(home), "fleet.env"))


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
    """[(label, path)] for every installed fleet service.

    Which extension a home uses follows the FLEET_OS backend it was installed
    with, not the host this tool runs on, so every known one is globbed.
    """
    sdir = service_dir()
    if not os.path.isdir(sdir):
        return []
    out = []
    for pattern, ext in (("*.plist", ".plist"), ("*.cmd", ".cmd"), ("*.sh", ".sh")):
        for p in sorted(glob.glob(os.path.join(sdir, pattern))):
            out.append((os.path.basename(p)[: -len(ext)], p))
    return out


def service_kind(label):
    """"plist" / "cmd" / "sh" -- whichever wrapper the installer actually wrote.

    A home follows the FLEET_OS backend it was installed with, so the answer is
    what is on disk, never what this host would have written.
    """
    sdir = service_dir()
    for ext in (".plist", ".cmd", ".sh"):
        if os.path.exists(os.path.join(sdir, label + ext)):
            return ext.lstrip(".")
    return ""


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
        pairs = dict(re.findall(r"<key>([A-Z0-9_]+)</key>\s*<string>([^<]*)</string>", text))
        if not pairs:
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


def _exe(name):
    """Absolute path for a helper executable.

    CreateProcess searches System32 before PATH, so a bare name can resolve
    to a same-named system shim -- bash lands on the WSL launcher there --
    while shutil.which follows PATH and finds the git-for-windows tool.
    """
    if os.path.dirname(name):
        return name
    return shutil.which(name) or name


def _run(cmd, timeout=8.0, env=None):
    cmd = [_exe(str(cmd[0]))] + [str(part) for part in cmd[1:]]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=timeout, env=env)
        return out.returncode, out.stdout.decode("utf-8", "replace")
    except Exception as exc:
        return 1, str(exc)


def _process_table():
    """[(pid, ppid, cmdline)] as reported by ps."""
    code, out = _run(["ps", "-ef"], timeout=15.0)
    if code != 0:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        if not (parts[1].isdigit() and parts[2].isdigit()):
            continue
        rows.append((int(parts[1]), int(parts[2]), parts[3]))
    return rows


def _msys(path):
    """C:/Users/... -> /c/Users/... : the spelling an msys ps table prints."""
    m = re.match(r"^([A-Za-z]):/(.*)$", path.replace("\\", "/"))
    if m:
        return "/%s/%s" % (m.group(1).lower(), m.group(2))
    return path


# a drive root written the msys way: preceded by nothing, by a separator or
# by a quote -- "/c/x", " cd /d/y", '"/d/y"'
_DRIVE = re.compile(r"(^|[^0-9a-z])/([a-z])/")


def _canon(text):
    """Spelling-insensitive form of a path, or of a whole command line.

    One and the same wrapper answers to C:/x when a native caller started
    it, /c/x when an msys shell did -- the spelling install.sh leaves
    behind -- and C:\\x when whoever built the path joined it with
    os.sep. Folding the drive root on both sides keeps a stop from
    finding only some of the wrappers.
    """
    t = (text or "").replace("\\", "/").lower()
    return _DRIVE.sub(lambda m: m.group(1) + m.group(2) + ":/", t)


def _service_pids(pattern):
    """Pids whose command line mentions ``pattern``, plus every descendant.

    ps is used rather than pgrep because a bare msys install ships ps but
    not procps, and stopping a .sh service has to reach the process the
    wrapper supervised -- otherwise the port stays bound. Matching folds
    path spellings because who started the wrapper decides whether the
    command line says C:/x or /c/x.
    """
    rows = _process_table()
    needle = _canon(pattern)
    pids = [pid for pid, _ppid, cmd in rows if needle in _canon(cmd)]
    for _ in range(4):
        grown = [pid for pid, ppid, cmd in rows
                 if pid not in pids and ppid in pids]
        if not grown:
            break
        pids.extend(grown)
    return pids


def service_status(label):
    """running / ready / missing"""
    os_hint = backend()
    if os_hint == "macos":
        sdir = service_dir()
        if not os.path.exists(os.path.join(sdir, label + ".plist")):
            return "missing"
        code, out = _run(["launchctl", "print",
                          "gui/%d/%s" % (os.getuid(), label)])
        if code == 0 and "state = running" in out:
            return "running"
        return "ready" if code == 0 else "missing"
    if os_hint == "windows":
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
    wrapper = os.path.join(sdir, label + ".sh")
    if not os.path.exists(wrapper):
        return "missing"
    return "running" if _service_pids(wrapper) else "ready"


def _platform_sh():
    """platform.sh ships next to this module."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "platform.sh")


def _fleet_env():
    """Environment for a bash helper.

    The wrapper that started this tool exports only the bridge keys, so the
    answers install.sh recorded in fleet.env have to travel along; without
    them platform.sh re-sniffs the host and picks the wrong backend.
    """
    env = dict(os.environ)
    for key, value in load_env().items():
        env.setdefault(key, value)
    # The helpers take either spelling, but /c/... is what install.sh left
    # behind (LAUNCH_DIR in fleet.env), so a restart has to reproduce that
    # command line rather than inventing a second one.
    env.setdefault("FLEET_SERVICE_DIR", _msys(service_dir()))
    env.setdefault("FLEET_LOG_DIR", _msys(log_dir()))
    home = os.path.dirname(fleet_env_path())
    if os.path.isdir(home):
        env.setdefault("FLEET_HOME", _msys(home))
    return env


def _fleet_shell(func, label, timeout=40.0):
    """Run one platform.sh helper in bash.

    Starting a .sh supervisor has to happen inside a shell: a native
    Windows python cannot detach an msys bash and cannot signal the msys
    pids ps reports, while platform.sh already knows how to do both.
    """
    # an msys shell eats the backslashes of a Windows path, so hand it the
    # msys spelling
    script = '. "%s/platform.sh" >/dev/null 2>&1 || exit 1; %s "$1"' % (
        _msys(os.path.dirname(_platform_sh())), func)
    code, out = _run(["bash", "-c", script, "fleet", label], timeout=timeout,
                     env=_fleet_env())
    return out.strip() if code == 0 else ""


def service_restart(label):
    if backend() == "macos":
        _run(["launchctl", "kickstart", "-k",
              "gui/%d/%s" % (os.getuid(), label)], timeout=25.0)
        return
    if backend() == "windows":
        for candidate in ("schtasks", "schtasks.exe"):
            _run([candidate, "//End", "//TN", label], timeout=10.0)
            if _run([candidate, "//Run", "//TN", label], timeout=15.0)[0] == 0:
                return
        return
    if not os.path.exists(os.path.join(service_dir(), label + ".sh")):
        return
    _fleet_shell("fleet_service_restart", label)


def ocx_exe():
    """Path to the ocx CLI.

    npm drops a shell script plus a .cmd/.ps1 pair next to it; subprocess on
    Windows can only exec the .cmd, and 'ocx' resolves to the shell script.
    """
    import shutil
    if os.name == "nt":
        for name in ("ocx.cmd", "ocx.exe", "ocx.bat"):
            found = shutil.which(name)
            if found:
                return found
    return shutil.which("ocx") or "ocx"


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
