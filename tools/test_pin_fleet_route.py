"""The fleet route pin must undo a provider switch without breaking the sessions it ran.

CC Switch owns ~/.codex/config.toml and rewrites it on every provider switch,
dropping the fleet markers (model_catalog_json, openai_base_url,
[model_providers.opencodex]) and setting model_provider = "custom". Measured
2026-09-30: the picker still listed the fleet bridge models, so a fleet pick
went to the switcher's upstream and came back 404 "model does not exist". The
pin therefore has to be surgical in both directions:

  * it restores the fleet route -- the catalog declaration, the gateway
    base_url, the opencodex provider table and the top-level model_provider --
    so a fresh Codex session reaches the gateway instead of the foreign host;
  * it leaves the foreign sections alone. [model_providers.custom] is what
    every already-open session resolves its provider against, so repointing it
    would break a live StepFun session while fixing the route;
  * it does not fire at all when the fleet markers are still declared, because
    that is an operator who deliberately aimed Codex somewhere else, and a pin
    that fights that is a pin nobody can schedule;
  * it is idempotent and atomic: a second run leaves the file byte-for-byte
    identical, and a config that changes underneath it is left alone.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
TOOL = os.path.join(KIT, "tools", "pin_fleet_route.py")
sys.path.insert(0, os.path.join(KIT, "tools"))

import pin_fleet_route  # noqa: E402  (path set up above)

GATEWAY = "http://127.0.0.1:10100/v1"

# What a provider switcher leaves behind: its own provider only, no fleet
# markers at all. This is the config shape that produced the 404.
SWITCHED = '''model_provider = "custom"
model = "step-5-preview"
model_reasoning_effort = "high"

[model_providers.custom]
name = "custom"
wire_api = "responses"
requires_openai_auth = true
base_url = "http://127.0.0.1:15721/v1"

[features]
hooks = true

[projects.'d:\\work\\thing']
trust_level = "trusted"
'''

# The same, but the switcher left a catalog line behind at the top level.
SWITCHED_WITH_CATALOG = '''model_provider = "custom"
model = "step-5-preview"
model_catalog_json = "%(catalog)s"
model_reasoning_effort = "high"

[model_providers.custom]
name = "custom"
base_url = "http://127.0.0.1:15721/v1"
'''

# What a healthy FleetKit deployment looks like.
PINNED = '''model = "combo/fleetcore"
model_provider = "opencodex"
model_catalog_json = "CATALOG"
openai_base_url = "GATEWAY"
experimental_realtime_ws_base_url = "GATEWAY"

[model_providers.custom]
name = "custom"
base_url = "http://127.0.0.1:15721/v1"

[model_providers.opencodex]
name = "OpenCodex Proxy"
base_url = "GATEWAY"
wire_api = "responses"
requires_openai_auth = false
'''


class FleetRoutePin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.config = os.path.join(self.home, "config.toml")
        self.catalog = os.path.join(self.home, "opencodex-catalog.json")
        self._write_catalog(["combo/fleetcore", "lingxi/lingxi-deepseek-flash",
                             "trae/trae-Doubao-Seed-2.1-Pro"])

    def _write_catalog(self, slugs):
        with open(self.catalog, "w", encoding="utf-8") as fh:
            json.dump({"models": [{"slug": slug} for slug in slugs]}, fh)

    def _write(self, text, **kw):
        text = text % kw if kw else text
        text = text.replace("CATALOG", self.catalog).replace("GATEWAY", GATEWAY)
        with open(self.config, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _read(self):
        with open(self.config, encoding="utf-8") as fh:
            return fh.read()

    def _decl(self, path):
        """The line the pin writes for a catalog path, backslashes escaped."""
        return 'model_catalog_json = "%s"' % self._toml_path(path)

    def _toml_path(self, path):
        """A Windows path as it has to appear inside a TOML basic string."""
        return path.replace("\\", "\\\\")

    def _pin(self, **kw):
        kw.setdefault("codex_home", self.home)
        return pin_fleet_route.pin_once(self.config, **kw)

    # -- the case that produced the 404 ------------------------------------- #

    def test_a_switched_config_is_repointed_at_the_gateway(self):
        self._write(SWITCHED)
        changed, detail = self._pin()
        self.assertTrue(changed, detail)
        text = self._read()
        self.assertIn(self._decl(self.catalog), text)
        self.assertIn('openai_base_url = "%s"' % GATEWAY, text)
        # The realtime-ws key is deliberately no longer pinned: the desktop
        # app prefers that transport when it exists and the proxy answers the
        # upgrade with 426, which locks compose.
        self.assertNotIn("experimental_realtime_ws_base_url", text)
        self.assertIn("[model_providers.opencodex]", text)
        self.assertIn('model_provider = "opencodex"', text)
        self.assertIn('wire_api = "responses"', text)
        # Parse the file: string assertions can pass on the copied-through
        # foreign section, so pin the fleet provider's own wire here -- the
        # pin wrote "chat" once and Codex refused to load for whole days.
        import tomllib
        with open(self.config, "rb") as fh:
            written = tomllib.load(fh)
        self.assertEqual(
            written["model_providers"]["opencodex"]["wire_api"], "responses")

    def test_the_foreign_provider_section_survives_verbatim(self):
        """Sessions already open on the switcher's provider must keep working.

        Their rollout records model_provider = "custom", so Codex resolves
        that name against this file at every turn. Repointing it would break a
        live StepFun session while the route looked fixed.
        """
        self._write(SWITCHED)
        self._pin()
        text = self._read()
        self.assertIn("[model_providers.custom]", text)
        self.assertIn('base_url = "http://127.0.0.1:15721/v1"', text)
        self.assertIn('wire_api = "responses"', text)
        self.assertIn("requires_openai_auth = true", text)

    def test_the_rest_of_the_file_is_copied_through(self):
        self._write(SWITCHED)
        self._pin()
        text = self._read()
        self.assertIn("[features]", text)
        self.assertIn("hooks = true", text)
        self.assertIn("[projects.'d:\\work\\thing']", text)
        self.assertIn('model_reasoning_effort = "high"', text)

    def test_a_fleet_model_the_gateway_cannot_serve_is_replaced(self):
        """A default the pinned route cannot answer is the next 404.

        The switcher leaves model = "step-5-preview", which the gateway does
        not serve, so a fresh session would fail before anyone picks a model.
        """
        self._write(SWITCHED)
        changed, detail = self._pin()
        self.assertTrue(changed, detail)
        self.assertIn('model = "combo/fleetcore"', self._read())

    def test_a_model_the_gateway_serves_is_kept(self):
        self._write(SWITCHED.replace('model = "step-5-preview"',
                                     'model = "trae/trae-Doubao-Seed-2.1-Pro"'))
        self._pin()
        self.assertIn('model = "trae/trae-Doubao-Seed-2.1-Pro"', self._read())

    # -- the case that must not be fought ----------------------------------- #

    def test_a_pinned_route_is_left_alone(self):
        self._write(PINNED)
        before = self._read()
        changed, detail = self._pin()
        self.assertFalse(changed, detail)
        self.assertIn("no change", detail)
        self.assertEqual(before, self._read())

    def test_a_deliberate_foreign_route_is_not_fought(self):
        """The markers are there, so an operator chose the foreign provider.

        Firing here would make the pin a loop that silently overrides the
        operator's own config on every timer tick.
        """
        self._write(PINNED.replace('model_provider = "opencodex"',
                                   'model_provider = "custom"'))
        before = self._read()
        changed, detail = self._pin()
        self.assertFalse(changed, detail)
        self.assertIn("on purpose", detail)
        self.assertEqual(before, self._read())

    def test_a_healthy_route_with_an_unusable_default_is_repaired(self):
        self._write(PINNED.replace('model = "combo/fleetcore"',
                                   'model = "step-5-preview"'))
        changed, detail = self._pin()
        self.assertTrue(changed, detail)
        self.assertIn('model = "combo/fleetcore"', self._read())
        self.assertIn('model_provider = "opencodex"', self._read())

    # -- the pin has to be safe on a timer ---------------------------------- #

    def test_a_second_run_reports_no_change(self):
        self._write(SWITCHED)
        self._pin()
        after_first = self._read()
        changed, detail = self._pin()
        self.assertFalse(changed, detail)
        self.assertIn("no change", detail)
        self.assertEqual(after_first, self._read())

    def test_a_dry_run_writes_nothing(self):
        self._write(SWITCHED)
        before = self._read()
        changed, detail = self._pin(dry_run=True)
        self.assertTrue(changed, detail)
        self.assertIn("dry-run", detail)
        self.assertEqual(before, self._read())

    def test_a_missing_config_is_not_an_error(self):
        proc = self._run_cli(os.path.join(self.home, "absent.toml"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("not found", proc.stdout + proc.stderr)

    def test_the_atomic_write_leaves_no_temporary_file_behind(self):
        self._write(SWITCHED)
        self._pin()
        leftovers = [name for name in os.listdir(self.home)
                     if name.endswith(".tmp")]
        self.assertEqual(leftovers, [],
                         "the atomic write left %s behind" % leftovers)

    def test_a_config_that_changes_mid_pin_is_left_alone(self):
        """The switcher writes this file too, and a half-parsed write breaks Codex."""
        self._write(SWITCHED)
        real_rewrite = pin_fleet_route.rewrite

        def rewrite_that_gets_clobbered(text, **kwargs):
            with open(self.config, "w", encoding="utf-8") as fh:
                fh.write(text + "\n# cc switch was writing here\n")
            return real_rewrite(text, **kwargs)

        pin_fleet_route.rewrite = rewrite_that_gets_clobbered
        try:
            changed, detail = self._pin()
        finally:
            pin_fleet_route.rewrite = real_rewrite
        self.assertFalse(changed, "the pin wrote a config the switcher had changed")
        self.assertIn("skipped", detail)
        self.assertIn("# cc switch was writing here", self._read())
        self.assertNotIn("openai_base_url", self._read(),
                         "the refused pin still repointed the route")

    # -- the catalog the picker reads is the one that is judged ------------- #

    def test_the_declared_catalog_is_the_one_judged(self):
        """A declared catalog that exists wins over the default name.

        Judging models against the default path would rate a working setup
        broken and rewrite a key the operator set on purpose.
        """
        other = os.path.join(self.home, "other-catalog.json")
        with open(other, "w", encoding="utf-8") as fh:
            json.dump({"models": [{"slug": "step-5-preview"}]}, fh)
        self._write(SWITCHED_WITH_CATALOG,
                    catalog=self._toml_path(other))
        changed, detail = self._pin()
        self.assertTrue(changed, detail)
        self.assertIn(self._decl(other), self._read())
        self.assertIn('model = "step-5-preview"', self._read(),
                      "the declared catalog serves this model; it must be kept")

    def test_a_declared_catalog_that_is_gone_is_repainted(self):
        gone = os.path.join(self.home, "gone-catalog.json")
        self._write(SWITCHED_WITH_CATALOG,
                    catalog=self._toml_path(gone))
        changed, detail = self._pin()
        self.assertTrue(changed, detail)
        self.assertIn(self._decl(self.catalog), self._read())

    def test_a_stray_declaration_inside_a_table_is_not_one(self):
        """A switch can leave model_catalog_json inside another table.

        That is not a declaration: the picker never reads it, so believing it
        is one is how a config looks healthy while no catalog is declared.
        """
        stray = SWITCHED + '\nmodel_catalog_json = "%s"\n' % self._toml_path(
            self.catalog)
        self._write(stray)
        changed, detail = self._pin()
        self.assertTrue(changed, detail)
        root = []
        for line in self._read().splitlines():
            if line.strip().startswith("["):
                break
            if line.startswith("model_catalog_json"):
                root.append(line)
        self.assertEqual(root, [self._decl(self.catalog)],
                         "no declaration the picker can read was added")

    def test_no_catalog_means_no_model_guessing(self):
        """Without a readable catalog the pin must not invent a default model."""
        os.remove(self.catalog)
        self._write(SWITCHED)
        changed, detail = self._pin()
        self.assertTrue(changed, detail)
        self.assertIn('model = "step-5-preview"', self._read())

    def test_the_gateway_is_overridable(self):
        self._write(SWITCHED)
        changed, detail = self._pin(gateway="http://127.0.0.1:10999/v1")
        self.assertTrue(changed, detail)
        self.assertIn('openai_base_url = "http://127.0.0.1:10999/v1"',
                      self._read())

    def test_the_provider_name_is_overridable(self):
        self._write(SWITCHED)
        changed, detail = self._pin(provider="fleetgw")
        self.assertTrue(changed, detail)
        self.assertIn("[model_providers.fleetgw]", self._read())
        self.assertIn('model_provider = "fleetgw"', self._read())

    def test_keep_model_provider_leaves_the_root_provider_alone(self):
        self._write(SWITCHED)
        changed, detail = self._pin(pin_provider=False)
        self.assertTrue(changed, detail)
        self.assertIn('model_provider = "custom"', self._read())
        self.assertIn('openai_base_url = "%s"' % GATEWAY, self._read())

    def test_pin_once_reports_a_decision_not_a_log_line(self):
        self._write(SWITCHED)
        changed, detail = self._pin()
        self.assertTrue(changed)
        self.assertIn(GATEWAY, detail)
        again, detail = self._pin()
        self.assertFalse(again)
        self.assertIn("no change", detail)

    def test_the_written_config_still_parses_as_toml(self):
        try:
            import tomllib
        except ModuleNotFoundError:
            self.skipTest("tomllib needs Python 3.11+")
        self._write(SWITCHED)
        self._pin()
        with open(self.config, "rb") as fh:
            data = tomllib.load(fh)
        self.assertEqual(data["model_provider"], "opencodex")
        self.assertEqual(data["model_providers"]["opencodex"]["base_url"], GATEWAY)
        self.assertEqual(data["model_providers"]["custom"]["base_url"],
                         "http://127.0.0.1:15721/v1")
        self.assertEqual(data["model"], "combo/fleetcore")

    def test_an_msys_style_home_writes_a_path_codex_can_open(self):
        """A timer reaches the pin with /c/Users/me/.codex.

        MSYS hands that over with forward slashes. Windows opens it, but the
        catalog line is the one nobody re-checks, so write the backslash form
        the rest of the config already uses.
        """
        self._write(SWITCHED)
        changed, detail = self._pin(codex_home="C:/Users/me/.codex")
        self.assertTrue(changed, detail)
        self.assertIn(
            self._decl("C:\\Users\\me\\.codex\\opencodex-catalog.json"),
            self._read())

    def test_the_cli_reports_what_it_did(self):
        self._write(SWITCHED)
        proc = self._run_cli(self.config, ["--codex-home", self.home])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pinned fleet route", proc.stdout)
        self.assertIn("opencodex", self._read())

    def _run_cli(self, config, extra=()):
        return subprocess.run([sys.executable, TOOL, "--config", config] +
                              list(extra), capture_output=True, text=True,
                              timeout=60)


if __name__ == "__main__":
    unittest.main()