"""The two cloudcode-pa bridges' shared Google pieces.

gemini and antigravity both speak to Google's Code Assist endpoint with the
same OAuth token file, and both started as a copy of the first one, so the
same five helpers existed twice with slightly different docstrings: the VALI
403 unwrap, the exception that keeps a verification link unclipped, the
clip function, and the atomic token-file read/write.

Import it the way the bridges already import _basehttp:

    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
    import _googlecode

The bridges keep their own UpstreamError (each names it in its own error
envelope), so the shared exception subclasses whatever the caller passes in.
"""
from __future__ import annotations

import json
import os


def google_validation_url(body):
    """The verification link inside a Google 403, when the gate asks for one.

    Measured 2026-10-02: cloudcode-pa answers the VALI gate with 403 plus
    ErrorInfo{reason: VALI, metadata.validation_url}. That link is the whole
    fix -- the login itself is fine and only the account has to pass a browser
    check -- and it sits deep in a JSON body the 502 envelope clips to 300
    chars, so a truncated body reads as a bare "verify your account" dead end.
    """
    try:
        parsed = json.loads(body)
    except Exception:
        return None
    try:
        for detail in parsed["error"]["details"]:
            url = (detail.get("metadata") or {}).get("validation_url")
            if url:
                return url
    except Exception:
        pass
    return None


def make_account_verification(base: type) -> type:
    """An UpstreamError subclass that keeps the verification link intact.

    Raised instead of a raw UpstreamError so the link survives every clip on
    the way to the 502 envelope: clip() prints this message in full, because a
    truncated accounts.google.com/signin/continue/... URL is worthless to the
    operator reading the error.
    """
    class AccountVerification(base):
        """Google wants the account verified in a browser before more calls."""

        def __init__(self, url):
            super().__init__("HTTP 403 VALIDATION_REQUIRED; account verification "
                             "required, open: " + url)
            self.validation_url = url

    AccountVerification.__name__ = "AccountVerification"
    AccountVerification.__qualname__ = "AccountVerification"
    return AccountVerification


def clip(exc) -> str:
    """Message for the 502 envelope -- long enough to keep a verify link."""
    if getattr(exc, "validation_url", None):
        return str(exc)
    return str(exc)[:300]


def read_json_file(path: str) -> dict:
    """One JSON document, or an exception the caller turns into its own kind."""
    with open(path) as handle:
        return json.load(handle)


def write_json_file(path: str, payload: dict) -> None:
    """Atomically replace a JSON document, 2-space indented like Google writes it."""
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(tmp, path)


def upstream_proxy_info(env_name: str) -> dict:
    """The exit a bridge is using: {'proxy': url|None, 'source': ...}.

    urllib eats the macOS system proxy by default, and the 2026-10-02 system
    proxy on this host has no international route: every google domain comes
    back 000 / ProxyError 503 whether or not the account is verified. Setting
    the bridge's own <NAME>_UPSTREAM_PROXY names an exit explicitly; unset, the
    system proxy (or none) carries on as before. /health reports the result so
    a dead bridge says which exit it was trying.
    """
    import urllib.request

    explicit = (os.environ.get(env_name) or "").strip()
    if explicit:
        return {"proxy": explicit, "source": "env"}
    try:
        env_proxies = urllib.request.getproxies() or {}
    except Exception:
        env_proxies = {}
    system = env_proxies.get("https") or env_proxies.get("http") or ""
    return {"proxy": system or None, "source": "system" if system else "direct"}


def upstream_urlopen(req, timeout: float, info: dict):
    """Open req through the exit described by upstream_proxy_info().

    With an explicit exit the opener is built per call, because ProxyHandler is
    what picks the exit up and a module-level opener would pin the first one.
    """
    import urllib.request

    if info["source"] == "env":
        handler = urllib.request.ProxyHandler({"http": info["proxy"],
                                               "https": info["proxy"]})
        return urllib.request.build_opener(handler).open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def to_contents(msgs):
    """Code Assist `contents` plus the system instruction, from OpenAI messages.

    Gemini and Antigravity take the same chat shape and both flatten a
    multi-part content list down to text, pull `system` out into a separate
    instruction, and pad an empty conversation with a ping so the upstream
    never sees a contents-less request.
    """
    contents, sys_parts = [], []
    for m in msgs:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        text = str(content)
        if role == "system":
            sys_parts.append({"text": text})
            continue
        gr = "model" if role == "assistant" else "user"
        contents.append({"role": gr, "parts": [{"text": text}]})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": "ping"}]}]
    return contents, ({"parts": sys_parts} if sys_parts else None)
