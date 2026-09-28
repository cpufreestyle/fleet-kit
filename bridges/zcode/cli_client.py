#!/usr/bin/env python3
"""cli_client.py - drive the ZCode CLI headlessly over its stdio protocol.

The ZCode CLI ships inside the app bundle at
  /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs   (also on a VPS alike)
and exposes `zcode app-server`: a line-delimited JSON protocol on stdio.

Protocol (NOT JSON-RPC - there is no "jsonrpc" member):
  request   {"id": "<str>", "method": "<name>", "params": {...}}
  notify    {"method": "<name>", "params": {...}}
  response  {"id": "<str>", "result": <any>}   |   {"id": "<str>", "error": {code,message,data}}

Two server->client requests must be answered before a session can be created:
  * runtime/capabilities                       -> {"independentPlanState": true}
  * session/requestRuntimePreferences (notify)  -> runtime materialisation prefs
And a coding-plan provider must be registered first, otherwise every turn dies
with "Model creation failed" and the picker shows no model at all:
  * provider/updateAccountConfig  with an account snapshot (see ACCOUNT_* below)
"""
import json
import os
import hashlib
import re
from pathlib import Path
import sys
import subprocess
import threading
import time

try:
    from _provider_env import provider_env
except ImportError:  # pragma: no cover - standalone use
    def provider_env():
        return {}

def _default_cli_candidates():
    """ZCode CLI location per platform: the macOS app bundle, the Windows
    install directory, or a plain checkout. FLEET_ZCODE_CLI wins over all."""
    home = os.path.expanduser("~")
    out = [
        "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
        os.path.join(home, "Applications", "ZCode.app", "Contents", "Resources",
                     "glm", "zcode.cjs"),
    ]
    if os.name == "nt" or sys.platform.startswith("win"):
        for base in (os.environ.get("LOCALAPPDATA", ""),
                     os.environ.get("PROGRAMFILES", ""),
                     os.path.join(home, "AppData", "Local", "Programs")):
            if base:
                out.append(os.path.join(base, "ZCode", "resources", "glm", "zcode.cjs"))
    else:
        out.append(os.path.join(home, ".local", "share", "zcode", "glm", "zcode.cjs"))
        out.append("/opt/zcode/resources/glm/zcode.cjs")
    return [c for c in out if c]


CLI_CANDIDATES = [os.environ.get("ZCODE_CLI", "")] + _default_cli_candidates()

# --------------------------------------------------------------------------
# provider runtime headers (interaction/requestProviderRuntimeHeaders)
# --------------------------------------------------------------------------
#
# createProviderRuntimeHeadersPort asks the host for the auth material of the
# provider about to be called. The result schema is a discriminated union on
# "headersApplied":
#   {headersApplied: true,  requestAuth: {apiKey?, headers?}, errorMessage?}
#   {headersApplied: false, errorMessage?}
# requestAuth.headers are merged verbatim into the provider request headers
# (no allow-list here; the allow-list only applies to the endpoint-routing
# config fetch), and requestAuth.apiKey becomes the AI-SDK Anthropic
# provider's apiKey ("x-api-key"); "Authorization: Bearer ..." is therefore
# best passed through requestAuth.headers.

RUNTIME_HEADER_KEYS = {
    "http-referer": "HTTP-Referer",
    "user-agent": "User-Agent",
    "x-client-language": "X-Client-Language",
    "x-client-timezone": "X-Client-Timezone",
    "x-device-mid": "X-Device-Mid",
    "x-os-category": "X-Os-Category",
    "x-os-version": "X-Os-Version",
    "x-platform": "X-Platform",
    "x-release-channel": "X-Release-Channel",
    "x-title": "X-Title",
    "x-zcode-app-version": "X-ZCode-App-Version",
}

ZCODE_APP_VERSION = "3.14.3"
ZCODE_DEVICE_MID = "bf259545-1315-48c6-af67-dd9beebcdeac"
ZCODE_CREDS_PATH = os.path.join(str(Path.home()), ".zcode", "v2",
                                "credentials.json")


def _trace(message):
    """Protocol-level breadcrumbs: on by default, off with ZCODE_QUIET=1."""
    if os.environ.get("ZCODE_QUIET"):
        return
    try:
        sys.stderr.write("[cli_client] %s\n" % message)
        sys.stderr.flush()
    except Exception:
        pass


def _safe_storage_key():
    """sha256 of ZCode's safeStorage fallback secret."""
    try:
        import platform as _platform
        system = _platform.system().lower()
    except Exception:
        system = sys.platform
    try:
        import pwd
        user = pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    secret = os.environ.get("ZCODE_CREDENTIAL_SECRET") or (
        "zcode-credential-fallback:%s:%s:%s" % (system, str(Path.home()), user))
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _b64d(s):
    return __import__("base64").urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _aes_gcm_decrypt(key, iv, tag, ct):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key).decrypt(iv, ct + tag, None)
    except Exception:
        pass
    # openssl enc cannot verify the GCM tag; use a pure-python GCM instead.
    return _gcm_python(key, iv, tag, ct)


def _gcm_python(key, iv, tag, ct):
    """GCM decrypt via the hazmat primitives (last resort, no AEAD helper)."""
    from cryptography.hazmat.primitives.ciphers import (Cipher, algorithms,
                                                       modes)
    dec = Cipher(algorithms.AES(key), modes.GCM(iv, tag)).decryptor()
    return dec.update(ct) + dec.finalize()


def _decrypt(value):
    if not value or not isinstance(value, str):
        return ""
    if not value.startswith("enc:v1:"):
        return value
    try:
        iv_b64, tag_b64, ct_b64 = value[len("enc:v1:"):].split(".")
        iv, tag, ct = _b64d(iv_b64), _b64d(tag_b64), _b64d(ct_b64)
        return _aes_gcm_decrypt(_safe_storage_key(), iv, tag, ct).decode(
            "utf-8", "replace")
    except Exception as exc:
        _trace("credential decrypt failed: %r" % (exc,))
        return ""


JWT_RE = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(\.[A-Za-z0-9_-]+)?$")


def zcode_credentials():
    try:
        with open(ZCODE_CREDS_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        _trace("credentials unreadable: %r" % (exc,))
        return {}


def zcode_api_key():
    """The bearer/api key the zhipu-account providers authenticate with.

    Priority: ZCODE_API_KEY override, then the OAuth JWT stored under
    zcodejwttoken (a JWT shape is what the plan endpoint accepts; the opaque
    coding-plan <id>.<secret> signing credential gets a 401 there).
    """
    override = os.environ.get("ZCODE_API_KEY") or ""
    if override.strip():
        return override.strip()
    raw = zcode_credentials().get("zcodejwttoken") or ""
    if not raw:
        _trace("no zcodejwttoken in credentials")
        return ""
    token = _decrypt(raw).strip()
    if not token:
        return ""
    if not JWT_RE.match(token):
        _trace("zcodejwttoken is not JWT shaped")
        return ""
    return token


def zcode_plan_api_key():
    """The opaque coding-plan <id>.<secret> credential, if present."""
    creds = zcode_credentials()
    for key, value in sorted(creds.items()):
        if "coding-plan" in key and key.endswith(":api-key"):
            out = _decrypt(value).strip()
            if out:
                return out
    return ""


def runtime_headers():
    """The desktop client's identifying headers, keyed as requestAuth wants."""
    try:
        import platform as _platform
        system = _platform.system().lower() or sys.platform
        if system == "darwin":
            os_category, os_version = "macos", _platform.mac_ver()[0] or "15.6"
        elif system.startswith("win"):
            os_category = "windows"
            os_version = _platform.release() or "10"
        else:
            os_category = system
            os_version = _platform.release() or "unknown"
    except Exception:
        os_category, os_version = sys.platform, "unknown"
    h = {
        "user-agent": "ZCode/%s" % ZCODE_APP_VERSION,
        "http-referer": "https://zcode.z.ai",
        "x-title": "Z Code@electron",
        "x-zcode-app-version": ZCODE_APP_VERSION,
        "x-platform": os_category,
        "x-release-channel": "stable",
        "x-client-language": os.environ.get("ZCODE_CLIENT_LANG", "zh-CN"),
        "x-client-timezone": (os.environ.get("ZCODE_CLIENT_TZ")
                              or _tzname()),
        "x-os-category": os_category,
        "x-os-version": os_version,
        "x-device-mid": os.environ.get("ZCODE_DEVICE_MID") or ZCODE_DEVICE_MID,
    }
    return h


def _tzname():
    """IANA zone name, the way the desktop client sends X-Client-Timezone."""
    tz = (os.environ.get("ZCODE_CLIENT_TZ") or os.environ.get("TZ") or "").strip()
    if tz and ":" not in tz and "\\" not in tz and "/" in tz:
        return tz
    for cand in ("/etc/localtime",):
        try:
            if os.path.islink(cand):
                target = os.readlink(cand)
                if "zoneinfo/" in target:
                    return target.split("zoneinfo/", 1)[1]
        except Exception:
            pass
    return "Asia/Shanghai"


def provider_runtime_headers(params=None):
    """Body for interaction/requestProviderRuntimeHeaders.

    Tries apiKey first (the AI-SDK Anthropic provider turns it into
    "x-api-key"); the same token is also sent as an Authorization bearer
    header so either upstream shape can pick it up.
    """
    token = zcode_api_key()
    headers = runtime_headers()
    if not token:
        plan = zcode_plan_api_key()
        if plan:
            _trace("falling back to the opaque coding-plan api key")
            token = plan
    if not token:
        return {"headersApplied": False,
                "errorMessage": "no zcode credential available (zcodejwttoken "
                                "missing); run: node zcode.cjs login --no-browser"}
    auth = {"apiKey": token,
            "headers": dict(headers, authorization="Bearer " + token)}
    return {"headersApplied": True, "requestAuth": auth}



RUNTIME_PREFS = {
    "askUserQuestionAutoResolutionEnabled": True,
    "nativeSearchEnhancementsEnabled": True,
    "memoryEnabled": False,
}


def find_cli() -> str:
    for path in CLI_CANDIDATES:
        if path and os.path.exists(path):
            return path
    raise SystemExit("ZCode CLI not found; set ZCODE_CLI=/path/to/zcode.cjs")


class ZCodeCLI:
    """Minimal blocking client for `zcode app-server`."""

    def __init__(self, workspace="/tmp", cli_path=None):
        self.workspace = workspace
        self.cli = cli_path or find_cli()
        self.events = []
        self._cond = threading.Condition()
        self._seq = 0
        self.log = None
        if os.environ.get("ZCODE_CLI_LOG"):
            self.log = open(os.environ["ZCODE_CLI_LOG"], "w")
        self.proc = subprocess.Popen(
            ["node", self.cli, "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1, cwd=workspace,
            env=dict(os.environ, **provider_env()))
        threading.Thread(target=self._reader, daemon=True).start()

    # ---- plumbing ----
    def _reader(self):
        for line in self.proc.stdout:
            if self.log:
                self.log.write(line); self.log.flush()
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            with self._cond:
                self.events.append(msg)
                self._cond.notify_all()
                is_server_request = ("id" in msg and "method" in msg
                                     and "result" not in msg and "error" not in msg)
            if is_server_request:
                # Every server->client request is schema-validated on its way
                # back: answering with the wrong payload (e.g. runtime prefs
                # for a interaction/* request) fails the turn with a Zod
                # invalid_union error, so dispatch by method name.
                try:
                    result = self.server_request_result(msg)
                except Exception as exc:  # noqa: BLE001 - never kill the reader
                    _trace("server_request_result failed %s: %r"
                           % (msg.get("method"), exc))
                    result = {"headersApplied": False,
                              "errorMessage": "fleetkit host error: %s" % exc}
                    self._send({"id": msg["id"], "result": result})
                    continue
                self._send({"id": msg["id"], "result": result})

    def server_request_result(self, msg):
        """Answer one app-server -> host request, by method name.

        session/requestRuntimePreferences is a notify the CLI needs before it
        will materialise a session. interaction/requestProviderRuntimeHeaders
        (createProviderRuntimeHeadersPort) is asked before every model attempt
        and must return requestAuth, otherwise the turn dies on
        "Provider runtime headers were not applied before model request
        attempt" / a headersApplied invalid_union.
        """
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        _trace("server request %s %s" % (method, json.dumps(params,
                                                        ensure_ascii=False)[:400]))
        if method.endswith("requestRuntimePreferences") or method.endswith("capabilities"):
            return RUNTIME_PREFS
        if method.endswith("requestProviderRuntimeHeaders"):
            return provider_runtime_headers(params)
        if method.endswith("requestOfficialMcpAuthHeaders"):
            # No official-MCP origin is trusted from a headless host.
            return {"ok": False, "reason": "official_mcp_origin_untrusted"}
        if method.endswith("requestPermission"):
            # Deny closed: a bridge turn must never silently execute tools.
            return {"decision": "deny", "reason": "fleetkit headless host"}
        if method.endswith("requestUserInput"):
            return {"action": "decline",
                    "reason": "fleetkit headless host has no user"}
        if method.endswith("browserList"):
            return {"browsers": []}
        if method.endswith("browserExecute"):
            return {"ok": False, "error": "fleetkit headless host has no browser"}
        _trace("unhandled server request %s" % method)
        return RUNTIME_PREFS

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _wait(self, pred, timeout):
        deadline = time.time() + timeout
        with self._cond:
            while time.time() < deadline:
                for msg in self.events:
                    if pred(msg):
                        return msg
                self._cond.wait(0.5)
        return None

    def call(self, method, params=None, timeout=120):
        self._seq += 1
        tag = "fk%d" % self._seq
        self._send({"id": tag, "method": method, "params": params or {}})
        return self._wait(
            lambda m: m.get("id") == tag and ("result" in m or "error" in m), timeout)

    def close(self):
        try:
            self.proc.kill()
        except Exception:
            pass

    # ---- high level ----
    def hello(self):
        return self.call("runtime/capabilities", {})

    def account_snapshot(self, providers=None,
                         current="account:zai-start-plan",
                         builtin_revision=None):
        """Build the object provider/updateAccountConfig expects.

        Two rules from parseProcessAccountProviderConfigSnapshot: an entitled
        provider must carry states[pid].current (bool), and availability
        must be one of available|pending|unavailable|unknown.

        providers=None reads every provider from the built-in config.

        Caveat: the returned providerCount only reports the snapshot this
        call delivered, NOT the live registry size. The registry size is
        logged by the process as provider_registry.ready.providerCount.
        app-server never passes `standalone` to its registry bootstrap, so
        it cannot load credentials and that counter stays 0.
        """
        if not builtin_revision:
            builtin_revision = (self._builtin_revision()
                                or os.environ.get("ZCODE_BUILTIN_REV")
                                or "zcode-builtin:30")
        if providers is None:
            providers = self._builtin_provider_ids()
        model_ids = self._builtin_model_ids()
        prov, states = {}, {}
        for pid in providers:
            entry = {"access": {"type": "zhipu-account", "entitled": True}}
            if pid in model_ids:
                entry["builtinModelIds"] = model_ids[pid]
            prov[pid] = entry
            states[pid] = {"availability": "available", "entitled": True,
                           "current": pid == current}
        return {
            "revision": "account:fk-%d" % int(time.time()),
            "basedOnZCodeBuiltinRevision": str(builtin_revision),
            "providers": prov,
            "states": states,
        }

    def _builtin_provider_ids(self):
        """Provider ids advertised by the built-in config, with a fallback."""
        path = os.environ.get("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE")
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            rules = data["config"]["providerConfigRules"]["providerRules"]
            ids = [r["providerId"] for r in rules if r.get("providerId")]
            if ids:
                return ids
        except Exception:
            pass
        return ["account:zai-start-plan", "account:zai-individual-coding-plan",
                "account:zai-team-coding-plan"]

    def _builtin_model_ids(self):
        """providerId -> builtinModelIds from the built-in config.

        The host account snapshot schema (kz.pick({builtinModelIds}).extend(
        {access})) accepts builtinModelIds per provider, and the real host
        sends them: without them the registry keeps the provider but drops
        every model, and session/create dies with "Provider Registry 中不
        存在 Model: <pid>/<model>".
        """
        out = {}
        path = os.environ.get("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE")
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            for r in data["config"]["providerConfigRules"]["providerRules"]:
                pid = r.get("providerId")
                mids = (r.get("config") or {}).get("builtinModelIds")
                if pid and mids:
                    out[pid] = list(mids)
        except Exception:
            pass
        return out

    def _builtin_revision(self):
        """The registry's zcodeBuiltinRevision: zcode-builtin:<rev>:<sha256(path)>.

        ProviderRegistryService drops every account refresh whose
        basedOnZCodeBuiltinRevision differs from the config revision
        (s.basedOnZCodeBuiltinRevision !== o.zcodeBuiltinRevision keeps
        the stale fail-closed snapshot: providers present, models empty).
        The revision hashes the resolved builtin config path, so a host
        that sends a bare "zcode-builtin:30" silently pins the registry
        into the "Provider Registry model missing" state.
        """
        path = os.environ.get("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE")
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                rev = json.load(fh).get("revision") or "0"
        except Exception:
            return None
        digest = hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()
        return "zcode-builtin:%s:%s" % (rev, digest)
    def create_session(self, provider, model, workspace=None,
                       reasoning_level=None):
        ws = workspace or self.workspace
        selection = {"providerId": provider, "modelId": model}
        if reasoning_level:
            selection["options"] = {"reasoningLevel": reasoning_level}
        return self.call("session/create", {
            "workspace": {"workspacePath": ws, "workspaceKey": ws},
            "model": selection,
        })

    def ask(self, session_id, prompt, settle=8.0):
        self.call("session/send", {"sessionId": session_id, "content": prompt})
        time.sleep(settle)
        return self.call("session/messages", {"sessionId": session_id})


def main():
    import sys
    provider = sys.argv[1] if len(sys.argv) > 1 else "account:zai-start-plan"
    model = sys.argv[2] if len(sys.argv) > 2 else "GLM-5.3-Flash"
    prompt = sys.argv[3] if len(sys.argv) > 3 else "Reply exactly: E2E_OK"
    reasoning = sys.argv[4] if len(sys.argv) > 4 else "high"
    cli = ZCodeCLI(workspace=os.environ.get("ZC_WS", "/tmp"))
    try:
        print("hello:", json.dumps(cli.hello(), ensure_ascii=False)[:200])
        r = cli.call("provider/updateAccountConfig", cli.account_snapshot())
        print("account:", json.dumps(r, ensure_ascii=False)[:300])
        r = cli.create_session(provider, model, reasoning_level=reasoning)
        print("create:", json.dumps(r, ensure_ascii=False)[:300])
        if not (r and "result" in r):
            return 1
        res = r["result"]
        sid = res.get("sessionId") or (res.get("session") or {}).get("sessionId")
        print("session:", sid)
        r = cli.ask(sid, prompt)
        print("messages:", json.dumps(r, ensure_ascii=False)[:2000])
    finally:
        cli.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
