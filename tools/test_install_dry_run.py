"""`install.sh --dry-run` must stay a plan: fast, local, network-free.

Measured 2026-09-29: the dry run spent ~4 minutes inside
`extract_client.py --verify`, which POSTs a refresh to
oauth2.googleapis.com for every candidate pair with a 25s timeout each. On an
unblocked network every attempt fails in milliseconds, so CI never noticed;
behind a blocking one the plan itself hung. A second, quieter trap: the dry run
defaulted FLEET_HOME to ~/FleetKit, so it planned an install of a fleet home
that is not the one in use and had no fleet.env to read keys from.

These are static checks -- the dry run writes nothing and returns 0 by
contract, so the interesting property is which flags it is *able* to pass.
"""
import os
import re
import subprocess

KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
INSTALL = os.path.join(KIT, "install.sh")


def _source():
    with open(INSTALL, encoding="utf-8") as fh:
        return fh.read()


def test_no_extract_client_call_carries_a_literal_verify_flag():
    for lineno, line in enumerate(_source().splitlines(), 1):
        if "extract_client.py" not in line:
            continue
        assert "--verify" not in line, (
            "install.sh:%d calls extract_client.py with a literal --verify; "
            "route it through a variable that the dry run can blank out" % lineno)


def test_the_verify_flag_is_gated_on_not_being_a_dry_run():
    src = _source()
    assert re.search(r'AGY_VERIFY="--verify"', src), \
        "the antigravity oauth verification is gone; did it move or regress?"
    assert re.search(r'if \[ "\$DRY_RUN" = "1" \]; then AGY_VERIFY=""\s*; fi', src), \
        "AGY_VERIFY is never cleared for a dry run"


def test_the_qwen_upstream_key_is_optional_and_only_injected_when_set():
    """A fabricated upstream key would 401 on every chat call."""
    src = _source()
    assert 'QWEN_API_KEY="$(pick_optional QWEN_API_KEY)"' in src
    assert re.search(r'if \[ "\$name" = "qwen" \] && \[ -n "\$QWEN_API_KEY" \]', src), \
        "QWEN_API_KEY must reach the service env only when it is non-empty"


def test_the_script_still_parses():
    subprocess.run(["bash", "-n", INSTALL], check=True)
