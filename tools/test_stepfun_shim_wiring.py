"""The image-cap shim must be installed, re-pinned and removed as one unit.

StepFun's Plan API answers a request with 70 images and refuses the 71st with
400 images_too_many, and Codex re-sends its whole history every turn, so a
session that pastes screenshots crosses that ceiling no matter what the operator
does (tools/image_cap.py holds the measurement). Installing the service alone is
not enough: CC Switch owns ~/.codex/config.toml and rewrites the custom
provider's base_url back to 127.0.0.1:15721 on every provider switch, so a setup
that only installs the shim leaves Codex talking straight to CC Switch while the
shim sits unused on 15722.

These tests pin the contracts the install depends on: both setup scripts install
the shim, they do it behind a dry-run branch so --dry-run stays a plan that
mutates nothing, uninstall.sh removes the job instead of orphaning it in
launchd, and the control script logs to the one file the launchd plist writes.
"""
import os
import subprocess
import tempfile
import unittest

KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SETUP = os.path.join(KIT, "opencodex", "setup-providers.sh")
INSTALL = os.path.join(KIT, "install.sh")
UNINSTALL = os.path.join(KIT, "uninstall.sh")
SHIM_SH = os.path.join(KIT, "tools", "stepfun_image_shim.sh")


def _source(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class StaticWiring(unittest.TestCase):
    """The three scripts must mention the shim, each in its own place."""

    def test_setup_providers_installs_and_repins(self):
        src = _source(SETUP)
        self.assertIn("stepfun_image_shim.sh", src,
                      "setup-providers.sh no longer installs the image-cap shim")
        self.assertIn("pin_shim_base_url.py", src,
                      "the base_url re-pin is gone; CC Switch rewrites it back to 15721 on every switch, so the shim goes unused")

    def test_setup_providers_guards_the_shim_install_with_dry_run(self):
        idx = _source(SETUP).index("stepfun_image_shim.sh")
        window = _source(SETUP)[max(0, idx - 500): idx + 1200]
        self.assertIn("DRY_RUN", window,
                      "the shim install is not behind the dry-run guard; --dry-run would install a launchd service")

    def test_setup_providers_prefers_the_deployed_copy(self):
        src = _source(SETUP)
        window = src[max(0, src.index("SHIM_SH=") - 200):]
        self.assertIn("FLEET_HOME", window,
                      "the shim path no longer prefers the deployed copy, so the launchd job would run the git checkout")

    def test_install_sh_installs_the_shim(self):
        src = _source(INSTALL)
        self.assertIn("stepfun_image_shim.sh", src,
                      "install.sh no longer installs the image-cap shim")

    def test_uninstall_sh_removes_the_shim_service(self):
        src = _source(UNINSTALL)
        line = [ln for ln in src.splitlines() if ln.startswith("SUFFIXES=")][0]
        self.assertIn("stepfun-image-cap", line,
                      "uninstall.sh leaves an orphaned launchd job behind")

    def test_the_control_script_logs_where_launchd_writes(self):
        """platform.sh points the plist at $LOG_DIR/<label>.log.

        A second log name meant install-timer advertised a file the launchd job
        never wrote, and an operator checking it saw an empty log on a service
        that was in fact serving traffic.
        """
        src = _source(SHIM_SH)
        self.assertIn("SHIM_LOG=\"$LOG_DIR/${SHIM_LABEL}.log\"", src,
                      "the control script logs to a second file launchd never writes")


def _throwaway_home(root, with_config=True):
    home = os.path.join(root, "home")
    os.makedirs(home)
    with open(os.path.join(home, "fleet.env"), "w", encoding="utf-8") as fh:
        fh.write("PORT_BASE=17887\nLABEL_PREFIX=com.localtest\n"
                 "CODEBUDDY2OPENAI_KEY=sk-test-only\n")
    if with_config:
        codex = os.path.join(home, ".codex")
        os.makedirs(codex)
        with open(os.path.join(codex, "config.toml"), "w",
                  encoding="utf-8") as fh:
            fh.write("model_provider = \"custom\"\n")
    return home


def _run_script(script, args, home, services):
    env = dict(os.environ)
    env["HOME"] = home
    env["FLEET_SERVICE_DIR"] = services
    # setup-providers.sh refuses to start without ocx on PATH, and a stub is
    # enough: the dry run only echoes what it would call.
    stub = os.path.join(os.path.dirname(services or home), "bin")
    os.makedirs(stub, exist_ok=True)
    ocx = os.path.join(stub, "ocx")
    with open(ocx, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(ocx, 0o755)
    env["PATH"] = "%s:%s" % (stub, os.environ.get("PATH", ""))
    return subprocess.run(["bash", script] + args, capture_output=True,
                          text=True, timeout=300, env=env)


class DryRunMutatesNothing(unittest.TestCase):
    """Behavioural: --dry-run prints the plan and writes no service file."""

    def test_setup_providers_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = _throwaway_home(tmp)
            services = os.path.join(tmp, "services")
            envfile = os.path.join(home, "fleet.env")
            proc = _run_script(SETUP, ["--dry-run", "--env-file", envfile,
                                       "--home", tmp], home, services)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertIn("install-timer", proc.stdout,
                          "the dry run does not plan the shim install")
            self.assertIn("pin_shim_base_url.py", proc.stdout)
            written = os.listdir(services) if os.path.isdir(services) else []
            self.assertEqual(written, [],
                             "the dry run wrote a service file: %s" % written)

    def test_install_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = _throwaway_home(tmp)
            services = os.path.join(tmp, "services")
            proc = _run_script(INSTALL, ["--dry-run", "--home", tmp,
                                         "--no-opencodex"], home, services)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertIn("image-cap shim", proc.stdout,
                          "install.sh does not plan the shim install")
            written = os.listdir(services) if os.path.isdir(services) else []
            self.assertEqual(written, [],
                             "the dry run wrote a service file: %s" % written)


if __name__ == "__main__":
    unittest.main()
