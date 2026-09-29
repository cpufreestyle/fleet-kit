"""ocx-catalog-guard must not heal a catalog the filter shortened on purpose.

Measured 2026-09-29: after catalog_filter.py hid the verified-not-REAL rows
the catalog held 66 slash rows against MIN_MODELS=60 -- six rows of headroom.
One more bridge going down and the guard would have run `ocx sync`, re-adding
every broken row for the filter to hide again 300s later, forever. The guard
now asks the filter how many rows it hid and only heals a shortfall the filter
cannot explain.
"""
import json
import os
import subprocess
import tempfile

GUARD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "ocx-catalog-guard.sh")


def _home(slash_rows, hidden):
    home = tempfile.mkdtemp(prefix="fk-guard-")
    with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
        fh.write('model_provider = "custom"\n'
                 'model_catalog_json = "catalog.json"\n')
    with open(os.path.join(home, "catalog.json"), "w", encoding="utf-8") as fh:
        json.dump({"models": [{"slug": "workbuddy/m%d" % i, "priority": 10}
                              for i in range(slash_rows)]
                   + [{"slug": "step-3.7-flash", "priority": 10}]}, fh)
    with open(os.path.join(home, ".catalog-filter-hidden.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"slash_rows_hidden": hidden,
                   "slugs": ["gemini/g%d" % i for i in range(hidden)]}, fh)
    stub = os.path.join(home, "bin")
    os.makedirs(stub)
    ocx = os.path.join(stub, "ocx")
    with open(ocx, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho \"ocx $* (stub)\"\n")
    os.chmod(ocx, 0o755)
    log = os.path.join(home, "guard.log")
    return home, stub, log


def _run(home, stub, log, *extra, **kwargs):
    env = dict(os.environ)
    env["CODEX_HOME"] = home
    env["PATH"] = "%s:/usr/bin:/bin:/usr/sbin:/sbin" % stub
    if kwargs.get("verbose"):
        env["OCX_GUARD_VERBOSE"] = "1"
    return subprocess.run(
        ["/bin/bash", GUARD, "run", "--codex-home", home, "--log", log]
        + list(extra),
        env=env, capture_output=True, text=True, timeout=120)


def _text(log):
    with open(log, encoding="utf-8") as fh:
        return fh.read()


def test_shortfall_the_filter_explains_does_not_heal():
    home, stub, log = _home(52, 20)
    proc = _run(home, stub, log, "--min-models", "60")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = _text(log)
    assert "not healing" in text
    assert "ocx sync (stub)" not in text


def test_shortfall_the_filter_cannot_explain_heals():
    home, stub, log = _home(30, 5)
    proc = _run(home, stub, log, "--min-models", "60", "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = _text(log)
    assert "[dry-run] ocx sync" in text
    assert "not healing" not in text


def test_healthy_catalog_is_left_alone():
    home, stub, log = _home(90, 0)
    proc = _run(home, stub, log, "--min-models", "60", verbose=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = _text(log)
    assert "ok: 90 bridge models in catalog" in text
    assert "not healing" not in text
    assert "heal" not in text


def test_no_hidden_sidecar_still_heals_a_stripped_catalog():
    home = tempfile.mkdtemp(prefix="fk-guard-")
    with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
        fh.write('model_catalog_json = "catalog.json"\n')
    with open(os.path.join(home, "catalog.json"), "w", encoding="utf-8") as fh:
        json.dump({"models": [{"slug": "workbuddy/m%d" % i} for i in range(3)]},
                  fh)
    stub = os.path.join(home, "bin")
    os.makedirs(stub)
    ocx = os.path.join(stub, "ocx")
    with open(ocx, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho \"ocx $* (stub)\"\n")
    os.chmod(ocx, 0o755)
    log = os.path.join(home, "guard.log")
    proc = _run(home, stub, log, "--min-models", "60", "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[dry-run] ocx sync" in _text(log)


def test_unreadable_catalog_is_skipped():
    home = tempfile.mkdtemp(prefix="fk-guard-")
    with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
        fh.write('model_catalog_json = "catalog.json"\n')
    with open(os.path.join(home, "catalog.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    stub = os.path.join(home, "bin")
    os.makedirs(stub)
    with open(os.path.join(stub, "ocx"), "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho \"ocx $* (stub)\"\n")
    os.chmod(os.path.join(stub, "ocx"), 0o755)
    log = os.path.join(home, "guard.log")
    proc = _run(home, stub, log, "--min-models", "60")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "skip: cannot count bridge models" in _text(log)
