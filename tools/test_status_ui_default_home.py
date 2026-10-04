"""A hand-run status panel must not report a healthy fleet as broken.

status_ui.py resolved its fleet root to a single hardcoded guess (~/fleet) when
neither --home nor FLEET_HOME was given, while its own --home help text promised
~/FleetKit/runtime. The launchd service always passes --home, so only a hand-run
`status_ui.py --once` landed on the guess -- and on this machine neither guess
existed, so the panel reported fleet.env missing, sent every probe without an
Authorization header, and raised one "log in and run finish.sh" warning per
bridge pointing at a path that does not exist. A healthy fleet looked dead.

Now the documented locations are tried in order and the missing-fleet.env
warning says how to point the panel at the real install.
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest

KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
spec = importlib.util.spec_from_file_location(
    "status_ui_default_home", os.path.join(KIT, "tools", "status_ui.py"))
sui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sui)


def _args(**override):
    base = {"home": None, "env_file": None, "port_base": None,
            "label_prefix": None, "log_dir": None, "launch_dir": None,
            "port": None, "checkin_home": None, "host": "127.0.0.1",
            "refresh": 10}
    base.update(override)
    return types.SimpleNamespace(**base)


class StatusUiDefaultHomeTest(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        for key in ("FLEET_HOME", "FLEET_ENV_FILE", "LOG_DIR", "LABEL_PREFIX",
                    "CODEX_CHECKIN_HOME", "FLEET_SERVICE_DIR",
                    "FLEET_LAUNCH_DIR", "PORT_BASE"):
            os.environ.pop(key, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "home")
        os.makedirs(self.home)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        # Since python 3.8, expanduser("~") on Windows prefers USERPROFILE
        # (then HOMEDRIVE/HOMEPATH) over HOME, so pin those to the sandbox
        # too or a real install on the test machine leaks into the case.
        self._old_profile = {key: os.environ.get(key)
                             for key in ("USERPROFILE", "HOMEDRIVE", "HOMEPATH")}
        os.environ["USERPROFILE"] = self.home
        os.environ["HOMEDRIVE"], _ = os.path.splitdrive(self.home)
        os.environ["HOMEPATH"] = self.home

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        for key, value in self._old_profile.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        os.environ.clear()
        os.environ.update(self._saved)

    def _make_env(self, relative):
        target = os.path.join(self.home, relative)
        os.makedirs(target)
        with open(os.path.join(target, "fleet.env"), "w", encoding="utf-8") as fh:
            fh.write("PORT_BASE=8787\n")
        return target

    def test_the_documented_location_wins_when_it_has_a_fleet_env(self):
        real = self._make_env(os.path.join("FleetKit", "runtime"))
        self.assertEqual(sui._default_home(), real)

    def test_the_legacy_location_is_still_tried(self):
        legacy = self._make_env("fleet")
        self.assertEqual(sui._default_home(), legacy)

    def test_without_any_install_the_warning_names_the_remedy(self):
        cfg = sui.build_config(_args())
        self.assertFalse(cfg["env_found"])
        joined = "\n".join(cfg["warnings"])
        self.assertIn("--home", joined,
                     "a missing fleet.env must say how to point at the real "
                     "install, not just that the keys are unknown")

    def test_an_explicit_home_still_wins(self):
        real = self._make_env(os.path.join("FleetKit", "runtime"))
        cfg = sui.build_config(_args(home=real))
        self.assertTrue(cfg["env_found"])


if __name__ == "__main__":
    unittest.main()
