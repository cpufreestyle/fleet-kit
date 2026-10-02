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

