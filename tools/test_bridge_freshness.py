"""Bridge freshness: a running process must be newer than the code it loaded.

Incidents covered (all observed on 2026-09-30):
- xhx/usage_ledger.py landed 60s AFTER the xhx process start; the single-file
  own-directory case kept running the old ledger format.
- runtime/bridges/_common.py (upstream guard) landed at 01:26:22 while five
  FastAPI bridges still ran processes started between 18:38 and 22:57, so the
  root-cause-26 protection was live in codely/cline/workbuddy only. Shared
  root modules must be charged to every importer.
- workbuddy-cn/gpt load bridges/workbuddy/core.py through sys.path while their
  command line only names their own directory.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
BASE = datetime(2026, 9, 30, 12, 0, 0).timestamp()


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(KIT, rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bf = _load("bridge_freshness", "tools/bridge_freshness.py")


def _write(root, rel, when, text=""):
    """Create a file with mtime BASE+when (seconds)."""
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.utime(full, (BASE + when, BASE + when))
    return full


def _bridge(home, name, when=0, text=""):
    return _write(home, os.path.join("bridges", name, name + "_bridge.py"),
                  when, text)


def _proc(pid, when, cmd):
    return bf.Proc(pid=pid, started=datetime.fromtimestamp(BASE + when),
                   command=cmd)


def _rows(rows):
    return {row.bridge: row for row in rows}


class BridgeFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_process_newer_than_code_is_ok(self):
        code = _bridge(self.home, "alpha", when=-3600)
        rows = bf.assess(self.home, [_proc(100, 0, "python " + code)])
        self.assertEqual(rows[0].status, "OK")

    def test_code_newer_than_process_is_stale(self):
        """xhx/usage_ledger.py: code 60s newer than the process."""
        code = _bridge(self.home, "xhx", when=60)
        rows = bf.assess(self.home, [_proc(24053, 0, "python " + code)])
        row = _rows(rows)["xhx"]
        self.assertEqual(row.status, "STALE")
        self.assertEqual(row.lag, "1m00s")

    def test_small_lag_within_tolerance_is_ok(self):
        code = _bridge(self.home, "epsilon", when=20)
        rows = bf.assess(self.home, [_proc(5, 0, "python " + code)],
                         tolerance=30.0)
        self.assertEqual(rows[0].status, "OK")

    def test_shared_module_stales_every_importer(self):
        """_common.py newer than five importers; restarted bridges stay OK."""
        late = ["qoder", "qwen", "lingxi", "trae", "zcode"]
        for name in late:
            _bridge(self.home, name, when=-100000,
                    text="import _common\n")
        codely = _bridge(self.home, "codely", when=-60)
        _write(self.home, "bridges/_common.py", 7200, "def guard():\n")
        procs = []
        for i, name in enumerate(late):
            cmd = "python " + os.path.join(self.home, "bridges", name,
                                          name + "_bridge.py")
            procs.append(_proc(50000 + i, 0, cmd))
        procs.append(_proc(1246, 0, "python " + codely))
        rows = _rows(bf.assess(self.home, procs))
        for name in late:
            self.assertEqual(rows[name].status, "STALE", name)
            self.assertIn("_common.py (shared)", rows[name].note)
        self.assertEqual(rows["codely"].status, "OK")
        self.assertEqual(rows["codely"].note, "codely_bridge.py")

    def test_shared_module_does_not_stale_non_importer(self):
        """A bridge that never imports the shared module is unaffected."""
        code = _bridge(self.home, "catpaw", when=-60)
        _write(self.home, "bridges/_common.py", 7200, "def guard():\n")
        rows = bf.assess(self.home, [_proc(2074, 0, "python " + code)])
        self.assertEqual(rows[0].status, "OK")
        self.assertEqual(rows[0].note, "catpaw_bridge.py")

    def test_sibling_directory_code_counts_via_sys_path(self):
        """workbuddy-cn loads bridges/workbuddy/core.py via sys.path."""
        _write(self.home, "bridges/workbuddy/core.py", 7200,
               "def main():\n    pass\n")
        converter = _write(
            self.home, "bridges/workbuddy-cn/converter.py", -100,
            "_HERE = os.path.dirname(os.path.abspath(__file__))\n"
            "_SHARED = os.path.join(os.path.dirname(_HERE), \"workbuddy\")\n"
            "sys.path.insert(0, _SHARED)\n"
            "from core import main\n",
        )
        rows = _rows(bf.assess(
            self.home, [_proc(1251, 0, "python " + converter + " --port 8787")]
        ))
        row = rows["workbuddy-cn"]
        self.assertEqual(row.status, "STALE")
        self.assertIn("workbuddy/core.py (sibling)", row.note)

    def test_pycache_auths_and_version_are_not_code(self):
        code = _bridge(self.home, "beta", when=-60)
        _write(self.home, "bridges/beta/__pycache__/stale.py", 9000, "junk\n")
        _write(self.home, "bridges/beta/auths/token.json", 9000, "{}")
        _write(self.home, "bridges/beta/VERSION", 9000, "1")
        rows = bf.assess(self.home, [_proc(7, 0, "python " + code)])
        self.assertEqual(rows[0].status, "OK")
        self.assertEqual(rows[0].note, "beta_bridge.py")

    def test_bridge_without_process_is_info_not_failure(self):
        _bridge(self.home, "lonely", when=-60)
        rows = _rows(bf.assess(self.home, []))
        self.assertEqual(rows["lonely"].status, "INFO")
        self.assertEqual(rows["lonely"].note, "no process")
        self.assertEqual(bf.run(home=self.home, procs=[]), 0)

    def test_run_returns_1_on_stale_and_0_when_fresh(self):
        stale = _bridge(self.home, "gamma", when=60)
        code = _bridge(self.home, "delta", when=-60)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = bf.run(home=self.home,
                        procs=[_proc(9, 0, "python " + stale),
                               _proc(10, 0, "python " + code)])
        self.assertEqual(rc, 1)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = bf.run(home=self.home,
                        procs=[_proc(11, 0, "python " + code)])
        self.assertEqual(rc, 0)

    def test_json_output_reports_home_and_stale(self):
        code = _bridge(self.home, "zeta", when=60)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bf.run(home=self.home,
                        procs=[_proc(12, 0, "python " + code)],
                        as_json=True)
        payload = json.loads(buf.getvalue())
        self.assertEqual(rc, 1)
        self.assertEqual(payload["home"], self.home)
        self.assertEqual(payload["stale"], ["zeta"])
        self.assertEqual(payload["rows"][0]["status"], "STALE")

    def test_parse_ps_reads_lstart_and_skips_bad_lines(self):
        text = ("1246 Wed Sep 30 01:26:34 2026 Python /x/bridges/codely/"
                "codely_bridge.py --port 8790\n"
                "garbage line\n")
        procs = bf.parse_ps(text)
        self.assertEqual(len(procs), 1)
        self.assertEqual(procs[0].pid, 1246)
        self.assertEqual(procs[0].started, datetime(2026, 9, 30, 1, 26, 34))
        self.assertTrue(procs[0].command.endswith("--port 8790"))

    def test_resolve_home_picks_the_tree_running_the_bridges(self):
        runtime_home = os.path.join(self.home, "runtime")
        _bridge(runtime_home, "qoder", when=-60)
        code = os.path.join(runtime_home, "bridges", "qoder",
                            "qoder_bridge.py")
        proc = _proc(3, 0, "python " + code + " --port 8789")
        tools_dir = os.path.join(runtime_home, "tools")
        self.assertEqual(bf.resolve_home(tools_dir, [proc]), runtime_home)

    def test_lag_text_formats_hours_minutes_seconds(self):
        self.assertEqual(bf._lag_text(25740), "7h09m")
        self.assertEqual(bf._lag_text(95), "1m35s")
        self.assertEqual(bf._lag_text(48), "48s")
        self.assertEqual(bf._lag_text(-30), "-30s")

    def test_render_summarises_stale_bridges(self):
        code = _bridge(self.home, "eta", when=-60)
        rows = bf.assess(self.home, [_proc(15, 0, "python " + code)])
        out = bf.render(rows)
        self.assertIn("BRIDGE", out)
        self.assertTrue(out.rstrip().endswith("stale: none"))


if __name__ == "__main__":
    unittest.main()
