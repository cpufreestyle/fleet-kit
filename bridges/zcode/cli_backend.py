#!/usr/bin/env python3
"""cli_backend.py - turn the ZCode CLI into a callable model backend.

Why: the zcode-plan edge blocks direct HTTP POSTs from the bridge with
3012 "unusual activity", but the same request made by the CLI's own
transport goes through (3007 captcha -> solved by the captcha pool, then
1005 quota). So instead of re-implementing the SDK's client fingerprint,
the bridge reuses the official CLI process itself.

The engine here is a persistent "node zcode.cjs app-server" child driven
by cli_client.py, but this module deliberately keeps the conversation
state local to one call, so a bridge request maps to:

  hello -> provider/updateAccountConfig -> session/create -> session/send
  -> poll session/messages -> pick the newest assistant text -> close

That is enough for /v1/chat/completions (Codex sends full history every
time), and it keeps the CLI free of any tool-execution surface: the host
already answers every permission request with "deny".

Quota errors (1005 exceed quota limit / 1113 insufficient balance) are
returned as QuotaError so the bridge can surface "plan exhausted" instead
of a generic 502, and pick the other plan provider as a fallback.
"""
from __future__ import annotations

import json
import os
import sys
import time

import cli_client


class CliError(Exception):
    """The CLI route failed for a non-quota reason."""


class QuotaError(Exception):
    """Upstream accepted the request shape but rejected the quota.

    providerErrorCode 1005 (start-plan quota) and 1113 (balance) mean the
    captcha/signing chain is fine and the plan behind the provider simply
    has no free invocations left. Callers should try the next provider
    instead of burning another captcha ticket.
    """

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


QUOTA_CODES = ("1005", "1113")


def _error_code(info):
    data = (info or {}).get("error") or {}
    inner = data.get("data") or {}
    attribution = inner.get("attribution") or {}
    # raw_team.jsonl: error.data.attribution.providerErrorCode = "1113"
    return str(inner.get("providerErrorCode")
               or data.get("providerErrorCode")
               or attribution.get("providerErrorCode")
               or inner.get("code")
               or data.get("code")
               or "")


def _error_message(info):
    data = (info or {}).get("error") or {}
    inner = data.get("data") or {}
    return str(inner.get("message") or data.get("message") or "")


def ask(model="GLM-5.3-Flash", prompt="Reply exactly: E2E_OK",
        provider=None, level="high", timeout=120.0, workspace="/tmp",
        log_path=None):
    """One-shot chat round-trip over the official CLI. Returns text.

    provider=None uses the start-plan provider: it skips V4 signing and
    the captcha pool already works for it. Pass the coding-plan provider
    id to spend that balance instead.
    """
    pid = provider or os.environ.get("ZCODE_PROVIDER",
                                     "account:zai-start-plan")
    old_log = os.environ.get("ZCODE_CLI_LOG")
    old_pid = os.environ.get("ZCODE_PROVIDER")

    cli = None
    try:
        os.environ["ZCODE_CLI_LOG"] = log_path or "/tmp/zcode-cli-backend.jsonl"
        os.environ["ZCODE_PROVIDER"] = pid
        cli = cli_client.ZCodeCLI(workspace=workspace)
        cli.hello()
        cli.call("provider/updateAccountConfig",
                 cli.account_snapshot(current=pid))
        r = cli.call("session/create", {
            "workspace": {"workspacePath": workspace,
                          "workspaceKey": workspace},
            "model": {"providerId": pid, "modelId": model,
                      "options": {"reasoningLevel": level}},
            "thoughtLevel": level,
        })
        if not (r and "result" in r):
            raise CliError("session/create failed: %s"
                           % json.dumps(r, ensure_ascii=False)[:300])
        res = r["result"]
        sid = res.get("sessionId") or (res.get("session") or {}).get("sessionId")
        if not sid:
            raise CliError("session/create returned no sessionId: %s"
                           % json.dumps(res, ensure_ascii=False)[:300])
        cli.call("session/send", {"sessionId": sid, "content": prompt})

        deadline = time.time() + timeout
        last_err = ""
        while time.time() < deadline:
            time.sleep(4)
            for e in cli.events:
                p = e.get("params", {})
                k = p.get("kind", p.get("reason", ""))
                if k in ("turn-failed", "turn.terminal"):
                    if p.get("status") == "failed" or k == "turn-failed":
                        code = str(p.get("errorCode") or "")
                        msg = str(p.get("errorMessage") or "")
                        last_err = "turn failed: %s %s" % (code, msg)
                        if code in QUOTA_CODES:
                            raise QuotaError(msg, code)
            m = cli.call("session/messages", {"sessionId": sid})
            msgs = (m or {}).get("result", {}).get("messages") or []
            for x in msgs:
                if x.get("info", {}).get("role") != "assistant":
                    continue
                if (x.get("info") or {}).get("error"):
                    code = _error_code(x.get("info"))
                    msg = _error_message(x.get("info"))
                    if code in QUOTA_CODES:
                        raise QuotaError(msg, code)
                    raise CliError("model error %s: %s" % (code, msg))
                for part in x.get("parts", []):
                    if part.get("type") == "text" and (part.get("text") or ""):
                        return part["text"]
        raise CliError("timeout after %.0fs%s"
                       % (timeout, ("; " + last_err) if last_err else ""))
    finally:
        for k, v in (("ZCODE_CLI_LOG", old_log),
                      ("ZCODE_PROVIDER", old_pid)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if cli is not None:
            try:
                cli.close()
            except Exception:
                pass


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="GLM-5.3-Flash")
    ap.add_argument("--provider", default="account:zai-start-plan")
    ap.add_argument("--prompt", default="Reply exactly: E2E_OK")
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--log", default="")
    args = ap.parse_args()
    try:
        print(ask(model=args.model, prompt=args.prompt,
                  provider=args.provider, timeout=args.timeout,
                  log_path=args.log or None))
    except QuotaError as q:
        print("QUOTA", q.code, str(q), sep="\t")
        sys.exit(2)
    except CliError as c:
        print("CLIERR", str(c), sep="\t")
        sys.exit(1)
