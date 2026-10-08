"""The image-cap shim must be installed, re-pinned and removed as one unit.

StepFun's Plan API answers a request with 70 images and refuses the 71st with
400 images_too_many, and Codex re-sends its whole history every turn, so a
session that pastes screenshots crosses that ceiling no matter what the operator
does (tools/image_cap.py holds the measurement). Installing the service alone is
not enough: CC Switch owns ~/.codex/config.toml and rewrites the custom
provider's base_url back to 127.0.0.1:15721 on every provider switch, so a setup
that only installs the shim leaves requests going straight through CC Switch to
StepFun while the shim sits unused on 15722 (its own health then shows only the
self-test). The re-point therefore has to move CC Switch's routing table, not the
file: tools/pin_cc_switch_endpoint.py rewrites the provider_endpoints row and the
base_url embedded in providers.settings_config so the chain becomes
Codex -> CC Switch -> shim -> step_plan, and the shim's upstream is a real
upstream rather than CC Switch, so the two cannot route into a loop.

Codex itself stays on CC Switch's port. Pointing its base_url at the shim instead
is measured 2026-10-01 to answer 401 on every turn: Codex sends
`Authorization: Bearer PROXY_MANAGED` and only CC Switch substitutes the real
StepFun key, so the shim forwards the placeholder and StepFun rejects it.

These tests pin the contracts the install depends on: both setup scripts install
the shim, they do it behind a dry-run branch so --dry-run stays a plan that
mutates nothing, uninstall.sh removes the job instead of orphaning it in
launchd, and the control script logs to the one file the launchd plist writes.

The shim also hangs sometimes with the process alive and the launchd job still
reporting state = running, and KeepAlive cannot see that: it relaunches a
service that exits, and a hung event loop never exits. So the install ships a
second watcher outside the process -- a launchd timer that probes the health
endpoint and force-restarts the service after repeated failures -- and the
tests below hold that pairing in place, from the static wiring down to what a
probe cycle actually does to the strike counter.
"""
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SETUP = os.path.join(KIT, "opencodex", "setup-providers.sh")
INSTALL = os.path.join(KIT, "install.sh")
UNINSTALL = os.path.join(KIT, "uninstall.sh")
SHIM_SH = os.path.join(KIT, "tools", "stepfun_image_shim.sh")
PLATFORM_SH = os.path.join(KIT, "tools", "platform.sh")
WD_SUFFIX = "stepfun-image-cap-watchdog"


def _source(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class StaticWiring(unittest.TestCase):
    """The three scripts must mention the shim, each in its own place."""

    def test_setup_providers_installs_and_repins(self):
        src = _source(SETUP)
        self.assertIn("stepfun_image_shim.sh", src,
                      "setup-providers.sh no longer installs the image-cap shim")
        self.assertNotIn('run python3 "$KIT/tools/pin_shim_base_url.py"', src,
                         "setup-providers.sh points Codex's base_url straight at the shim again; Codex sends Bearer PROXY_MANAGED and only CC Switch holds the real key, so every turn answers 401")

    def test_the_routing_table_pin_runs_inside_the_service(self):
        """The CC Switch re-point has to survive without anyone re-running setup.

        tools/pin_cc_switch_endpoint.py is not called from setup-providers.sh any
        more: the shim re-points CC Switch's routing table on its own timer, so a
        provider switch that clobbers the row is undone within the interval. The
        control script is what turns that timer on, and a service started without
        it would leave the shim bypassed while every check here stayed green.
        """
        src = _source(SHIM_SH)
        self.assertIn("IMAGE_CAP_CC_PIN_INTERVAL", src,
                      "the shim service no longer re-points CC Switch, so a provider switch drops the shim out of the chain")

    def test_setup_providers_guards_the_shim_install_with_dry_run(self):
        idx = _source(SETUP).index("stepfun_image_shim.sh")
        window = _source(SETUP)[max(0, idx - 500): idx + 1200]
        self.assertIn("DRY_RUN", window,
                      "the shim install is not behind the dry-run guard; --dry-run would install a launchd service")

    def test_setup_providers_prefers_the_deployed_copy(self):
        src = _source(SETUP)
        window = src[max(0, src.index("SHIM_SH=") - 200):]
        self.assertIn("FLEET_HOME", window,
                      "the shim path no longer prefers the deployed copy, so the launchd job would run the git checkout")

    def test_install_sh_installs_the_shim(self):
        src = _source(INSTALL)
        self.assertIn("stepfun_image_shim.sh", src,
                      "install.sh no longer installs the image-cap shim")

    def test_uninstall_sh_removes_the_shim_service(self):
        src = _source(UNINSTALL)
        line = [ln for ln in src.splitlines() if ln.startswith("SUFFIXES=")][0]
        self.assertIn("stepfun-image-cap", line,
                      "uninstall.sh leaves an orphaned launchd job behind")

    def test_the_control_script_logs_where_launchd_writes(self):
        """platform.sh points the plist at $LOG_DIR/<label>.log.

        A second log name meant install-timer advertised a file the launchd job
        never wrote, and an operator checking it saw an empty log on a service
        that was in fact serving traffic.
        """
        src = _source(SHIM_SH)
        self.assertIn("SHIM_LOG=\"$LOG_DIR/${SHIM_LABEL}.log\"", src,
                      "the control script logs to a second file launchd never writes")


def _throwaway_home(root, with_config=True):
    home = os.path.join(root, "home")
    os.makedirs(home)
    with open(os.path.join(home, "fleet.env"), "w", encoding="utf-8") as fh:
        fh.write("PORT_BASE=17887\nLABEL_PREFIX=com.localtest\n"
                 "CODEBUDDY2OPENAI_KEY=sk-test-only\n")
    if with_config:
        codex = os.path.join(home, ".codex")
        os.makedirs(codex)
        with open(os.path.join(codex, "config.toml"), "w",
                  encoding="utf-8") as fh:
            fh.write("model_provider = \"custom\"\n")
    return home


def _run_script(script, args, home, services):
    env = dict(os.environ)
    env["HOME"] = home
    env["FLEET_SERVICE_DIR"] = services
    # setup-providers.sh refuses to start without ocx on PATH, and a stub is
    # enough: the dry run only echoes what it would call.
    stub = os.path.join(os.path.dirname(services or home), "bin")
    os.makedirs(stub, exist_ok=True)
    ocx = os.path.join(stub, "ocx")
    with open(ocx, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(ocx, 0o755)
    env["PATH"] = "%s:%s" % (stub, os.environ.get("PATH", ""))
    return subprocess.run(["bash", script] + args, capture_output=True,
                          text=True, timeout=300, env=env)


class DryRunMutatesNothing(unittest.TestCase):
    """Behavioural: --dry-run prints the plan and writes no service file."""

    def test_setup_providers_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = _throwaway_home(tmp)
            services = os.path.join(tmp, "services")
            envfile = os.path.join(home, "fleet.env")
            proc = _run_script(SETUP, ["--dry-run", "--env-file", envfile,
                                       "--home", tmp], home, services)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertIn("install-timer", proc.stdout,
                          "the dry run does not plan the shim install")
            self.assertNotIn("${KIT}/tools/pin_shim_base_url.py", proc.stdout,
                             "the dry run plans the config.toml pin, which points Codex past CC Switch and breaks authentication")
            written = os.listdir(services) if os.path.isdir(services) else []
            self.assertEqual(written, [],
                             "the dry run wrote a service file: %s" % written)

    def test_install_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = _throwaway_home(tmp)
            services = os.path.join(tmp, "services")
            proc = _run_script(INSTALL, ["--dry-run", "--home", tmp,
                                         "--no-opencodex"], home, services)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertIn("image-cap shim", proc.stdout,
                          "install.sh does not plan the shim install")
            written = os.listdir(services) if os.path.isdir(services) else []
            self.assertEqual(written, [],
                             "the dry run wrote a service file: %s" % written)


def _shim_home(root):
    """A throwaway fleet home with the platform helpers the shim sources.

    platform.sh is copied in on purpose: fleet_service_exists and friends come
    from it, and a watchdog cycle that reaches the restart path calls into
    them before it ever touches a pidfile. FLEET_PYTHON pins the interpreter
    to the one running these tests, because `start` re-launches the shim with
    a plain `python3` from PATH otherwise and that interpreter has no uvicorn.
    """
    home = os.path.join(root, "home")
    os.makedirs(os.path.join(home, "tools"), exist_ok=True)
    shutil.copy(PLATFORM_SH, os.path.join(home, "tools", "platform.sh"))
    with open(os.path.join(home, "fleet.env"), "w", encoding="utf-8") as fh:
        fh.write("LABEL_PREFIX=com.localtest\n")
        # Quoted because the interpreter may live under a path with a
        # space ("AI Shared") and a bare value splits when fleet.env is sourced.
        fh.write("FLEET_PYTHON=%s\n" % shlex.quote(sys.executable))
    return home


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _port_open(port):
    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _wait_port_closed(port, timeout=15):
    """SIGTERM is not instant: the interpreter tears down before the socket goes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_open(port):
            return True
        time.sleep(0.3)
    return not _port_open(port)


def _watchdog_env(home, services, logs, port, extra=None):
    env = dict(os.environ)
    env["HOME"] = home
    env["FLEET_SERVICE_DIR"] = services
    env["FLEET_LOG_DIR"] = logs
    env["IMAGE_CAP_PORT"] = str(port)
    # Keep the background instance quiet: the re-pin threads would wake up
    # and log about a missing CC Switch db, and a slow one could outlive the
    # test.
    env.setdefault("IMAGE_CAP_CC_PIN_INTERVAL", "0")
    env.setdefault("IMAGE_CAP_REPIN_INTERVAL", "0")
    env.update(extra or {})
    return env


def _run_shim(args, home, env, timeout=120):
    return subprocess.run(["bash", SHIM_SH] + args + ["--home", home],
                          capture_output=True, text=True, timeout=timeout,
                          env=env)


def _strikes_path(logs):
    return os.path.join(logs, "com.localtest.%s.strikes" % WD_SUFFIX)


def _read_strikes(logs):
    path = _strikes_path(logs)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read().split()


def _stop_shim(home, env, port):
    """Stop the background instance and wait out the port release."""
    try:
        _run_shim(["stop"], home, env, timeout=60)
    except subprocess.TimeoutExpired:
        pass
    deadline = time.time() + 15
    while time.time() < deadline and _port_open(port):
        time.sleep(0.3)
    return not _port_open(port)


_STUB_SERVER = """import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""


class WatchdogWiring(unittest.TestCase):
    """The install has to ship the external watcher with the service.

    KeepAlive relaunches a service that exits. A shim whose event loop has
    stopped answering never exits, so the launchd service alone is not a
    recovery plan: the timer that probes the health endpoint from outside the
    process is what recovers it. install-timer is the one entry point an
    operator uses, so the timer has to be part of that same install rather
    than a second command nobody learns about -- and uninstall, in both
    scripts, has to take it out again.
    """

    def test_install_timer_installs_the_watchdog_timer(self):
        src = _source(SHIM_SH)
        window = src[src.index("install-timer)"):][:300]
        self.assertIn("install_watchdog_plist", window,
                      "install-timer no longer installs the watchdog timer, so a hung loop stays hung until someone notices by hand")
        self.assertIn(
            'fleet_timer_install "$SHIM_WATCHDOG_LABEL"',
            src, "the watchdog timer is not a repeating launchd job any more")
        self.assertIn('"$SELF" "watchdog"', src,
                      "the timer no longer runs this script\'s watchdog subcommand")

    def test_uninstall_timer_removes_the_watchdog_timer(self):
        src = _source(SHIM_SH)
        window = src[src.index("uninstall-timer)"):][:300]
        self.assertIn("remove_watchdog_plist", window,
                      "uninstall-timer leaves the watchdog timer probing a service that is gone")

    def test_uninstall_sh_removes_the_watchdog_timer(self):
        src = _source(UNINSTALL)
        line = [ln for ln in src.splitlines() if ln.startswith("SUFFIXES=")][0]
        self.assertIn(WD_SUFFIX, line,
                      "uninstall.sh orphans the watchdog timer in launchd")

    def test_the_watchdog_knobs_reach_the_plist_and_a_foreground_run(self):
        """ENVPAIRS and EXPORTS are two lists that must not drift.

        A knob added to one only makes the launchd service and a `run` from a
        terminal behave differently, which is the worst shape for this bug:
        it reproduces in one and not in the other.
        """
        src = _source(SHIM_SH)
        envpairs = "".join(ln for ln in src.splitlines()
                           if ln.startswith("ENVPAIRS="))
        exports = "".join(ln for ln in src.splitlines()
                          if ln.startswith("EXPORTS="))
        for knob in ("IMAGE_CAP_WATCHDOG=", "IMAGE_CAP_WATCHDOG_INTERVAL=",
                     "IMAGE_CAP_WATCHDOG_TIMEOUT=",
                     "IMAGE_CAP_WATCHDOG_STRIKES="):
            self.assertIn(knob, envpairs,
                          "%s is not in the plist EnvironmentVariables" % knob)
            self.assertIn(knob, exports,
                          "%s is not exported by run/start" % knob)

    def test_the_timer_runs_the_probe_cycle(self):
        src = _source(SHIM_SH)
        window = src[src.index("  watchdog)"):][:200]
        self.assertIn("watchdog_cycle", window,
                      "the watchdog subcommand no longer runs a probe cycle")

    def test_the_external_probe_bypasses_an_ambient_proxy(self):
        """Loopback probing behind a proxy reads as a hang.

        The shim\'s own docstring records an ambient proxy answering its own
        503 for a target it never reached, which looks exactly like a wedged
        loop -- and would restart a perfectly healthy service.
        """
        src = _source(SHIM_SH)
        window = src[src.index("shim_probe() {"):][:400]
        self.assertIn("--noproxy", window,
                      "the watchdog probe can be answered by an ambient proxy, so a healthy shim looks hung")

class WatchdogStrikes(unittest.TestCase):
    """What one probe cycle does to the strike counter and the instance.

    These run the real script against a throwaway home, service dir and log
    dir, with the shim\'s port pointed at a free loopback port: no launchd job,
    no pidfile, and nothing here can touch the installed service.
    """

    def _setup(self, tmp, extra=None):
        port = _free_port()
        home = _shim_home(tmp)
        services = os.path.join(tmp, "services")
        logs = os.path.join(tmp, "logs")
        os.makedirs(services)
        os.makedirs(logs)
        env = _watchdog_env(home, services, logs, port, extra)
        return home, services, logs, port, env

    def test_one_failed_probe_writes_a_strike_and_restarts_nothing(self):
        """A single failure must not cost a restart.

        One queued 70-image rewrite blocks the loop for seconds without the
        service being lost, so the probe is deliberately slow to accuse: the
        first failure only records a strike.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home, _services, logs, port, env = self._setup(
                tmp, extra={"IMAGE_CAP_WATCHDOG_STRIKES": "3"})
            proc = _run_shim(["watchdog"], home, env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertIn("strike 1/3", proc.stdout,
                          "the first failed probe did not record a strike")
            self.assertNotIn(
                "restarting", proc.stdout,
                "one failed probe restarted the shim; a slow request would cost an outage")
            self.assertEqual(_read_strikes(logs)[1], "1",
                             "the strike counter was not written to disk")
            self.assertFalse(
                os.path.exists(os.path.join(logs, "stepfun-image-cap.pid")),
                "a watchdog cycle started an instance on its own")

    def test_a_strike_older_than_the_ttl_starts_over(self):
        """A machine asleep is not a consecutive failure.

        The timer fires every 30s, so a strike from before a sleep must
        expire: otherwise the first probe after wake restarts a shim that was
        never wedged.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home, _services, logs, port, env = self._setup(
                tmp, extra={"IMAGE_CAP_WATCHDOG_STRIKES": "2"})
            with open(_strikes_path(logs), "w", encoding="utf-8") as fh:
                fh.write("%d 1\n" % (int(time.time()) - 3600))
            proc = _run_shim(["watchdog"], home, env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertIn("strike 1/2", proc.stdout,
                          "the expired strike was counted, so a probe after wake could restart a healthy shim")
            self.assertNotIn("restarting", proc.stdout,
                             "an expired strike pushed the counter to the limit")

    def test_a_healthy_probe_clears_the_slate(self):
        """A pass resets the counter immediately, not after the TTL.

        Anything that answers on the port counts as healthy: the probe keys
        on the request completing, which is the event loop being alive.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home, _services, logs, port, env = self._setup(tmp)
            stub_path = os.path.join(tmp, "stub.py")
            with open(stub_path, "w", encoding="utf-8") as fh:
                fh.write(_STUB_SERVER)
            with open(_strikes_path(logs), "w", encoding="utf-8") as fh:
                fh.write("0 2\n")
            devnull = open(os.devnull, "w")
            server = subprocess.Popen(
                [sys.executable, stub_path, str(port)], stdout=devnull,
                stderr=devnull, stdin=subprocess.DEVNULL,
                start_new_session=True)
            self.addCleanup(self._kill, server)
            deadline = time.time() + 10
            while time.time() < deadline and not _port_open(port):
                time.sleep(0.2)
            self.assertTrue(_port_open(port), "the stub server never bound")

            proc = _run_shim(["watchdog"], home, env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertNotIn("strike", proc.stdout,
                             "a healthy probe was counted as a failure")
            self.assertEqual(_read_strikes(logs), ["0", "0"],
                             "a healthy probe did not reset the strike counter")

    def test_consecutive_failures_restart_the_instance(self):
        """The threshold is the whole point: repeated failures, then a restart.

        The restart takes the pidfile path here (the throwaway service dir
        holds no launchd job), which also pins that a recovered instance is
        healthy afterwards and that `stop` still owns the port.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home, _services, logs, port, env = self._setup(
                tmp, extra={"IMAGE_CAP_WATCHDOG_STRIKES": "2"})
            with open(_strikes_path(logs), "w", encoding="utf-8") as fh:
                fh.write("%d 1\n" % int(time.time()))
            self.addCleanup(_stop_shim, home, env, port)

            proc = _run_shim(["watchdog"], home, env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertIn("failed probes in a row", proc.stdout,
                          "the strike limit did not trigger a restart")
            self.assertIn(
                "recovered", proc.stdout,
                "the restarted instance never became healthy: %s" % proc.stdout[-500:])
            self.assertEqual(_read_strikes(logs), ["0", "0"],
                             "the strike counter was not reset after a restart")
            self.assertTrue(_port_open(port),
                            "the restarted instance is not listening")

            stop = _run_shim(["stop"], home, env)
            self.assertIn("stopped", stop.stdout, stop.stdout[-500:])
            self.assertTrue(_wait_port_closed(port),
                            "the restarted instance survived stop")

    def test_a_disabled_watchdog_probes_nothing(self):
        """IMAGE_CAP_WATCHDOG=0 has to be a real off switch."""
        with tempfile.TemporaryDirectory() as tmp:
            home, _services, logs, port, env = self._setup(
                tmp, extra={"IMAGE_CAP_WATCHDOG": "0"})
            proc = _run_shim(["watchdog"], home, env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertEqual(proc.stdout.strip(), "",
                             "the disabled watchdog still did something")
            self.assertIsNone(_read_strikes(logs),
                              "the disabled watchdog wrote a strike file")

    @staticmethod
    def _kill(server):
        try:
            server.terminate()
            server.wait(timeout=5)
        except Exception:
            try:
                server.kill()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
