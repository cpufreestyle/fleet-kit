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


CONFIG_WITH_SIBLINGS = '''model_provider = "custom"
model = "stepfun/step-5-preview"

[model_providers.custom]
name = "custom"
base_url = "http://127.0.0.1:15721/v1"
wire_api = "responses"

[model_providers.other]
name = "other"
base_url = "http://127.0.0.1:15721"

[shell_environment_policy]
inherit = "all"
ANTHROPIC_BASE_URL = "http://127.0.0.1:15721"
'''


def _repin_config(port=15722):
    config = shim.Config("127.0.0.1", port, "http://127.0.0.1:15721", 32, "step")
    config.repin_interval = 300
    return config


def test_repin_points_a_clobbered_config_back_at_the_shim(tmp_path):
    """CC Switch rewrites the base_url on every provider switch, so the shim
    has to win it back on its own -- through the same surgical pin, at that:
    the sibling provider on 15721 and the ANTHROPIC_BASE_URL on the same port
    must survive, because repointing either of those breaks a working path.
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(CONFIG_WITH_SIBLINGS, encoding="utf-8")
    config = _repin_config()
    config.pin_config = str(config_file)

    logs = []
    detail = shim.repin_codex_base_url(config, log=logs.append)

    text = config_file.read_text(encoding="utf-8")
    assert 'base_url = "http://127.0.0.1:15722/v1"' in text
    assert 'base_url = "http://127.0.0.1:15721"' in text, "a sibling provider was repointed"
    assert 'ANTHROPIC_BASE_URL = "http://127.0.0.1:15721"' in text, "the Anthropic path was repointed"
    assert "15722" in detail
    assert logs and "[repin]" in logs[0]


def test_repin_pins_to_the_port_the_shim_is_actually_on(tmp_path):
    """The re-pin target must come from this shim's own host:port, so a
    deployment that moved the shim off 15722 is still pinned correctly instead
    of being pointed at a port nothing listens on.
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(CONFIG_WITH_SIBLINGS, encoding="utf-8")
    config = _repin_config(port=15999)
    config.pin_config = str(config_file)

    shim.repin_codex_base_url(config)

    assert "127.0.0.1:15999" in config_file.read_text(encoding="utf-8")


def test_repin_can_be_disabled(tmp_path):
    """IMAGE_CAP_REPIN_INTERVAL <= 0 turns the timer off; the config must then
    be left byte-for-byte alone rather than half-rewritten.
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(CONFIG_WITH_SIBLINGS, encoding="utf-8")
    config = _repin_config()
    config.pin_config = str(config_file)
    config.repin_interval = 0

    detail = shim.repin_codex_base_url(config)

    assert "disabled" in detail
    assert config_file.read_text(encoding="utf-8") == CONFIG_WITH_SIBLINGS


def test_repin_reports_a_missing_config_without_raising(tmp_path):
    """A timer that raised on a missing config would take the proxy down with
    it; the answer is a detail string the log can carry.
    """
    config = _repin_config()
    config.pin_config = str(tmp_path / "absent.toml")

    detail = shim.repin_codex_base_url(config)

    assert "no codex config" in detail


def test_start_repin_thread_returns_none_when_disabled():
    config = _repin_config()
    config.repin_interval = 0
    assert shim.start_repin_thread(config) is None


def test_the_repin_thread_pins_before_its_first_sleep(tmp_path):
    """launchd may restart the shim while Codex is already talking to CC
    Switch, so the first pass must run at startup, not one interval later.
    The interval here is an hour: only the immediate pass can pin.
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(CONFIG_WITH_SIBLINGS, encoding="utf-8")
    config = _repin_config()
    config.pin_config = str(config_file)
    config.repin_interval = 3600

    thread = shim.start_repin_thread(config)
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            if "15722" in config_file.read_text(encoding="utf-8"):
                break
            time.sleep(0.02)
        assert "15722" in config_file.read_text(encoding="utf-8")
        assert thread.daemon is True
    finally:
        if thread:
            thread.join(timeout=0.1)
