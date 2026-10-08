"""Last-resort envelope for BaseHTTPRequestHandler services.

http.server has no exception-handler stage like Starlette's: when do_POST
raises, socketserver prints a traceback and closes the socket, so the client
sees a reset with no status line and no error body at all. gemini and
antigravity both died that way -- their json.loads of the request body has
no guard, so one malformed body killed the connection instead of answering.

install_basehttp_guard wraps every do_* method so an escaped exception
still leaves an envelope: ValueError (almost always a malformed body) ->
400, anything else -> 500. The routes' own answers are untouched: they
return normally and never reach this path, and _send failures (the caller
hung up) stay silent.

This module is deliberately stdlib-only. zcode's captcha-relay is an
http.server service that must keep running under a bare python3 (it is
launched with the Xcode framework python, which has no fastapi/httpx), so
it cannot import _common. Shared code for these services lives here, in a
file nothing but http.server services import.
"""
import json
import socket
import threading
import time


def install_basehttp_guard(handler_cls):
    """Wrap every do_* method of handler_cls with an exception envelope.

    Returns the same class; wrapping happens in place. Idempotent: a second
    call sees the _basehttp_guard marker and skips already wrapped methods,
    so re-installing never double-wraps (and never double-sends).
    """
    for name in sorted(n for n in vars(handler_cls) if n.startswith("do_")):
        original = vars(handler_cls)[name]
        if not callable(original) or getattr(original, "_basehttp_guard", False):
            continue

        def wrapper(self, *args, _original=original, **kwargs):
            try:
                return _original(self, *args, **kwargs)
            except Exception as exc:  # last resort: must never re-raise
                code = 400 if isinstance(exc, ValueError) else 500
                try:
                    self._send(code, json.dumps({"error": {
                        "message": str(exc)[:300],
                        "type": "bad_request" if code == 400 else "bridge_error",
                    }}))
                except Exception:
                    pass

        wrapper._basehttp_guard = True
        wrapper.__name__ = getattr(original, "__name__", name)
        setattr(handler_cls, name, wrapper)
    return handler_cls



# ---------------------------------------------------------------- deadlines
# One bridge owns a request budget in seconds and every outbound connect has
# to stay inside it. The stdlib hands each address getaddrinfo() returns the
# same timeout, so one blocked urlopen() costs N x timeout -- and
# cloudcode-pa.googleapis.com resolves to 16 of them (8 IPv6 first). Measured
# 2026-09-29 behind a VPN: a 20s timeout cost 40s on oauth2.googleapis.com's
# two addresses, so a 60s CHAT_BUDGET still produced a 90s request.
#
# Walk the addresses here and cap each attempt at the time that is actually
# left, so the total -- not just the first connect -- stays inside the budget.
# The deadline is thread-local: each request runs in its own thread under
# ThreadingHTTPServer, so concurrent requests cannot clobber one another.
_tls = threading.local()
_real_create_connection = socket.create_connection


def arm_deadline(when):
    """Bound this thread's connects to the epoch seconds `when`."""
    _tls.deadline = when


def disarm_deadline():
    _tls.deadline = None


def _budgeted_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                                source_address=None, **kwargs):
    """socket.create_connection that charges every address to the deadline."""
    deadline = getattr(_tls, "deadline", None)
    if deadline is None:
        return _real_create_connection(address, timeout, source_address, **kwargs)
    host, port = address[:2]
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except OSError:
        infos = []
    requested = None if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
    last = None
    for af, socktype, proto, _canon, sa in infos:
        left = deadline - time.time()
        if left <= 0:
            last = OSError("request budget exhausted")
            break
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            sock.settimeout(left if requested is None else min(requested, left))
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)
            return sock
        except OSError as exc:
            if sock is not None:
                sock.close()
            last = exc
    if last is not None:
        raise last
    # getaddrinfo itself failed; resolution surfaces immediately, so let the
    # stdlib raise the familiar error.
    return _real_create_connection(address, timeout, source_address, **kwargs)


def install_budgeted_connect():
    """Route every socket.create_connection through the deadline-aware one.

    idempotent, so a bridge may call it from its entrypoint without checking.
    """
    if socket.create_connection is not _budgeted_create_connection:
        socket.create_connection = _budgeted_create_connection
