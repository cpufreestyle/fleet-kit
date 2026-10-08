"""Load a bridge module from source for the bridge tests.

A bridge is not an importable package: it lives in bridges/<name>/ and reaches
its siblings by inserting its parent on sys.path at import time, so importing
one the normal way from tools/ fails. Every bridge test therefore loads the
module by path -- and each of them had grown the same six lines for it.

The one non-obvious part is registering the module in sys.modules *before*
exec: on 3.14 dataclasses resolves cls.__module__ through sys.modules, so a
module loaded by spec alone breaks any bridge that declares a dataclass. The
caller passes the name it wants the module registered under, which is also how
two tests load the same bridge with different environment variables.
"""
import importlib.util
import os
import sys

BRIDGES = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "bridges"))


def load_bridge(name, rel):
    """Load bridges/<rel> as `name` and return the module."""
    path = os.path.join(BRIDGES, rel)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
