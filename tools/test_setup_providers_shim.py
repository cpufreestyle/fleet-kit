"""`setup-providers.sh` must install the ocx codex-shim with the service.

Measured 2026-09-29 on a fully provisioned machine: `ocx status` answered
"Restart safety: AT RISK after restart (custom local gateway lifecycle is not
managed by opencodex; run 'ocx restore')" together with "Codex autostart shim
is not installed", even though the script had already run `ocx service`. The
launchd service starts the proxy at boot, but `codex` can be launched before
that job runs and then points at a gateway that is not listening; the shim
makes the codex binary ensure the proxy itself, and it is reversible with
`ocx codex-shim uninstall`.

Static checks only: the dry run must print the install instead of running it,
and the script must still parse.
"""
import os
import re
import subprocess

KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SCRIPT = os.path.join(KIT, "opencodex", "setup-providers.sh")


def _source():
    with open(SCRIPT, encoding="utf-8") as fh:
        return fh.read()


def test_the_codex_shim_is_installed_alongside_the_service():
    src = _source()
    assert "run ocx codex-shim install" in src, (
        "the codex-shim install is gone; a reboot can leave codex pointed at "
        "a gateway that has not started yet")


def test_the_shim_install_refuses_an_app_bundle_binary():
    """Desktop Codex puts its bundled binary on PATH and must not be wrapped.

    Renaming a file inside a running app bundle is not something a setup
    script should do, so the shim install is guarded on the discovered codex
    path and the launchd service still autostarts the proxy without it.
    """
    src = _source()
    assert '.app/Contents/Resources/' in src, (
        "the .app-bundle guard is gone; 'ocx codex-shim install' would wrap a "
        "desktop app's bundled codex binary when that is first on PATH")


def test_the_shim_install_is_routed_through_run_for_the_dry_run():
    """`run` prints under --dry-run, so the plan never mutates the machine."""
    src = _source()
    assert not re.search(r"^\s*ocx codex-shim install", src, re.M), (
        "codex-shim install must go through run() so --dry-run stays a plan")


def test_the_script_still_parses():
    subprocess.run(["bash", "-n", SCRIPT], check=True)


def test_the_dry_run_never_prints_a_literal_api_key(tmp_path):
    """Behavioral: the plan must show `--api-key ***`, never the fleet secret.

    Measured 2026-09-29: `setup-providers.sh --dry-run` echoed
    `ocx provider add ... --api-key <real key>` for every provider, putting the
    fleet's live keys into terminal scrollback and anything that captures it.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "fleet.env").write_text(
        "PORT_BASE=17887\nLABEL_PREFIX=com.localtest\n"
        "CODEBUDDY2OPENAI_KEY=sk-secret-literal-value\n", encoding="utf-8")
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    ocx = stub_bin / "ocx"
    ocx.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ocx.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = "%s:%s" % (stub_bin, os.environ["PATH"])
    env["HOME"] = str(home)
    proc = subprocess.run(["bash", SCRIPT, "--home", str(home), "--dry-run"],
                          capture_output=True, text=True, timeout=180, env=env)

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "--api-key ***" in proc.stdout
    assert "sk-secret-literal-value" not in proc.stdout + proc.stderr


def test_the_shim_install_is_skipped_for_an_app_bundle_codex(tmp_path):
    """Behavioral: an .app-bundle codex on PATH must not be wrapped."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "fleet.env").write_text(
        "PORT_BASE=17887\nLABEL_PREFIX=com.localtest\n"
        "CODEBUDDY2OPENAI_KEY=sk-secret-literal-value\n", encoding="utf-8")
    bundle = tmp_path / "ChatGPT.app" / "Contents" / "Resources"
    bundle.mkdir(parents=True)
    codex = bundle / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex.chmod(0o755)
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    ocx = stub_bin / "ocx"
    ocx.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ocx.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = "%s:%s:%s" % (bundle, stub_bin, os.environ["PATH"])
    env["HOME"] = str(home)
    proc = subprocess.run(["bash", SCRIPT, "--home", str(home), "--dry-run"],
                          capture_output=True, text=True, timeout=180, env=env)

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "[dry-run] ocx codex-shim install" not in proc.stdout
    assert "skipping codex-shim" in proc.stdout
    assert "--api-key ***" in proc.stdout  # the rest of the setup still plans
