"""The status panel must see fleet.env key rotations without a restart.

Measured 2026-09-29: the fleet-ui daemon builds its config once at startup, so
a key written into fleet.env afterwards never reached it. qwen's
QWEN2CODEX_KEY landed two minutes after the daemon started; the panel kept the
startup-time (empty) key set, probed /v1/models without an Authorization
header, got the bridge's 401, and warned "key 未读取到 - 登录后执行 finish.sh"
-- pointing the operator at an expired session that did not exist.

refresh_keys re-parses fleet.env on every probe round so the panel tracks the
file an operator actually edits.
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "status_ui", os.path.join(HERE, "status_ui.py"))
status_ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status_ui)


def _write_env(path, **extra):
    lines = ['PORT_BASE="8787"', '# a comment', '', 'CODEBUDDY2OPENAI_KEY="sk-quoted"',
             'QODER2CODEX_KEY=sk-bare']
    lines += ["%s=%s" % kv for kv in extra.items()]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_env_file_handles_quotes_bare_and_comments():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "fleet.env")
        _write_env(path)
        keys = status_ui.parse_env_file(path)
        assert keys["CODEBUDDY2OPENAI_KEY"] == "sk-quoted"
        assert keys["QODER2CODEX_KEY"] == "sk-bare"
        assert keys["PORT_BASE"] == "8787"
        assert [k for k in keys if k.startswith("#")] == []


def test_refresh_keys_picks_up_a_rotation():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "fleet.env")
        _write_env(path)
        cfg = {"env_file": path, "keys": {}}
        status_ui.refresh_keys(cfg)
        assert cfg["keys"]["QODER2CODEX_KEY"] == "sk-bare"

        _write_env(path, QODER2CODEX_KEY="sk-rotated")
        status_ui.refresh_keys(cfg)
        assert cfg["keys"]["QODER2CODEX_KEY"] == "sk-rotated"


def test_refresh_keys_survives_a_missing_file():
    cfg = {"env_file": "/nonexistent/fleet.env", "keys": {"OLD": "keep"}}
    status_ui.refresh_keys(cfg)
    assert cfg["keys"] == {"OLD": "keep"}


def test_a_key_added_after_startup_reaches_the_probe_input():
    """The regression itself: startup-time keys must not stay frozen.

    collect_bridge reads cfg["keys"]; with the old frozen dict a key that only
    appeared later probed keyless and produced the false re-login warning.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "fleet.env")
        _write_env(path)
        cfg = {"env_file": path, "keys": status_ui.parse_env_file(path)}
        assert "QWEN2CODEX_KEY" not in cfg["keys"]

        _write_env(path, QWEN2CODEX_KEY="sk-local-qwen")
        status_ui.refresh_keys(cfg)
        assert cfg["keys"].get("QWEN2CODEX_KEY") == "sk-local-qwen"
