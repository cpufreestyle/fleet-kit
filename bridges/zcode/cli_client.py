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
                # The CLI refuses to create a session until these are answered.
                self._send({"id": msg["id"], "result": RUNTIME_PREFS})

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
            builtin_revision = os.environ.get("ZCODE_BUILTIN_REV",
                                           "zcode-builtin:30")
        if providers is None:
            providers = self._builtin_provider_ids()
        prov, states = {}, {}
        for pid in providers:
            prov[pid] = {"access": {"type": "zhipu-account", "entitled": True}}
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
    def create_session(self, provider, model, workspace=None):
        ws = workspace or self.workspace
        return self.call("session/create", {
            "workspace": {"workspacePath": ws, "workspaceKey": ws},
            "model": {"providerId": provider, "modelId": model},
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
    cli = ZCodeCLI(workspace=os.environ.get("ZC_WS", "/tmp"))
    try:
        print("hello:", json.dumps(cli.hello(), ensure_ascii=False)[:200])
        r = cli.call("provider/updateAccountConfig", cli.account_snapshot())
        print("account:", json.dumps(r, ensure_ascii=False)[:300])
        r = cli.create_session(provider, model)
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
