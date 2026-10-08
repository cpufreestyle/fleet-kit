"""Load kit modules from source for the tests.

A bridge is not an importable package: it lives in bridges/<name>/ and reaches
its siblings by inserting its parent on sys.path at import time, so importing
one the normal way from tools/ fails. Every test that exercises a bridge or a
standalone tool therefore loads the module by path -- and between them they had
grown four copies of the same loader, differing only in which directory they
resolved against.

Three base directories cover every caller: bridges/ (the bridge modules),
tools/ (the standalone tools, which are scripts rather than a package) and the
kit root (for the few tests that spell out a full relative path).

The one non-obvious part is registering the module in sys.modules *before*
exec: on 3.14 dataclasses resolves cls.__module__ through sys.modules, so a
module loaded by spec alone breaks any module that declares a dataclass. The
caller passes the name it wants the module registered under, which is also how
two tests load the same bridge with different environment variables -- so
those names are deliberately unique per test file, and a shared name would
make the second load overwrite the first.
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.abspath(os.path.join(HERE, os.pardir))
BRIDGES = os.path.join(KIT, "bridges")
TOOLS = HERE


def load_module(name, path):
    """Load the file at `path` as `name` and return the module."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_bridge(name, rel):
    """Load bridges/<rel> as `name`."""
    return load_module(name, os.path.join(BRIDGES, rel))


def load_tool(name, rel):
    """Load tools/<rel> as `name`."""
    return load_module(name, os.path.join(TOOLS, rel))


def load_kit(name, rel):
    """Load <rel> under the kit root as `name`."""
    return load_module(name, os.path.join(KIT, rel))


def closed_port() -> int:
    """A port nothing listens on, found by binding and releasing one.

    A raw socket to a closed port really is refused (connection refused), but a
    forward through httpx is not: a box that exports HTTP_PROXY makes httpx
    hand any unreachable address to that proxy, which answers with its own
    empty 503 -- which the bridge then relays faithfully and the test proves
    nothing. Binding port 0 hands back a port the kernel just gave away, which
    stays closed long enough for a connect to fail once the client stops
    inheriting the proxy.
    """
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def load_free_models():
    """The free_models module plus the free-windows.json database it annotates.

    build() shells out to "ocx models live" and reads the picker catalog, so on
    a checkout with neither -- CI, a fresh clone -- snap["models"] is empty and
    every credits or free-window assertion lands on an empty set instead of on
    the data. The questions those tests ask are about the annotation, so read
    the rows straight from the database.
    """
    with open(os.path.join(KIT, "free-windows.json"), encoding="utf-8") as fh:
        db = json.load(fh)
    module = load_tool("free_models", "free_models.py")

    def rows():
        return [module.annotate(db, *key.split("/", 1)) for key in db["models"]] + \
               [module.annotate(db, name, "__unknown-model__") for name in db["providers"]]

    return module, db, rows


def probe_answer(content: str) -> str:
    """Build the reply a real model gives to verify_real_calls.make_probe.

    The probe is an arithmetic question with a nonce ("(37+5)" plus a
    "暗号：XXXX" marker), so a fake server that pattern-matches on the question
    still has to do the arithmetic to answer it. Tests that stand in for a
    bridge share this answer rather than each re-deriving the two regexes.
    """
    import re

    m = re.search(r"\((\d+)\+(\d+)", content)
    n, add = int(m.group(1)), int(m.group(2))
    nonce = re.search(r"暗号：(\S+)", content).group(1)
    return "%s %d" % (nonce[:4], n + add)


def site_factory(make):
    """A pytest fixture factory that opens shims and closes them all at the end.

    Both stepfun shim test files need "start a real shim on a real socket in
    this process, remember it, stop every one of them when the test ends".
    Closing matters: an abandoned shim keeps its uvicorn event loop running for
    the rest of the session, and that loop ticks asyncio.sleep into any
    process-wide patch another test installed before it -- so the shutdown is
    in reverse order, newest first.

    `make` is the per-file shim constructor; only the bookkeeping is shared,
    because the two shims take genuinely different arguments.
    """
    import pytest

    @pytest.fixture()
    def _site_factory():
        opened = []

        def _open(*args, **kwargs):
            site = make(*args, **kwargs)
            opened.append(site)
            return site

        yield _open
        for site in reversed(opened):
            site.close()

    return _site_factory
