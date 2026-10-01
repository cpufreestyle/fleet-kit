"""The CC Switch re-point must move two columns and refuse everything else.

CC Switch owns ~/.codex/config.toml, so repointing Codex's own provider at the
image-cap shim loses the race the moment a provider is switched and leaves the
shim unused on 15722 (tools/pin_cc_switch_endpoint.py carries the measurement).
The re-point that holds instead targets CC Switch's own routing table, which
lives in a 55MB sqlite database CC Switch keeps open while it answers requests.
Writing into a live database by hand is how a fleet ends up with a broken
provider, so the pin's guarantees are the thing worth testing:

  * it moves both places the app may read -- provider_endpoints.url and the
    base_url embedded in providers.settings_config -- or Codex still boots at
    the old address after CC Switch restarts;
  * it touches only the named provider for the named app_type. The same name
    exists for claude and codex with different endpoints, and a sibling codex
    provider that legitimately talks straight upstream has to survive a run
    untouched;
  * it refuses a target that is neither the stepfun upstream nor the shim. A
    provider somebody repointed at another host by hand is their decision, and
    silently rewriting it is worse than not capping at all;
  * a second run reports "no change", because a timer runs this every few
    minutes and re-writing an already-correct row is churn on a live database;
  * it takes one backup before the first write and never another;
  * nothing raises -- every caller is either a setup script or a daemon thread
    that has to carry on with its next job.
"""
import glob
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
TOOL = os.path.join(KIT, "tools", "pin_cc_switch_endpoint.py")
sys.path.insert(0, os.path.join(KIT, "tools"))

import pin_cc_switch_endpoint  # noqa: E402  (path set up above)

STEPFUN = "https://api.stepfun.com/step_plan/v1"
SHIM = "http://127.0.0.1:15722/v1"
PROVIDER_ID = "3a20aad7-bc99-4b10-8a72-d7b7dacd2c16"
SIBLING_BASE = "7b1f0c64-2f3d-4a19-9e58"
MINIMAX = "https://api.minimaxi.com/v1"

SCHEMA = """
CREATE TABLE providers (
    id TEXT NOT NULL,
    app_type TEXT NOT NULL,
    name TEXT NOT NULL,
    settings_config TEXT NOT NULL,
    website TEXT,
    notes TEXT,
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


def _settings_config(base_url=STEPFUN, provider="custom"):
    """The shape CC Switch really stores: JSON whose "config" value is TOML.

    json.dumps escapes the quotes the TOML carries, which is what the live
    database holds, so the embedded base_url arrives as base_url = \\"...\\".
    A pattern that does not allow those backslashes matches nothing at all,
    which is the bug this spelling exists to guard against.
    """
    config = (
        'model_provider = "%s"\n'
        'model = "stepfun/step-5-preview"\n'
        '\n'
        '[model_providers.%s]\n'
        'name = "custom"\n'
        'base_url = "%s"\n'
        'wire_api = "responses"\n'
    ) % (provider, provider, base_url)
    return json.dumps({"name": "StepFun", "config": config, "official": True})


def _db(path, provider="StepFun", app_type="codex", endpoint=STEPFUN,
        embedded=STEPFUN, siblings=()):
    """Build a stand-in for ~/.cc-switch/cc-switch.db.

    siblings is a list of (name, app_type, url) triples written after the
    StepFun row, so a test can prove the pin moved exactly one row.
    """
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "insert into providers (id, app_type, name, settings_config)"
        " values (?, ?, ?, ?)",
        (PROVIDER_ID, app_type, provider, _settings_config(embedded)))
    if endpoint is not None:
        conn.execute(
            "insert into provider_endpoints (provider_id, app_type, url,"
            " added_at) values (?, ?, ?, ?)",
            (PROVIDER_ID, app_type, endpoint, int(time.time())))
    for name, sib_app, sib_url in siblings:
        conn.execute(
            "insert into providers (id, app_type, name, settings_config)"
            " values (?, ?, ?, ?)",
            (SIBLING_BASE + sib_app, sib_app, name, _settings_config(sib_url)))
        conn.execute(
            "insert into provider_endpoints (provider_id, app_type, url,"
            " added_at) values (?, ?, ?, ?)",
            (SIBLING_BASE + sib_app, sib_app, sib_url, int(time.time())))
    conn.commit()
    conn.close()


def _load(path):
    conn = sqlite3.connect(path)
    try:
        endpoints = conn.execute(
            "select provider_id, app_type, url from provider_endpoints"
            " order by id").fetchall()
        settings = conn.execute(
            "select id, app_type, settings_config from providers"
            " order by id").fetchall()
    finally:
        conn.close()
    return endpoints, settings


def _backups(directory):
    """Every backup this feature has ever taken inside one directory."""
    return glob.glob(os.path.join(
        directory, pin_cc_switch_endpoint.BACKUP_PREFIX + "*"))


class PinDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "cc-switch.db")

    def _pin(self, **kw):
        kw.setdefault("shim_base", SHIM)
        return pin_cc_switch_endpoint.pin_once(self.db, **kw)

    def _backups(self):
        return _backups(self.tmp.name)

    def test_both_the_endpoint_row_and_the_embedded_config_move(self):
        _db(self.db)
        changed, detail = self._pin()
        self.assertTrue(changed, detail)
        self.assertEqual(
            detail.split(" [backup")[0],
            "repointed StepFun/codex provider_endpoints: %s -> %s"
            " (1 endpoint row, 1 embedded config)" % (STEPFUN, SHIM))
        endpoints, settings = _load(self.db)
        self.assertIn((PROVIDER_ID, "codex", SHIM), endpoints,
                      "the endpoint row CC Switch forwards to was not moved")
        base = pin_cc_switch_endpoint._config_target(settings[0][2])
        self.assertEqual(
            base, SHIM,
            "the base_url embedded in settings_config is still %s, so Codex"
            " boots at the old address after CC Switch restarts" % base)

    def test_a_second_run_reports_no_change(self):
        _db(self.db)
        first, detail = self._pin()
        self.assertTrue(first, detail)
        again, detail = self._pin()
        self.assertFalse(again, "the second pin rewrote a live database")
        self.assertIn("no change", detail)
        self.assertIn(SHIM, detail)

    def test_a_provider_pointing_elsewhere_is_left_alone(self):
        elsewhere = "https://api.openai.com/v1"
        _db(self.db, endpoint=elsewhere, embedded=elsewhere)
        changed, detail = self._pin()
        self.assertFalse(changed)
        self.assertIn("left alone", detail)
        endpoints, settings = _load(self.db)
        self.assertIn((PROVIDER_ID, "codex", elsewhere), endpoints)
        self.assertEqual(
            pin_cc_switch_endpoint._config_target(settings[0][2]), elsewhere)

    def test_an_already_pinned_database_reports_no_change(self):
        _db(self.db, endpoint=SHIM, embedded=SHIM)
        changed, detail = self._pin()
        self.assertFalse(changed)
        self.assertIn("no change", detail)

    def test_only_the_named_provider_and_app_type_move(self):
        """The same name exists for claude, and for a rival codex provider."""
        _db(self.db, siblings=[("StepFun", "claude", STEPFUN),
                               ("MiniMax", "codex", STEPFUN)])
        changed, detail = self._pin()
        self.assertTrue(changed, detail)
        endpoints, _ = _load(self.db)
        moved = [url for provider_id, _app, url in endpoints
                 if provider_id == PROVIDER_ID]
        self.assertEqual(moved, [SHIM])
        for provider_id, app_type, url in endpoints:
            if provider_id != PROVIDER_ID:
                self.assertEqual(
                    url, STEPFUN,
                    "%s/%s was rewritten too" % (provider_id, app_type))

    def test_a_database_without_the_provider_is_a_noop(self):
        _db(self.db, provider="MiniMax")
        changed, detail = self._pin()
        self.assertFalse(changed)
        self.assertIn("no StepFun provider for app_type=codex", detail)
        self.assertEqual(self._backups(), [],
                         "a no-op pin still wrote a backup")

    def test_a_missing_database_is_not_an_error(self):
        changed, detail = self._pin()
        self.assertFalse(changed)
        self.assertEqual(detail,
                         "cc-switch db not found: %s"
                         % os.path.abspath(self.db))

    def test_no_shim_base_url_is_refused(self):
        _db(self.db)
        changed, detail = self._pin(shim_base="")
        self.assertFalse(changed)
        self.assertEqual(detail, "no shim base url given")

    def test_a_rowless_endpoint_table_is_filled_from_the_config(self):
        """Databases written before provider_endpoints existed still move."""
        _db(self.db, endpoint=None, embedded=STEPFUN)
        changed, detail = self._pin()
        self.assertTrue(changed, detail)
        self.assertIn("providers.settings_config", detail)
        endpoints, settings = _load(self.db)
        self.assertEqual(
            endpoints, [(PROVIDER_ID, "codex", SHIM)],
            "the pin did not insert the endpoint row it reads")
        self.assertEqual(
            pin_cc_switch_endpoint._config_target(settings[0][2]), SHIM)

    def test_the_backup_is_taken_once(self):
        _db(self.db)
        self.assertTrue(self._pin()[0])
        self.assertEqual(len(self._backups()), 1,
                         "the first pin wrote no backup of a live database")
        again, _detail = self._pin()
        self.assertFalse(again)
        self.assertEqual(
            len(self._backups()), 1,
            "a timer calling this every 300s filled the disk with 55MB backups")

    def test_no_backup_when_asked(self):
        _db(self.db)
        self.assertTrue(self._pin(backup_db=False)[0])
        self.assertEqual(self._backups(), [])

    def test_the_backup_holds_the_database_before_the_first_write(self):
        """The runbook's rollback section restores this file, so a copy taken

        after the commit would replay the pin instead of undoing it.
        """
        _db(self.db)
        self.assertTrue(self._pin()[0])
        backups = self._backups()
        self.assertEqual(len(backups), 1, backups)
        conn = sqlite3.connect(backups[0])
        try:
            row = conn.execute(
                "select url from provider_endpoints").fetchone()
        finally:
            conn.close()
        self.assertEqual(
            row[0], STEPFUN,
            "the backup holds the post-write database; restoring it would keep"
            " the provider pointed at the shim")

    def test_a_provider_with_no_target_at_all_is_a_detail(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(SCHEMA)
            conn.execute(
                "insert into providers (id, app_type, name, settings_config)"
                " values (?, ?, ?, ?)",
                (PROVIDER_ID, "codex", "StepFun",
                 json.dumps({"config": 'model_provider = "custom"\n'})))
            conn.commit()
        finally:
            conn.close()
        changed, detail = self._pin()
        self.assertFalse(changed, detail)
        self.assertIn("no target to repoint", detail)

    def test_a_pin_reports_failure_rather_than_raising(self):
        """A timer must be able to log and carry on after a locked database.

        The lock is released by rollback in the finally, so an assertion here
        also proves the pin did not leave a half-open transaction behind. What
        the detail says is not the contract: a locked read and a genuinely
        missing provider both come back as a sentence the caller can log, and
        neither may raise.
        """
        _db(self.db)
        holder = sqlite3.connect(self.db)
        holder.execute("begin exclusive")
        try:
            changed, detail = self._pin(backup_db=False)
        finally:
            holder.rollback()
            holder.close()
        self.assertFalse(changed)
        self.assertTrue(detail)
        endpoints, settings = _load(self.db)
        self.assertIn((PROVIDER_ID, "codex", STEPFUN), endpoints,
                      "a locked pin still moved a row")
        self.assertEqual(
            pin_cc_switch_endpoint._config_target(settings[0][2]), STEPFUN)


class LooksLikeStepfun(unittest.TestCase):
    def test_the_upstream_and_the_shim_are_allowed(self):
        self.assertTrue(pin_cc_switch_endpoint.looks_like_stepfun(STEPFUN))
        self.assertTrue(pin_cc_switch_endpoint.looks_like_stepfun(SHIM))
        self.assertTrue(pin_cc_switch_endpoint.looks_like_stepfun(
            "http://localhost:15722/v1"))

    def test_anything_else_is_somebody_elses_decision(self):
        for target in (None, "", "   ", "https://api.openai.com/v1",
                       "http://127.0.0.1:15721/v1",
                       "http://127.0.0.1:9999/v1",
                       "https://api.deepseek.com/v1",
                       "file:///etc/passwd"):
            self.assertFalse(
                pin_cc_switch_endpoint.looks_like_stepfun(target),
                "%r was repointable" % (target,))


class ReadTarget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "cc-switch.db")

    def test_the_endpoint_row_wins_when_it_is_there(self):
        _db(self.db)
        self.assertEqual(pin_cc_switch_endpoint.read_target(self.db),
                         (STEPFUN, "provider_endpoints"))

    def test_the_embedded_config_is_the_fallback(self):
        _db(self.db, endpoint=None, embedded=STEPFUN)
        self.assertEqual(pin_cc_switch_endpoint.read_target(self.db),
                         (STEPFUN, "providers.settings_config"))

    def test_a_missing_database_reports_why(self):
        target, source = pin_cc_switch_endpoint.read_target(
            os.path.join(self.tmp.name, "absent.db"))
        self.assertIsNone(target)
        self.assertIn("cc-switch db not found", source)

    def test_the_shim_url_is_spelled_the_way_both_apps_expect(self):
        self.assertEqual(pin_cc_switch_endpoint.shim_base_url(),
                         "http://127.0.0.1:15722/v1")
        self.assertEqual(pin_cc_switch_endpoint.shim_base_url("localhost"),
                         "http://localhost:15722/v1")


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "cc-switch.db")

    def _run(self, *extra):
        return subprocess.run([sys.executable, TOOL, "--db", self.db] +
                              list(extra), capture_output=True, text=True,
                              timeout=60)

    def test_a_dry_run_plans_and_writes_nothing(self):
        _db(self.db)
        before = _load(self.db)
        proc = self._run("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("would write it to %s" % SHIM, proc.stdout)
        self.assertEqual(_load(self.db), before,
                         "a dry run wrote to a live database")
        self.assertEqual(_backups(self.tmp.name), [])

    def test_a_real_run_reports_what_it_did(self):
        _db(self.db)
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("repointed StepFun/codex", proc.stdout)
        self.assertIn(SHIM, proc.stdout)

    def test_a_real_run_is_idempotent_on_the_command_line(self):
        _db(self.db)
        self._run()
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("no change", proc.stdout)

    def test_a_missing_database_still_exits_zero(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("cc-switch db not found", proc.stdout)


if __name__ == "__main__":
    unittest.main()


class SweepEveryStepfunRow(unittest.TestCase):
    # --all-stepfun. The measured bypass, on this machine's real database
    # 2026-10-01: codex carries two providers aimed at
    # api.stepfun.com/step_plan/v1, one named StepFun (pinned) and one named
    # nv spark (not). Selecting the second in the CC Switch UI forwards
    # straight to StepFun, the same 71 images come back as the same 400, and
    # the pin on the first row has nothing to say about it. The sweep is keyed
    # on the target a request actually reaches, never on the provider's
    # display name, so a stale label cannot buy a bypass; and the allow-list is
    # unchanged, so a provider forwarding anywhere else still survives.

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "cc-switch.db")

    def _backups(self):
        return glob.glob(os.path.join(
            self.tmp.name, pin_cc_switch_endpoint.BACKUP_PREFIX + "*"))

    def _pin(self, **kw):
        kw.setdefault("sweep", True)
        return pin_cc_switch_endpoint.pin_once(self.db, SHIM, **kw)

    def test_a_second_codex_row_aimed_at_stepfun_is_capped_too(self):
        _db(self.db, siblings=[("nv spark", "codex", STEPFUN)])
        changed, detail = self._pin()
        self.assertTrue(changed, detail)
        self.assertEqual(detail.count("repointed"), 2, detail)
        endpoints, _ = _load(self.db)
        self.assertEqual(sorted(url for _pid, _at, url in endpoints),
                         [SHIM, SHIM],
                         "one StepFun row was left pointing straight upstream")

    def test_the_same_name_under_a_different_app_is_left_alone(self):
        _db(self.db, siblings=[("StepFun", "claude", STEPFUN)])
        changed, detail = self._pin()
        self.assertTrue(changed, detail)
        by_row = {(pid, at): url for pid, at, url in _load(self.db)[0]}
        self.assertEqual(by_row[(SIBLING_BASE + "claude", "claude")], STEPFUN,
                         "a claude provider was rewritten by a codex sweep")

    def test_a_provider_forwarding_elsewhere_survives_the_sweep(self):
        _db(self.db, siblings=[("MiniMax", "codex", MINIMAX)])
        changed, detail = self._pin()
        self.assertTrue(changed, detail)
        self.assertEqual(detail.count("repointed"), 1, detail)
        endpoints, _ = _load(self.db)
        self.assertIn((SIBLING_BASE + "codex", "codex", MINIMAX), endpoints,
                      "a provider forwarding elsewhere was rewritten")

    def test_a_database_with_nothing_pointing_at_stepfun_is_a_noop(self):
        _db(self.db, provider="MiniMax", endpoint=MINIMAX, embedded=MINIMAX)
        changed, detail = self._pin()
        self.assertFalse(changed)
        self.assertIn("no StepFun provider for app_type=codex", detail)
        self.assertEqual(self._backups(), [], "a no-op sweep still wrote a backup")

    def test_a_sweep_of_an_already_pinned_database_reports_no_change(self):
        _db(self.db, endpoint=SHIM, embedded=SHIM)
        changed, detail = self._pin()
        self.assertFalse(changed)
        self.assertIn("no change", detail)

    def test_the_default_still_touches_only_the_named_provider(self):
        # The narrow default is the operator-facing promise; the sweep is opt-in.
        _db(self.db, siblings=[("nv spark", "codex", STEPFUN)])
        changed, detail = pin_cc_switch_endpoint.pin_once(self.db, SHIM)
        self.assertTrue(changed, detail)
        endpoints, _ = _load(self.db)
        self.assertIn((PROVIDER_ID, "codex", SHIM), endpoints)
        self.assertIn((SIBLING_BASE + "codex", "codex", STEPFUN), endpoints,
                      "the default run swept a row it was not asked about")

    def test_the_command_line_reports_every_row_it_would_move(self):
        _db(self.db, siblings=[("nv spark", "codex", STEPFUN)])
        proc = subprocess.run([sys.executable, TOOL, "--db", self.db,
                               "--dry-run", "--all-stepfun"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertEqual(proc.stdout.count("would write it to"), 2, proc.stdout)
