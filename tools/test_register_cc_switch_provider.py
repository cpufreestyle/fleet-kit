"""The opencodex reverse proxy must appear in CC Switch as a real provider.

CC Switch owns ~/.codex/config.toml and rewrites it on every provider switch, so
without a row of its own the fleet route cannot be selected in the UI and the
provider that *is* selected writes back a foreign base_url -- measured
2026-10-05, a workbuddy model answered 404 by the StepFun image-cap shim on
15722 while the fleet's own gateway sat unused on 10100.
tools/register_cc_switch_provider.py writes that row, and the guarantees worth
testing are the ones an operator would otherwise discover the hard way:

  * it creates the row, the endpoint row and the embedded TOML together. Either
    half alone leaves Codex booting at an address the app no longer forwards to;
  * a second run updates instead of duplicating, because setup runs this on
    every install and providers is only unique on (id, app_type);
  * it reuses the id the same provider already has for another app, the way the
    claude and claude-desktop rows do, so one provider stays one provider;
  * it touches only its own row. A sibling codex provider that legitimately
    talks straight upstream, and the same provider under another app, survive;
  * the config it writes points both provider tables at the reverse proxy --
    the 15722 regression was exactly one table pointing somewhere else;
  * --no-set-current registers without stealing the operator's selection;
  * --dry-run changes nothing, and a missing database is a detail, not a crash,
    because the caller is a setup script that has to carry on.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(KIT, "tools"))

import register_cc_switch_provider as reg  # noqa: E402  (path set up above)

BASE = "http://127.0.0.1:10100/v1"
CATALOG = "/tmp/fleet-catalog.json"
MODEL = "workbuddy/deepseek-v4-flash"
FLEETKIT_ID = "fc41d7fa-8fba-4739-9cc4-1502ae29a6fe"
STEPFUN_ID = "3a20aad7-bc99-4b10-8a72-d7b7dacd2c16"
SHIM = "http://127.0.0.1:15722/v1"

SCHEMA = """
CREATE TABLE providers (
    id TEXT NOT NULL,
    app_type TEXT NOT NULL,
    name TEXT NOT NULL,
    settings_config TEXT NOT NULL,
    website_url TEXT,
    category TEXT,
    created_at INTEGER,
    sort_index INTEGER,
    notes TEXT,
    icon TEXT,
    icon_color TEXT,
    meta TEXT,
    is_current INTEGER DEFAULT 0,
    in_failover_queue INTEGER DEFAULT 0,
    cost_multiplier TEXT,
    limit_daily_usd REAL,
    limit_monthly_usd REAL,
    provider_type TEXT,
    PRIMARY KEY (id, app_type)
);
CREATE TABLE provider_endpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    app_type TEXT NOT NULL,
    url TEXT NOT NULL,
    added_at INTEGER
);
"""


class RegisterDatabase(unittest.TestCase):
    """A database shaped like CC Switch's, in a directory nobody else touches."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cc-switch-register-")
        self.db = os.path.join(self.dir, "cc-switch.db")
        conn = sqlite3.connect(self.db)
        conn.executescript(SCHEMA)
        # The fleet's claude-side row and a codex provider that is currently
        # selected: both have to survive a run that only adds the codex row.
        conn.execute("insert into providers (id, app_type, name,"
                     " settings_config, created_at, is_current) values (?,?,?,?,?,?)",
                     (FLEETKIT_ID, "claude", "FleetKit", "{}", 1, 1))
        conn.execute("insert into provider_endpoints (provider_id, app_type,"
                     " url) values (?,?,?)",
                     (FLEETKIT_ID, "claude", "http://127.0.0.1:8801"))
        conn.execute("insert into providers (id, app_type, name,"
                     " settings_config, created_at, is_current) values (?,?,?,?,?,?)",
                     (STEPFUN_ID, "codex", "StepFun",
                      json.dumps({"config": 'base_url = "%s"' % SHIM}), 1, 1))
        conn.execute("insert into provider_endpoints (provider_id, app_type,"
                     " url) values (?,?,?)", (STEPFUN_ID, "codex", SHIM))
        conn.commit()
        conn.close()

    def rows(self, name="FleetKit", app_type="codex"):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("select id, name, settings_config, meta, notes,"
                                " is_current from providers where name = ?"
                                " and app_type = ?", (name, app_type)).fetchall()
        finally:
            conn.close()

    def endpoints(self, provider_id=FLEETKIT_ID, app_type="codex"):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("select url from provider_endpoints"
                                " where provider_id = ? and app_type = ?",
                                (provider_id, app_type)).fetchall()
        finally:
            conn.close()

    def embedded(self, row):
        return json.loads(row[2])["config"]

    def register(self, base_url=BASE, **kwargs):
        kwargs.setdefault("backup", False)
        return reg.register(self.db, "FleetKit", "codex", base_url, CATALOG,
                            MODEL, **kwargs)

    def test_a_run_creates_the_row_the_endpoint_and_the_embedded_base_url(self):
        changed, detail = self.register()
        self.assertTrue(changed, detail)
        rows = self.rows()
        self.assertEqual(1, len(rows))
        self.assertIn(BASE, self.embedded(rows[0]))
        self.assertEqual([(BASE,)], self.endpoints())
        self.assertIn(BASE, detail)

    def test_a_second_run_updates_instead_of_duplicating(self):
        self.register()
        changed, detail = self.register()
        self.assertTrue(changed, detail)
        self.assertEqual(1, len(self.rows()))
        self.assertEqual(1, len(self.endpoints()))
        self.assertIn("updated", detail)

    def test_the_id_of_the_same_provider_in_another_app_is_reused(self):
        self.register()
        self.assertEqual(FLEETKIT_ID, self.rows()[0][0])
        # the claude row is still there and untouched
        self.assertEqual(1, len(self.rows(app_type="claude")))

    def test_the_written_config_points_both_provider_tables_at_the_proxy(self):
        self.register()
        config = self.embedded(self.rows()[0])
        self.assertIn('model_provider = "custom"', config)
        self.assertEqual(4, config.count(BASE),
                         "every route in the config must name the proxy")
        self.assertIn("[model_providers.custom]", config)
        self.assertIn("[model_providers.opencodex]", config)
        # the regression this guards: one table pointing at the StepFun shim
        self.assertNotIn("15722", config)
        self.assertIn(MODEL, config)
        self.assertIn(CATALOG, config)

    def test_a_refresh_rewrites_a_row_that_pointed_somewhere_else(self):
        self.register(base_url=SHIM)
        self.register()
        self.assertNotIn(SHIM, self.embedded(self.rows()[0]))

    def test_other_providers_and_other_apps_survive(self):
        self.register()
        stepfun = self.rows(name="StepFun")[0]
        self.assertIn(SHIM, self.embedded(stepfun))
        self.assertEqual([("http://127.0.0.1:8801",)],
                         self.endpoints(app_type="claude"))

    def test_it_becomes_the_current_provider_for_its_app_only(self):
        self.register()
        self.assertEqual(1, self.rows()[0][5])
        self.assertEqual(0, self.rows(name="StepFun")[0][5])
        # claude keeps its own selection
        self.assertEqual(1, self.rows(app_type="claude")[0][5])

    def test_no_set_current_leaves_the_operators_selection_alone(self):
        self.register(set_current=False)
        self.assertEqual(0, self.rows()[0][5])
        self.assertEqual(1, self.rows(name="StepFun")[0][5])

    def test_dry_run_changes_nothing(self):
        changed, detail = self.register(dry_run=True)
        self.assertTrue(changed, detail)
        self.assertEqual([], self.rows())
        self.assertEqual([], self.endpoints())

    def test_the_backup_is_taken_once_and_holds_the_pre_write_database(self):
        changed, detail = self.register(backup=True)
        self.assertTrue(changed, detail)
        backups = [name for name in os.listdir(self.dir)
                   if name.startswith("cc-switch.db." + reg.BACKUP_PREFIX)]
        self.assertEqual(1, len(backups), backups)
        self.assertIn(backups[0], detail)

    def test_no_backup_when_asked(self):
        self.register(backup=False)
        self.assertEqual([], [name for name in os.listdir(self.dir)
                              if reg.BACKUP_PREFIX in name])

    def test_a_missing_database_is_a_detail_not_a_crash(self):
        missing = os.path.join(self.dir, "nope.db")
        changed, detail = reg.register(missing, "FleetKit", "codex", BASE,
                                       CATALOG, MODEL, backup=False)
        self.assertFalse(changed)
        self.assertIn(missing, detail)
        # and it must not have been created behind the operator's back
        self.assertFalse(os.path.exists(missing))

    def test_a_database_without_the_table_is_a_detail_not_a_crash(self):
        empty = os.path.join(self.dir, "empty.db")
        sqlite3.connect(empty).close()
        changed, detail = reg.register(empty, "FleetKit", "codex", BASE,
                                       CATALOG, MODEL, backup=False)
        self.assertFalse(changed)
        self.assertIn("providers", detail)

    def test_main_reports_failure_without_raising(self):
        argv = ["--db", os.path.join(self.dir, "nope.db"), "--no-backup"]
        with open(os.devnull, "w") as sink:
            self.assertEqual(1, self._main(argv, sink))

    def test_main_succeeds_on_a_real_database(self):
        argv = ["--db", self.db, "--no-backup", "--catalog", CATALOG]
        with open(os.devnull, "w") as sink:
            self.assertEqual(0, self._main(argv, sink))
        self.assertEqual(1, len(self.rows()))

    def _main(self, argv, sink):
        """main() prints its summary; keep it out of the test output."""
        import contextlib
        with contextlib.redirect_stdout(sink):
            return reg.main(argv)


class ConfigToml(unittest.TestCase):
    """The TOML is what CC Switch actually writes into config.toml."""

    def test_it_carries_the_ocx_route_in_both_spellings(self):
        config = reg.config_toml(BASE, CATALOG, MODEL, notify=False)
        self.assertIn('openai_base_url = "%s"' % BASE, config)
        self.assertIn('base_url = "%s"' % BASE, config)
        self.assertNotIn("15722", config)

    def test_notify_follows_the_flag_and_the_installed_app(self):
        self.assertNotIn("notify", reg.config_toml(BASE, CATALOG, MODEL,
                                                   notify=False))
        with_notify = reg.config_toml(BASE, CATALOG, MODEL, notify=True)
        self.assertEqual(os.path.exists(reg.DEFAULT_NOTIFY),
                         "notify" in with_notify)


if __name__ == "__main__":
    unittest.main()
