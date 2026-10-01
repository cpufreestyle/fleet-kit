#!/usr/bin/env python3
"""Anthropic Messages API gateway for FleetKit.

Why this exists, in one sentence: Claude Code (desktop and CLI) speaks only the
Anthropic Messages API -- POST /v1/messages with an x-api-key header and an
SSE grammar of message_start / content_block_delta / message_delta -- while
every FleetKit model lives behind the bridges on 8787..8800 and behind
StepFun's Plan API, both of which speak OpenAI Chat Completions. This gateway
is the one place that translates between the two, so Claude Code picks a model
the same way Codex does, from the same bridges and the same catalog, with no
second copy of the model list to keep in sync.

    Claude Code -> 127.0.0.1:8801 (this gateway)
                    -> 127.0.0.1:8787..8800 (bridges, one per provider)
                    -> https://api.stepfun.com/step_plan/v1 (StepFun Plan)
                    -> 127.0.0.1:10100 (ocx gateway, providers with no bridge)

What a client gets:

  GET  /health                     every route: port, key present, upstream up
  GET  /v1/models                  the shared catalog, strongest first
  POST /v1/messages                translated both ways, stream or not
  POST /v1/messages/count_tokens   a coarse estimate, labelled as one

Rules this file keeps, because breaking them is how a model picker lies:

  * resolve() never silently substitutes a model. An unknown id answers 404
    invalid_request_error. The four Claude slot names (claude-opus-5 and
    friends) are aliases onto each platform's strongest model, and when that
    alias target's own route is not answering the request goes to the first
    catalog row that is answering right now -- reported in the
    x-fleetkit-resolved-model response header, never hidden in the body.
  * A reply carrying an upstream refusal marker (upstream_errors.MARKERS:
    11128 unapproved channel, team_model_access_denied, ...) is an error even
    when it arrived as HTTP 200, exactly as fleet_probe.py already decides.
  * Ports, key env names and label suffixes are imported from fleet_platform
    and default_model_guard. A second copy of those tables would drift.
  * stdlib only, so the same file runs under launchd, a Windows Task
    Scheduler supervisor and a bare setsid wrapper on Linux.
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    import fleet_platform
except Exception:  # pragma: no cover - only when the file is run from a copy
    fleet_platform = None

try:
    import default_model_guard as guard
except Exception:  # pragma: no cover
    guard = None

try:
    from upstream_errors import is_error_body, matched_marker
except Exception:  # pragma: no cover
    def matched_marker(text):
        low = " ".join((text or "").lower().split())
        for marker in ("unapproved channel", "team not allowed to access model",
                       "request blocked", "illegal api invocation"):
            if marker in low:
                return marker
        return None

    def is_error_body(text):
        return matched_marker(text) is not None

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8801
DEFAULT_TIMEOUT = 240.0
CATALOG_PATH = os.environ.get(
    "FLEET_ANTHROPIC_CATALOG",
    os.path.expanduser("~/.codex/cc-switch-model-catalog.json"))
ANTHROPIC_VERSION = "2023-06-01"
NONCE = "E2E_OK"
# a real reply that simply ignores "reply exactly" is still reachable
REFUSAL_PREFIXES = ("error", "sorry, i can", "i cannot", '"error"')

# StepFun's Plan API is the one provider that is not a bridge: the fleet talks
# to it directly (measured 2026-10-01; the chat.completion shape is the same
# one the bridges return, so nothing else in this file has to care).
DIRECT_UPSTREAMS = {
    "stepfun": {
        "url": "https://api.stepfun.com/step_plan/v1/chat/completions",
        "keyenv": "STEPFUN_PLAN_API_KEY",
    },
}

# ocx forwards straight to a provider's vendor. It is the route for a catalog
# row whose provider owns no local bridge (the bare gpt-5.6-luna family), so
# every listed model keeps a route instead of quietly dropping out.
OCX_URL = "http://127.0.0.1:10100/v1/chat/completions"
OCX_PORT = 10100
OCX_KEY = "PROXY_MANAGED"

HARBOR = "stepfun/step-5-preview"

# The four Claude Code slots, onto each platform's strongest model. The picker
# floats the strongest rows to the front (catalog_sort.py), so these follow
# whatever is strongest on the day rather than a name written down once.
CLAUDE_ALIASES = {
    "claude-opus-5": "workbuddy-gpt/hy4-preview",
    "claude-sonnet-5": "trae/trae-seed-code-pro-0430",
    "claude-haiku-4-5": "workbuddy/glm-5.2",
    "claude-fable-5": "workbuddy-gpt/gpt-5.6-luna",
    # slots older Claude Code builds still ask for
    "claude-opus-4-8": "workbuddy-gpt/hy4-preview",
    "claude-sonnet-4-5": "trae/trae-seed-code-pro-0430",
    "claude-3-5-sonnet-latest": "trae/trae-seed-code-pro-0430",
    "claude-3-5-haiku-latest": "workbuddy/glm-5.2",
}

# urllib otherwise hands loopback calls to the macOS system proxy and they
# leave through the tunnel: fine in a terminal, a silent hang under launchd.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PORT_BASE = getattr(fleet_platform, "PORT_BASE", 8787) if fleet_platform else 8787
PORT_OFFSETS = dict(getattr(fleet_platform, "PORT_OFFSETS", {}) or {}) if fleet_platform else {}
LABEL_SUFFIX = dict(getattr(fleet_platform, "LABEL_SUFFIX", {}) or {}) if fleet_platform else {}
BRIDGE_PORTS = dict(getattr(guard, "BRIDGE_PORTS", {}) or {}) if guard else {}
KEY_ENV = dict(getattr(guard, "KEY_ENV", {}) or {}) if guard else {}

_CATALOG = {"stamp": None, "rows": []}
_BRIDGE_CATALOG = {"stamp": 0.0, "rows": []}

# cc-switch rewrites the shared catalog with the models of its own current
# provider every time it starts, so a file this thin is a foreign write
# rather than a small fleet: the gateway then answers from the bridges.
MIN_CATALOG_ROWS = 20


# ---------------------------------------------------------------- fleet facts
_ENV_CACHE = {"stamp": None, "values": {}}


def fleet_env(path=None):
    """fleet.env as a dict; the same KEY=VALUE shape fleet_probe reads.

    Cached on (path, mtime, size) because a /v1/models listing routes every
    catalog row and 117 unread files per request is a slow way to learn
    nothing new.
    """
    if fleet_platform is not None:
        try:
            env_path = path or fleet_platform.fleet_env_path()
        except Exception:
            env_path = path
    else:
        env_path = path
    if not env_path:
        env_path = os.path.expanduser("~/AI Shared/repo/FleetKit/runtime/fleet.env")
    try:
        st = os.stat(env_path)
    except OSError:
        return {}
    stamp = (env_path, st.st_mtime_ns, st.st_size)
    if _ENV_CACHE["stamp"] != stamp:
        if fleet_platform is not None:
            try:
                values = dict(fleet_platform.load_env(env_path))
            except Exception:
                values = {}
        else:
            values = {}
        if not values:
            try:
                with open(env_path, encoding="utf-8") as fh:
                    raw = fh.read()
            except OSError:
                raw = ""
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip(chr(34)).strip(chr(39))
        _ENV_CACHE["stamp"] = stamp
        _ENV_CACHE["values"] = values
    return dict(_ENV_CACHE["values"])


def bridge_ports():
    """{provider: port} for every bridge, installed or defaulted."""
    if fleet_platform is not None:
        try:
            return fleet_platform.service_ports()
        except Exception:
            pass
    out = dict(BRIDGE_PORTS)
    for name, offset in PORT_OFFSETS.items():
        out.setdefault(name, PORT_BASE + offset)
    return out


def service_key(provider):
    """The bridge key for a provider: fleet.env first, then its service file."""
    keyenv = KEY_ENV.get(provider, "")
    if not keyenv:
        return ""
    value = fleet_env().get(keyenv, "")
    if value:
        return value
    if fleet_platform is None:
        return ""
    suffix = LABEL_SUFFIX.get(provider, "")
    if not suffix:
        return ""
    label = "%s.%s" % (fleet_platform.label_prefix(), suffix)
    try:
        return fleet_platform.service_key(label, keyenv) or ""
    except Exception:
        return ""


def bridge_catalog_rows():
    """Rows listed straight from the bridges, strongest first.

    The catalog file is shared with Codex and with cc-switch, and cc-switch
    rewrites it with the models of its own provider on every start
    (measured 2026-10-01: one row left of 117). Enumerating the bridges
    costs one tiny GET per live port and is cached for a minute, so a
    foreign write costs the listing some freshness instead of breaking
    every direct model id.
    """
    now = time.time()
    if _BRIDGE_CATALOG["rows"] and now - _BRIDGE_CATALOG["stamp"] < 60:
        return _BRIDGE_CATALOG["rows"]
    rows = []
    for provider in sorted(set(bridge_ports()) | set(DIRECT_UPSTREAMS)):
        if provider in DIRECT_UPSTREAMS:
            spec = DIRECT_UPSTREAMS[provider]
            key = fleet_env().get(spec["keyenv"], "")
            if not key:
                continue
            url = spec["url"].replace("/chat/completions", "/models")
        else:
            port = bridge_ports().get(provider)
            if not port or not _tcp_alive("127.0.0.1", int(port)):
                continue
            url = "http://127.0.0.1:%d/v1/models" % port
            key = service_key(provider)
        try:
            headers = {"Authorization": "Bearer " + key} if key else {}
            with OPENER.open(urllib.request.Request(url, headers=headers),
                             timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            continue
        for pos, item in enumerate(data.get("data") or []):
            model_id = item.get("id") if isinstance(item, dict) else item
            if not model_id:
                continue
            slug = model_id if "/" in model_id else provider + "/" + model_id
            rows.append({"slug": slug, "priority": pos,
                         "display_name": model_id})
    rows.sort(key=lambda r: (r.get("priority", 10 ** 6), r.get("slug") or ""))
    _BRIDGE_CATALOG["stamp"] = now
    _BRIDGE_CATALOG["rows"] = rows
    return rows


def catalog_rows():
    """Every catalog model, strongest first (priority ascending)."""
    try:
        st = os.stat(CATALOG_PATH)
    except OSError:
        return bridge_catalog_rows()
    stamp = (st.st_mtime_ns, st.st_size)
    if _CATALOG["stamp"] != stamp:
        rows = []
        try:
            with open(CATALOG_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            rows = [r for r in (data.get("models") or []) if r.get("slug")]
        except (OSError, ValueError):
            rows = []
        rows.sort(key=lambda r: (r.get("priority", 10 ** 6), r.get("slug") or ""))
        _CATALOG["stamp"] = stamp
        _CATALOG["rows"] = rows
    if len(_CATALOG["rows"]) >= MIN_CATALOG_ROWS:
        return _CATALOG["rows"]
    return bridge_catalog_rows()


def provider_of(slug):
    """The provider a catalog slug belongs to, or "" when it carries none."""
    head, sep, _rest = (slug or "").partition("/")
    if sep:
        return head
    # a bare row (step-3.7-flash, gpt-5.6-luna) belongs to StepFun when it
    # names one, otherwise it has no bridge and travels the ocx gateway
    return "stepfun" if slug.startswith("step") else ""


def route_for(slug):
    """{url, key, model, transport, provider} for a catalog slug, or None."""
    slug = (slug or "").strip()
    if not slug:
        return None
    provider = provider_of(slug)
    if provider in DIRECT_UPSTREAMS:
        spec = DIRECT_UPSTREAMS[provider]
        key = fleet_env().get(spec["keyenv"], "")
        if not key:
            return None
        return {"url": spec["url"], "key": key,
                "model": slug.partition("/")[2] or slug,
                "transport": "direct", "provider": provider}
    if provider:
        port = bridge_ports().get(provider)
        if port:
            return {"url": "http://127.0.0.1:%d/v1/chat/completions" % port,
                    "key": service_key(provider), "model": slug,
                    "transport": "bridge", "provider": provider}
    return {"url": OCX_URL, "key": OCX_KEY, "model": slug,
            "transport": "gateway", "provider": provider or "ocx"}


def _tcp_alive(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def route_alive(route, timeout=1.0):
    """Is this route's endpoint answering a TCP connect right now.

    A closed port is the only liveness signal worth having on the request
    path: probing a real chat call per request would double the traffic to
    every bridge and still only prove the past.
    """
    if not route:
        return False
    if route["transport"] == "direct":
        return _tcp_alive("api.stepfun.com", 443, timeout)
    if route["transport"] == "gateway":
        return _tcp_alive("127.0.0.1", OCX_PORT, timeout)
    try:
        port = int(route["url"].rsplit(":", 1)[1].split("/")[0])
    except (IndexError, ValueError):
        return False
    return _tcp_alive("127.0.0.1", port, timeout)


def known_providers():
    """Providers this fleet can actually route to, bridges plus direct."""
    return set(bridge_ports()) | set(DIRECT_UPSTREAMS)


def resolve(name):
    """(slug, note) for a requested model id, or (None, reason).

    Never substitutes quietly. An alias whose own target is not answering
    moves to the first catalog row that is answering and says so in note,
    which the handler puts in the x-fleetkit-resolved-model header.
    """
    wanted = (name or "").strip()
    if not wanted:
        return None, "no model requested"
    rows = catalog_rows()
    slugs = [r.get("slug") for r in rows if r.get("slug")]
    if wanted in slugs:
        return wanted, "catalog slug"
    target = CLAUDE_ALIASES.get(wanted)
    if target is None and wanted.startswith("claude"):
        # a slot this table does not name yet lands on the harbor rather than
        # a 404, because a picker that cannot answer is worse than a default
        target = HARBOR
    if target:
        route = route_for(target)
        if route and route_alive(route):
            return target, "alias of %s" % target
        for row in rows:
            slug = row.get("slug")
            if not slug or slug == target:
                continue
            route = route_for(slug)
            if route and route_alive(route):
                return slug, "alias target %s is not answering; fell back to %s" % (target, slug)
        return target, "alias of %s (no reachable fallback found)" % target
    for slug in slugs:
        if slug == wanted or slug.split("/")[-1] == wanted:
            return slug, "bare model name matched %s" % slug
    # the catalog is a shared file another tool may have rewritten: a slug
    # whose provider answers right now is a real model, not a typo
    route = route_for(wanted)
    if (route and route.get("provider") in known_providers()
            and route_alive(route)):
        return wanted, ("route derived from the model name; no catalog row"
                        " is visible right now")
    return None, "unknown model %r; not in %s" % (wanted, CATALOG_PATH)


# ------------------------------------------------- Anthropic -> OpenAI request
def _text_of(content):
    """Anthropic content (str | list of blocks) -> plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "".join(parts)
    return ""


def _image_url(block):
    src = block.get("source") or {}
    stype = src.get("type")
    if stype == "base64":
        data = src.get("data") or ""
        if not data:
            return None
        return "data:%s;base64,%s" % (src.get("media_type") or "image/png", data)
    if stype == "url":
        return src.get("url")
    return None


def message_id(prefix="msg"):
    return "%s_%d%s" % (prefix, int(time.time() * 1000), os.urandom(4).hex())


def to_openai(body, route):
    """Anthropic /v1/messages body -> OpenAI chat.completions body."""
    out = {"model": route["model"], "messages": []}
    system = _text_of(body.get("system"))
    if system:
        out["messages"].append({"role": "system", "content": system})
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        content = msg.get("content")
        if isinstance(content, str) or content is None:
            out["messages"].append({"role": role, "content": content or ""})
            continue
        texts, images, calls, results = [], [], [], []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text") or "")
            elif btype == "image":
                url = _image_url(block)
                if url:
                    images.append({"type": "image_url", "image_url": {"url": url}})
            elif btype == "tool_use":
                calls.append({"id": block.get("id") or message_id("toolu"),
                              "type": "function",
                              "function": {"name": block.get("name") or "",
                                           "arguments": json.dumps(
                                               block.get("input") or {},
                                               ensure_ascii=False)}})
            elif btype == "tool_result":
                # role=tool is the only shape OpenAI can pair with the earlier
                # tool_calls, so a tool_result travels as its own message and
                # keeps its position relative to the surrounding blocks
                results.append({"role": "tool",
                                "tool_call_id": block.get("tool_use_id") or "",
                                "content": _text_of(block.get("content"))})
        for item in results:
            out["messages"].append(item)
        if role == "assistant":
            entry = {"role": "assistant", "content": "".join(texts)}
            if calls:
                entry["tool_calls"] = calls
            if entry["content"] or calls:
                out["messages"].append(entry)
            continue
        parts = []
        if texts:
            parts.append({"type": "text", "text": "".join(texts)})
        parts.extend(images)
        if parts:
            # content stays a list even for one part: OpenAI takes a bare string
            # or an array of parts, and a lone part object is neither, so a
            # single-block user turn would be rejected or silently dropped.
            out["messages"].append({"role": role, "content": parts})
    tools = [t for t in (body.get("tools") or []) if isinstance(t, dict)]
    if tools:
        out["tools"] = [{"type": "function",
                         "function": {
                             "name": t.get("name") or "",
                             "description": t.get("description") or "",
                             "parameters": t.get("input_schema")
                             or {"type": "object", "properties": {}}}}
                        for t in tools]
        choice = body.get("tool_choice") or {}
        ctype = choice.get("type") or "auto"
        if ctype in ("auto", "any"):
            out["tool_choice"] = "auto" if ctype == "auto" else "required"
        elif ctype == "tool":
            name = choice.get("name") or ""
            out["tool_choice"] = ({"type": "function", "function": {"name": name}}
                                  if name else "required")
        elif ctype == "none":
            out["tool_choice"] = "none"
    budget = body.get("max_tokens")
    # the caller's budget is forwarded verbatim: only a degenerate 0 and an
    # absurd headroom need rewriting, since a floor quietly turns a 64-token
    # ask into a 256-token one nobody requested
    out["max_tokens"] = min(max(int(budget or 1024), 1), 32768)
    if body.get("stream"):
        out["stream"] = True
        # include_usage is what makes the final usage arrive; a bridge that
        # rejects it is retried without it, never failed
        out["stream_options"] = {"include_usage": True}
    if body.get("stop_sequences"):
        out["stop"] = [str(s) for s in body["stop_sequences"]]
    for key in ("temperature", "top_p", "presence_penalty",
                "frequency_penalty", "seed", "user"):
        if body.get(key) is not None:
            out[key] = body[key]
    return out


# ------------------------------------------------- OpenAI -> Anthropic response
def stop_reason_for(finish, blocks):
    if finish in ("length", "max_tokens"):
        return "max_tokens"
    if finish in ("tool_calls", "function_call"):
        return "tool_use"
    if finish == "content_filter":
        return "refusal"
    if any(b.get("type") == "tool_use" for b in blocks):
        return "tool_use"
    return "end_turn"


def _blocks_from_message(msg):
    blocks = []
    text = msg.get("content")
    if isinstance(text, list):
        text = "".join(p.get("text") or "" for p in text if isinstance(p, dict))
    text = text or ""
    calls = msg.get("tool_calls") or []
    if text.strip() or not calls:
        # an empty text block is what keeps Claude Code from choking on a
        # message whose model spent the whole budget on reasoning
        blocks.append({"type": "text", "text": text})
    for call in calls:
        fn = call.get("function") or {}
        raw = fn.get("arguments") or ""
        try:
            args = json.loads(raw) if raw else {}
        except ValueError:
            args = {"_raw_arguments": raw}
        if not isinstance(args, dict):
            args = {"value": args}
        blocks.append({"type": "tool_use",
                       "id": call.get("id") or message_id("toolu"),
                       "name": fn.get("name") or "",
                       "input": args})
    return blocks


def to_anthropic(data, model):
    choice = (data.get("choices") or [{}])[0] or {}
    msg = choice.get("message") or {}
    blocks = _blocks_from_message(msg)
    usage = data.get("usage") or {}
    return {
        "id": data.get("id") or message_id(),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop_reason_for(choice.get("finish_reason"), blocks),
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens") or 0,
                  "output_tokens": usage.get("completion_tokens") or 0,
                  "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0},
    }


def upstream_refusal(data):
    """The refusal hidden in a 200, or None. Same markers fleet_probe uses."""
    err = data.get("error")
    if err:
        return json.dumps(err, ensure_ascii=False)[:300]
    choice = (data.get("choices") or [{}])[0] or {}
    text = (choice.get("message") or {}).get("content")
    if isinstance(text, list):
        text = "".join(p.get("text") or "" for p in text if isinstance(p, dict))
    if text and is_error_body(text):
        return text[:300]
    return None


# ------------------------------------------------------- OpenAI SSE -> Anthropic
class SseTranslator:
    """chat.completion SSE chunks -> Anthropic Messages SSE events."""

    def __init__(self, model, emit):
        self.model = model
        self.emit = emit
        self.started = False
        self.text_index = None
        self.next_index = 0
        self.tool_blocks = {}
        self.finish = None
        self.usage = {}

    def _start(self):
        if self.started:
            return
        self.started = True
        self.emit("message_start", {
            "type": "message_start",
            "message": {"id": message_id(), "type": "message",
                        "role": "assistant", "model": self.model,
                        "content": [], "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0}}})
        self.emit("ping", {"type": "ping"})

    def _open_text(self):
        self._start()
        if self.text_index is None:
            self.text_index = self.next_index
            self.next_index += 1
            self.emit("content_block_start", {
                "type": "content_block_start", "index": self.text_index,
                "content_block": {"type": "text", "text": ""}})

    def _close_text(self):
        if self.text_index is not None:
            self.emit("content_block_stop", {
                "type": "content_block_stop", "index": self.text_index})
            self.text_index = None

    def feed(self, chunk):
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if isinstance(text, list):
                text = "".join(p.get("text") or "" for p in text if isinstance(p, dict))
            if text:
                self._open_text()
                self.emit("content_block_delta", {
                    "type": "content_block_delta", "index": self.text_index,
                    "delta": {"type": "text_delta", "text": text}})
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                self._close_text()
                key = call.get("index", 0)
                if key not in self.tool_blocks:
                    index = self.next_index
                    self.next_index += 1
                    self.tool_blocks[key] = index
                    fn = call.get("function") or {}
                    self.emit("content_block_start", {
                        "type": "content_block_start", "index": index,
                        "content_block": {"type": "tool_use",
                                          "id": call.get("id") or message_id("toolu"),
                                          "name": fn.get("name") or "",
                                          "input": {}}})
                args = (call.get("function") or {}).get("arguments")
                if args:
                    self.emit("content_block_delta", {
                        "type": "content_block_delta",
                        "index": self.tool_blocks[key],
                        "delta": {"type": "input_json_delta",
                                  "partial_json": args}})

    def close(self):
        self._start()
        if self.text_index is None and not self.tool_blocks:
            # reasoning-only answer: Claude Code still has to be owed one block
            self._open_text()
        self._close_text()
        for index in sorted(self.tool_blocks.values()):
            self.emit("content_block_stop", {
                "type": "content_block_stop", "index": index})
        self.emit("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason_for(
                          self.finish,
                          [{"type": "tool_use"}] if self.tool_blocks else []),
                      "stop_sequence": None},
            "usage": {"output_tokens": self.usage.get("completion_tokens") or 0,
                      "input_tokens": self.usage.get("prompt_tokens") or 0}})
        self.emit("message_stop", {"type": "message_stop"})


# ------------------------------------------------------------------- payloads
def error_payload(etype, message):
    return {"type": "error", "error": {"type": etype, "message": message}}


def status_error_type(code):
    if code in (401, 403):
        return "authentication_error"
    if code == 404:
        return "not_found_error"
    if code == 429:
        return "rate_limit_error"
    if code == 529:
        return "overloaded_error"
    return "api_error"


def models_payload():
    """The catalog as Anthropic models, strongest first.

    A row no route claims is left out on purpose: a model that cannot be
    reached must not sit in the picker looking like an answer.
    """
    data = []
    for row in catalog_rows():
        slug = row.get("slug")
        if not slug:
            continue
        route = route_for(slug)
        if route is None:
            continue
        data.append({
            "type": "model",
            "id": slug,
            "display_name": row.get("display_name") or slug,
            "created_at": "2026-01-01T00:00:00Z",
            "provider": route["provider"],
            "transport": route["transport"],
        })
    return {"object": "list", "data": data, "has_more": False,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None}


def health_payload(port):
    rows = catalog_rows()
    routes = {}
    for row in rows:
        slug = row.get("slug")
        if not slug:
            continue
        provider = provider_of(slug) or "ocx"
        entry = routes.setdefault(provider, {"provider": provider, "models": 0,
                                             "example": slug, "transport": None,
                                             "url": None, "key": False, "up": False})
        entry["models"] += 1
        if entry["transport"] is not None:
            continue
        route = route_for(slug)
        if route is None:
            entry.update(transport="none", url=None, up=False,
                         key=False, reason="no key for " + slug)
            continue
        entry.update(transport=route["transport"], url=route["url"],
                     key=bool(route["key"]), up=route_alive(route))
    return {
        "service": "fleetkit-anthropic-gateway",
        "base_url": "http://%s:%d" % (DEFAULT_HOST, port),
        "catalog": CATALOG_PATH,
        "models_listed": len(models_payload()["data"]),
        "catalog_rows": len(rows),
        "routes": sorted(routes.values(), key=lambda r: r["provider"]),
        "aliases": CLAUDE_ALIASES,
        "harbor": HARBOR,
        "count_tokens": "estimated (chars/4 plus 1200 per image), not a tokenizer count",
        "auth": "FLEET_ANTHROPIC_TOKEN" if os.environ.get("FLEET_ANTHROPIC_TOKEN") else "open (loopback only)",
    }


def estimate_tokens(body):
    total = len(_text_of(body.get("system")))
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
            continue
        for block in content or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                total += len(block.get("text") or "")
            elif btype == "image":
                total += 1200
            elif btype == "tool_use":
                total += len(json.dumps(block.get("input") or {}, ensure_ascii=False))
            elif btype == "tool_result":
                total += len(_text_of(block.get("content")))
    for tool in body.get("tools") or []:
        total += len(json.dumps(tool, ensure_ascii=False))
    return max(1, total // 4)


# ---------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    server_version = "FleetKitAnthropicGateway/1"
    protocol_version = "HTTP/1.1"
    timeout = 30.0

    # -- plumbing
    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"),
                                      fmt % args))
        sys.stderr.flush()

    def send_json(self, code, payload, extra=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}, None
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            return None, str(exc)
        if not isinstance(data, dict):
            return None, "body is not a JSON object"
        return data, None

    def authorized(self):
        wanted = os.environ.get("FLEET_ANTHROPIC_TOKEN", "").strip()
        if not wanted:
            return True
        if self.client_address[0] not in ("127.0.0.1", "::1", "localhost"):
            return False
        header = self.headers.get("x-api-key") or ""
        auth = self.headers.get("Authorization") or ""
        if header.strip() == wanted:
            return True
        if auth.strip() == "Bearer " + wanted:
            return True
        return False

    # -- routes
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        port = self.server.server_address[1]
        if path in ("/health", "/__health"):
            self.send_json(200, health_payload(port))
            return
        if path in ("/v1/models", "/models"):
            self.send_json(200, models_payload())
            return
        if path == "/":
            self.send_json(200, {"service": "fleetkit-anthropic-gateway",
                                 "endpoints": ["/health", "/v1/models",
                                               "/v1/messages",
                                               "/v1/messages/count_tokens"],
                                 "anthropic_version": ANTHROPIC_VERSION})
            return
        self.send_json(404, error_payload("not_found_error",
                                          "no such endpoint: " + path))

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/v1/messages/count_tokens":
            self.handle_count_tokens()
            return
        if path == "/v1/messages":
            self.handle_messages()
            return
        self.send_json(404, error_payload("not_found_error",
                                          "no such endpoint: " + path))

    def handle_count_tokens(self):
        body, err = self.read_body()
        if err is not None:
            self.send_json(400, error_payload("invalid_request_error",
                                              "unreadable body: " + err))
            return
        count = estimate_tokens(body)
        self.send_json(200, {"input_tokens": count,
                             "estimated": True,
                             "note": "coarse chars/4 estimate; this gateway has no tokenizer"})

    def handle_messages(self):
        if not self.authorized():
            self.send_json(401, error_payload(
                "authentication_error",
                "missing or wrong x-api-key (FLEET_ANTHROPIC_TOKEN)"))
            return
        body, err = self.read_body()
        if err is not None:
            self.send_json(400, error_payload("invalid_request_error",
                                              "unreadable body: " + err))
            return
        slug, note = resolve(body.get("model"))
        if not slug:
            self.send_json(404, error_payload("invalid_request_error", note))
            return
        route = route_for(slug)
        if route is None:
            self.send_json(502, error_payload(
                "api_error", "no route to %s: its provider has no key or bridge" % slug))
            return
        payload = to_openai(body, route)
        headers = {"Content-Type": "application/json"}
        if route["key"]:
            headers["Authorization"] = "Bearer " + route["key"]
        request = urllib.request.Request(route["url"],
                                         data=json.dumps(payload).encode("utf-8"),
                                         headers=headers, method="POST")
        try:
            upstream = OPENER.open(request, timeout=UPSTREAM_TIMEOUT)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read()[:400].decode("utf-8", "replace")
            except Exception:
                pass
            self.send_json(exc.code, error_payload(
                status_error_type(exc.code),
                "upstream %s answered HTTP %d: %s" % (route["provider"], exc.code,
                                                      detail.strip())))
            return
        except Exception as exc:
            self.send_json(502, error_payload(
                "api_error", "upstream %s unreachable: %s" % (route["provider"], exc)))
            return
        extra = {"x-fleetkit-resolved-model": slug}
        if note and note != "catalog slug":
            extra["x-fleetkit-resolve-note"] = note
        ctype = upstream.headers.get("Content-Type") or ""
        if payload.get("stream") and "text/event-stream" in ctype.lower():
            self.stream_sse(upstream, slug, extra)
            return
        raw = upstream.read()
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            self.send_json(502, error_payload(
                "api_error",
                "upstream %s did not answer JSON: %s"
                % (route["provider"], raw[:200].decode("utf-8", "replace"))))
            return
        refusal = upstream_refusal(data)
        if refusal:
            self.send_json(502, error_payload(
                "api_error",
                "upstream %s refused the call: %s" % (route["provider"], refusal)))
            return
        if payload.get("stream"):
            # a bridge that ignores stream= and answers with one JSON body:
            # translate it into the one-shot event sequence instead of failing
            self.stream_single(data, slug, extra)
            return
        self.send_json(200, to_anthropic(data, slug), extra)

    # -- SSE out
    def emit_sse(self, event, payload):
        text = "event: %s\ndata: %s\n\n" % (event,
                                             json.dumps(payload, ensure_ascii=False))
        self.wfile.write(text.encode("utf-8"))
        try:
            self.wfile.flush()
        except Exception:
            pass

    def stream_single(self, data, slug, extra):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        for key, value in extra.items():
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        translator = SseTranslator(slug, self.emit_sse)
        choice = (data.get("choices") or [{}])[0] or {}
        msg = choice.get("message") or {}
        delta = {}
        text = msg.get("content")
        if isinstance(text, list):
            text = "".join(p.get("text") or "" for p in text if isinstance(p, dict))
        if text:
            delta["content"] = text
        if msg.get("tool_calls"):
            delta["tool_calls"] = msg["tool_calls"]
        if data.get("usage"):
            translator.usage = data["usage"]
        translator.feed({"choices": [{"delta": delta,
                                      "finish_reason": choice.get("finish_reason")}]})
        translator.close()
        try:
            self.wfile.flush()
        except Exception:
            pass

    def stream_sse(self, upstream, slug, extra):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        for key, value in extra.items():
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        translator = SseTranslator(slug, self.emit_sse)
        data_lines = []

        def feed_lines(lines):
            # Flush the buffered data lines. Returns True once the stream is
            # finished, i.e. once the upstream sent its [DONE] sentinel.
            if not lines:
                return False
            text = "\n".join(lines)
            del lines[:]
            if text.strip() == "[DONE]":
                return True
            try:
                chunk = json.loads(text)
            except ValueError:
                return False
            translator.feed(chunk)
            return False

        try:
            for raw_line in upstream:
                line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    if feed_lines(data_lines):
                        continue
                if line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                    # A bridge that omits the blank line between events still
                    # has to deliver: a data line that already parses on its
                    # own is a finished chunk, so flush it instead of waiting
                    # for a separator that never comes. Multi-line data keeps
                    # buffering until it either parses or the blank line lands.
                    if feed_lines(data_lines):
                        break
            translator.close()
        except Exception as exc:
            # the status line is already out: the only honest channel left is
            # an in-band error event
            self.emit_sse("error", error_payload(
                "api_error", "upstream stream broke: %s" % exc))
        finally:
            try:
                upstream.close()
            except Exception:
                pass
            try:
                self.wfile.flush()
            except Exception:
                pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ------------------------------------------------------------------ cli modes
def probe_route(slug, budgets=(120, 600), timeout=90.0):
    """One real chat call on this route. (ok, why); ok may be None=retry bigger."""
    route = route_for(slug)
    if route is None:
        return False, "no route"
    if not route_alive(route, timeout=2.0):
        return False, "endpoint not answering a connect"
    last = ""
    for budget in budgets:
        body = {"model": route["model"], "max_tokens": budget,
                "messages": [{"role": "user",
                              "content": "Reply exactly: " + NONCE}]}
        headers = {"Content-Type": "application/json"}
        if route["key"]:
            headers["Authorization"] = "Bearer " + route["key"]
        request = urllib.request.Request(route["url"],
                                         data=json.dumps(body).encode("utf-8"),
                                         headers=headers, method="POST")
        try:
            with OPENER.open(request, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            return False, "HTTP %d" % exc.code
        except Exception as exc:
            return False, str(exc)[:80]
        refusal = upstream_refusal(data)
        if refusal:
            return False, "upstream refused: " + refusal[:120]
        choice = (data.get("choices") or [{}])[0] or {}
        msg = choice.get("message") or {}
        text = msg.get("content")
        if isinstance(text, list):
            text = "".join(p.get("text") or "" for p in text if isinstance(p, dict))
        text = (text or "").strip()
        if NONCE in text:
            return True, text[:60]
        if len(text) >= 8 and not text.lower().startswith(REFUSAL_PREFIXES):
            return True, text[:60]
        if not text and choice.get("finish_reason") == "length":
            last = "empty at %d tokens (reasoning ate the budget)" % budget
            continue
        last = text[:60] or "empty reply"
    return False, last


def run_check(slugs, timeout):
    """Real call per slug; exit code is the verdict. No fake green."""
    failed = 0
    for slug in slugs:
        ok, why = probe_route(slug, timeout=timeout)
        print("%-34s %s" % (slug, ("E2E_OK -> " + why) if ok else ("FAIL: " + why)),
              flush=True)
        if not ok:
            failed += 1
    print("%d/%d routes answered" % (len(slugs) - failed, len(slugs)), flush=True)
    return 1 if failed else 0


def resolve_host_port(args):
    host = args.host or os.environ.get("FLEET_ANTHROPIC_HOST") or DEFAULT_HOST
    port = args.port or int(os.environ.get("FLEET_ANTHROPIC_PORT") or DEFAULT_PORT)
    return host, port


def main(argv=None):
    global UPSTREAM_TIMEOUT
    parser = argparse.ArgumentParser(
        description="FleetKit Anthropic Messages API gateway")
    parser.add_argument("--host", default="", help="listen host")
    parser.add_argument("--port", type=int, default=0, help="listen port")
    parser.add_argument("--timeout", type=float,
                        default=float(os.environ.get("FLEET_ANTHROPIC_TIMEOUT")
                                      or DEFAULT_TIMEOUT),
                        help="upstream read timeout in seconds")
    parser.add_argument("--check", nargs="*", metavar="SLUG",
                        help="one real call per slug (default: every alias target "
                             "plus the harbor) and exit")
    parser.add_argument("--print-config", action="store_true",
                        help="dump resolved routes and keys, then exit")
    args = parser.parse_args(argv)
    UPSTREAM_TIMEOUT = args.timeout
    host, port = resolve_host_port(args)

    if args.check is not None:
        slugs = list(args.check)
        if not slugs:
            slugs = sorted(set(list(CLAUDE_ALIASES.values()) + [HARBOR]))
        return run_check(slugs, args.timeout)

    if args.print_config:
        print(json.dumps({
            "host": host, "port": port,
            "catalog": CATALOG_PATH,
            "catalog_rows": len(catalog_rows()),
            "models_listed": len(models_payload()["data"]),
            "routes": health_payload(port)["routes"],
            "aliases": CLAUDE_ALIASES, "harbor": HARBOR,
            "auth": "token required" if os.environ.get("FLEET_ANTHROPIC_TOKEN") else "open on loopback",
        }, indent=2, ensure_ascii=False))
        return 0

    server = Server((host, port), Handler)
    print("fleetkit anthropic gateway on http://%s:%d -> %d catalog models"
          % (host, port, len(models_payload()["data"])), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


UPSTREAM_TIMEOUT = DEFAULT_TIMEOUT


if __name__ == "__main__":
    sys.exit(main())
