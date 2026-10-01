"""The default-model guard must rescue a dead default and refuse to fake one.

The incident behind this tool (2026-10-01): fleet.env still carried
FLEET_DEFAULT_MODEL=trae/trae-step-5-preview after that route died 401 -> 502,
so every setup re-pinned a model that could not answer. The pin's gating
already refuses to name a provider the installer skipped; what was missing
is a check that the pinned model's own route still answers, and a jump back
to the stepfun harbor when it does not.

These tests pin the contracts the guarantee rests on:

  * the probe targets the route the route actually serves -- the catalog
    slug's provider prefix is stripped, because the Plan API and every
    bridge name their models bare and a prefixed id 404s upstream;
  * a dead non-harbor default is re-pinned to the harbor and the rest of
    config.toml survives byte for byte;
  * a dead harbor is reported and the config is left alone: there is
    nowhere to jump back to, and rewriting it would only churn;
  * the bare step-5-preview spelling (CC Switch's own template) is the
    harbor too, so the guard does not "rescue" a healthy file into a
    cosmetic rewrite;
  * --dry-run reports the jump without performing it;
  * a dying fleet.env override is reported even when the live default is
    healthy, because setup re-pins it on the next run;
  * CC Switch's failover flags are checked read-only and a drift fails the
    run with the repair printed -- the guard never writes another app's
    database.
"""
import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(KIT, "tools"))

import default_model_guard as guard  # noqa: E402  (path set up above)


def write_config(path, model=None):
    lines = ['model_provider = "custom"']
    if model is not None:
        lines.append('model = "%s"' % model)
    lines.append('model_reasoning_effort = "high"')
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def write_env(path, **pairs):
    with open(path, "w", encoding="utf-8") as fh:
        for key, value in pairs.items():
            fh.write("%s=%s\n" % (key, value))


def build_cc_db(path, auto_failover=1, stepfun_queue=1, stepfun_row=True):
    conn = sqlite3.connect(path)
    conn.execute("create table proxy_config (app_type text,"
                 " auto_failover_enabled integer)")
    conn.execute("create table providers (id text, app_type text,"
                 " name text, in_failover_queue integer)")
    conn.execute("insert into proxy_config values ('codex', ?)",
                 (auto_failover,))
    if stepfun_row:
        conn.execute("insert into providers values"
                     " ('3a20aad7', 'codex', 'StepFun', ?)",
                     (stepfun_queue,))
    conn.commit()
    conn.close()


def run_guard(argv):
    """Run main() with a stubbed probe; returns (exit_code, stdout)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = guard.main(argv)
    return code, out.getvalue()


class RouteForTest(unittest.TestCase):
    """The probe model is the part after the provider prefix, or nothing works."""

    def test_stepfun_slug_probes_the_shim_with_the_bare_model(self):
        url, key, model, detail = guard.route_for(
            "stepfun/step-5-preview", {"STEPFUN_PLAN_API_KEY": "sk-x"})
        self.assertIn("15722", url)
        self.assertEqual(model, "step-5-preview")
        self.assertEqual(key, "sk-x")

    def test_bridge_slug_probes_its_own_port_with_the_bare_model(self):
        url, key, model, detail = guard.route_for(
            "workbuddy/hy4-preview", {"CODEBUDDY2OPENAI_KEY": "k"})
        self.assertIn("8787", url)
        self.assertEqual(model, "hy4-preview")

    def test_double_slash_slug_keeps_the_rest_of_the_model(self):
        # trae models are named trae-* on the bridge; only the provider
        # prefix comes off.
        url, key, model, detail = guard.route_for(
            "trae/trae-step-5-preview", {"TRAE2CODEX_KEY": "k"})
        self.assertIn("8791", url)
        self.assertEqual(model, "trae-step-5-preview")

    def test_unknown_provider_is_refused(self):
        url, key, model, detail = guard.route_for("nosuch/thing", {})
        self.assertIsNone(url)
        self.assertIn("no local route", detail)

    def test_missing_bridge_key_is_refused(self):
        url, key, model, detail = guard.route_for("qwen/qwen3.8-max", {})
        self.assertIsNone(url)
        self.assertIn("QWEN2CODEX_KEY", detail)


class JumpTest(unittest.TestCase):
    """A dead non-harbor default is re-pinned; a dead harbor is only reported."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = os.path.join(self.tmp.name, "config.toml")
        self.env = os.path.join(self.tmp.name, "fleet.env")
        pairs = {"STEPFUN_PLAN_API_KEY": "sk-x",
                 "FLEET_DEFAULT_MODEL": guard.HARBOR}
        # every bridge key, so route_for() resolves instead of refusing the
        # slug for a missing key before the stubbed probe ever runs
        for key_name in guard.KEY_ENV.values():
            pairs.setdefault(key_name, "k")
        write_env(self.env, **pairs)
        self.cc_db = os.path.join(self.tmp.name, "cc-switch.db")
        build_cc_db(self.cc_db)

    def argv(self, *extra):
        return ["--config", self.config, "--env-file", self.env,
                "--cc-db", self.cc_db] + list(extra)

    def test_dead_default_jumps_back_to_the_harbor(self):
        write_config(self.config, model="qwen/qwen3.8-max")
        with mock.patch.object(guard, "probe",
                               return_value=(False, "HTTP 503 no key")):
            code, out = run_guard(self.argv())
        self.assertEqual(code, 0, out)
        self.assertIn("jumped back", out)
        self.assertIn("stepfun/step-5-preview", out)
        with open(self.config, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn('model = "stepfun/step-5-preview"', body)
        self.assertIn('model_provider = "custom"', body)
        self.assertIn('model_reasoning_effort = "high"', body)

    def test_dead_harbor_is_reported_without_writing(self):
        write_config(self.config, model=guard.HARBOR)
        before = open(self.config, encoding="utf-8").read()
        with mock.patch.object(guard, "probe",
                               return_value=(False, "HTTP 401 bad key")):
            code, out = run_guard(self.argv())
        self.assertEqual(code, 1, out)
        self.assertIn("HARBOR DEAD", out)
        self.assertEqual(open(self.config, encoding="utf-8").read(), before)

    def test_bare_harbor_spelling_is_not_rescued(self):
        # CC Switch's template pins the bare name; it is the same route.
        write_config(self.config, model="step-5-preview")
        before = open(self.config, encoding="utf-8").read()
        with mock.patch.object(guard, "probe",
                               return_value=(False, "HTTP 401 bad key")):
            code, out = run_guard(self.argv())
        self.assertEqual(code, 1, out)
        self.assertIn("HARBOR DEAD", out)
        self.assertEqual(open(self.config, encoding="utf-8").read(), before)

    def test_dry_run_reports_the_jump_without_performing_it(self):
        write_config(self.config, model="qwen/qwen3.8-max")
        before = open(self.config, encoding="utf-8").read()
        with mock.patch.object(guard, "probe",
                               return_value=(False, "HTTP 503 no key")):
            code, out = run_guard(self.argv("--dry-run"))
        self.assertEqual(code, 0, out)
        self.assertIn("would jump", out)
        self.assertEqual(open(self.config, encoding="utf-8").read(), before)

    def test_healthy_default_is_left_alone(self):
        write_config(self.config, model="step-5-preview")
        before = open(self.config, encoding="utf-8").read()
        with mock.patch.object(guard, "probe",
                               return_value=(True, "E2E_OK")):
            code, out = run_guard(self.argv())
        self.assertEqual(code, 0, out)
        self.assertIn("answers via", out)
        self.assertEqual(open(self.config, encoding="utf-8").read(), before)

    def test_dead_env_override_is_reported_even_when_live_is_healthy(self):
        write_config(self.config, model="step-5-preview")
        pairs = {"STEPFUN_PLAN_API_KEY": "sk-x",
                 "FLEET_DEFAULT_MODEL": "trae/trae-step-5-preview"}
        for key_name in guard.KEY_ENV.values():
            pairs.setdefault(key_name, "k")
        write_env(self.env, **pairs)

        def fake_probe(url, key, model, timeout=25.0):
            # the live default is healthy, the override's route is not
            return (model != "trae-step-5-preview"), "stub"

        with mock.patch.object(guard, "probe", side_effect=fake_probe):
            code, out = run_guard(self.argv())
        self.assertEqual(code, 0, out)
        self.assertIn("WARNING: fleet.env override trae/trae-step-5-preview"
                      " is dead", out)
        self.assertIn("set FLEET_DEFAULT_MODEL=stepfun/step-5-preview", out)


class CcFailoverCheckTest(unittest.TestCase):
    """CC Switch's failover flags are read, never written."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cc_db = os.path.join(self.tmp.name, "cc-switch.db")
        self.config = os.path.join(self.tmp.name, "config.toml")
        write_config(self.config, model="step-5-preview")
        self.env = os.path.join(self.tmp.name, "fleet.env")
        write_env(self.env, STEPFUN_PLAN_API_KEY="sk-x")

    def argv(self):
        return ["--config", self.config, "--env-file", self.env,
                "--cc-db", self.cc_db]

    def test_intact_flags_pass(self):
        build_cc_db(self.cc_db)
        with mock.patch.object(guard, "probe",
                               return_value=(True, "E2E_OK")):
            code, out = run_guard(self.argv())
        self.assertEqual(code, 0, out)
        self.assertIn("auto failover on for codex", out)
        self.assertIn("StepFun sits in the codex failover queue", out)

    def test_failover_off_is_drift_with_a_repair(self):
        build_cc_db(self.cc_db, auto_failover=0)
        with mock.patch.object(guard, "probe",
                               return_value=(True, "E2E_OK")):
            code, out = run_guard(self.argv())
        self.assertEqual(code, 1, out)
        self.assertIn("DRIFT: auto_failover_enabled=0", out)
        self.assertIn("repair: update proxy_config", out)

    def test_harbor_out_of_the_queue_is_drift(self):
        build_cc_db(self.cc_db, stepfun_queue=0)
        with mock.patch.object(guard, "probe",
                               return_value=(True, "E2E_OK")):
            code, out = run_guard(self.argv())
        self.assertEqual(code, 1, out)
        self.assertIn("DRIFT: StepFun in_failover_queue=0", out)
        self.assertIn("update providers set in_failover_queue=1", out)

    def test_missing_harbor_provider_is_drift(self):
        build_cc_db(self.cc_db, stepfun_row=False)
        with mock.patch.object(guard, "probe",
                               return_value=(True, "E2E_OK")):
            code, out = run_guard(self.argv())
        self.assertEqual(code, 1, out)
        self.assertIn("no StepFun provider for codex", out)

    def test_missing_db_is_not_a_failure(self):
        with mock.patch.object(guard, "probe",
                               return_value=(True, "E2E_OK")):
            code, out = run_guard(self.argv())
        self.assertEqual(code, 0, out)
        self.assertIn("unchecked", out)


class PinModelTest(unittest.TestCase):
    """The transform must match setup-providers.sh's inline python exactly."""

    def test_rewrites_the_model_line_and_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            write_config(path, model="qwen/qwen3.8-max")
            guard.pin_model(path, guard.HARBOR)
            body = open(path, encoding="utf-8").read()
        self.assertIn('model = "stepfun/step-5-preview"', body)
        self.assertNotIn("qwen", body)
        self.assertIn('model_reasoning_effort = "high"', body)

    def test_inserts_after_model_provider_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('model_provider = "custom"\n')
                fh.write('model_reasoning_effort = "high"\n')
            guard.pin_model(path, guard.HARBOR)
            lines = open(path, encoding="utf-8").read().splitlines()
        self.assertEqual(lines[0], 'model_provider = "custom"')
        self.assertEqual(lines[1], 'model = "stepfun/step-5-preview"')


if __name__ == "__main__":
    unittest.main()
