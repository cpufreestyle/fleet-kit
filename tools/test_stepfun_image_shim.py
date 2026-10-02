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
import sqlite3
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

    def __init__(self, upstream_port, max_images=32, models="step",
                 upstream_url=None, watchdog=False, watchdog_strikes=None,
                 watchdog_interval=None, watchdog_timeout=None,
                 watchdog_kwargs=None):
        import uvicorn

        config = shim.Config("127.0.0.1", 0,
                             upstream_url or "http://127.0.0.1:%d"
                             % upstream_port,
                             max_images, models,
                             watchdog_strikes=watchdog_strikes,
                             watchdog_interval=watchdog_interval,
                             watchdog_timeout=watchdog_timeout)
        self.app = shim.build_app(config)
        self.config = config
        self.watchdog = None
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
        if watchdog:
            # Seams (probe/restart/running) only: the defaults would execv
            # this test process, which is the one state no test may reach.
            kwargs = dict(watchdog_kwargs or {})
            kwargs.setdefault("running", lambda: not self.server.should_exit)
            self.watchdog = shim.start_watchdog_thread(config, **kwargs)

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
        if self.watchdog is not None and self.watchdog.is_alive():
            # running() reads should_exit, so the thread exits by itself; join
            # it first so a watchdog that decides to restart never outlives
            # the site it was watching.
            self.server.should_exit = True
            self.watchdog.join(timeout=5)
        _shut_server(self)

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)


def _shut_server(site):
    site.server.should_exit = True
    site.thread.join(timeout=10)


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


def test_the_version_prefix_the_upstream_carries_is_not_repeated(
        upstream, client, site_factory):
    """The doubled /v1 that made StepFun answer 404 on 2026-09-30.

    The upstream default is an OpenAI base URL that already ends in /v1 and the
    pinned client base_url ends in /v1 too, so a plain join forwarded
    /v1/v1/models. What the upstream records here is the whole request line,
    which is the same difference StepFun prices as 401 against 404.
    """
    site = site_factory(upstream.port,
                        upstream_url="http://127.0.0.1:%d/v1" % upstream.port)
    response = client.get(site.url("/v1/models"))
    assert response.status_code == 200
    assert upstream.seen[-1][1] == "/v1/models"


def test_forward_path_leaves_a_different_first_segment_alone():
    """Only an exact duplicate is dropped, never a path that merely looks similar."""
    upstream = "https://api.stepfun.com/step_plan/v1"
    assert shim.forward_path(upstream, "responses") == "responses"
    assert shim.forward_path(upstream, "v1/responses") == "responses"
    assert shim.forward_path(upstream, "v1") == ""
    bare = "http://127.0.0.1:15721"
    assert shim.forward_path(bare, "v1/responses") == "v1/responses"
    versioned = "http://127.0.0.1:15721/v1"
    assert shim.forward_path(versioned, "v1/chat/completions") == (
        "chat/completions")


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


CC_DB_SCHEMA = """
CREATE TABLE providers (
    id TEXT NOT NULL,
    app_type TEXT NOT NULL,
    name TEXT NOT NULL,
    settings_config TEXT NOT NULL,
    PRIMARY KEY (id, app_type)
);
CREATE TABLE provider_endpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    app_type TEXT NOT NULL,
    url TEXT NOT NULL,
    added_at INTEGER
);
"""

CC_STEPFUN = "https://api.stepfun.com/step_plan/v1"
CC_SHIM = "http://127.0.0.1:15722/v1"
CC_SECOND = "a1b2c3d4-second-row"


def _cc_settings(base_url):
    import json
    config = (
        'model_provider = "custom"\n'
        '\n'
        '[model_providers.custom]\n'
        'name = "custom"\n'
        'base_url = "%s"\n'
        'wire_api = "responses"\n'
    ) % base_url
    return json.dumps({"config": config})


def _cc_db(path, rows):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(CC_DB_SCHEMA)
        for provider_id, app_type, name, target in rows:
            conn.execute(
                "insert into providers (id, app_type, name, settings_config)"
                " values (?, ?, ?, ?)",
                (provider_id, app_type, name, _cc_settings(target)))
            conn.execute(
                "insert into provider_endpoints (provider_id, app_type, url,"
                " added_at) values (?, ?, ?, ?)",
                (provider_id, app_type, target, int(time.time())))
        conn.commit()
    finally:
        conn.close()


def _cc_rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "select providers.name, provider_endpoints.app_type,"
            " provider_endpoints.url from providers join provider_endpoints"
            " on providers.id = provider_endpoints.provider_id"
            " order by providers.name").fetchall()
    finally:
        conn.close()


def _cc_config(db):
    config = shim.Config("127.0.0.1", 15722, CC_STEPFUN, 32, "step")
    config.cc_db = str(db)
    config.cc_pin_interval = 3600
    return config


def test_cc_pin_sweeps_every_codex_row_that_forwards_to_stepfun(tmp_path):
    # The measured bypass, 2026-10-01: this machine's CC Switch carries two
    # codex providers aimed at api.stepfun.com, only one of them named StepFun.
    # Pinning just the named one leaves selecting the other as a way to walk
    # straight past the cap, so the shim's pin has to move both.
    db = tmp_path / "cc-switch.db"
    _cc_db(db, [("stepfun-row", "codex", "StepFun", CC_STEPFUN),
                (CC_SECOND, "codex", "nv spark", CC_STEPFUN)])

    detail = shim.pin_cc_switch_endpoint(_cc_config(db))

    self_urls = sorted(url for _name, _app, url in _cc_rows(db))
    assert self_urls == [CC_SHIM, CC_SHIM], self_urls
    assert "nv spark" in detail, detail


def test_cc_pin_leaves_a_provider_forwarding_elsewhere_alone(tmp_path):
    db = tmp_path / "cc-switch.db"
    _cc_db(db, [("stepfun-row", "codex", "StepFun", CC_STEPFUN),
                (CC_SECOND, "codex", "MiniMax", "https://api.minimaxi.com/v1")])

    shim.pin_cc_switch_endpoint(_cc_config(db))

    rows = {(name, app): url for name, app, url in _cc_rows(db)}
    assert rows[("MiniMax", "codex")] == "https://api.minimaxi.com/v1"


def test_cc_pin_can_be_narrowed_to_the_named_provider(tmp_path):
    db = tmp_path / "cc-switch.db"
    _cc_db(db, [("stepfun-row", "codex", "StepFun", CC_STEPFUN),
                (CC_SECOND, "codex", "nv spark", CC_STEPFUN)])
    os.environ["IMAGE_CAP_CC_PIN_ALL"] = "0"
    try:
        config = _cc_config(db)
    finally:
        del os.environ["IMAGE_CAP_CC_PIN_ALL"]

    shim.pin_cc_switch_endpoint(config)

    rows = {(name, app): url for name, app, url in _cc_rows(db)}
    assert rows[("StepFun", "codex")] == CC_SHIM
    assert rows[("nv spark", "codex")] == CC_STEPFUN, (
        "IMAGE_CAP_CC_PIN_ALL=0 did not narrow the pin")


def test_cc_pin_reports_a_missing_database_without_raising(tmp_path):
    config = _cc_config(tmp_path / "absent.db")
    detail = shim.pin_cc_switch_endpoint(config)
    assert "cc-switch db not found" in detail


def test_cc_pin_is_off_when_the_interval_is_zero(tmp_path):
    db = tmp_path / "cc-switch.db"
    _cc_db(db, [("stepfun-row", "codex", "StepFun", CC_STEPFUN)])
    config = _cc_config(db)
    config.cc_pin_interval = 0

    detail = shim.pin_cc_switch_endpoint(config)

    assert "disabled" in detail
    assert _cc_rows(db)[0][2] == CC_STEPFUN, "a disabled pin still wrote"


def test_the_cc_pin_thread_pins_before_its_first_sleep(tmp_path):
    db = tmp_path / "cc-switch.db"
    _cc_db(db, [("stepfun-row", "codex", "StepFun", CC_STEPFUN)])
    config = _cc_config(db)
    config.cc_pin_interval = 3600

    thread = shim.start_cc_pin_thread(config)
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            if _cc_rows(db)[0][2] == CC_SHIM:
                break
            time.sleep(0.02)
        assert _cc_rows(db)[0][2] == CC_SHIM, "the first pass never ran"
    finally:
        if thread:
            thread.join(timeout=0.1)


def test_health_reports_the_cc_pin_outcome(tmp_path):
    db = tmp_path / "cc-switch.db"
    _cc_db(db, [("stepfun-row", "codex", "StepFun", CC_STEPFUN)])
    config = _cc_config(db)
    shim.pin_cc_switch_endpoint(config)

    health = shim.health_payload(config, shim.Stats())
    assert health["cc_pin"]["changed"] is True
    assert CC_SHIM in health["cc_pin"]["detail"]


# ---------- self-watchdog ----------
# The shim is occasionally found with its process alive and its loop no longer
# answering: launchd still reports state = running and a health probe never
# comes back. KeepAlive relaunches a service that exits and never fires for
# that, so a daemon thread probes this process's own health over a raw socket
# -- never through the loop it watches -- and replaces the process with a
# fresh copy of itself after two consecutive failures. These tests pin the
# escalation: the probe must survive a wedged loop, the accusation must need
# two failures in a row, and a server on its way out must be left alone.


def _wd_config(**kwargs):
    kwargs.setdefault("watchdog_interval", 0.05)
    kwargs.setdefault("watchdog_timeout", 0.2)
    kwargs.setdefault("watchdog_strikes", 2)
    return shim.Config("127.0.0.1", 0, "http://127.0.0.1:1", 32, "step",
                       **kwargs)


@pytest.fixture()
def clean_watchdog_state():
    """LAST_WATCHDOG is process-wide; give each test a known slate."""
    saved = dict(shim.LAST_WATCHDOG)
    shim.LAST_WATCHDOG.update({"detail": "not run yet", "strikes": 0,
                               "restarts": 0, "last_probe_ok": None})
    yield shim.LAST_WATCHDOG
    shim.LAST_WATCHDOG.clear()
    shim.LAST_WATCHDOG.update(saved)


def test_health_status_ok_reads_only_the_status_line():
    assert shim.health_status_ok(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
    assert shim.health_status_ok(b"HTTP/1.0 204 No Content\r\n\r\n")
    assert not shim.health_status_ok(b"HTTP/1.1 503 Service Unavailable\r\n\r\n")
    assert not shim.health_status_ok(b"HTTP/1.1 429 Too Many Requests\r\n\r\n")
    assert not shim.health_status_ok(b"")
    assert not shim.health_status_ok(b"connection reset by peer")


def test_probe_health_is_false_when_nothing_listens():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()
    assert shim.probe_health("127.0.0.1", dead_port, 1.0) is False


def test_probe_health_is_true_against_a_live_shim(upstream, site_factory):
    site = site_factory(upstream.port)
    assert shim.probe_health("127.0.0.1", site.port, 5.0) is True


def test_restart_argv_preserves_the_invocation():
    argv = shim.restart_argv()
    assert argv[0] == sys.executable
    assert argv[1] == os.path.abspath(shim.__file__)
    assert argv[2:] == sys.argv[1:]


def test_the_watchdog_restarts_the_shim_once_probes_keep_failing():
    restarts = []
    alive = {"v": True}

    def restart(log=None):
        restarts.append("restart")
        alive["v"] = False  # a real execv never returns

    thread = shim.start_watchdog_thread(
        _wd_config(), probe=lambda host, port, timeout: False,
        restart=restart, running=lambda: alive["v"])
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert restarts == ["restart"]


def test_the_watchdog_stays_quiet_while_the_probes_pass():
    restarts = []
    calls = {"n": 0}

    def probe(host, port, timeout):
        calls["n"] += 1
        return True

    thread = shim.start_watchdog_thread(
        _wd_config(), probe=probe,
        restart=lambda log=None: restarts.append("restart"),
        running=lambda: calls["n"] < 6)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert restarts == []
    assert calls["n"] >= 5


def test_the_watchdog_needs_the_failures_to_be_consecutive():
    """A failure, a good probe, then two failures: one restart, for the second
    pair alone -- two failures with a healthy probe between them are a blip,
    and restarting on them would kill a request that was only slow."""
    answers = iter([False, True, False, False, False, False, False, False])
    restarts = []
    alive = {"v": True}

    def probe(host, port, timeout):
        try:
            return next(answers)
        except StopIteration:
            return False

    def restart(log=None):
        restarts.append("restart")
        alive["v"] = False

    thread = shim.start_watchdog_thread(
        _wd_config(), probe=probe, restart=restart,
        running=lambda: alive["v"])
    thread.join(timeout=5)
    assert restarts == ["restart"]


def test_the_watchdog_never_restarts_a_server_that_is_exiting():
    """A shim shutting down on a signal must exit, not restart: execv from the
    watchdog is how a Ctrl-C would turn into an immortal process."""
    probed = []
    restarts = []
    thread = shim.start_watchdog_thread(
        _wd_config(), probe=lambda *a: probed.append(1) or False,
        restart=lambda log=None: restarts.append("restart"),
        running=lambda: False)
    thread.join(timeout=2)
    assert probed == []
    assert restarts == []


def test_the_watchdog_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("IMAGE_CAP_WATCHDOG", "0")
    config = shim.Config("127.0.0.1", 0, "http://127.0.0.1:1", 32, "step")
    assert config.watchdog is False
    assert shim.start_watchdog_thread(config) is None


def test_a_wedged_event_loop_trips_the_watchdog(upstream, client, site_factory,
                                                monkeypatch):
    """The failure itself: a loop blocked in a body rewrite.

    cap_images runs inside the request handler, so a slow one stops the whole
    event loop -- the process stays alive, launchd sees state = running, and
    every request in flight never comes back. The watchdog thread probes over
    its own socket, so it survives the loop it is watching and still restarts.
    """
    restarted = threading.Event()

    def wedged_cap_images(payload, max_images):
        time.sleep(30)
        return payload, {"images": 0, "unique": 0, "kept": 0,
                         "dropped_duplicate": 0, "dropped_cap": 0}

    monkeypatch.setattr(shim.image_cap, "cap_images", wedged_cap_images)
    site = site_factory(upstream.port, watchdog=True,
                        watchdog_strikes=2, watchdog_interval=0.3,
                        watchdog_timeout=1.0,
                        watchdog_kwargs={"probe": shim.probe_health,
                                         "restart": lambda log=None: restarted.set()})
    threading.Thread(target=lambda: client.post(
        site.url("/v1/responses"), json=_request_body(1)),
        daemon=True).start()
    assert restarted.wait(10), "a wedged loop was never restarted"


def test_health_reports_the_watchdog_outcome(upstream, client, site_factory,
                                             clean_watchdog_state):
    state = {"n": 0, "stop": False}

    def probe(host, port, timeout):
        state["n"] += 1
        if state["n"] >= 6:
            state["stop"] = True
        return state["n"] > 2  # two failures, then healthy again

    site = site_factory(upstream.port, watchdog=True, watchdog_strikes=5,
                        watchdog_interval=0.05, watchdog_timeout=0.1,
                        watchdog_kwargs={"probe": probe,
                                         "running": lambda: not state["stop"]})
    deadline = time.time() + 5
    body = {}
    while time.time() < deadline:
        body = client.get(site.url(shim.HEALTH_PATH)).json()
        if "recovered after 2 strikes" in body["watchdog"]["detail"]:
            break
        time.sleep(0.05)
    assert body["watchdog"]["enabled"] is True
    assert "recovered after 2 strikes" in body["watchdog"]["detail"]
    assert body["watchdog"]["strikes"] == 0
    assert body["watchdog"]["restarts"] == 0
    assert body["watchdog"]["last_probe_ok"] is True
