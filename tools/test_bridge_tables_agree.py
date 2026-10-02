"""Every bridge table in the fleet must name the same bridges.

zcode was added as the 13th bridge (port 8800) and install.sh, fleet_probe.py,
status.sh and finish.sh were updated -- but four other tables were not:
status_ui.py, verify_real_calls.py, catalog_filter.py and fleet_chat_test.py.
The consequences were silent and operational: the panel showed 12 bridges
instead of 13, verify_real_calls never verified zcode so its verdict never
existed, and catalog_filter derives its bridge set from the panel, so zcode
could never be judged at all. A bridge nobody counts is a bridge nobody
notices is broken.

install.sh joined this test last, on 2026-10-03: kimi and minimax
were named by the other five tables and would have shipped without a
service at all.

status_ui.py even carries the instruction "Keep in sync with tools/status.sh,
bridges/finish.sh and deploy.sh" -- this test enforces it, and extends it to
every table that enumerates bridges.
"""
import importlib.util
import os
import re
import unittest

KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
PORT_BASE = 8787


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(KIT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _from_shell(path):
    """(name, offset) rows out of a shell heredoc table."""
    out = set()
    with open(os.path.join(KIT, path), encoding="utf-8") as fh:
        for line in fh:
            parts = line.strip().split("|")
            if len(parts) < 3:
                continue
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", parts[0]):
                continue
            if "2codex" not in parts[1]:
                continue
            try:
                offset = int(parts[2])
            except ValueError:
                continue
            out.add((parts[0], offset))
    return out



def _from_install_sh():
    """(name, offset) rows out of install.sh's BRIDGES array.

    install.sh is the table that actually creates the services, and it was
    not in this test when zcode shipped -- which is why a bridge could be
    named by five tables and still never be installed. Its columns are
    name|label|dir|script|offset|key|args|env, so the offset is parts[4],
    not parts[2].
    """
    out = set()
    with open(os.path.join(KIT, "install.sh"), encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped.startswith('"') or "2codex" not in stripped:
                continue
            parts = stripped.strip('"').split("|")
            if len(parts) < 5:
                continue
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", parts[0]):
                continue
            try:
                out.add((parts[0], int(parts[4])))
            except ValueError:
                continue
    return out



def _from_setup_providers():
    """(name, offset) rows out of opencodex/setup-providers.sh.

    This is the table that decides which bridges Codex can actually reach:
    a bridge missing here listens fine and is invisible to the model
    picker, which is the same failure zcode had one table at a time.
    Its columns are name|offset|key, so the offset is parts[1].
    """
    out = set()
    with open(os.path.join(KIT, "opencodex", "setup-providers.sh"),
              encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped.startswith('"') or "|" not in stripped:
                continue
            parts = stripped.strip('"').split("|")
            if len(parts) < 2:
                continue
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", parts[0]):
                continue
            try:
                out.add((parts[0], int(parts[1])))
            except ValueError:
                continue
    return out


# Every table that enumerates bridges, reduced to (name, port offset).
NAME_OFFSET_SOURCES = {
    "status_ui.py": lambda: {(r[0], r[2]) for r in _load("sui_tbl", "tools/status_ui.py").BRIDGES},
    "verify_real_calls.py": lambda: {
        (r[0], r[2]) for r in _load("vrc_tbl", "tools/verify_real_calls.py").BRIDGES},
    "fleet_chat_test.py": lambda: {
        (r[0], r[1]) for r in _load("fct_tbl", "tools/fleet_chat_test.py").BRIDGE_NAMES},
    "status.sh": lambda: _from_shell("tools/status.sh"),
    "finish.sh": lambda: _from_shell("bridges/finish.sh"),
    "install.sh": lambda: _from_install_sh(),
    "setup-providers.sh": lambda: _from_setup_providers(),
}

# The tables that also carry the launchd label, so the label can be checked too.
LABEL_SOURCES = {
    "status_ui.py": lambda: {(r[0], r[1], r[2])
                             for r in _load("sui_tbl", "tools/status_ui.py").BRIDGES},
    "verify_real_calls.py": lambda: {
        (r[0], r[1], r[2])
        for r in _load("vrc_tbl", "tools/verify_real_calls.py").BRIDGES},
}


class BridgeTablesAgreeTest(unittest.TestCase):
    def test_every_table_names_the_same_bridges_and_offsets(self):
        tables = {name: build() for name, build in NAME_OFFSET_SOURCES.items()}
        expected = tables["status.sh"]
        self.assertIn(("zcode", 13), expected,
                     "status.sh is the reference; it must still carry zcode")
        for name, rows in tables.items():
            self.assertEqual(rows, expected,
                             "%s disagrees with status.sh: missing=%s extra=%s"
                             % (name, sorted(expected - rows),
                                sorted(rows - expected)))

    def test_fleet_probe_ports_match_the_offsets(self):
        probe = _load("fp_tbl", "tools/fleet_probe.py")
        for name, offset in NAME_OFFSET_SOURCES["status.sh"]():
            self.assertEqual(probe.PORTS[name], PORT_BASE + offset,
                             "%s: fleet_probe port disagrees with the offset" % name)

    def test_the_thirteenth_bridge_is_present_everywhere(self):
        """zcode is the one that was forgotten; keep it named."""
        for name, build in NAME_OFFSET_SOURCES.items():
            rows = build()
            self.assertIn(("zcode", 13), rows,
                         "%s is missing the zcode bridge" % name)

    def test_labels_agree_where_a_table_carries_them(self):
        tables = {name: build() for name, build in LABEL_SOURCES.items()}
        expected = tables["status_ui.py"]
        for name, rows in tables.items():
            self.assertEqual(rows, expected,
                             "%s disagrees on (name, label, offset): "
                             "missing=%s extra=%s"
                             % (name, sorted(expected - rows),
                                sorted(rows - expected)))


if __name__ == "__main__":
    unittest.main()
