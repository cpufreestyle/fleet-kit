"""End-to-end: the shim must make a 100-image Codex request survive.

A fake CC Switch stands in for the real one on both sides of the contract that
matters here: it answers 200 while a request carries at most 70 images and 400
images_too_many above that, exactly as measured on 127.0.0.1:15721 on
2026-09-30, and it reports back how many images it actually received. The
assertions are therefore about what the upstream sees, not about the shim's
own bookkeeping -- a rewrite that left the count at 100 would still pass a
unit test of cap_images().

The rest pins the transparency the operator relies on: a request with fewer
images than the cap is forwarded with its body intact, a model the cap does
not apply to is left alone, non-JSON paths are relayed verbatim, and an
unreachable upstream answers 503 instead of a bare 500.
"""
import importlib.util
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)

import image_cap  # noqa: E402

SHIM_PATH = os.path.join(TOOLS, "stepfun_image_shim.py")
_spec = importlib.util.spec_from_file_location("stepfun_image_shim", SHIM_PATH)
shim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shim)

CEILING = 70  # measured: 70 images -> 200, 71 -> 400 images_too_many


class _FakeUpstream:
    """CC Switch stand-in: 200 up to 70 images, 400 above, count echoed back."""

    def __init__(self):
        self.seen = []
        handler = self._make_handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def _reply(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.seen.append(("GET", self.path, None))
                self._reply(200, {"object": "list",
                                  "data": [{"id": "step-5-preview"}]})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {}
                count = image_cap.images_in(payload)
                outer.seen.append(("POST", self.path, count))
                if count > CEILING:
                    self._reply(400, {"error": {"message": "images_too_many",
                                                "type": "invalid_request_error"}})
                else:
                    self._reply(200, {"ok": True, "images": count})

        return Handler

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _Shim:
    """The real shim app on a real socket, in this process."""

    def __init__(self, upstream_port, max_images=32, models="step"):
        import uvicorn

        config = shim.Config("127.0.0.1", 0,
                             "http://127.0.0.1:%d" % upstream_port,
                             max_images, models)
        self.app = shim.build_app(config)
        self.config = config
        server = uvicorn.Server(uvicorn.Config(
            self.app, host="127.0.0.1", port=0, log_level="warning"))
        self.thread = threading.Thread(target=server.run,
                                       daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise AssertionError("shim server did not start")
        self.port = server.servers[0].sockets[0].getsockname()[1]
        self.server = server

    def close(self):
        """Stop the loop uvicorn never leaves on its own.

        uvicorn's main_loop only returns once should_exit is set, so every
        shim server this file starts used to keep a live event loop after its
        test, calling asyncio.sleep(0.1) for the rest of the session. Those are
        daemon threads that outlive their test, and in a full-suite run they
        land on whatever another test patched onto the process-wide
        asyncio.sleep -- measured 2026-09-30, the leaked loops pushed a retry
        budget of 3 past four million recorded sleeps, on a run where every
        file also passes in isolation.
        """
        self.server.should_exit = True
        self.thread.join(timeout=10)

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)


@pytest.fixture()
def upstream():
    server = _FakeUpstream()
    yield server
    server.close()


@pytest.fixture()
def client():
    with httpx.Client(timeout=30) as http:
        yield http



@pytest.fixture()
def site_factory():
    """Start the real shim app and stop it again when the test ends.

    See _Shim.close: an abandoned shim keeps an event loop running for the
    rest of the session, and that loop ticks asyncio.sleep into any
    process-wide patch another test installed before it.
    """
    opened = []

    def _open(upstream_port, **kwargs):
        site = _Shim(upstream_port, **kwargs)
        opened.append(site)
        return site

    yield _open

    for site in reversed(opened):
        site.close()

def _request_body(count, model="stepfun/step-5-preview"):
    content = [{"type": "input_text", "text": "这些图里有什么"}]
    content += [{"type": "input_image",
                 "image_url": "data:image/png;base64,iVBORw0KGgo=%d" % i}
                for i in range(count)]
    return {"model": model, "input": [{"role": "user", "content": content}]}
    return {"model": model, "input": [{"role": "user", "content": content}]}


def test_a_hundred_images_are_capped_before_the_upstream_sees_them(upstream, client, site_factory):
    site = site_factory(upstream.port)
    response = client.post(site.url("/v1/responses"), json=_request_body(100))
    assert response.status_code == 200
    assert response.json()["images"] <= shim.image_cap.DEFAULT_MAX_IMAGES
    method, path, count = upstream.seen[-1]
    assert (method, path) == ("POST", "/v1/responses")
    assert count <= shim.image_cap.DEFAULT_MAX_IMAGES


def test_the_default_cap_leaves_margin_under_the_measured_ceiling(upstream, client, site_factory):
    # 70 is the measured pass mark; the default must sit well under it, not on it.
    assert shim.image_cap.DEFAULT_MAX_IMAGES < CEILING
    site = site_factory(upstream.port, max_images=shim.image_cap.DEFAULT_MAX_IMAGES)
    body = _request_body(CEILING + 30)
    response = client.post(site.url("/v1/responses"), json=body)
    assert response.status_code == 200
    assert response.json()["images"] <= shim.image_cap.DEFAULT_MAX_IMAGES


def test_a_small_request_is_forwarded_with_its_body_intact(upstream, client, site_factory):
    site = site_factory(upstream.port)
    response = client.post(site.url("/v1/responses"), json=_request_body(3))
    assert response.status_code == 200
    assert response.json()["images"] == 3
    _method, _path, count = upstream.seen[-1]
    assert count == 3


def test_a_model_outside_the_filter_is_left_alone(upstream, client, site_factory):
    site = site_factory(upstream.port, models="step")
    response = client.post(site.url("/v1/responses"),
                           json=_request_body(CEILING + 20, model="gpt-5-codex"))
    # the fake upstream answers 400 above 70, and that is the point: nothing
    # was rewritten, so the shim never silently degrades another provider
    assert response.status_code == 400
    _method, _path, count = upstream.seen[-1]
    assert count == CEILING + 20


def test_an_empty_model_filter_caps_every_model(upstream, client, site_factory):
    site = site_factory(upstream.port, models="")
    response = client.post(site.url("/v1/responses"),
                           json=_request_body(CEILING + 20, model="gpt-5-codex"))
    assert response.status_code == 200
    assert response.json()["images"] <= shim.image_cap.DEFAULT_MAX_IMAGES


def test_a_non_json_path_is_relayed_verbatim(upstream, client, site_factory):
    site = site_factory(upstream.port)
    response = client.get(site.url("/v1/models"))
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "step-5-preview"
    assert upstream.seen[-1][0] == "GET"


def test_the_query_string_survives_the_hop(upstream, client, site_factory):
    site = site_factory(upstream.port)
    response = client.get(site.url("/v1/models?limit=1"))
    assert response.status_code == 200
    assert upstream.seen[-1][1] == "/v1/models?limit=1"


def test_health_reports_the_configuration(client, site_factory):
    site = site_factory(12345, max_images=48, models="step,glm")
    response = client.get(site.url(shim.HEALTH_PATH))
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["max_images"] == 48
    assert payload["models"] == "step,glm"
    assert payload["upstream"].endswith(":12345")


def test_an_unreachable_upstream_answers_503_not_a_bare_500(client, site_factory):
    site = site_factory(_closed_port())
    response = client.post(site.url("/v1/responses"), json=_request_body(2))
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "upstream_unreachable"


def test_a_malformed_json_body_is_forwarded_not_dropped(upstream, client, site_factory):
    site = site_factory(upstream.port)
    response = client.post(
        site.url("/v1/responses"), content=b"{not json at all",
        headers={"Content-Type": "application/json"})
    # the fake upstream counts 0 images and answers 200: the body survived
    assert response.status_code == 200
    assert response.json()["images"] == 0


def test_a_streaming_request_still_reaches_the_upstream(upstream, client, site_factory):
    site = site_factory(upstream.port)
    body = _request_body(2)
    body["stream"] = True
    response = client.post(site.url("/v1/responses"), json=body)
    assert response.status_code == 200
    assert response.json()["images"] == 2


def _closed_port() -> int:
    """A port nothing listens on, found by binding and releasing one.

    A raw socket to a closed port really is refused here (connection refused,
    errno 61), but a forward through httpx is not: this box exports
    HTTP_PROXY=http://127.0.0.1:1082, and httpx trusts the environment by
    default, so a forward to any unreachable address was handed to that proxy
    instead and answered with its own empty 503 -- which the shim then relayed
    faithfully, and the test proved nothing. Binding port 0 hands back a port
    the kernel just gave away, which stays closed long enough for a connect to
    fail once the shim stops inheriting the proxy (test below).
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _FakeProxy:
    """A stand-in for the ambient HTTP_PROXY this machine exports.

    Records every request it is asked to perform, so a test can prove the shim
    never asked it to perform one. Answers 200 with a marker body: if the shim
    ever does route through it, the response is still a valid HTTP answer and
    only the marker gives the mistake away.
    """

    def __init__(self):
        self.seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_POST(self):
                outer.seen.append(("POST", self.path))
                body = json.dumps({"proxied": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_a_loopback_forward_never_inherits_the_ambient_http_proxy(upstream, client,
                                                                monkeypatch,
                                                                site_factory):
    """A loopback forward that goes through HTTP_PROXY gets the proxy's answer
    instead of CC Switch's, which reads as a bridge failure.

    Measured 2026-09-30: httpx trusts the environment, this machine exports
    HTTP_PROXY, and the shim's forward to 127.0.0.1:15721 was handed to that
    proxy -- the request still worked, so nothing looked broken, until the
    upstream was unreachable and the proxy's own 503 came back as if CC Switch
    had refused the call. The client here is built before the env var is set,
    so the only client that can be misrouted is the shim's.
    """
    proxy = _FakeProxy()
    try:
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:%d" % proxy.port)
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:%d" % proxy.port)
        site = site_factory(upstream.port)
        response = client.post(site.url("/v1/responses"), json=_request_body(100))
        assert response.status_code == 200
        assert proxy.seen == []
        assert "proxied" not in response.json()
        assert response.json()["images"] <= shim.image_cap.DEFAULT_MAX_IMAGES
        assert upstream.seen[-1][0] == "POST"
    finally:
        proxy.close()
