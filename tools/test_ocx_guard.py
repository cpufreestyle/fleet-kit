"""ocx-catalog-guard must heal what it can count and must not heal what it cannot explain.

The guard has two jobs and this file pins both: it restores the fleet route a
provider switcher strips out of config.toml (see tools/pin_fleet_route.py,
wired in by route_pin), and it heals a catalog that is stripped or unreadable
instead of skipping it, which is what used to leave a wiped declaration wiped
for as long as the timer ran. Both are covered below next to the original rule:
a shortfall the filter shortened on purpose is not healed, because healing it
re-adds broken rows for the filter to hide again 300s later, forever.

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
    env["FLEET_ROUTE_PIN"] = "1" if kwargs.get("route_pin") else "0"
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


def test_unreadable_catalog_is_healed():
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
    assert "heal: cannot count bridge models" in _text(log)
    assert "ocx sync (stub)" in _text(log)


def test_a_stripped_declaration_is_healed():
    """The state that used to report skip forever.

    Measured 2026-09-30: a provider switcher dropped model_catalog_json, and
    because the count then failed the guard logged skip every 300s while 123
    bridge models sat in a catalog file nobody declared. Not being able to
    count is the worst state, so it heals.
    With the route pin in front of it the count can usually be taken after
    all: the pin declares the catalog file that was sitting right there, and
    the shortfall path heals just the same. Either way ocx sync has to run,
    which is what this pins down -- where python3 is missing the pin never
    fires and the cannot-count path does the work instead.
    """
    home = tempfile.mkdtemp(prefix="fk-guard-")
    with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
        fh.write('model_provider = "custom"\nmodel = "step-5-preview"\n'
                 '[model_providers.custom]\nbase_url = "http://127.0.0.1:15721/v1"\n')
    with open(os.path.join(home, "opencodex-catalog.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"models": [{"slug": "lingxi/lingxi-deepseek-flash"}]}, fh)
    stub = os.path.join(home, "bin")
    os.makedirs(stub)
    with open(os.path.join(stub, "ocx"), "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho \"ocx $* (stub)\"\n")
    os.chmod(os.path.join(stub, "ocx"), 0o755)
    log = os.path.join(home, "guard.log")
    proc = _run(home, stub, log, "--min-models", "60", route_pin=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    logged = _text(log)
    assert "heal" in logged
    assert "ocx sync (stub)" in logged
    # The pin that runs first has to leave the foreign provider alone: it is
    # what every session already open on that provider resolves.
    with open(os.path.join(home, "config.toml"), encoding="utf-8") as fh:
        text = fh.read()
    assert 'model_provider = "opencodex"' in text
    assert 'base_url = "http://127.0.0.1:15721/v1"' in text


def test_the_route_pin_can_be_switched_off():
    """A caller that owns the config itself must be able to turn the pin off."""
    home = tempfile.mkdtemp(prefix="fk-guard-")
    switched = ('model_provider = "custom"\nmodel = "step-5-preview"\n'
                '[model_providers.custom]\n'
                'base_url = "http://127.0.0.1:15721/v1"\n')
    with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
        fh.write(switched)
    stub = os.path.join(home, "bin")
    os.makedirs(stub)
    with open(os.path.join(stub, "ocx"), "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho \"ocx $* (stub)\"\n")
    os.chmod(os.path.join(stub, "ocx"), 0o755)
    log = os.path.join(home, "guard.log")
    proc = _run(home, stub, log, "--min-models", "60")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    with open(os.path.join(home, "config.toml"), encoding="utf-8") as fh:
        assert fh.read() == switched, "the pin ran even though it was disabled"


def main():
    """Run this file's tests and report a count, pytest or no pytest."""
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print("ok   %s" % name)
        except AssertionError as exc:
            failures += 1
            print("FAIL %s: %s" % (name, exc))
        except Exception as exc:  # a broken helper is a failing test too
            failures += 1
            print("FAIL %s: %r" % (name, exc))
    print("%d tests, %d failures" % (len(tests), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
