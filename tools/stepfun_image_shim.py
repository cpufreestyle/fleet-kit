#!/usr/bin/env python3
"""Local pass-through that de-duplicates and caps images below CC Switch.

Why this exists, in one sentence: StepFun's Plan API answers a request with 70
images and refuses the 71st with 400 images_too_many, and Codex re-sends its
whole history every turn, so a session that pastes screenshots eventually
crosses that line no matter what the operator does. tools/image_cap.py
documents the measurement; this service applies it.

The shim is a transparent forwarder, not a second bridge. It listens on
127.0.0.1 (default 15722, one above CC Switch's 15721 so the pair is obvious
in a port list), rewrites the body of the JSON endpoints it recognises when
the model is one of the configured ones, and hands everything else -- method,
path, query, headers, status, SSE stream -- to StepFun's Plan API untouched.
A request with no images, or fewer than the cap, is forwarded byte for byte.

It sits below CC Switch rather than above it, and that placement is the whole
lesson of this project's 2026-09-30 Go: with the shim in front, CC Switch owned
~/.codex/config.toml, rewrote the provider's base_url back to its own port on
every provider switch, and a Codex that was already running never re-read the
file -- measured, the config read 15722/v1 while the failing request still
arrived at CC Switch's own port and the shim's own health showed exactly one
self-test. Below CC Switch the cap applies on every path regardless of who
wrote which config, because CC Switch has to forward through the shim to reach
StepFun at all. The chain is

    Codex -> 127.0.0.1:15721 (CC Switch) -> 127.0.0.1:15722 (this shim)
          -> https://api.stepfun.com/step_plan/v1

and the shim's upstream is therefore a real upstream, never CC Switch, so the
two cannot route into a loop. tools/pin_cc_switch_endpoint.py is what points
CC Switch at the shim; this service re-runs it on a timer because an operator
who re-adds or re-edits the StepFun provider gets a fresh row pointing
straight at StepFun again.

Everything is configured through the environment so tools/stepfun_image_shim.sh
and the launchd service definition stay argument-free and identical:

    IMAGE_CAP_PORT      listen port            (default 15722)
    IMAGE_CAP_UPSTREAM  where to forward       (default
                        https://api.stepfun.com/step_plan/v1)
    IMAGE_CAP_MAX       images kept per request (default 32; <= 0 = no cap)
    IMAGE_CAP_MODELS    comma-separated model substrings the cap applies to
                        (default "step"; empty = every model)
    IMAGE_CAP_CC_DB     CC Switch database to keep pointed here
                        (default ~/.cc-switch/cc-switch.db)
    IMAGE_CAP_CC_PROVIDER, IMAGE_CAP_CC_APP_TYPE  the provider row to repoint
                        (default StepFun / codex)
    IMAGE_CAP_CC_PIN_INTERVAL  seconds between CC Switch re-points
                        (default 300; <= 0 disables)
    IMAGE_CAP_REPIN_INTERVAL  seconds between Codex config re-pins
                        (default 0, i.e. off; pin_cc_switch_endpoint.py records
                         why the file pin is only a fallback now)
    IMAGE_CAP_PIN_CONFIG      Codex config to keep pinned
                        (default ~/.codex/config.toml)

Concurrency governor (added 2026-10-01 after a week of intermittent 503s):

    IMAGE_CAP_MAX_INFLIGHT    requests forwarded upstream at once
                        (default 8, under the Plan API's measured limit
                         of 10 concurrent requests on this account)
    IMAGE_CAP_QUEUE_TIMEOUT   seconds a request may wait for a slot
                        (default 75, under CC Switch's 90s first-byte
                         timeout; on expiry the shim answers 429 itself)
    IMAGE_CAP_429_RETRIES     retries with backoff on an upstream 429
                        (default 3)

Failure policy: an unparseable body, an unknown path, an unreachable upstream
or a cap that would leave nothing behind all mean "forward what came in". The
shim sits in front of a working chain and must never be the thing that breaks
it; if the cap cannot be applied safely the request goes through exactly as it
arrived, which is the behaviour the operator already had.

The same policy covers the CC Switch pin. CC Switch caches its routing table in
memory, and an operator who re-adds or re-edits the StepFun provider gets a row
pointing straight at StepFun again, which quietly takes the shim out of the
path for every later request. The shim therefore re-points it on a timer, best
effort, logging and moving on when it cannot. The Codex config.toml pin it used
to carry is off by default: measured 2026-09-30, CC Switch rewrote that file
back while Codex was already running, so the pin lost the file race and changed
nothing -- the routing table below it is the lever that actually holds.

The forwarder also bypasses any ambient HTTP_PROXY for loopback: a proxy hop
there turns a dead upstream into an answer that looks like it came from the
target. An upstream on another host -- api.stepfun.com by default -- still
honours the operator's proxy settings, which on this box is a measured working
route to that host (see LOOPBACK_MOUNTS below; a 401 from the real API means
the hop worked).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import threading
import time
import sys

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import image_cap  # noqa: E402  (path set up above, like the bridges do)

HEALTH_PATH = "/__image_cap/health"

# The CC Switch pin runs on a timer that has no handle on build_app()'s closure,
# so its last outcome lives here and the health endpoint reports it: without
# it, the only way to tell whether the cap is actually in the request path is
# to read the shim's log, which is not something an operator does at 3am.
LAST_CC_PIN = {"detail": "not run yet", "changed": False}

DEFAULT_CC_DB = "~/.cc-switch/cc-switch.db"
DEFAULT_CC_PROVIDER = "StepFun"
DEFAULT_CC_APP_TYPE = "codex"
DEFAULT_CC_PIN_INTERVAL = 300.0

# Request headers that must not be relayed: they describe our hop, not the
# client's, and a stale content-length on a rewritten body is a hung request.
HOP_REQUEST_HEADERS = frozenset({
    "host", "content-length", "transfer-encoding", "connection", "keep-alive",
    "upgrade", "proxy-authorization", "te", "trailer",
})

# Response headers relayed as-is. content-length and content-encoding are
# dropped because the body is re-emitted decoded (aiter_bytes decompresses),
# and forwarding either would make the client reject a correct body.
PASSTHROUGH_RESPONSE_HEADERS = frozenset({
    "content-length", "content-encoding", "transfer-encoding", "connection",
    "keep-alive",
})

# Path suffixes whose JSON body may carry images. /v1/responses is Codex's
# wire API here, /v1/chat/completions the one CC Switch also serves, and
# /v1/messages the Anthropic spelling the same local port answers.
CAP_PATH_SUFFIXES = ("/responses", "/chat/completions", "/messages")


def forward_path(upstream: str, path: str) -> str:
    """Drop a leading path segment the upstream base already ends with.

    Both ends of the wire carry the version prefix and concatenating them
    doubles it. The upstream default is an OpenAI base URL that already ends
    in /v1 (https://api.stepfun.com/step_plan/v1), and the pinned client base_url
    is http://127.0.0.1:15722/v1, so Codex sends GET/POST /v1/responses and a
    plain join forwards https://api.stepfun.com/step_plan/v1/v1/responses.
    Measured 2026-09-30: that doubled path answers 404 while
    /step_plan/v1/responses answers 401 for the same bodyless probe -- the 404
    reaches the operator as "unexpected status 404 Not Found: Unknown error",
    which says nothing about either half being duplicated.

    Only an exact leading-segment match is dropped, so an upstream without a
    version suffix (a bare http://127.0.0.1:15721 in the tests) is unaffected
    and a genuinely different first segment is forwarded untouched.
    """
    upstream_tail = upstream.rstrip("/").rsplit("/", 1)[-1]
    rest = path.lstrip("/")
    head, _, remainder = rest.partition("/")
    if upstream_tail and head == upstream_tail:
        return remainder.lstrip("/")
    return rest


# A loopback hop must never inherit HTTP_PROXY. Measured 2026-09-30: in a shell
# whose HTTP_PROXY points at a local proxy, httpx routed even 127.0.0.1
# forwarding through it, and the proxy answered its own empty 503 for a target
# it would not reach -- so the shim's "upstream unreachable" branch never ran,
# the 503 was relayed as if the target had produced it, and nothing in the log
# said the request was misrouted. checkin.py documents the same trap on the same
# day. Mounts with a None transport disable the proxy for loopback only; the
# default upstream is now a remote host, so that path still honours the
# operator's proxy settings, which is a working route here. ("all://::1" is not
# a valid httpx pattern; the bracketed form is.)
LOOPBACK_MOUNTS = {pattern: None for pattern in (
    "all://127.0.0.1", "all://localhost", "all://[::1]")}


class Config:
    """Everything the shim needs, resolved once at startup."""

    def __init__(self, host: str, port: int, upstream: str, max_images: int,
                 models: str, connect_timeout: float = 15.0, cc_db: str = "",
                 cc_provider: str = DEFAULT_CC_PROVIDER,
                 cc_app_type: str = DEFAULT_CC_APP_TYPE,
                cc_pin_interval: float | None = None,
                cc_pin_all: bool | None = None,
                max_inflight: int | None = None,
                queue_timeout: float | None = None,
                retry_429: int | None = None):
        self.host = host
        self.port = port
        self.upstream = upstream.rstrip("/")
        self.max_images = max_images
        self.models = models
        self.repin_interval = float(
            os.environ.get("IMAGE_CAP_REPIN_INTERVAL", "0"))
        self.pin_config = (os.environ.get("IMAGE_CAP_PIN_CONFIG")
                           or os.path.expanduser("~/.codex/config.toml"))
        # The CC Switch pin is the one that holds (see the module docstring), so
        # it carries its own database, its own provider row and its own cadence
        # rather than sharing the file pin's.
        self.cc_db = (cc_db or os.environ.get("IMAGE_CAP_CC_DB")
                      or os.path.expanduser(DEFAULT_CC_DB))
        self.cc_provider = (os.environ.get("IMAGE_CAP_CC_PROVIDER")
                            or cc_provider)
        self.cc_app_type = (os.environ.get("IMAGE_CAP_CC_APP_TYPE")
                            or cc_app_type)
        self.cc_pin_interval = float(
            DEFAULT_CC_PIN_INTERVAL if cc_pin_interval is None
            else cc_pin_interval)
        # Sweep every StepFun-forwarding codex row, not just the one called
        # "StepFun": this machine also carries "nv spark" aimed at the same
        # upstream, and selecting it in the CC Switch UI would otherwise step
        # around the cap. Set IMAGE_CAP_CC_PIN_ALL=0 for the narrow behaviour.
        self.cc_pin_all = (os.environ.get("IMAGE_CAP_CC_PIN_ALL", "1")
                           .strip().lower() not in ("0", "false", "no", "off")
                           if cc_pin_all is None else bool(cc_pin_all))
        self.connect_timeout = connect_timeout
        # Counted-concurrency governor. The Plan API answers the request
        # that exceeds its limit with 429 "concurrency reached" -- measured
        # 2026-10-01: current 11, limit 10 -- and CC Switch counts an
        # upstream error, four of them open the codex circuit, and the open
        # circuit answers every later request with 503 "所有供应商已熔断".
        # Every StepFun hop on this machine forwards through here (both the
        # StepFun row and nv spark), so the shim is the one place that sees
        # the account's whole demand; the defaults sit under the limit so
        # the fleet's own burst can never be the request that 429s.
        self.max_inflight = int(
            os.environ.get("IMAGE_CAP_MAX_INFLIGHT", "8")
            if max_inflight is None else max_inflight)
        self.queue_timeout = float(
            os.environ.get("IMAGE_CAP_QUEUE_TIMEOUT", "75")
            if queue_timeout is None else queue_timeout)
        self.retry_429 = int(
            os.environ.get("IMAGE_CAP_429_RETRIES", "3")
            if retry_429 is None else retry_429)
        self.gate = asyncio.Semaphore(self.max_inflight)

    @property
    def model_filters(self):
        return [m.strip().lower() for m in (self.models or "").split(",") if m.strip()]

    def applies_to(self, model: str) -> bool:
        """True when the cap should be applied to this model id."""
        filters = self.model_filters
        if not filters:
            return True
        low = (model or "").lower()
        return any(f in low for f in filters)


def parse_args(argv=None) -> Config:
    parser = argparse.ArgumentParser(
        description="de-duplicate and cap images before CC Switch")
    parser.add_argument("--host", default=os.environ.get("IMAGE_CAP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("IMAGE_CAP_PORT", "15722")))
    parser.add_argument("--upstream",
                        default=os.environ.get(
                            "IMAGE_CAP_UPSTREAM",
                            "https://api.stepfun.com/step_plan/v1"))
    parser.add_argument("--max-images", type=int,
                        default=int(os.environ.get(
                            "IMAGE_CAP_MAX", str(image_cap.DEFAULT_MAX_IMAGES))))
    parser.add_argument("--models", default=os.environ.get("IMAGE_CAP_MODELS", "step"))
    parser.add_argument("--max-inflight", type=int, default=None,
                        help="requests forwarded upstream at once"
                             " (env IMAGE_CAP_MAX_INFLIGHT, default 8)")
    parser.add_argument("--queue-timeout", type=float, default=None,
                        help="seconds a request may wait for a slot"
                             " (env IMAGE_CAP_QUEUE_TIMEOUT, default 75)")
    parser.add_argument("--retry-429", type=int, default=None,
                        help="retries with backoff on an upstream 429"
                             " (env IMAGE_CAP_429_RETRIES, default 3)")
    args = parser.parse_args(argv)
    return Config(args.host, args.port, args.upstream, args.max_images,
                  args.models,
                  max_inflight=args.max_inflight,
                  queue_timeout=args.queue_timeout,
                  retry_429=args.retry_429)


def health_payload(config, stats):
    """What /health reports, including whether the CC Switch pin still holds.

    The cc_pin block is here because it is the one piece of shim state an
    operator has to be able to read without a shell: when it stops holding, the
    cap silently leaves the request path and the 400 comes back.
    """
    return {"ok": True, "upstream": config.upstream,
            "max_images": config.max_images, "models": config.models,
            "concurrency": {"max_inflight": config.max_inflight,
                            "queue_timeout": config.queue_timeout,
                            "retry_429": config.retry_429},
            "cc_pin": dict(LAST_CC_PIN), "stats": stats.as_dict()}


class Stats:
    """Running counters, exposed on the health endpoint."""

    def __init__(self):
        self.requests = 0
        self.rewritten = 0
        self.images_seen = 0
        self.images_kept = 0
        self.passthrough = 0
        self.inflight_now = 0
        self.queued = 0
        self.retried_429 = 0
        self.queue_timeouts = 0
        self.upstream_429 = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def repin_codex_base_url(config, log=None) -> str:
    """Point Codex's custom provider at this shim and keep it there.

    CC Switch owns ~/.codex/config.toml and rewrites the custom provider's
    base_url back to 127.0.0.1:15721 the moment the operator switches
    providers. A pin run once at setup therefore survives only until the next
    switch, and one switch is enough to send Codex straight past the shim while
    the images fall back to the 400 the shim exists to prevent. tools/
    pin_shim_base_url.py does the rewrite; this runs it against this shim's
    own host:port, so a config pointed anywhere else is still left alone.

    Best effort by design: every outcome comes back as a string and nothing
    raises, so a timer can log and carry on. A shim that stopped proxying
    because its re-pin raised would be a worse failure than an un-pinned one.
    """
    if config.repin_interval <= 0:
        return "re-pin disabled (IMAGE_CAP_REPIN_INTERVAL=%.0f)" % (
            config.repin_interval)
    path = config.pin_config
    if not os.path.isfile(path):
        return "no codex config at %s" % path
    try:
        import pin_shim_base_url
    except Exception as exc:  # pragma: no cover - same directory, always there
        return "re-pin unavailable: %s" % exc
    hostport = "%s:%d" % (config.host, config.port)
    try:
        changed, detail = pin_shim_base_url.pin_once(
            path, to_hostport=hostport)
    except Exception as exc:
        return "re-pin failed: %s" % exc
    if changed:
        detail = "%s (-> %s)" % (detail, hostport)
        if log:
            log("[repin] %s" % detail)
    return detail


def start_repin_thread(config, log=None):
    """Re-pin now, then every config.repin_interval seconds.

    The first pass runs immediately because launchd may have restarted the shim
    while Codex was already talking to CC Switch. Returns the daemon thread,
    or None when the interval disables the feature.
    """
    if config.repin_interval <= 0:
        return None

    def loop():
        while True:
            repin_codex_base_url(config, log=log)
            time.sleep(config.repin_interval)

    thread = threading.Thread(target=loop, name="fleetkit-image-cap-repin",
                             daemon=True)
    thread.start()
    return thread


def pin_cc_switch_endpoint(config, log=None) -> str:
    """Repoint CC Switch's StepFun provider at this shim and keep it there.

    This is the pin that actually holds, and it replaced the file pin on
    2026-09-30 after the measurement recorded in tools/
    pin_cc_switch_endpoint.py: CC Switch owns ~/.codex/config.toml, so pointing
    Codex at the shim loses the file race the moment a provider is switched, and
    a Codex already running does not re-read the file either. Repointing CC
    Switch's own routing table at the shim puts the cap in the path for every
    route -- which is what the shim's one-self-test health count proved was
    missing -- and this shim then forwards to StepFun directly, so the two can
    never route into each other.

    A provider row that was re-added or re-edited by hand points at StepFun
    again, which is why this runs on a timer instead of once at setup. Best
    effort by design: every outcome comes back as a string and nothing raises,
    so the thread can log and carry on even while the database is locked or CC
    Switch is mid-write.
    """
    if config.cc_pin_interval <= 0:
        return "cc pin disabled (IMAGE_CAP_CC_PIN_INTERVAL=%.0f)" % (
            config.cc_pin_interval)
    try:
        import pin_cc_switch_endpoint
    except Exception as exc:  # pragma: no cover - same directory, always there
        return "cc pin unavailable: %s" % exc
    try:
        shim = pin_cc_switch_endpoint.shim_base_url(config.host, config.port)
        changed, detail = pin_cc_switch_endpoint.pin_once(
            config.cc_db, shim, config.cc_provider, config.cc_app_type,
            sweep=config.cc_pin_all)
    except Exception as exc:
        return "cc pin failed: %s" % exc
    LAST_CC_PIN["detail"] = detail
    LAST_CC_PIN["changed"] = bool(changed)
    if changed and log:
        log("[cc-pin] %s" % detail)
    return detail


def start_cc_pin_thread(config, log=None):
    """Re-point CC Switch now, then every config.cc_pin_interval seconds.

    The first pass runs immediately because launchd may have restarted the shim
    while a fresh StepFun provider row already pointed past it. Returns the
    daemon thread, or None when the interval disables the feature.
    """
    if config.cc_pin_interval <= 0:
        return None

    def loop():
        while True:
            pin_cc_switch_endpoint(config, log=log)
            time.sleep(config.cc_pin_interval)

    thread = threading.Thread(target=loop, name="fleetkit-image-cap-cc-pin",
                             daemon=True)
    thread.start()
    return thread


def build_app(config: Config):
    app = FastAPI(title="fleetkit-stepfun-image-cap", version="1.0.0")
    stats = Stats()
    # No read timeout: an SSE answer from a reasoning model can legitimately
    # run for many minutes, and cutting it off mid-stream is worse than
    # waiting. connect_timeout still bounds a dead upstream.
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(None, connect=config.connect_timeout),
        mounts=LOOPBACK_MOUNTS)

    def rewrite_body(raw: bytes):
        """Return (bytes to forward, log line or None). Never raises."""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return raw, None
        if not isinstance(payload, dict):
            return raw, None
        model = payload.get("model") or ""
        if not config.applies_to(model):
            return raw, None
        payload, info = image_cap.cap_images(payload, config.max_images)
        if not info["images"] or (info["dropped_duplicate"] == 0
                                   and info["dropped_cap"] == 0):
            return raw, None
        stats.rewritten += 1
        stats.images_seen += info["images"]
        stats.images_kept += info["kept"]
        return json.dumps(payload, ensure_ascii=False).encode("utf-8"), (
            "model=%s images=%d unique=%d kept=%d dup_dropped=%d cap_dropped=%d"
            % (model, info["images"], info["unique"], info["kept"],
               info["dropped_duplicate"], info["dropped_cap"]))

    def is_cap_path(path: str) -> bool:
        return any(path.endswith(suffix) for suffix in CAP_PATH_SUFFIXES)

    @app.get(HEALTH_PATH)
    async def health():
        return health_payload(config, stats)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH",
                                            "DELETE", "HEAD", "OPTIONS"])
    async def proxy(request: Request, path: str):
        stats.requests += 1
        body = await request.body()
        note = None
        if body and is_cap_path("/" + path.lstrip("/")):
            body, note = rewrite_body(body)
        else:
            stats.passthrough += 1

        headers = {name: value for name, value in request.headers.items()
                   if name.lower() not in HOP_REQUEST_HEADERS}
        joined = "%s/%s" % (config.upstream,
                            forward_path(config.upstream, path))
        url = joined.rstrip("/") or config.upstream
        if request.url.query:
            url += "?" + request.url.query

        # Take a slot before touching the upstream. The account's 10
        # concurrent requests are shared by every Codex thread, subagent and
        # probe on this machine, and the one that arrives eleventh used to be
        # answered 429 by StepFun, counted as a failure by CC Switch, and
        # eventually trip the circuit that 503s everything. Queueing here
        # turns that burst into latency on one request instead of an outage
        # on all of them; the timeout stays under CC Switch's 90s
        # first-byte budget so a queued request never dies at that layer.
        stats.queued += 1
        try:
            await asyncio.wait_for(config.gate.acquire(),
                                   timeout=config.queue_timeout)
        except asyncio.TimeoutError:
            stats.queued -= 1
            stats.queue_timeouts += 1
            print("[concurrency] queue full after %.0fs, refusing locally"
                  % config.queue_timeout, flush=True)
            return JSONResponse(
                {"error": {"message": "stepfun queue full after %.0fs; the"
                            " account is at its concurrency limit"
                            % config.queue_timeout,
                            "type": "local_queue_full"}},
                status_code=429)
        stats.queued -= 1
        stats.inflight_now += 1

        def release():
            config.gate.release()
            stats.inflight_now -= 1

        try:
            resp = None
            for attempt in range(config.retry_429 + 1):
                # rebuilt per attempt: a sent stream request cannot be replayed
                upstream_request = client.build_request(
                    request.method, url, headers=headers, content=body)
                try:
                    resp = await client.send(upstream_request, stream=True)
                except httpx.HTTPError as exc:
                    release()
                    return JSONResponse(
                        {"error": {"message": "upstream unreachable: %s"
                                   % str(exc)[:200],
                                   "type": "upstream_unreachable"}},
                        status_code=503)
                if resp.status_code != 429:
                    break
                # A 429 means this request was still born over the limit --
                # other clients share the key -- so hold it back and retry
                # instead of forwarding a failure to CC Switch's circuit.
                stats.retried_429 += 1
                content = await resp.aread()
                await resp.aclose()
                if attempt >= config.retry_429:
                    stats.upstream_429 += 1
                    release()
                    return Response(content=content, status_code=429,
                                    media_type=resp.headers.get(
                                        "content-type", "application/json"))
                delay = min(0.5 * (2 ** attempt), 8.0) * (0.5 + random.random())
                print("[concurrency] upstream 429, retry %d/%d in %.1fs"
                      % (attempt + 1, config.retry_429, delay), flush=True)
                await asyncio.sleep(delay)
        except BaseException:
            release()
            raise

        if note:
            print("[image-cap] %s" % note, flush=True)

        if resp.status_code != 200:
            content = await resp.aread()
            await resp.aclose()
            release()
            return Response(content=content, status_code=resp.status_code,
                            media_type=resp.headers.get("content-type", "application/json"))

        relayed = {name: value for name, value in resp.headers.items()
                   if name.lower() not in PASSTHROUGH_RESPONSE_HEADERS}

        async def pump_gated(resp: httpx.Response):
            # The slot is held for the whole stream: StepFun counts the
            # request until the answer finishes, not until it starts.
            try:
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk
            finally:
                await resp.aclose()
                release()

        return StreamingResponse(pump_gated(resp), status_code=resp.status_code,
                                 headers=relayed)

    return app


def main(argv=None) -> None:
    import uvicorn

    config = parse_args(argv)
    app = build_app(config)
    # Best effort: log the outcomes, never let a failed pin stop the proxy.
    start_cc_pin_thread(config, log=lambda line: print(line, flush=True))
    start_repin_thread(config, log=lambda line: print(line, flush=True))
    banner = ("[stepfun-image-cap] :%d -> %s (max %d images, models: %s)"
              % (config.port, config.upstream, config.max_images,
                 config.models or "*"))
    print("%s; gate %d in flight, %.0fs queue, %d x429 retries"
          % (banner, config.max_inflight, config.queue_timeout,
             config.retry_429), flush=True)
    uvicorn.run(app, host=config.host, port=config.port, log_level="info",
                access_log=False)


if __name__ == "__main__":
    main()
