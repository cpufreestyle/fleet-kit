#!/usr/bin/env python3
"""Local pass-through that de-duplicates and caps images before CC Switch.

Why this exists, in one sentence: StepFun's Plan API answers a request with 70
images and refuses the 71st with 400 images_too_many, and Codex re-sends its
whole history every turn, so a session that pastes screenshots eventually
crosses that line no matter what the operator does. tools/image_cap.py
documents the measurement; this service applies it.

The shim is a transparent forwarder, not a second bridge. It listens on
127.0.0.1 (default 15722, one above CC Switch's 15721 so the pair is obvious
in a port list), rewrites the body of the JSON endpoints it recognises when
the model is one of the configured ones, and hands everything else -- method,
path, query, headers, status, SSE stream -- to CC Switch untouched. A request
with no images, or fewer than the cap, is forwarded byte for byte.

Everything is configured through the environment so tools/stepfun_image_shim.sh
and the launchd service definition stay argument-free and identical:

    IMAGE_CAP_PORT      listen port            (default 15722)
    IMAGE_CAP_UPSTREAM  where to forward       (default http://127.0.0.1:15721)
    IMAGE_CAP_MAX       images kept per request (default 32; <= 0 = no cap)
    IMAGE_CAP_MODELS    comma-separated model substrings the cap applies to
                        (default "step"; empty = every model)

Failure policy: an unparseable body, an unknown path, an unreachable upstream
or a cap that would leave nothing behind all mean "forward what came in". The
shim sits in front of a working chain and must never be the thing that breaks
it; if the cap cannot be applied safely the request goes through exactly as it
arrived, which is the behaviour the operator already had.

The forwarder also bypasses any ambient HTTP_PROXY. CC Switch is on loopback,
and a proxy hop there turns a dead upstream into an answer that looks like it
came from CC Switch (see LOOPBACK_MOUNTS below).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import image_cap  # noqa: E402  (path set up above, like the bridges do)

HEALTH_PATH = "/__image_cap/health"

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


# The upstream is CC Switch on loopback, so this hop must never inherit
# HTTP_PROXY. Measured 2026-09-30: in a shell whose HTTP_PROXY points at a
# local proxy, httpx routed even 127.0.0.1 forwarding through it, and the proxy
# answered its own empty 503 for a target it would not reach -- so the shim's
# "upstream unreachable" branch never ran, the 503 was relayed as if CC Switch
# had produced it, and nothing in the log said the request was misrouted.
# checkin.py documents the same trap on the same day. Mounts with a None
# transport disable the proxy for loopback only; an upstream on another host
# still honours the operator's proxy settings. ("all://::1" is not a valid
# httpx pattern; the bracketed form is.)
LOOPBACK_MOUNTS = {pattern: None for pattern in (
    "all://127.0.0.1", "all://localhost", "all://[::1]")}


class Config:
    """Everything the shim needs, resolved once at startup."""

    def __init__(self, host: str, port: int, upstream: str, max_images: int,
                 models: str, connect_timeout: float = 15.0):
        self.host = host
        self.port = port
        self.upstream = upstream.rstrip("/")
        self.max_images = max_images
        self.models = models
        self.connect_timeout = connect_timeout

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
                            "IMAGE_CAP_UPSTREAM", "http://127.0.0.1:15721"))
    parser.add_argument("--max-images", type=int,
                        default=int(os.environ.get(
                            "IMAGE_CAP_MAX", str(image_cap.DEFAULT_MAX_IMAGES))))
    parser.add_argument("--models", default=os.environ.get("IMAGE_CAP_MODELS", "step"))
    args = parser.parse_args(argv)
    return Config(args.host, args.port, args.upstream, args.max_images, args.models)


class Stats:
    """Running counters, exposed on the health endpoint."""

    def __init__(self):
        self.requests = 0
        self.rewritten = 0
        self.images_seen = 0
        self.images_kept = 0
        self.passthrough = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


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

    async def pump(resp: httpx.Response):
        try:
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            await resp.aclose()

    @app.get(HEALTH_PATH)
    async def health():
        return {"ok": True, "upstream": config.upstream,
                "max_images": config.max_images, "models": config.models,
                "stats": stats.as_dict()}

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
        url = "%s/%s" % (config.upstream, path.lstrip("/"))
        if request.url.query:
            url += "?" + request.url.query

        try:
            upstream_request = client.build_request(
                request.method, url, headers=headers, content=body)
            resp = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"error": {"message": "upstream unreachable: %s" % str(exc)[:200],
                            "type": "upstream_unreachable"}},
                status_code=503)

        if note:
            print("[image-cap] %s" % note, flush=True)

        if resp.status_code != 200:
            content = await resp.aread()
            await resp.aclose()
            return Response(content=content, status_code=resp.status_code,
                            media_type=resp.headers.get("content-type", "application/json"))

        relayed = {name: value for name, value in resp.headers.items()
                   if name.lower() not in PASSTHROUGH_RESPONSE_HEADERS}
        return StreamingResponse(pump(resp), status_code=resp.status_code,
                                 headers=relayed)

    return app


def main(argv=None) -> None:
    import uvicorn

    config = parse_args(argv)
    app = build_app(config)
    banner = ("[stepfun-image-cap] :%d -> %s (max %d images, models: %s)"
              % (config.port, config.upstream, config.max_images,
                 config.models or "*"))
    print(banner, flush=True)
    uvicorn.run(app, host=config.host, port=config.port, log_level="info",
                access_log=False)


if __name__ == "__main__":
    main()
