"""The governor must turn a StepFun 429 into latency, not into a circuit trip.

Measured 2026-10-01 on this machine: the Plan API answers the request that
exceeds its limit with 429 "concurrency reached, current: 11, limit: 10".
CC Switch counts that as an upstream failure, four of them open the codex
circuit, and the open circuit answers every later request with 503
"所有供应商已熔断" -- the intermittent outage the user kept hitting. Both
StepFun rows (the provider and nv spark) forward through the shim, so the
shim is where the account's whole demand is visible and can be held under
the limit.

These tests pin the behaviour the guarantee rests on, against the real shim
app on a real socket:

  * an upstream 429 is retried with backoff and the caller only ever sees
    the eventual answer -- the retry must not reach CC Switch as a failure;
  * retries exhausted relays the upstream 429 as-is (never a hang, never a
    fabricated 200);
  * the semaphore really caps in-flight upstream requests, and the slot is
    held for the whole stream, because StepFun counts the request until the
    answer finishes, not until it starts;
  * a request that waits past queue_timeout is refused locally with a 429
    that names itself, instead of dying at CC Switch's 90s first-byte
    budget;
  * the health endpoint reports the governor, because "is it queuing or
    giving up" is the first question when the 503s come back.
"""
import importlib.util
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)

SHIM_PATH = os.path.join(TOOLS, "stepfun_image_shim.py")
_spec = importlib.util.spec_from_file_location("stepfun_image_shim", SHIM_PATH)
shim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shim)

RATE_LIMIT_BODY = json.dumps({
    "error": {"message": "concurrency reached, current: 11, limit: 10",
              "type": "rate_limited"}}).encode()


class _ScriptedUpstream:
    """StepFun stand-in: scripted statuses, optional hold door, concurrency
    high-water mark, and an arrival order log."""

    def __init__(self):
        self.status_plan = []       # statuses in order; last entry repeats
        self.served = 0
        self.seen = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.hold = threading.Event()
        self.door = threading.Event()
        self.finished = []          # labels in completion order
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    label = json.loads(raw.decode()).get("model") or "?"
                except Exception:
                    label = "?"
                outer.seen += 1
                outer.in_flight += 1
                outer.max_in_flight = max(outer.max_in_flight,
                                          outer.in_flight)
                try:
                    if outer.hold.is_set():
                        outer.door.wait(timeout=15)
                    index = min(outer.served, len(outer.status_plan) - 1) \
                        if outer.status_plan else 0
                    status = outer.status_plan[index] \
                        if outer.status_plan else 200
                    outer.served += 1
                    if status == 429:
                        body = RATE_LIMIT_BODY
                        self.send_response(429)
                        self.send_header("content-type", "application/json")
                    elif status == "slow-stream":
                        self.send_response(200)
                        self.send_header("content-type", "text/event-stream")
                        # the exact byte count of the two chunks below: a
                        # wrong content-length leaks bytes into the next
                        # response on the kept-alive connection and the shim
                        # (correctly) reports an upstream framing error
                        self.send_header("content-length", "15")
                        self.end_headers()
                        self.wfile.write(b"first-chunk")
                        self.wfile.flush()
                        time.sleep(1.0)
                        self.wfile.write(b"tail")
                        self.wfile.flush()
                        outer.finished.append(label)
                        return
                    else:
                        body = json.dumps({
                            "id": "resp_stub", "model": label,
                            "output": []}).encode()
                        self.send_response(200)
                        self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    outer.finished.append(label)
                finally:
                    outer.in_flight -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()

    def close(self):
        self.server.shutdown()


class _Shim:
    """The real shim app on a real socket, in this process."""

    def __init__(self, upstream_port, **config):
        import uvicorn

        cfg = shim.Config("127.0.0.1", 0,
                          "http://127.0.0.1:%d" % upstream_port,
                          32, "step", **config)
        self.app = shim.build_app(cfg)
        self.config = cfg
        server = uvicorn.Server(uvicorn.Config(
            self.app, host="127.0.0.1", port=0, log_level="warning"))
        self.thread = threading.Thread(target=server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise AssertionError("shim server did not start")
        self.port = server.servers[0].sockets[0].getsockname()[1]
        self.server = server

    def close(self):
        # See test_stepfun_image_shim.py: an abandoned shim keeps its event
        # loop ticking into any process-wide asyncio.sleep patch.
        self.server.should_exit = True
        self.thread.join(timeout=10)

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)


@pytest.fixture()
def upstream():
    site = _ScriptedUpstream()
    yield site
    site.close()


@pytest.fixture()
def site_factory():
    opened = []

    def _open(upstream_port, **kwargs):
        site = _Shim(upstream_port, **kwargs)
        opened.append(site)
        return site

    yield _open
    for site in reversed(opened):
        site.close()


def _post(client, url, model="step-5-preview"):
    return client.post(url, json={"model": model,
                                  "input": "ping"}, timeout=30)


class TestRetry:
    """An upstream 429 never reaches the caller when a retry succeeds."""

    def test_429_is_retried_until_the_upstream_answers(self, upstream,
                                                        site_factory, client):
        upstream.status_plan = [429, 429, 200]
        site = site_factory(upstream.port, retry_429=3)
        response = _post(client, site.url("/v1/chat/completions"))
        assert response.status_code == 200
        assert upstream.served == 3
        health = client.get(site.url("/__image_cap/health")).json()
        assert health["stats"]["retried_429"] == 2

    def test_exhausted_retries_relay_the_429(self, upstream, site_factory,
                                             client):
        upstream.status_plan = [429]
        site = site_factory(upstream.port, retry_429=1)
        response = _post(client, site.url("/v1/chat/completions"))
        assert response.status_code == 429
        assert "concurrency reached" in response.text
        health = client.get(site.url("/__image_cap/health")).json()
        assert health["stats"]["upstream_429"] == 1


class TestGate:
    """The semaphore caps in-flight upstream requests, streams included."""

    def test_inflight_is_capped_and_the_slot_spans_the_stream(
            self, upstream, site_factory, client):
        upstream.status_plan = ["slow-stream", 200]
        upstream.hold.set()          # hold the second request at the door
        site = site_factory(upstream.port, max_inflight=1)
        results = {}

        def slow():
            results["slow"] = _post(client, site.url("/v1/chat/completions"),
                                    model="slow-one")

        def fast():
            time.sleep(0.4)          # let the stream take the only slot
            started = time.monotonic()
            results["fast"] = _post(client, site.url("/v1/chat/completions"),
                                    model="fast-two")
            results["fast_waited"] = time.monotonic() - started

        threads = [threading.Thread(target=slow),
                   threading.Thread(target=fast)]
        for thread in threads:
            thread.start()
        time.sleep(0.4)
        upstream.door.set()           # release the upstream's hold
        for thread in threads:
            thread.join(timeout=30)
        assert results["slow"].status_code == 200
        assert results["fast"].status_code == 200
        # the fast request waited for the stream to finish, it did not
        # overtake it: the slot is held until the answer ends
        assert results["fast_waited"] >= 0.5
        assert upstream.max_in_flight < 2
        assert upstream.finished == ["slow-one", "fast-two"]

    def test_queue_timeout_refuses_locally(self, upstream, site_factory,
                                           client):
        upstream.status_plan = [200]
        upstream.hold.set()
        site = site_factory(upstream.port, max_inflight=1, queue_timeout=0.4)
        first = {}

        def hold_request():
            first["resp"] = _post(client, site.url("/v1/chat/completions"),
                                  model="holder")

        thread = threading.Thread(target=hold_request)
        thread.start()
        time.sleep(0.3)               # the holder owns the only slot
        response = _post(client, site.url("/v1/chat/completions"),
                         model="queued-one")
        assert response.status_code == 429
        body = response.json()
        assert body["error"]["type"] == "local_queue_full"
        health = client.get(site.url("/__image_cap/health")).json()
        assert health["stats"]["queue_timeouts"] == 1
        upstream.door.set()
        thread.join(timeout=30)


class TestHealth:
    """The governor is readable without a shell."""

    def test_health_reports_the_gate_and_the_counters(
            self, upstream, site_factory, client):
        upstream.status_plan = [200]
        site = site_factory(upstream.port, max_inflight=5,
                            queue_timeout=30, retry_429=2)
        _post(client, site.url("/v1/chat/completions"))
        health = client.get(site.url("/__image_cap/health")).json()
        assert health["concurrency"] == {"max_inflight": 5,
                                        "queue_timeout": 30.0,
                                        "retry_429": 2}
        for key in ("inflight_now", "queued", "retried_429",
                    "queue_timeouts", "upstream_429"):
            assert key in health["stats"]
        assert health["stats"]["inflight_now"] == 0


@pytest.fixture()
def client():
    with httpx.Client(timeout=30) as http:
        yield http
