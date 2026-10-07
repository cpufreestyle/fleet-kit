#!/usr/bin/env python3
"""FleetKit deploy preflight.

Checks everything install.sh/deploy.sh assume before they touch the disk, so a
deploy fails in two seconds instead of half-way through: interpreters, the bash
that every .sh script needs, the bridge port range, and the install root.

Pure stdlib, cross-platform (macOS / Windows / Linux). It is the only part of
the deploy flow that runs without bash, which matters on a Windows host that
has python but no Git Bash: the fix is then a clear message, not a mystery.

Usage:
  preflight.py --home DIR [--port-base N] [--kit DIR] [--json]

Exit codes:
  0 ready (warnings allowed)   1 hard block
"""
import argparse
import json
import os
import shutil
import socket
import sys

BRIDGES = [
    ("workbuddy", 0), ("workbuddy-gpt", 1), ("qoder", 2), ("codely", 3),
    ("trae", 4), ("lingxi", 5), ("xhx", 6), ("gemini", 7), ("catpaw", 8),
    ("antigravity", 10), ("qwen", 11), ("cline", 12), ("zcode", 13),
]

GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
    r"C:\Program Files\Git\git-bash.exe",
)

GIT_BASH_RELS = ("bin\\bash.exe", "usr\\bin\\bash.exe")

PWSH_CANDIDATES = (
    os.path.join(os.environ.get("LOCALAPPDATA",
                                os.path.expanduser("~\\AppData\\Local")),
                 "Programs", "PowerShell-7", "pwsh.exe"),
    r"C:\Program Files\PowerShell\7\pwsh.exe",
)


def git_install_root():
    """Best-effort Git for Windows install root.

    Git is not always on C:. A D: install is invisible to the hardcoded
    candidates below, which then falls through to the WSL launcher and blocks
    the deploy for no real reason. Ask Windows first (registry), then back the
    root out of git.exe (<root>\\cmd\\git.exe -> <root>).
    """
    if sys.platform != "win32":
        return ""
    try:
        import winreg
    except ImportError:
        winreg = None
    if winreg:
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for sub in ("SOFTWARE\\GitForWindows",
                        "SOFTWARE\\WOW6432Node\\GitForWindows"):
                try:
                    with winreg.OpenKey(hive, sub) as key:
                        val, _ = winreg.QueryValueEx(key, "InstallPath")
                except OSError:
                    continue
                if val and os.path.isdir(val):
                    return val
    git = shutil.which("git")
    if git:
        root = os.path.dirname(os.path.dirname(os.path.abspath(git)))
        if os.path.isdir(root):
            return root
    return ""


def find_bash():
    """Return (path, source, is_wsl_shim).

    Windows ships C:\\Windows\\system32\\bash.exe as the WSL launcher. It is a
    bash, but it runs the kit inside WSL where schtasks does not exist and the
    runtime would land on a different filesystem, so it must not count as the
    bash the deploy scripts need.
    """
    wsl = None
    found = shutil.which("bash")
    if found:
        low = found.lower()
        if "system32" in low or "syswow64" in low:
            wsl = found
        else:
            return found, "PATH", False
    root = git_install_root()
    if root:
        for rel in GIT_BASH_RELS:
            candidate = os.path.join(root, rel)
            if os.path.exists(candidate):
                return candidate, "Git for Windows (%s)" % root, False
    for candidate in GIT_BASH_CANDIDATES:
        if os.path.exists(candidate):
            return candidate, "Git for Windows", False
    if wsl:
        return wsl, "WSL launcher (system32\\bash.exe)", True
    return None, "", False


def port_in_use(port, host="127.0.0.1", timeout=0.4):
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def backend():
    system = sys.platform
    if system == "darwin":
        return "macos", "~/Library/LaunchAgents"
    if system == "win32":
        return "windows", os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local")),
            "FleetKit", "services")
    return "linux", os.path.join(
        os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
        "FleetKit", "services")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    # <kit>/.codebuddy/skills/fleetkit-deploy/scripts -> four levels up
    default_kit = os.path.abspath(os.path.join(here, "..", "..", "..", ".."))
    ap = argparse.ArgumentParser(description="FleetKit deploy preflight")
    ap.add_argument("--home", default=os.path.join(os.path.expanduser("~"),
                                                   "FleetKit", "runtime"))
    ap.add_argument("--port-base", type=int, default=8787)
    ap.add_argument("--kit", default=default_kit)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    checks = []

    def add(level, name, detail, fix=""):
        checks.append({"level": level, "name": name, "detail": detail, "fix": fix})

    kit = os.path.abspath(args.kit)
    if os.path.exists(os.path.join(kit, "install.sh")):
        add("ok", "kit", kit)
    else:
        add("block", "kit", "install.sh not found under %s" % kit,
            "pass --kit <repo root>")

    python = sys.executable or shutil.which("python3") or shutil.which("python")
    version = ".".join(str(p) for p in sys.version_info[:3])
    add("ok" if python else "block", "python3", "%s (%s)" % (python, version),
        "" if python else "install python3 >= 3.9")

    bash, bash_src, bash_is_wsl = find_bash()
    if bash and not bash_is_wsl:
        add("ok", "bash", "%s (%s)" % (bash, bash_src))
    elif bash_is_wsl:
        add("block", "bash", "%s: %s is the WSL launcher, not Git Bash" % (bash, bash_src),
            "install Git for Windows (C:\\Program Files\\Git\\bin\\bash.exe); "
            "install.sh needs schtasks on the Windows side, which WSL cannot reach")
    else:
        add("block", "bash", "not on PATH and no Git for Windows found",
            "install Git for Windows (provides bash) - install.sh, finish.sh "
            "and status.sh are bash scripts")

    curl = shutil.which("curl")
    add("ok" if curl else "warn", "curl", curl or "missing (bridges fall back to python)",
        "" if curl else "optional")

    ocx = shutil.which("ocx")
    add("ok" if ocx else "warn", "ocx", ocx or "missing",
        "" if ocx else "npm install -g @bitkyc08/opencodex "
                       "(skips provider wiring; run setup-providers.sh later)")

    busy = [p for p in range(args.port_base, args.port_base + 14) if port_in_use(p)]
    if busy:
        add("block", "ports", "in use: %s" % ", ".join(str(p) for p in busy),
            "use --port-base <other>, or uninstall the old fleet first: "
            "bash <old-home>/uninstall.sh --home <old-home>")
    else:
        add("ok", "ports", "%d..%d free" % (args.port_base, args.port_base + 13))

    home = os.path.abspath(os.path.expanduser(args.home))
    parent = os.path.dirname(home)
    if os.path.isdir(parent) and os.access(parent, os.W_OK):
        add("ok", "home", home)
    else:
        add("block", "home", "parent not writable: %s" % parent,
            "pick a home under a writable directory")
    if " " in home:
        add("warn", "home-spaces", "path contains a space",
            "quote every path; every value in fleet.env must be double-quoted")

    if os.path.exists(os.path.join(home, "fleet.env")):
        add("warn", "existing-runtime", "fleet.env already exists in %s" % home,
            "re-running install.sh rewrites service definitions; uninstall first "
            "if you want a clean fleet")
    else:
        add("ok", "existing-runtime", "no runtime yet (fresh install)")

    name, service_dir = backend()
    add("ok", "backend", "%s -> %s" % (name, service_dir))
    if name == "macos" and not shutil.which("launchctl"):
        add("block", "launchctl", "required on macOS", "run on a mac")
    if name == "windows" and not shutil.which("schtasks"):
        add("block", "schtasks", "required on Windows", "use a Windows build with schtasks")
    if name == "windows":
        pwsh = shutil.which("pwsh")
        if not pwsh:
            # A freshly installed pwsh is on the user PATH only after the next
            # logon refreshes it, so check where it actually gets installed too.
            for candidate in PWSH_CANDIDATES:
                if os.path.exists(candidate):
                    pwsh = candidate
                    break
        if pwsh:
            add("ok", "pwsh", pwsh)
        else:
            add("warn", "pwsh", "PowerShell 7 not found (only Windows PowerShell 5.1)",
                "winget install -e --id Microsoft.PowerShell  (the default shell for "
                "every command in this skill); re-open the terminal after installing")

    blocks = [c for c in checks if c["level"] == "block"]
    warns = [c for c in checks if c["level"] == "warn"]

    if args.json:
        print(json.dumps({"home": home, "port_base": args.port_base,
                          "ready": not blocks, "checks": checks},
                         indent=2, ensure_ascii=False))
    else:
        print("FleetKit preflight")
        print("  home  : %s" % home)
        print("  ports : %d..%d" % (args.port_base, args.port_base + 13))
        for c in checks:
            print("  [%-5s] %-16s %s" % (c["level"].upper(), c["name"], c["detail"]))
            if c["fix"] and c["level"] != "ok":
                print("          fix: %s" % c["fix"])
        print()
        if blocks:
            print("BLOCKED: %d hard problem(s). Fix them, then re-run." % len(blocks))
        else:
            deploy = os.path.join(kit, "deploy.sh")
            if sys.platform == "win32" and bash and not bash_is_wsl:
                next_cmd = ('pwsh -NoProfile -Command "& \'%s\' -lc \\"bash \'%s\' --home \'%s\'\\""'
                            % (bash, deploy.replace("\\", "/"), home.replace("\\", "/")))
            else:
                next_cmd = 'bash "%s" --home "%s"' % (deploy, home)
            print("READY (%d warning(s)). Next: %s" % (len(warns), next_cmd))
    return 1 if blocks else 0


if __name__ == "__main__":
    sys.exit(main())
