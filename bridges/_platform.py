"""Cross-platform helpers shared by the bridges.

Upstream providers identify clients by a platform string ("darwin-arm64",
"win32-x64", ...). FleetKit keeps the macOS value as the default so existing
sessions are never invalidated by a reinstall, but honours FLEET_CLIENT_PLATFORM
so a Windows or Linux host can advertise itself instead.
"""
import os
import platform
import sys


def os_name():
    forced = os.environ.get("FLEET_OS")
    if forced:
        return forced
    p = platform.system().lower()
    if p == "darwin":
        return "macos"
    if p.startswith("cygwin") or p.startswith("mingw") or p == "windows":
        return "windows"
    if os.environ.get("OSTYPE", "").lower().startswith("msys"):
        return "windows"
    return "linux" if p == "linux" else "unknown"


def client_platform(default="darwin-arm64"):
    """Platform string sent upstream; env-overridable for Windows/Linux."""
    return os.environ.get("FLEET_CLIENT_PLATFORM") or default


def short_platform(default="darwin"):
    """Bare platform token ("darwin" / "win32" / "linux") sent upstream."""
    if os.environ.get("FLEET_CLIENT_PLATFORM"):
        return os.environ["FLEET_CLIENT_PLATFORM"].split("-")[0]
    forced = os.environ.get("FLEET_PLATFORM_SHORT")
    if forced:
        return forced
    return default


def arch():
    return os.environ.get("FLEET_CLIENT_ARCH") or (
        "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64")


def cli_binary(name, unix_glob=None, windows_names=None):
    """Resolve a vendored CLI: FLEET_<NAME>_PATH wins, then PATH, then globs."""
    import glob as _glob
    import shutil as _shutil
    env_key = "FLEET_%s_PATH" % name.upper()
    forced = os.environ.get(env_key)
    if forced:
        return forced
    found = _shutil.which(name)
    if found:
        return found
    home = os.path.expanduser("~")
    if os_name() == "windows":
        for cand in (windows_names or []):
            found = _shutil.which(cand)
            if found:
                return found
        return ""
    for pattern in (unix_glob or []):
        hits = sorted(_glob.glob(os.path.join(home, pattern)))
        for hit in hits:
            if os.path.exists(hit):
                return hit
    return ""


def home():
    return os.path.expanduser("~")


def app_support_dirs(*parts):
    """Candidate per-user application-data directories for *parts.

    macOS  : ~/Library/Application Support/<parts>
    Windows: %APPDATA%\\<parts>  (+ %LOCALAPPDATA%\\<parts>)
    Linux  : ~/.config/<parts>    (+ ~/.local/share/<parts>)
    """
    out = []
    h = home()
    rel = os.path.join(*parts)
    if os_name() == "macos":
        out.append(os.path.join(h, "Library", "Application Support", rel))
    elif os_name() == "windows":
        for base in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA"),
                     os.path.join(h, "AppData", "Roaming"),
                     os.path.join(h, "AppData", "Local")):
            if base:
                out.append(os.path.join(base, rel))
    else:
        out.append(os.path.join(h, ".config", rel))
        out.append(os.path.join(h, ".local", "share", rel))
    return out


def native_os():
    """Host OS as Python sees it, ignoring the FLEET_OS routing hint.

    FLEET_OS is a deployment hint (it selects the .sh wrapper layout), not a
    statement about the machine, so bridges must not use it to locate a
    desktop application's per-user data.
    """
    p = platform.system().lower()
    if p.startswith("cygwin") or p.startswith("mingw") or p == "windows":
        return "windows"
    if p == "darwin":
        return "macos"
    if p == "linux":
        return "linux"
    return "unknown"


def app_data_containers():
    r"""Directories that hold per-edition application folders.

    Windows: %APPDATA% / %LOCALAPPDATA%   ("Trae CN", "TRAE SOLO CN", ...)
    macOS  : ~/Library/Application Support
    Linux  : ~/.config / ~/.local/share

    Unlike app_support_dirs() this does not append an application name, so a
    bridge that supports several editions of one product can join the edition
    itself and avoid inventing paths such as %APPDATA%\Trae\Trae CN.
    """
    out = []
    h = home()
    if native_os() == "macos":
        out.append(os.path.join(h, "Library", "Application Support"))
    elif native_os() == "windows":
        for base in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA"),
                     os.path.join(h, "AppData", "Roaming"),
                     os.path.join(h, "AppData", "Local")):
            if base and base not in out:
                out.append(base)
    else:
        for base in (os.path.join(h, ".config"), os.path.join(h, ".local", "share")):
            if base and base not in out:
                out.append(base)
    return out


def app_bin_candidates(*parts):
    """Candidate install locations for a CLI bundled inside an app."""
    rel = os.path.join(*parts)
    out = []
    h = home()
    if os_name() == "macos":
        out += [os.path.join("/Applications", rel),
                os.path.join(h, "Applications", rel)]
    elif os_name() == "windows":
        for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("PROGRAMFILES"),
                     os.environ.get("PROGRAMFILES(X86)"),
                     os.path.join(h, "AppData", "Local", "Programs")):
            if base:
                out.append(os.path.join(base, rel))
    else:
        out += [os.path.join("/opt", rel), os.path.join("/usr", "lib", rel),
                os.path.join(h, ".local", "share", rel)]
    return out


def first_existing(candidates):
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return candidates[0] if candidates else ""


def chrome_user_data():
    """Chrome/Chromium per-user profile root, per platform."""
    h = home()
    if os_name() == "macos":
        return os.path.join(h, "Library", "Application Support", "Google", "Chrome")
    if os_name() == "windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(h, "AppData", "Local")
        return os.path.join(base, "Google", "Chrome", "User Data")
    return os.path.join(h, ".config", "google-chrome")
