#!/usr/bin/env python3
"""Shared Windows/Git discovery for the FleetKit deploy scripts.

preflight.py and acceptance.py are two standalone argparse entry points, and
both need the same three questions answered before they can talk to a runtime
root on Windows: where Git for Windows is installed, whether a TCP port is
already taken, and what a bridge row is called. Keeping one copy here means a
fix to the Git-for-Windows lookup lands in both tools at once.

No third-party imports; importable from the same directory with a plain
``from _deploylib import ...``.
"""
import os
import shutil
import socket
import sys

GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)

GIT_BASH_RELS = ("bin\\bash.exe", "usr\\bin\\bash.exe")


def git_install_root():
    """Best-effort Git for Windows install root.

    Git is not always on C:. A D: install is invisible to the hardcoded
    candidates, which then falls through to the WSL launcher and blocks the
    deploy for no real reason. Ask Windows first (registry), then back the
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


def git_bash():
    """Path to a bash that really is Git Bash, or bare ``bash`` as a fallback."""
    root = git_install_root()
    if root:
        for rel in GIT_BASH_RELS:
            candidate = os.path.join(root, rel)
            if os.path.exists(candidate):
                return candidate
    for candidate in GIT_BASH_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return "bash"


def port_in_use(port, host="127.0.0.1", timeout=0.4):
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()
