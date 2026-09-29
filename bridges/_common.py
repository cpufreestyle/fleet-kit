"""Shared scaffolding for the FastAPI-based FleetKit bridges.

Every FastAPI bridge (qwen, xhx, lingxi, codely, trae, zcode, cline, qoder)
started life as a copy of the first one, so the same fifteen lines of
plumbing -- the app object, the lazily created httpx client, the bearer-key
check, the /health payload, the SSE pump, the uvicorn entrypoint -- were
copied eight times and had already drifted apart.

This module keeps that plumbing in one place. Each bridge still owns
everything provider-specific (upstream URLs, credentials, model remapping,
catalog shape); it just stops re-implementing the parts that are identical.

Import it the way the bridges already import _platform:

    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
    import _common

Helpers live here only when they are behaviour-preserving across providers;
anything a bridge needs to do differently stays in that bridge.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse


def vendor_httpx():
    """Return httpx, preferring a bridge's own .venv when FLEET_PYTHON lacks it.

    Several bridges are vendored from npm modules that ship their own venv
    containing fastapi/httpx. install.sh picks a python that can import them,
    but a hand-run bridge may not. Adding the sibling venv to sys.path keeps
    the bridge alive instead of dying at import time.
    """
    try:
        import httpx as _httpx
        return _httpx
    except ImportError:
        pass
    venv = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), os.pardir, ".venv"))
    for sub in ("lib/python3.14/site-packages", "lib/python3.13/site-packages",
                "lib/python3.12/site-packages", "lib/python3.11/site-packages",
                "Lib/site-packages"):
        cand = os.path.join(venv, sub)
        if os.path.isdir(cand) and cand not in sys.path:
            sys.path.insert(0, cand)
    import httpx as _httpx
    return _httpx


def client_kwargs(timeout: float, proxy: str = "", connect: float = 15.0,
                  headers: Optional[dict] = None) -> dict:
    """httpx.AsyncClient kwargs that every bridge builds the same way."""
    kw: dict = {"timeout": httpx.Timeout(timeout, connect=connect)}
    if proxy:
        kw["proxy"] = proxy
    if headers:
        kw["headers"] = headers
    return kw


def make_client_getter(**kwargs):
    """Build a client() that lazily creates and revives one shared AsyncClient.

    The bridges only ever pass literal kwargs, so they are captured once here
    rather than rebuilt on every call (which is what the copies did).
    """
    frozen = dict(kwargs)
    state: dict = {"http": None}

    def client() -> httpx.AsyncClient:
        http = state["http"]
        if http is None or http.is_closed:
            http = httpx.AsyncClient(**frozen)
            state["http"] = http
        return http

    return client


def make_app(title: str, version: str = "") -> FastAPI:
    """The app = FastAPI(...) line, identical in the FastAPI bridges."""
    if version:
        return FastAPI(title=title, version=version)
    return FastAPI(title=title)


def safe_header_value(value: str, max_chars: int = 200) -> str:
    """Make an upstream diagnostic usable as an HTTP header value.

    A gateway error body is a JSON blob full of newlines. h11 rejects any
    header value containing them, and it rejects with LocalProtocolError
    mid-response, so uvicorn aborts the connection and the client gets an
    empty reply instead of the bridge's own fallback payload -- the exact
    opposite of the graceful degradation the caller wanted. Collapsing the
    whitespace keeps the diagnostic and the request.
    """
    flat = " ".join(str(value).split())
    return flat[:max_chars]


def check_bridge_auth(request: Request, bridge_key: str) -> None:
    """Local access control shared by the bridges.

    An unset key leaves the bridge open (it only ever listens on 127.0.0.1);
    a set key requires the exact Authorization: Bearer <key> header.
    """
    if not bridge_key:
        return
    if (request.headers.get("authorization") or "") != f"Bearer {bridge_key}":
        raise HTTPException(status_code=401, detail="invalid bridge key")


def make_auth_checker(bridge_key: str):
    """check_bridge_auth(request) bound to one bridge's key."""

    def check(request: Request) -> None:
        check_bridge_auth(request, bridge_key)

    return check


def make_model_remapper(catalog_prefix: str, double_prefix: str = "",
                        double_strip: str = "", default: Optional[str] = None):
    """Undo the <provider>/ namespacing a catalog adds to upstream model ids.

    Strips catalog_prefix once, then the historical hyphenated spelling that
    early catalog injections produced (double_prefix triggers, double_strip is
    the length removed -- they differ because the old slug was <prov>-<prov>foo
    while only <prov>- had to come off). default is returned for an empty/None
    model when a provider has a routing fallback (e.g. qoder's "auto");
    bridges without one leave it unset and keep returning None.
    """

    def remap(model: Optional[str]) -> Optional[str]:
        if not model:
            return default if default is not None else model
        if model.startswith(catalog_prefix):
            return model[len(catalog_prefix):]
        if double_prefix and model.startswith(double_prefix):
            return model[len(double_strip):]
        return model

    return remap


def make_prefix_stripper(catalog_prefix: str):
    """Strip every leading catalog_prefix, not just the first.

    opencodex discovery can double-namespace a slug (xhx/xhx-<model>), so these
    bridges peel the prefix in a loop rather than once.
    """

    def strip(model: Optional[str]) -> Optional[str]:
        while model and model.startswith(catalog_prefix):
            model = model[len(catalog_prefix):]
        return model

    return strip


def upstream_error_response(status_code: int, body: str, upstream_name: str,
                            error_type: str, max_chars: int = 400,
                            message: str = "") -> JSONResponse:
    """Forward a non-200 upstream reply in the OpenAI error envelope.

    message overrides the default "<upstream> upstream <code>: <body>" wording
    for callers that already phrase the failure themselves (e.g. an
    unreachable-host error, which is not an upstream status at all).
    """
    text = (body or "")[:max_chars]
    detail = message or f"{upstream_name} upstream {status_code}: {text}"
    return JSONResponse(
        {"error": {"message": detail, "type": error_type}},
        status_code=status_code,
    )


async def sse_pump(resp: httpx.Response):
    """Relay an upstream streaming body and always release it.

    Decoded, not raw: an upstream that answers with Content-Encoding: gzip
    would otherwise hand us compressed bytes that we forward while dropping
    the header, so the client parses a stream that is not UTF-8 text at all.
    httpx only advertises codecs it can decode, so aiter_bytes never turns a
    working relay into a failing one.
    """
    try:
        async for chunk in resp.aiter_bytes():
            if chunk:
                yield chunk
    finally:
        await resp.aclose()

async def stream_response(resp: httpx.Response, *, stream: bool,
                          upstream_name: str, error_type: str,
                          media_type: str = "application/json",
                          error_chars: int = 400):
    """Turn an already-sent upstream reply into the bridge's HTTP response.

    Non-200 becomes the shared error envelope; 200 streams through untouched
    when the caller asked for SSE, and is read into one JSON body otherwise.
    """
    if resp.status_code != 200:
        body = (await resp.aread()).decode("utf-8", "replace")
        await resp.aclose()
        return upstream_error_response(resp.status_code, body, upstream_name,
                                       error_type, max_chars=error_chars)
    if stream:
        ctype = resp.headers.get("content-type", "text/event-stream")
        return StreamingResponse(sse_pump(resp), media_type=ctype)
    content = await resp.aread()
    ctype = resp.headers.get("content-type", media_type)
    await resp.aclose()
    return Response(content=content, media_type=ctype)


def parse_args(default_port: int, description: str = "",
               extra_args=None) -> argparse.Namespace:
    """--host/--port for every bridge, normalised into one namespace.

    extra_args lets a bridge that also takes its own flag (qoder's --api-key)
    keep one argparse setup instead of hand-rolling a second parser.
    """
    ap = argparse.ArgumentParser(description=description or None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=default_port)
    for flag, kwargs in (extra_args or ()):
        ap.add_argument(flag, **kwargs)
    return ap.parse_args()


def serve(app: FastAPI, default_port: int, banner: str,
          log_level: str = "info", description: str = "",
          extra_args=None, on_args=None) -> None:
    """Parse --host/--port, print the banner, hand the app to uvicorn.

    banner is a printf-style template taking (host, port) so a bridge can
    describe its own upstream without this helper knowing anything about it.
    on_args runs after parsing (and before the banner), for bridges whose
    startup log or state depends on the parsed flags. Pass banner=None when a
    bridge prints its whole startup banner from on_args instead.
    """
    import uvicorn

    args = parse_args(default_port, description, extra_args)
    if on_args is not None:
        on_args(args)
    if banner is not None:
        print(banner % (args.host, args.port), flush=True)
    uvicorn.run(app, host=args.host, port=args.port,
                log_level=log_level, access_log=True)
