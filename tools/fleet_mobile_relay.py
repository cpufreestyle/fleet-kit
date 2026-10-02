#!/usr/bin/env python3
"""A LAN-facing, token-gated front door to the fleet, for the phone clients.

Every bridge binds 127.0.0.1 and (except for two) demands its own API key. That
is fine for Codex on this Mac and for the Android emulator behind `adb reverse`,
but a phone on Wi-Fi or a VPN cannot reach the fleet at all. This relay is the
one LAN entry point:

    phone --(Wi-Fi/VPN)--> 0.0.0.0:8820 relay --(127.0.0.1)--> bridge

It reads every bridge port and key from the same place the desktop tools do
(fleet_platform.service_ports()/service_keys()), so the phone stores one address
and one token instead of nine keys. It speaks the OpenAI shape the phone client
already understands:

    GET  /health               liveness plus the bridge count
    GET  /api/status           the document the phone status strip reads
    GET  /v1/models            every model, canonical "bridge/model"
    POST /v1/chat/completions  routed by the model's bridge, streamed through

Auth: one shared token, checked on everything except /health. Send it as
X-Fleet-Token or Authorization: Bearer <token>. The token lives in
runtime/mobile-token (mode 0600) and is generated and printed on first start.
"""

import argparse
import hmac
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fleet_platform import (  # noqa: E402
    LABEL_SUFFIX,
    fleet_env_path,
    load_env,
    service_keys,
    service_ports,
)

PORT = 8820
CATALOG_TTL_SEC = 90
PROBE_TIMEOUT_SEC = 4.0
CHAT_TIMEOUT_SEC = 600.0
MAX_BODY_BYTES = 2 * 1024 * 1024

# Order bare model ids are resolved in when the caller omits a bridge. xhx then
# lingxi first keeps the relay's own recommendation stable.
PREFERRED = ["xhx", "lingxi", "codely", "workbuddy", "kimi", "minimax",
             "qoder", "trae", "zcode", "cline", "qwen", "gemini",
             "antigravity", "catpaw", "workbuddy-gpt"]


def _bridge_keys():
    """{bridge_name: api key} from the installed *2codex* services."""
    suffix_to_name = {}
    for name, suffix in LABEL_SUFFIX.items():
        suffix_to_name[suffix] = name
    out = {}
    for label, key in service_keys().items():
        # labels look like "com.local.xhx2codex" (and "com.local.workbuddy2codex-gpt"),
        # so the service suffix is the last dotted segment, not everything after
        # the first dot.
        suffix = label.rsplit(".", 1)[-1]
        name = suffix_to_name.get(suffix)
        if name and key:
            out[name] = key
    return out


class Fleet:
    """Ports, keys and the live per-bridge model catalog."""

    def __init__(self):
        self.ports = service_ports()
        self.keys = _bridge_keys()
        env = load_env(fleet_env_path())
        try:
            self.ui_port = int(env.get("UI_PORT") or 8796)
        except ValueError:
            self.ui_port = 8796
        self._lock = threading.Lock()
        self._catalog = None
        self._catalog_at = 0.0
        self._errors = {}

    def has_bridge(self, name):
        return name in self.ports

    def _get_models(self, name, timeout):
        port = self.ports.get(name)
        if not port:
            return None
        req = urllib.request.Request("http://127.0.0.1:%d/v1/models" % port)
        key = self.keys.get(name)
        if key:
            req.add_header("Authorization", "Bearer " + key)
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        return [str(m.get("id", "")) for m in (data.get("data") or [])
                if m.get("id")]

    def refresh(self):
        """Probe every bridge in parallel and rebuild the canonical catalog."""
        names = [n for n in PREFERRED if n in self.ports]
        catalog = {}
        errors = {}
        with ThreadPoolExecutor(max_workers=max(4, len(names))) as pool:
            futures = {pool.submit(self._get_models, n, PROBE_TIMEOUT_SEC): n
                       for n in names}
            for fut, name in futures.items():
                try:
                    raw_ids = fut.result()
                except Exception as exc:
                    errors[name] = type(exc).__name__ + ": " + str(exc)[:120]
                    continue
                if not raw_ids:
                    errors[name] = "no models returned"
                    continue
                for raw in raw_ids:
                    raw = str(raw)
                    # Some bridges already prefix their ids with their own name
                    # (xhx does); strip it once so routing always sends the bare
                    # id upstream, and canonical is always "bridge/upstream".
                    if raw.startswith(name + "/"):
                        raw = raw[len(name) + 1:]
                    if not raw:
                        continue
                    catalog[name + "/" + raw] = (name, raw)
        with self._lock:
            self._catalog = catalog
            self._catalog_at = time.time()
            self._errors = errors
        return catalog

    def catalog(self, force=False):
        with self._lock:
            fresh = (self._catalog is not None
                     and time.time() - self._catalog_at < CATALOG_TTL_SEC)
        if fresh and not force:
            with self._lock:
                return dict(self._catalog)
        return self.refresh()

    def resolve(self, model):
        """(bridge, upstream_model) for a canonical, raw or bare model id."""
        model = (model or "").strip()
        if not model:
            return None
        cat = self.catalog()
        if model in cat:
            return cat[model]
        if "/" in model:
            bridge, rest = model.split("/", 1)
            if self.has_bridge(bridge) and rest:
                return bridge, rest
        # bare id: the first preferred bridge that actually lists it
        for canonical, pair in cat.items():
            if pair[1] == model:
                return pair
        # not in the cached catalog: probe the preferred bridges directly
        for name in PREFERRED:
            if name not in self.ports:
                continue
            try:
                raws = self._get_models(name, PROBE_TIMEOUT_SEC) or []
            except Exception:
                continue
            if model in raws:
                return name, model
        return None


class Relay:
    def __init__(self, fleet, token, host, port):
        self.fleet = fleet
        self.token = token
        self.host = host
        self.port = port

    def bridge_url(self, name, path):
        return "http://127.0.0.1:%d%s" % (self.fleet.ports[name], path)


class Handler(BaseHTTPRequestHandler):
    server_version = "FleetMobileRelay/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def relay(self):
        return self.server.relay

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s %s\n" % (time.strftime("%H:%M:%S"),
                                           self.address_string(), fmt % args))

    # ---- helpers -------------------------------------------------------
    def _json(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self):
        if not self.relay.token:
            return True
        supplied = self.headers.get("X-Fleet-Token", "")
        if not supplied:
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
        return hmac.compare_digest(supplied, self.relay.token)

    def _deny(self):
        self._json(401, {"error": {"message":
            "token missing or wrong; send X-Fleet-Token or Authorization: Bearer <token>",
            "type": "invalid_request_error"}})

    # ---- routes --------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            self._json(200, {"ok": True, "bridges": len(self.relay.fleet.ports),
                             "host": self.relay.host, "port": self.relay.port})
            return
        if not self._authorized():
            self._deny()
            return
        if path == "/":
            self._json(200, self._describe())
            return
        if path in ("/api/status", "/status"):
            self._json(200, self._status())
            return
        if path in ("/v1/models", "/models"):
            self._models()
            return
        self._json(404, {"error": {"message": "no route " + path}})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if not self._authorized():
            self._deny()
            return
        if path in ("/v1/chat/completions", "/chat/completions"):
            self._chat()
            return
        self._json(404, {"error": {"message": "no route " + path}})

    # ---- payloads ------------------------------------------------------
    def _describe(self):
        cat = self.relay.fleet.catalog()
        return {
            "name": "fleet-mobile-relay",
            "endpoints": ["/health", "/api/status", "/v1/models",
                          "/v1/chat/completions"],
            "bridges": sorted(self.relay.fleet.ports.keys()),
            "models": len(cat),
        }

    def _models(self):
        cat = self.relay.fleet.catalog(force=True)
        data = []
        for canonical in sorted(cat.keys()):
            bridge = cat[canonical][0]
            data.append({"id": canonical, "object": "model",
                         "owned_by": bridge})
        self._json(200, {"object": "list", "data": data})

    def _status(self):
        cat = self.relay.fleet.catalog()
        per_bridge = {}
        for canonical, (bridge, _raw) in cat.items():
            per_bridge.setdefault(bridge, []).append(canonical)
        bridges = []
        up = 0
        for name, port in sorted(self.relay.fleet.ports.items(),
                                 key=lambda kv: kv[1]):
            models = sorted(per_bridge.get(name, []))
            ok = bool(models)
            if ok:
                up += 1
            bridges.append({
                "name": name,
                "port": port,
                "label": "com.local." + LABEL_SUFFIX.get(name, name + "2codex"),
                "agent": {"loaded": ok, "state": "running" if ok else "unknown"},
                "listen": {"ok": ok},
                "probe": {"ok": ok, "http": 200 if ok else 0,
                          "count": len(models), "ms": 0, "models": models},
            })
        return {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_ms": 0,
            "summary": {"bridges": len(bridges), "bridges_up": up,
                        "models": len(cat)},
            "bridges": bridges,
            "errors": dict(self.relay.fleet._errors),
            "via": "fleet-mobile-relay",
        }

    # ---- chat ----------------------------------------------------------
    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        return self.rfile.read(length)

    def _chat(self):
        raw = self._read_body()
        if raw is None:
            self._json(413, {"error": {"message": "missing or oversized body"}})
            return
        try:
            body = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            self._json(400, {"error": {"message": "bad json: " + str(exc)}})
            return

        requested = str(body.get("model") or "")
        resolved = self.relay.fleet.resolve(requested)
        if not resolved:
            self._json(404, {"error": {"message":
                "unknown model %r; GET /v1/models for the catalog" % requested,
                "type": "model_not_found"}})
            return
        bridge, upstream = resolved

        body["model"] = upstream
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.relay.bridge_url(bridge, "/v1/chat/completions"),
                                     data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", self.headers.get("Accept") or "text/event-stream")
        key = self.relay.fleet.keys.get(bridge)
        if key:
            req.add_header("Authorization", "Bearer " + key)

        streaming = bool(body.get("stream"))
        try:
            upstream_resp = urllib.request.urlopen(req, timeout=CHAT_TIMEOUT_SEC)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            self._json(exc.code, {"error": {"message":
                "bridge %s rejected the call: %s" % (bridge, detail),
                "type": "upstream_error"}})
            return
        except Exception as exc:
            self._json(502, {"error": {"message":
                "cannot reach bridge %s: %s" % (bridge, exc),
                "type": "upstream_unreachable"}})
            return

        try:
            if streaming:
                self._pump_stream(upstream_resp)
            else:
                data = upstream_resp.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        finally:
            upstream_resp.close()

    def _pump_stream(self, upstream_resp):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                chunk = upstream_resp.read(1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    relay = None


def load_token(path, explicit=None):
    if explicit:
        return explicit
    env = os.environ.get("FLEET_MOBILE_TOKEN")
    if env:
        return env.strip()
    try:
        with open(path, encoding="utf-8") as fh:
            token = fh.read().strip()
            if token:
                return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        print("  generated a new mobile token at %s" % path)
    except OSError as exc:
        print("  WARNING cannot persist token (%s); it will rotate on restart" % exc)
    return token


def lan_address():
    """A friendly hint at which LAN address the phone should use."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("192.168.1.1", 80))
        addr = sock.getsockname()[0]
        sock.close()
        return addr
    except OSError:
        return "your-mac-ip"


def main(argv=None):
    home = os.environ.get("FLEET_HOME") or os.path.expanduser(
        "~/AI Shared/repo/FleetKit/runtime")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0",
                        help="bind address (default 0.0.0.0, LAN + loopback)")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--token", default=None,
                        help="shared token (default: runtime/mobile-token)")
    parser.add_argument("--token-file", default=None)
    args = parser.parse_args(argv)

    token_path = args.token_file or os.path.join(home, "mobile-token")
    token = load_token(token_path, args.token)
    fleet = Fleet()
    fleet.refresh()

    host = args.host
    if ":" in host:
        server_cls = RelayServer
    else:
        server_cls = RelayServer
    server = server_cls((host, args.port), Handler)
    server.relay = Relay(fleet, token, host, args.port)

    shown = host if host not in ("0.0.0.0", "::") else lan_address()
    print("fleet mobile relay on %s:%d" % (host, args.port))
    print("  bridges   %d  (%d models)" % (len(fleet.ports), len(fleet._catalog or {})))
    print("  phone URL http://%s:%d" % (shown, args.port))
    print("  token     %s" % token)
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
