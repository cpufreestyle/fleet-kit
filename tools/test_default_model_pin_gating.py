"""The default-model pin must not name a provider the installer just skipped.

setup-providers.sh registers the stepfun provider only when
STEPFUN_PLAN_API_KEY is set, but pinned the default model
unconditionally -- and the hardcoded fallback is stepfun/step-5-preview. On a
machine where the operator set neither FLEET_DEFAULT_MODEL nor the stepfun key,
the script therefore wrote model = "stepfun/step-5-preview" into
~/.codex/config.toml while having just printed "skipping stepfun plan api".
Codex then opens on a provider that is not registered.

The machine this was found on hides it: FLEET_DEFAULT_MODEL points at trae and
STEPFUN_PLAN_API_KEY is set, so both branches are healthy. These tests drive the
dry run with a controlled fleet.env and a throwaway HOME so the latent path is
actually exercised.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SCRIPT = os.path.join(KIT, "opencodex", "setup-providers.sh")

# Windows resolves a bare "bash" to System32\bash.exe -- the WSL launcher,
# which CreateProcess checks before PATH -- and the script then dies inside
# WSL while wsl.exe sprays a UTF-16LE proxy warning into stderr. Ask PATH
# for the real bash and decode defensively so stray host chatter can never
# kill a stream reader again.
BASH = shutil.which("bash") or "bash"


def _dry_run(env_extra):
    """Run the provider setup as a dry run against a throwaway HOME."""
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "home")
        os.makedirs(os.path.join(home, ".codex"))
        with open(os.path.join(home, ".codex", "config.toml"), "w",
                  encoding="utf-8") as fh:
            fh.write('model_provider = "custom"\n')
        envfile = os.path.join(tmp, "fleet.env")
        with open(envfile, "w", encoding="utf-8") as fh:
            for key, value in env_extra.items():
                fh.write("%s=%s\n" % (key, value))
        proc = subprocess.run(
            [BASH, SCRIPT, "--dry-run", "--env-file", envfile, "--home", tmp],
            capture_output=True, text=True, errors="replace", timeout=180,
            env=dict(os.environ, HOME=home))
        return proc.returncode, proc.stdout + proc.stderr


class DefaultModelPinGatingTest(unittest.TestCase):
    def test_neither_key_nor_override_does_not_pin_the_stepfun_fallback(self):
        code, out = _dry_run({})
        self.assertEqual(code, 0, out[-400:])
        self.assertIn("skipping stepfun plan api", out)
        self.assertNotIn("pin stepfun/step-5-preview", out,
                         "the stepfun provider was skipped, so its model must "
                         "not become the Codex default")

    def test_an_explicit_override_is_pinned_even_without_stepfun(self):
        code, out = _dry_run({"FLEET_DEFAULT_MODEL": "trae/trae-step-5-preview"})
        self.assertEqual(code, 0, out[-400:])
        self.assertIn("pin trae/trae-step-5-preview", out)

    def test_with_the_stepfun_key_the_fallback_is_pinned(self):
        code, out = _dry_run({"STEPFUN_PLAN_API_KEY": "sk-test"})
        self.assertEqual(code, 0, out[-400:])
        self.assertIn("pin stepfun/step-5-preview", out)


INSTALL = os.path.join(KIT, "install.sh")


def test_a_reinstall_keeps_the_operators_default_model_override():
    """emit_fleet_env regenerates fleet.env from scratch.

    It carries a few operator-owned lines across, and FLEET_DEFAULT_MODEL was
    not one of them -- so any re-install silently reverted the Codex picker
    default to the hardcoded fallback.
    """
    with open(INSTALL, encoding="utf-8") as fh:
        src = fh.read()
    match = re.search(r"preserved=.*grep -E '\^\(([^)]*)\)=", src)
    assert match, "the operator-owned preserve list is gone"
    kept = match.group(1).split("|")
    assert "FLEET_DEFAULT_MODEL" in kept, (
        "a re-install drops FLEET_DEFAULT_MODEL, so the picker default "
        "silently reverts to the stepfun fallback")


if __name__ == "__main__":
    unittest.main()
