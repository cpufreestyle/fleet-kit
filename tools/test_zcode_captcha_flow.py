"""The captcha path must stop jumping at the operator, and must not double-spend.

Measured 2026-10-02: with the pool empty and no usable ticket, every zcode
call ran captcha-mint.py without --headless, which opens a real Chrome window
in the middle of the request; the mint cannot solve the slider, so it failed
after its timeout and the operator was sent back to the relay page -- whose
own script auto-started the verification 2.2s after every load. One call, one
popup, one manual solve: the verification never stopped coming back.

The shape that ends that loop, and what these tests hold in place:

* a mint during a call is headless -- it may fail, but it fails invisibly,
  and the caller asks for one human ticket instead of a window;
* the relay page never auto-starts the verification; the operator clicks when
  they choose to, and can bank several tickets in one sitting;
* a save lands exactly one ticket in the pool and nowhere else: the param is
  single-use upstream, and the same param sitting in captcha.txt as well is a
  second claim on one use, which comes back as 3007;
* the minter's Chrome profile lives outside /tmp, because a profile that is
  wiped on every reboot is a device fingerprint that never matures -- and a
  cold fingerprint is what makes the traceless verification fail in the first
  place.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
ZCODE = os.path.join(KIT, "bridges", "zcode")
RELAY = os.path.join(ZCODE, "captcha-relay.py")
BRIDGE = os.path.join(ZCODE, "zcode_bridge.py")
MINTER = os.path.join(ZCODE, "captcha-mint.py")
PAGE = os.path.join(ZCODE, "captcha", "index.html")
TICKET = "eyJhbGciOiJFUzI1NiJ9." + "x" * 60


def _source(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class NoJumpScares(unittest.TestCase):
    """Nothing in the call path may open a window or self-trigger."""

    def test_the_bridge_mints_headless(self):
        src = _source(BRIDGE)
        self.assertIn('"--once", "--headless"', src,
                      "the bridge mints headed again: a Chrome window pops up mid-request")

    def test_the_relay_page_never_auto_starts_the_verification(self):
        src = _source(PAGE)
        self.assertNotIn("setTimeout", src,
                         "the page auto-triggers the verification again")
        self.assertIn("getElementById('verify')", src,
                      "the page has no manual start button any more")
        self.assertIn("startTracelessVerification", src,
                      "the manual button no longer starts a verification")

    def test_the_minter_profile_is_not_under_tmp(self):
        src = _source(MINTER)
        self.assertNotIn("tempfile.gettempdir()", src,
                         "the mint profile is back under /tmp, where a reboot wipes it")

    def test_a_save_never_writes_captcha_txt(self):
        """One param, one claim: the pool file is the only copy."""
        src = _source(RELAY)
        self.assertNotIn('STATE_FILE, "w"', src,
                         "a save writes captcha.txt again, so the same param can be spent twice")


class RelayBanksIntoThePool(unittest.TestCase):
    """Behavioural: the real relay on a real port, throwaway dirs."""

    def _start_relay(self, tmp):
        pool = os.path.join(tmp, "pool")
        legacy = os.path.join(tmp, "captcha.txt")
        env = dict(os.environ)
        env["ZCODE_CAPTCHA_POOL"] = pool
        env["ZCODE_CAPTCHA_FILE"] = legacy
        proc = subprocess.Popen(
            [sys.executable, RELAY, "--port", "0"], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + 10
        port = None
        while time.time() < deadline and port is None:
            try:
                out = subprocess.run(
                    ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a",
                     "-p", str(proc.pid)],
                    capture_output=True, text=True)
                for line in out.stdout.splitlines():
                    if "(LISTEN)" in line:
                        port = int(line.split(":")[-1].split(" ")[0])
                        break
            except Exception:
                pass
            if port is None:
                time.sleep(0.2)
        self.assertIsNotNone(port, "the relay never listened")
        return proc, port, pool, legacy

    def _post(self, port, path, body):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (port, path),
            data=body.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as resp:
            return resp.status, resp.read().decode()

    def _get(self, port, path):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:%d%s" % (port, path),
                         timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())

    def test_two_saves_land_two_pool_tickets_and_no_legacy_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, port, pool, legacy = self._start_relay(tmp)
            self.addCleanup(proc.kill)
            try:
                for i in range(2):
                    status, body = self._post(port, "/save", "p=" + TICKET)
                    self.assertEqual(status, 200, body)
                    self.assertIn("pool=%d" % (i + 1), body)
                tickets = [n for n in os.listdir(pool) if n.endswith(".txt")]
                self.assertEqual(len(tickets), 2,
                                 "a save did not land exactly one pool ticket")
                self.assertFalse(os.path.exists(legacy),
                                 "the relay wrote the legacy file again")
                _status, info = self._get(port, "/status")
                self.assertEqual(info["tickets"], 2, info)
            finally:
                proc.kill()

    def test_a_short_param_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, port, pool, legacy = self._start_relay(tmp)
            self.addCleanup(proc.kill)
            try:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self._post(port, "/save", "p=tooshort")
                self.assertEqual(caught.exception.code, 400)
                self.assertEqual(
                    os.listdir(pool) if os.path.isdir(pool) else [], [])
            finally:
                proc.kill()


class MinterContracts(unittest.TestCase):

    def test_the_minter_presses_the_button_itself(self):
        """The page stays manual for humans; the minter is the automation"""
        src = _source(MINTER)
        self.assertIn("_press_verify", src,
                      "nothing starts the verification any more: traceless never runs")
        self.assertIn('page.click("#verify"', src,
                      "the minter no longer presses the page's verify button")

    """The mint waits on the pool, and no consumer opens a headed window."""

    def test_the_minter_waits_on_the_pool_not_a_page_global(self):
        src = _source(MINTER)
        self.assertNotIn("page.evaluate", src,
                         "the minter reads a page global again: a human solve banked the ticket and the mint still timed out reading it")
        self.assertIn("_claim_pool_ticket", src,
                      "the minter no longer watches the pool, so a banked ticket is not a success")

    def test_the_minter_never_drags_the_slider(self):
        src = _source(MINTER)
        self.assertNotIn("drag_slider", src,
                         "the scripted slider drag is back; Aliyun refuses it with F001/F015 and it can only disturb a human mid-drag")

    def test_cli_client_mints_headless(self):
        src = _source(os.path.join(ZCODE, "cli_client.py"))
        self.assertIn('"--once", "--pool", "--headless"', src,
                      "cli_client mints headed again: a Chrome window pops up mid-turn on the CLI route too")

    def test_cli_client_probes_its_own_interpreter_first(self):
        src = _source(os.path.join(ZCODE, "cli_client.py"))
        self.assertIn('sys.executable, "/usr/bin/python3"', src,
                      "the interpreter probe no longer starts with the bridge's own venv")


class PoolKeeper(unittest.TestCase):
    """The keeper that keeps tickets flowing must be silent and self-contained."""

    KEEPER = os.path.join(ZCODE, "captcha-pool-keeper.sh")
    CONTROL = os.path.join(ZCODE, "captcha-keeper.sh")
    UNINSTALL = os.path.join(KIT, "uninstall.sh")

    def test_the_keeper_checks_the_pool_before_minting(self):
        src = _source(self.KEEPER)
        self.assertIn('if [ "$fresh" -ge "$TARGET" ]; then', src,
                      "the keeper mints unconditionally again: a timer would stack tickets nobody spends")
        self.assertIn("FRESH_AGE", src,
                      "the keeper counts tickets without the freshness gate, so stale ones look spendable")

    def test_the_keeper_runs_the_mint_offscreen(self):
        src = _source(self.KEEPER)
        self.assertIn("--window-position=20000,20000", src,
                      "the keeper's headed window is on screen again; offscreen is what makes it silent")

    def test_the_keeper_picks_a_python_with_playwright(self):
        src = _source(self.KEEPER)
        self.assertIn("import playwright", src,
                      "the keeper does not probe for playwright: bare python3 has none and the mint dies on an import")
        self.assertNotIn("ZCAP_KEEPER_PYTHON:-$(command -v python3)", src,
                         "the keeper defaults to whatever python3 is on PATH again")

    def test_the_control_script_installs_from_its_own_tree(self):
        src = _source(self.CONTROL)
        self.assertIn("fleet_timer_install", src,
                      "the control script no longer installs a launchd timer")
        self.assertIn('dir="$SCRIPT_DIR"', src,
                      "the control script does not walk up for platform.sh, so a runtime install fills the kit pool")

    def test_uninstall_removes_the_keeper_and_the_relay(self):
        src = _source(self.UNINSTALL)
        line = [ln for ln in src.splitlines() if ln.startswith("SUFFIXES=")][0]
        for suffix in ("zcode-captcha-keeper", "zcode-captcha-relay"):
            self.assertIn(suffix, line,
                          "uninstall.sh leaves the %s job behind" % suffix)



if __name__ == "__main__":
    unittest.main()

