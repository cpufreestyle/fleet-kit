"""Provider/env resolution for a bare `zcode app-server` process.

A bare CLI process leaves its provider registry empty unless BOTH built-in and
personal config paths are exported (see prepareCliProviderRuntimeEnv in zcode.cjs).
This module mirrors the CLI path derivation so callers do not have to guess it.
"""
import hashlib
import os


def builtin_config_path(app_version="0.0.0-dev",
                        origin="https://zcode.z.ai",
                        platform="darwin-aarch64",
                        home=None):
    """<root>/runtime/provider/<platform>/<appVersion>/endpoint-<sha256(origin)[:32]>/...

    app-server reports appVersion 0.0.0-dev, not the app's 3.14.x.
    Disk uses the Electron-style platform segment darwin-aarch64.
    """
    home = home or os.path.expanduser("~")
    digest = hashlib.sha256(origin.strip().encode()).hexdigest()[:32]
    return os.path.join(home, ".zcode", "v2", "runtime", "provider",
                        platform, app_version, "endpoint-" + digest,
                        "zcode-builtin.json")


def provider_env(home=None):
    """Env vars a bare app-server needs, or no provider is ever registered."""
    home = home or os.path.expanduser("~")
    builtin = os.environ.get(
        "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE") or builtin_config_path(home=home)
    personal = os.environ.get(
        "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE") or os.path.join(
        home, ".zcode", "v2", "provider_config.json")
    env = {"ZCODE_DATA_BASE_DIR": home}
    if os.path.exists(builtin):
        env["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = builtin
        env["ZCODE_BUILTIN_PROVIDER_BUNDLED_CONFIG_FILE"] = builtin
    if os.path.exists(personal):
        env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = personal
    return env


# Export on import so every caller (and any grandchild process) sees them.
os.environ.update(provider_env())
