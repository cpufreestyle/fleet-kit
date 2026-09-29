"""catalog_filter.py layout guard, verdict staleness, and the native probe.

Measured 2026-09-29 on the live install: the round-trip proof demanded
json.dumps(..., indent=2) + "\\n" while the only writer in this chain
(catalog_sort.write_json) emits indent=1 with no trailing newline, so every
write was refused with exit 5 and no message at all. The wrapper then read the
status of its own negated test (`if ! cmd; then rc=$?`) -- always 0 -- and
logged "ok:" with an empty summary, so a filter that had never filtered
anything looked healthy for days.

Two more live facts drive the other tests here: the verdict snapshot was three
days old (the prover runs on demand, so that is normal, not an incident), and
the native-pool probe sent `"input": "ping"` without stream=true, which the
gateway rejects with 400 before it ever reaches the account pool -- so the 401
the probe exists to detect could never be observed.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location(
    "catalog_filter", os.path.join(HERE, "catalog_filter.py"))
catalog_filter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog_filter)

WRAPPER = os.path.join(HERE, "catalog-filter.sh")


def _write(path, data, indent=1, tail="", ensure_ascii=False):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=indent, ensure_ascii=ensure_ascii)
                 + tail)
    return path


def _catalog(models):
    return {"models": models}


def _row(slug):
    return {"slug": slug, "priority": 10}


def _summary_of(capsys):
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------- layout ---

def test_layout_guard_accepts_the_layout_the_fleet_actually_writes():
    # catalog_sort.write_json: indent=1, ensure_ascii=False, no trailing
    # newline. This is the on-disk shape that used to be refused.
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        original = _catalog([_row("workbuddy/hy4"), _row("tokendance/glm-5.3")])
        _write(path, original)
        with open(path, encoding="utf-8") as fh:
            before = fh.read()
        summary = {}
        keepers = [_row("workbuddy/hy4")]
        assert catalog_filter.write_catalog(path, original, keepers, summary,
                                            no_backup=True)
        with open(path, encoding="utf-8") as fh:
            after = fh.read()
        assert json.loads(after)["models"] == keepers
        # Same shape as before: one indent level, no trailing newline added.
        assert after == json.dumps(
            {"models": keepers}, indent=1, ensure_ascii=False)
        assert not after.endswith("\n")
        assert after.startswith(before[:12])
        assert "error" not in summary
    finally:
        os.remove(path)


def test_layout_guard_accepts_a_trailing_newline_layout():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        data = _catalog([_row("qoder/Auto"), _row("qoder/MODEL")])
        _write(path, data, indent=2, tail="\n")
        summary = {}
        assert catalog_filter.write_catalog(path, data, [_row("qoder/Auto")],
                                            summary, no_backup=True)
        with open(path, encoding="utf-8") as fh:
            after = fh.read()
        assert after.endswith("\n")
        assert json.loads(after)["models"] == [_row("qoder/Auto")]
    finally:
        os.remove(path)


def test_unknown_layout_is_refused_and_says_why(capsys):
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        data = _catalog([_row("a/x")])
        _write(path, data, indent=5)  # no json.dumps layout in use here
        summary = {}
        assert catalog_filter.write_catalog(path, data, [], summary,
                                            no_backup=True) is False
        assert "layout" in summary["error"]
        err = capsys.readouterr().err
        assert "refusing to rewrite" in err
        # The file is untouched.
        with open(path, encoding="utf-8") as fh:
            assert len(json.load(fh)["models"]) == 1
    finally:
        os.remove(path)


def test_catalog_changed_on_disk_is_refused(capsys):
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        on_disk = _catalog([_row("a/x")])
        _write(path, on_disk)
        summary = {}
        # data is what the run read; the file now holds something else.
        assert catalog_filter.write_catalog(path, _catalog([_row("a/y")]), [],
                                            summary, no_backup=True) is False
        assert "changed on disk" in summary["error"]
        assert "changed on disk" in capsys.readouterr().err
    finally:
        os.remove(path)


def test_detect_layout_covers_the_known_writers():
    for kwargs in ({"indent": 1}, {"indent": 2, "tail": "\n"},
                   {"indent": 4, "tail": "\r\n"},
                   {"indent": 1, "ensure_ascii": True}):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            data = _catalog([_row("qoder/Auto"), "bare-native-row"])
            _write(path, data, **kwargs)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            found = catalog_filter.detect_layout(text, data)
            assert found is not None, kwargs
        finally:
            os.remove(path)


def test_detect_layout_rejects_an_unknown_shape():
    data = _catalog([_row("qoder/Auto")])
    assert catalog_filter.detect_layout(json.dumps(data, indent=5), data) is None
    assert catalog_filter.detect_layout("", data) is None
    assert catalog_filter.detect_layout("{not json", data) is None


# ------------------------------------------------------------- staleness ---

def _age_stamp(seconds_ago, tz_aware=False):
    import datetime
    when = datetime.datetime.now() - datetime.timedelta(seconds=seconds_ago)
    if tz_aware:
        when = when.astimezone()
    return when.isoformat()


def test_verify_age_reads_a_local_stamp():
    # The panel's now_str() writes naive local time.
    age = catalog_filter.verify_age_seconds(
        {"generated_at": _age_stamp(3600)})
    assert age is not None and 3500 < age < 3700


def test_verify_age_reads_an_offset_stamp():
    age = catalog_filter.verify_age_seconds(
        {"generated_at": _age_stamp(3600, tz_aware=True)})
    assert age is not None and 3500 < age < 3700


def test_verify_age_is_none_when_undatable():
    assert catalog_filter.verify_age_seconds({}) is None
    assert catalog_filter.verify_age_seconds({"generated_at": ""}) is None
    assert catalog_filter.verify_age_seconds(
        {"generated_at": "yesterday"}) is None


def _run_filter(argv, status, capsys, junk=True):
    """main() against a stubbed panel, in an isolated codex home."""
    home = tempfile.mkdtemp(prefix="cf-home-")
    catalog = os.path.join(home, "catalog.json")
    with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
        fh.write('model_provider = "custom"\n'
                 'model_catalog_json = "catalog.json"\n')
    rows = [_row("workbuddy/hy4"), _row("qoder/Auto"), _row("gemini/g2")]
    if junk:
        rows.append(_row("tokendance/mimo-v2.5-tts"))
    _write(catalog, _catalog(rows))
    real_get = catalog_filter.fetch_status

    def fake(url, timeout):
        return status

    catalog_filter.fetch_status = fake
    try:
        rc = catalog_filter.main(argv + ["--codex-home", home,
                                         "--catalog", catalog,
                                         "--no-restore",
                                         "--keep-backups", "-1"])
    finally:
        catalog_filter.fetch_status = real_get
    summary = _summary_of(capsys)
    with open(catalog, encoding="utf-8") as fh:
        final = json.load(fh)
    return rc, summary, final


def _status(real, hours_old):
    stamp = (time.time() - hours_old * 3600)
    import datetime
    when = datetime.datetime.fromtimestamp(stamp)
    return {"bridges": [{"name": "workbuddy"}, {"name": "qoder"},
                        {"name": "gemini"}],
            "verify": {"real": list(real),
                       "generated_at": when.isoformat()}}


def test_stale_verdict_skips_the_bridge_filtering(capsys):
    rc, summary, final = _run_filter(
        [], _status(["workbuddy", "qoder"], 72), capsys)
    # A three day old verdict is not allowed to delete rows, but the junk
    # cleanup still runs.
    assert rc == 0
    assert summary["bridge_filter"] == "skipped (stale verdict)"
    assert "too old" in summary["error"]
    slugs = [m["slug"] for m in final["models"]]
    assert "gemini/g2" in slugs          # unverified bridge row survives
    assert "tokendance/mimo-v2.5-tts" not in slugs  # junk row is gone


def test_fresh_verdict_drops_the_unverified_bridges(capsys):
    rc, summary, final = _run_filter(
        [], _status(["workbuddy", "qoder"], 2), capsys)
    assert rc == 0
    assert summary["unavailable"] == ["gemini"]
    slugs = [m["slug"] for m in final["models"]]
    assert "gemini/g2" not in slugs
    assert "workbuddy/hy4" in slugs


def test_no_verified_bridge_at_all_refuses_to_filter(capsys):
    # No junk row here, so the exit code is the whole story.
    rc, summary, final = _run_filter(
        [], _status([], 2), capsys, junk=False)
    assert rc == 4
    assert "no bridge verified REAL" in summary["error"]
    slugs = [m["slug"] for m in final["models"]]
    assert "gemini/g2" in slugs  # nothing is trustable, so nothing is removed


def test_undated_verdict_is_refused(capsys):
    rc, summary, final = _run_filter(
        [], {"bridges": [{"name": "workbuddy"}],
             "verify": {"real": ["workbuddy"]}},
        capsys, junk=False)
    assert rc == 6
    assert "undated" in summary["error"]
    slugs = [m["slug"] for m in final["models"]]
    assert "gemini/g2" in slugs


# ------------------------------------------------------- hidden row state --

def _status_with_counts(counts, real, hours_old=2):
    return {"bridges": [{"name": name, "probe": {"ok": True, "count": count}}
                        for name, count in sorted(counts.items())],
            "verify": {"real": sorted(real),
                       "generated_at": _age_stamp(hours_old * 3600)}}


def test_the_shortfall_the_filter_explains_is_recorded(capsys):
    rc, summary, final = _run_filter(
        [], _status_with_counts({"workbuddy": 3, "gemini": 2}, ["workbuddy"]),
        capsys)
    assert rc == 0
    # gemini's two rows are gone and gemini is not REAL, and workbuddy
    # advertises three models while only one row survived.
    assert summary["slash_rows_hidden"] == 4
    sidecar = os.path.join(os.path.dirname(summary["catalog"]),
                           ".catalog-filter-hidden.json")
    with open(sidecar, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["slash_rows_hidden"] == 4
    assert data["slash_rows_in_catalog"] >= 1
    assert catalog_filter.read_hidden_count(summary["catalog"]) == 4


def test_a_stripped_catalog_reports_no_shortfall():
    home = tempfile.mkdtemp(prefix="cf-hidden-")
    path = os.path.join(home, "catalog.json")
    keepers = [_row("gemini/g2")]     # only an unverified bridge survived
    status = _status_with_counts({"workbuddy": 3, "gemini": 2}, ["workbuddy"])
    # No working bridge has a row, so this is a strip, not a filter: the guard
    # must heal it, which means reporting no explained shortfall at all.
    assert catalog_filter.record_hidden_count(path, keepers, status,
                                              ["workbuddy"]) == 0
    assert catalog_filter.read_hidden_count(path) == 0


def test_rows_the_working_bridges_still_have_are_not_counted():
    home = tempfile.mkdtemp(prefix="cf-hidden-")
    path = os.path.join(home, "catalog.json")
    keepers = [_row("workbuddy/m%d" % i) for i in range(3)]
    status = _status_with_counts({"workbuddy": 3, "gemini": 2}, ["workbuddy"])
    assert catalog_filter.record_hidden_count(path, keepers, status,
                                              ["workbuddy"]) == 2


def test_a_bridge_whose_probe_failed_contributes_nothing():
    home = tempfile.mkdtemp(prefix="cf-hidden-")
    path = os.path.join(home, "catalog.json")
    keepers = [_row("workbuddy/m%d" % i) for i in range(3)]
    status = {"bridges": [{"name": "workbuddy", "probe": {"ok": False,
                                                          "count": 0}},
                          {"name": "gemini", "probe": {"ok": False}}],
              "verify": {"real": ["workbuddy"]}}
    # An unreadable count is not a shortfall: guessing one could stop the
    # guard from healing a catalog that really was stripped.
    assert catalog_filter.record_hidden_count(path, keepers, status,
                                              ["workbuddy"]) == 0


def test_read_hidden_count_survives_a_broken_sidecar():
    home = tempfile.mkdtemp(prefix="cf-hidden-")
    path = os.path.join(home, "catalog.json")
    with open(catalog_filter.hidden_count_path(path), "w",
              encoding="utf-8") as fh:
        fh.write("{not json")
    assert catalog_filter.read_hidden_count(path) == 0


# ------------------------------------------------------------ probe base ---

def test_codex_provider_base_reads_the_configured_provider():
    home = tempfile.mkdtemp(prefix="cf-home-")
    with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
        fh.write('model_provider = "custom"\n'
                 'model = "step-5-preview"\n'
                 '\n'
                 '[model_providers.custom]\n'
                 'base_url = "http://127.0.0.1:15721/v1"\n'
                 'wire_api = "responses"\n'
                 '\n'
                 '[model_providers.other]\n'
                 'base_url = "http://127.0.0.1:10100/v1"\n')
    try:
        assert catalog_filter.codex_provider_base(home) == "http://127.0.0.1:15721"
    finally:
        for name in os.listdir(home):
            os.remove(os.path.join(home, name))
        os.rmdir(home)


def test_codex_provider_base_is_none_without_a_provider():
    home = tempfile.mkdtemp(prefix="cf-home-")
    with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
        fh.write('model = "step-5-preview"\n')
    try:
        assert catalog_filter.codex_provider_base(home) is None
    finally:
        os.remove(os.path.join(home, "config.toml"))
        os.rmdir(home)
    assert catalog_filter.codex_provider_base(os.path.join(home, "gone")) is None


# ----------------------------------------------------------- native probe ---

class _ProbeHandler:
    """Answers the shape the gateway really has, and 404s what it lacks."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    body = {}
                if not isinstance(body.get("input"), list) \
                        or body.get("stream") is not True:
                    outer.shape_errors.append(body.get("model"))
                    outer.send_error(400, "Input must be a list")
                    return
                model = body.get("model")
                if model == "ghost":
                    self.send_error(404, "model_not_found")
                    return
                if model == "deadpool":
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(
                        {"error": "OpenAI account pool has no usable "
                                  "account credential"}).encode())
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"data: {}\n\n")

        self.Handler = Handler
        self.shape_errors = []

    def start(self):
        from http.server import HTTPServer
        import threading
        self.server = HTTPServer(("127.0.0.1", 0), self.Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.port = self.server.server_address[1]
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port


@pytest.fixture
def probe_server():
    server = _ProbeHandler().start()
    try:
        yield server
    finally:
        server.stop()


def test_native_probe_sends_a_well_formed_request(probe_server):
    ok, detail = catalog_filter.probe_native_pool(
        probe_server.base, 10, ["gpt-5.5"])
    assert ok is True
    assert "HTTP 200" in detail
    # Nothing was rejected for its shape.
    assert probe_server.shape_errors == []


def test_native_probe_skips_a_model_the_gateway_lacks(probe_server):
    ok, detail = catalog_filter.probe_native_pool(
        probe_server.base, 10, ["ghost", "gpt-5.5"])
    assert ok is True
    assert "via gpt-5.5" in detail


def test_native_probe_reports_an_empty_pool(probe_server):
    ok, detail = catalog_filter.probe_native_pool(
        probe_server.base, 10, ["ghost", "deadpool"])
    assert ok is False
    assert "no usable credential" in detail


def test_native_probe_treats_no_answer_as_usable():
    ok, detail = catalog_filter.probe_native_pool(
        "http://127.0.0.1:1", 2, ["gpt-5.5"])
    assert ok is True
    # Nothing on port 1 answers, so the run falls back to "usable".
    assert "refused" in detail


def test_native_probe_without_a_base_is_usable():
    assert catalog_filter.probe_native_pool("", 2, ["gpt-5.5"]) == (
        True, "no proxy base configured")


# ------------------------------------------------------------- the wrapper --

def _write_stub(path, code):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("import json,sys\n"
                 "print(json.dumps({'removed': 3, 'models_after': 90,\n"
                 "                   'unavailable': ['gemini']}))\n")
    os.chmod(path, 0o644)
    return path


def _run_wrapper(logdir, tmpdir, stub_code=None):
    env = dict(os.environ)
    env["CATALOG_FILTER_LOG"] = os.path.join(logdir, "filter.log")
    env["TMPDIR"] = tmpdir
    env["CODEX_HOME"] = os.path.join(tmpdir, "no-codex-home")
    if stub_code is None:
        argv = [WRAPPER, "run", "--dry-run", "--no-hide-native"] \
            if os.path.exists(os.path.join(HERE, "catalog_filter.py")) else None
        argv = [WRAPPER, "run", "--dry-run"]
    stub = os.path.join(tmpdir, "stub_filter.py")
    if stub_code is not None:
        _write_stub(stub, stub_code)
        env["CATALOG_FILTER_PY"] = stub
        argv = [WRAPPER, "run"]
    proc = subprocess.run(["/bin/bash"] + argv, env=env,
                          capture_output=True, text=True, timeout=120)
    log = os.path.join(logdir, "filter.log")
    text = open(log, encoding="utf-8").read() if os.path.exists(log) else ""
    return proc.returncode, text


def test_wrapper_logs_a_failing_filter_as_a_failure(capsys):
    logdir = tempfile.mkdtemp(prefix="cf-log-")
    tmpdir = tempfile.mkdtemp(prefix="cf-tmp-")
    try:
        with open(os.path.join(tmpdir, "stub_filter.py"), "w",
                  encoding="utf-8") as fh:
            fh.write("import sys\n"
                     "sys.stderr.write('catalog-filter: refusing to rewrite"
                     " /x: layout\\n')\n"
                     "sys.exit(5)\n")
        env = dict(os.environ)
        env["CATALOG_FILTER_LOG"] = os.path.join(logdir, "filter.log")
        env["TMPDIR"] = tmpdir
        env["CATALOG_FILTER_PY"] = os.path.join(tmpdir, "stub_filter.py")
        env["CODEX_HOME"] = os.path.join(tmpdir, "no-codex-home")
        subprocess.run(["/bin/bash", WRAPPER, "run"], env=env,
                       capture_output=True, text=True, timeout=120)
        with open(os.path.join(logdir, "filter.log"), encoding="utf-8") as fh:
            text = fh.read()
        # The status of the filter itself, not of the negated test.
        assert "FAILED (rc=5)" in text
        assert "refusing to rewrite" in text
        assert "\nok: " not in text
        # And the report temp file did not survive the run.
        assert [n for n in os.listdir(tmpdir) if n.startswith("catalog-filter.")] == []
    finally:
        for name in os.listdir(logdir):
            os.remove(os.path.join(logdir, name))
        os.rmdir(logdir)
        for name in os.listdir(tmpdir):
            os.remove(os.path.join(tmpdir, name))
        os.rmdir(tmpdir)


def test_wrapper_logs_counts_on_success():
    logdir = tempfile.mkdtemp(prefix="cf-log-")
    tmpdir = tempfile.mkdtemp(prefix="cf-tmp-")
    try:
        with open(os.path.join(tmpdir, "stub_filter.py"), "w",
                  encoding="utf-8") as fh:
            fh.write("import json\n"
                     "print(json.dumps({'removed': 3, 'models_after': 90,\n"
                     "                   'unavailable': ['gemini'],\n"
                     "                   'error': 'stale verdict'}))\n")
        env = dict(os.environ)
        env["CATALOG_FILTER_LOG"] = os.path.join(logdir, "filter.log")
        env["TMPDIR"] = tmpdir
        env["CATALOG_FILTER_PY"] = os.path.join(tmpdir, "stub_filter.py")
        env["CODEX_HOME"] = os.path.join(tmpdir, "no-codex-home")
        subprocess.run(["/bin/bash", WRAPPER, "run"], env=env,
                       capture_output=True, text=True, timeout=120)
        with open(os.path.join(logdir, "filter.log"), encoding="utf-8") as fh:
            text = fh.read()
        assert "ok: removed=3 after=90 unavailable=gemini" in text
        # A run that succeeded but skipped the main job still says why.
        assert "error=stale verdict" in text
        assert [n for n in os.listdir(tmpdir)
                if n.startswith("catalog-filter.")] == []
    finally:
        for name in os.listdir(logdir):
            os.remove(os.path.join(logdir, name))
        os.rmdir(logdir)
        for name in os.listdir(tmpdir):
            os.remove(os.path.join(tmpdir, name))
        os.rmdir(tmpdir)


def test_wrapper_reports_a_broken_filter_report():
    logdir = tempfile.mkdtemp(prefix="cf-log-")
    tmpdir = tempfile.mkdtemp(prefix="cf-tmp-")
    try:
        with open(os.path.join(tmpdir, "stub_filter.py"), "w",
                  encoding="utf-8") as fh:
            fh.write("print('{not json')\n")
        env = dict(os.environ)
        env["CATALOG_FILTER_LOG"] = os.path.join(logdir, "filter.log")
        env["TMPDIR"] = tmpdir
        env["CATALOG_FILTER_PY"] = os.path.join(tmpdir, "stub_filter.py")
        env["CODEX_HOME"] = os.path.join(tmpdir, "no-codex-home")
        subprocess.run(["/bin/bash", WRAPPER, "run"], env=env,
                       capture_output=True, text=True, timeout=120)
        with open(os.path.join(logdir, "filter.log"), encoding="utf-8") as fh:
            text = fh.read()
        assert "unreadable-report: {not json" in text
    finally:
        for name in os.listdir(logdir):
            os.remove(os.path.join(logdir, name))
        os.rmdir(logdir)
        for name in os.listdir(tmpdir):
            os.remove(os.path.join(tmpdir, name))
        os.rmdir(tmpdir)


def test_wrapper_passes_the_staleness_bound_to_the_filter():
    with open(WRAPPER, encoding="utf-8") as fh:
        src = fh.read()
    assert "--max-verify-age" in src
    assert '--interval) shift; INTERVAL="${1:?--interval needs a value}" ;;' in src
    assert src.count("--report-only) REPORT_ONLY=1; DRY_RUN=1 ;;") == 1
    assert "HIDE_ENABLED" not in src
