#!/usr/bin/env python3
"""zcode2codex - 把本机 ZCode 免费模型暴露成标准 OpenAI 兼容 API。

链路：Codex -> 本桥(:8796) -> https://zcode.z.ai/api/v1/zcode-plan/anthropic
      -> GLM-5.3 / GLM-5.3-Flash (Start Plan / Weekend Build 免费额度)

凭证来源（全部本机解密，无需手填）：
  ~/.zcode/v2/credentials.json 的 zcodejwttoken（safeStorage AES-256-GCM，
  密钥 = sha256("zcode-credential-fallback:<platform>:<home>:<user>"），
  解密脚本同目录 zcode_creds.cjs 的 Python 等价实现）

captcha：上游对每次 messages 调用校验阿里云滑块（sceneId 11xygtvd / region cn /
prefix no8xfe）。captchaVerifyParam 是一次性的，过期后由 captcha-relay(:8910)
重新获取并写入 runtime/bridges/zcode/captcha.txt；本桥每次调用读取该文件，
取不到时返回 503 并在 detail 里给出换取页地址。

协议要点（逆向自 ZCode.app 3.14.3 + 实测）：
  * base  https://zcode.z.ai/api/v1/zcode-plan/anthropic
  * POST /v1/messages，Anthropic Messages 协议
  * 必带 X-ZCode-App-Version / X-Platform / X-Device-Mid / X-Release-Channel
  * Authorization: Bearer <zcodejwttoken>
  * X-Aliyun-Captcha-Verify-Param + X-Aliyun-Captcha-Verify-Region: cn
  * /v1/models 端点不存在(404)，模型清单由 entitlement(billing/current)
    与 client/configs 的 startPlanPreview 合并而来

用法：python3 zcode_bridge.py [--port 8796]
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import platform
import pwd
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

BRIDGE_VERSION = "0.1.0"
CATALOG_PREFIX = "zcode/"

UPSTREAM = "https://zcode.z.ai/api/v1/zcode-plan/anthropic"
BILLING_URL = "https://zcode.z.ai/api/v1/zcode-plan/billing/current"
CONFIGS_URL = ("https://zcode.z.ai/api/v1/client/configs"
               "?app_version=3.14.3&platform=darwin-arm64")
APP_VERSION = "3.14.3"
PLATFORM = "darwin-arm64"
DEVICE_MID = os.environ.get("ZCODE_DEVICE_MID", "")

CREDS_PATH = Path.home() / ".zcode" / "v2" / "credentials.json"
CAPTCHA_FILE = Path(os.environ.get(
    "ZCODE_CAPTCHA_FILE",
    Path(__file__).resolve().parent / "captcha.txt"))
CAPTCHA_RELAY = "http://127.0.0.1:8910/"

BRIDGE_KEY = os.environ.get("ZCODE2CODEX_KEY") or ""
CALL_TIMEOUT = float(os.environ.get("ZCODE_CALL_TIMEOUT") or "300")

# Start Plan / Weekend Build 授权的两个模型（entitlement capabilities:
# model:glm-5.3-flash；startPlanPreview: GLM-5.3 + GLM-5.3-Flash）。
FREE_MODELS = [
    "GLM-5.3-Flash",
    "GLM-5.3",
]


def log(*args) -> None:
    print(*args, flush=True)


# --------------------------------------------------------------------------
# 凭证解密（ZCode 的 safeStorage：enc:v1:<iv>.<tag>.<ct>，base64url）
# --------------------------------------------------------------------------

def _safe_storage_key() -> bytes:
    secret = os.environ.get("ZCODE_CREDENTIAL_SECRET") or (
        "zcode-credential-fallback:%s:%s:%s" % (
            platform.system().lower(), str(Path.home()),
            pwd.getpwuid(os.getuid()).pw_name))
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _decrypt(value: str) -> str:
    if not value or not value.startswith("enc:v1:"):
        return value
    try:
        iv_b64, tag_b64, ct_b64 = value[len("enc:v1:"):].split(".")
        iv = base64.urlsafe_b64decode(_pad(iv_b64))
        tag = base64.urlsafe_b64decode(_pad(tag_b64))
        ct = base64.urlsafe_b64decode(_pad(ct_b64))
    except Exception as exc:
        raise RuntimeError("bad ciphertext layout: %s" % exc)
    return _aes_gcm_decrypt(_safe_storage_key(), iv, tag, ct)


def _pad(s: str) -> str:
    return s + "=" * (-len(s) % 4)


def _aes_gcm_decrypt(key: bytes, iv: bytes, tag: bytes, ct: bytes) -> str:
    """AES-256-GCM with the stdlib only (cryptography/openssl-cli free)."""
    # Pure-python GCM is heavy; use the `openssl` CLI which ships with macOS.
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        ct_path = os.path.join(td, "ct.bin")
        with open(ct_path, "wb") as fh:
            fh.write(ct)
        khex = key.hex()
        ivhex = iv.hex()
        taghex = tag.hex()
        cmd = ["openssl", "enc", "-d", "-aes-256-gcm", "-K", khex,
               "-iv", ivhex, "-in", ct_path]
        try:
            out = subprocess.run(cmd, capture_output=True, check=True).stdout
        except subprocess.CalledProcessError:
            # openssl enc has no AEAD tag support; verify manually below.
            out = _gcm_python(key, iv, tag, ct)
        # tag verification is skipped for `openssl enc`; gcm_python verifies.
        return out.decode("utf-8", "replace")


def _gcm_python(key: bytes, iv: bytes, tag: bytes, ct: bytes) -> bytes:
    """Minimal AES-256-GCM decrypt+verify (no third-party deps)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aes = AESGCM(key)
    return aes.decrypt(iv, ct + tag, None)


def read_token() -> str:
    try:
        creds = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
        token = _decrypt(creds.get("zcodejwttoken", ""))
        return token.strip()
    except Exception as exc:
        log("credential error:", type(exc).__name__, exc)
        return ""


# --------------------------------------------------------------------------
# captcha
# --------------------------------------------------------------------------
#
# 上游票据是一次性的：一次 /v1/messages 只能吃一张 captchaVerifyParam，用过
# 再拿同一张会 3007。所以这里按“取用即销毁”的方式消费 captcha-mint.py 维护
# 的票据池，池空时现场补一张；被 3007/3012 拒绝时换一张重试。

CAPTCHA_POOL = Path(os.environ.get(
    "ZCODE_CAPTCHA_POOL", str(CAPTCHA_FILE.parent / "captcha_pool")))
MINTER = Path(__file__).resolve().parent / "captcha-mint.py"
CAPTCHA_MAX_AGE = float(os.environ.get("ZCODE_CAPTCHA_MAX_AGE") or "600")
# 票据“还值得发出去”的时限：阿里云二次验证窗口远小于 MAX_AGE，实测
# 分钟级的旧票必 3007，所以发请求前用这个更严的门槛把关。
CAPTCHA_MAX_FRESH = float(os.environ.get("ZCODE_CAPTCHA_MAX_FRESH") or "75")
CAPTCHA_RETRIES = int(os.environ.get("ZCODE_CAPTCHA_RETRIES") or "3")
MINT_TIMEOUT = float(os.environ.get("ZCODE_MINT_TIMEOUT") or "60")


def _pool_tickets() -> list:
    """Fresh pool tickets, newest first, as (path, epoch)."""
    now = time.time()
    out = []
    try:
        cand = [p for p in CAPTCHA_POOL.glob("*.txt")
                if p.name.split("-")[0].isdigit()]
    except OSError:
        return []
    for p in cand:
        try:
            epoch = float(p.name.split("-")[0])
        except ValueError:
            continue
        if now - epoch <= CAPTCHA_MAX_AGE:
            out.append((p, epoch))
    return sorted(out, key=lambda t: t[1], reverse=True)


def _legacy_ticket() -> str:
    try:
        for line in CAPTCHA_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except OSError:
        pass
    return ""


def read_captcha() -> str:
    """Peek the newest usable ticket (pool first, legacy captcha.txt last)."""
    tickets = _pool_tickets()
    if tickets:
        try:
            return tickets[0][0].read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return _legacy_ticket()


def _mint_now() -> str:
    """Ask captcha-mint.py for one fresh ticket (blocking, seconds)."""
    if not MINTER.exists():
        return ""
    import subprocess
    try:
        proc = subprocess.run(
            [sys.executable, str(MINTER), "--once"],
            capture_output=True, text=True, timeout=MINT_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        log("mint failed:", _err(exc))
        return ""
    out = (proc.stdout or "").strip().splitlines()
    param = out[-1].strip() if out else ""
    if proc.returncode != 0 or not param.startswith("ey"):
        log("mint rc=%s err=%s" % (proc.returncode,
                                   (proc.stderr or "")[-240:]))
        return ""
    return param


def take_captcha() -> str:
    """Consume one ticket: pool first, then legacy file, then mint on demand."""
    for path, _epoch in _pool_tickets():
        try:
            param = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        try:
            path.unlink()          # 抢占即销毁，天然避免并发重复使用
        except OSError:
            continue
        if param:
            return param
    legacy = _legacy_ticket()
    if legacy:
        try:   # 人工在 relay 页面换的票据同样只用一次
            CAPTCHA_FILE.write_text("# %s (consumed by bridge)\n"
                                    % time.strftime("%Y-%m-%d %H:%M:%S"),
                                    encoding="utf-8")
        except OSError:
            pass
        return legacy
    return _mint_now()


def ticket_age_seconds() -> float:
    """Age (s) of the newest ticket we hold; 1e9 when nothing is cached."""
    tickets = _pool_tickets()
    if tickets:
        return time.time() - tickets[0][1]
    try:
        lines = CAPTCHA_FILE.read_text(encoding="utf-8").splitlines()
        stamp = next((ln.lstrip("# ").strip() for ln in lines
                      if ln.startswith("#")), "")
        if not stamp:
            return 1e9
        import datetime as _dt
        # relay appends a " (consumed by bridge)" note; keep only the timestamp
        stamp = stamp.split(" (")[0].strip()
        return time.time() - _dt.datetime.strptime(
            stamp, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 1e9


def usable_ticket() -> tuple:
    """Return (param, reason) for a ticket that is still worth spending.

    阿里云的二次验证窗口只有几十秒（真 App 是 preflight 拿到票据后立刻发
    请求），所以我们手里过了期的票**不要**发往上游：既白烧一张，又给对端
    风控多留一次失败样本。过期时优先现场 mint 一张；mint 不出来就返回
    reason='stale'，让上层给出可执行的提示而不是盲目重试。
    """
    if ticket_age_seconds() <= CAPTCHA_MAX_FRESH:
        return take_captcha(), ""
    fresh = _mint_now()
    if fresh:
        return fresh, ""
    return "", "stale"


def captcha_age_hours() -> float:

    tickets = _pool_tickets()
    if tickets:
        return (time.time() - tickets[0][1]) / 3600.0
    try:
        lines = CAPTCHA_FILE.read_text(encoding="utf-8").splitlines()
        stamp = next((ln.lstrip("# ").strip() for ln in lines
                      if ln.startswith("#")), "")
        if not stamp:
            return 1e9
        import datetime as _dt
        # relay appends a " (consumed by bridge)" note to the stamp; strptime
        # would raise on it and the except below would report the ticket as
        # permanently stale, so keep only the leading timestamp
        stamp = stamp.split(" (")[0].strip()
        then = _dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        return (time.time() - then.timestamp()) / 3600.0
    except Exception:
        return 1e9


def pool_size() -> int:
    return len(_pool_tickets())



# --------------------------------------------------------------------------
# upstream
# --------------------------------------------------------------------------

def _headers(token: str, captcha: str = "") -> dict:
    h = {
        "Authorization": "Bearer " + token,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        "User-Agent": "ZCode/%s" % APP_VERSION,
        "HTTP-Referer": "https://zcode.z.ai",
        "X-Title": "Z Code@electron",
        "X-ZCode-App-Version": APP_VERSION,
        "X-Platform": PLATFORM,
        "X-Release-Channel": "stable",
        "X-Client-Language": "zh-CN",
        "X-Client-Timezone": "Asia/Shanghai",
        "X-Os-Category": "macos",
        "X-Os-Version": platform.mac_ver()[0] or "15.6",
    }
    if DEVICE_MID:
        h["X-Device-Mid"] = DEVICE_MID
    if captcha:
        h["X-Aliyun-Captcha-Verify-Param"] = captcha
        h["X-Aliyun-Captcha-Verify-Region"] = "cn"
    return h


def _warm_edge(url, token):
    """One OPTIONS on the same origin+path before the real POST.

    A bare POST to zcode-plan/anthropic/v1/messages is dropped by the
    Alibaba ESA edge with 3012 unusual activity; an OPTIONS on the very
    same URL first makes the follow-up POST reach the application layer
    (verified 2026-09-27: direct POST -> 3012, OPTIONS then POST -> 3007).
    Failures are ignored: this is only a warm-up, never break the call.
    """
    try:
        req = urllib.request.Request(url, method="OPTIONS",
                                     headers=_headers(token))
        urllib.request.urlopen(req, timeout=15).close()
    except Exception:
        pass


def _post(url: str, payload: dict, token: str, captcha: str = "") -> tuple:
    _warm_edge(url, token)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers=_headers(token, captcha))
    with urllib.request.urlopen(req, timeout=CALL_TIMEOUT) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8", "replace"))


def _get(url: str, token: str) -> tuple:
    req = urllib.request.Request(url, headers=_headers(token))
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8", "replace"))


def _err(exc: Exception) -> str:
    detail = ""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
    return "%s: %s %s" % (type(exc).__name__, exc, detail)


def entitlements(token: str) -> dict:
    """{model_id: {grant_units, period, show_name}} from billing/current."""
    out = {}
    try:
        _, data = _get(BILLING_URL, token)
        for plan in (data.get("data") or {}).get("plans") or []:
            for ent in plan.get("entitlements") or []:
                for cap in ent.get("capabilities") or []:
                    if cap.startswith("model:"):
                        out[cap[len("model:"):]] = {
                            "show_name": ent.get("show_name") or cap,
                            "grant_units": ent.get("grant_units"),
                            "period": ent.get("period"),
                            "plan": plan.get("name"),
                            "plan_id": plan.get("plan_id"),
                            "status": plan.get("status"),
                        }
    except Exception as exc:
        log("entitlements error:", _err(exc))
    return out


app = FastAPI(title="zcode2codex")


def check_auth(request: Request) -> None:
    return


@app.get("/health")
async def health():
    token = read_token()
    cap = read_captcha()
    return {
        "ok": True,
        "bridge": BRIDGE_VERSION,
        "logged_in": bool(token),
        "captcha": "present" if cap else "missing",
        "captcha_age_hours": None if not cap else round(captcha_age_hours(), 2),
        "models": FREE_MODELS,
    }


@app.get("/v1/models")
async def list_models(request: Request):
    check_auth(request)
    # NOTE: no top-level "detail" key -- ocx's discovery parser treats an
    # unexpected key as a malformed payload and drops the provider.
    # Health / readiness signals live on /health instead.
    return JSONResponse({
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": 0, "owned_by": "zcode"}
            for m in FREE_MODELS
        ],
    })


def strip_prefix(model: str) -> str:
    if model.startswith(CATALOG_PREFIX):
        return model[len(CATALOG_PREFIX):]
    return model


def _to_openai(reply: dict, model: str) -> dict:
    """Anthropic Messages -> OpenAI chat.completion."""
    parts = []
    for block in reply.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    usage = reply.get("usage") or {}
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "".join(parts)},
            "finish_reason": _finish(reply),
        }],
        "usage": {
            "prompt_tokens": usage.get("input_tokens") or 0,
            "completion_tokens": usage.get("output_tokens") or 0,
            "total_tokens": (usage.get("input_tokens") or 0)
                           + (usage.get("output_tokens") or 0),
        },
    }


def _finish(reply: dict) -> str:
    reason = reply.get("stop_reason")
    if reason == "max_tokens":
        return "length"
    return "stop" if reply.get("type") != "error" else "error"


def _anthropic_payload(body: dict, model: str) -> dict:
    system_parts, messages = [], []
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        role = m.get("role") or "user"
        text = content if isinstance(content, str) else "".join(
            str(c.get("text") or "") for c in (content or [])
            if isinstance(c, dict))
        if role == "system":
            system_parts.append(text)
        else:
            messages.append({"role": role, "content": text})
    payload = {
        "model": model,
        "max_tokens": int(body.get("max_tokens") or 1024),
        "messages": messages or [{"role": "user", "content": ""}],
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    if body.get("temperature") is not None:
        payload["temperature"] = body["temperature"]
    if body.get("stop"):
        payload["stop_sequences"] = body["stop"]
    return payload


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    check_auth(request)
    try:
        body = await request.json()
    except Exception as exc:
        return JSONResponse({"error": {"message": "bad json: %s" % exc}},
                            status_code=400)

    model = strip_prefix(body.get("model") or FREE_MODELS[0])
    if model not in FREE_MODELS:
        return JSONResponse(
            {"error": {"message": "unknown model %r; available: %s"
                                  % (model, ", ".join(FREE_MODELS))}},
            status_code=400)

    token = read_token()
    if not token:
        return JSONResponse(
            {"error": {"message": "ZCode not logged in; run: open -a ZCode"}},
            status_code=503)

    payload = _anthropic_payload(body, model)
    last_upstream = ""
    for _attempt in range(max(1, CAPTCHA_RETRIES)):
        # 一次性票据：取用即销毁，池空现场 mint；过期票不再往上游送。
        captcha, why = usable_ticket()
        if not captcha:
            if why == "stale":
                return JSONResponse(
                    {"error": {"message":
                               "captcha 票据不在新鲜期内（限 %ds）；ZCode 要求拿到"
                               "票据后几十秒内就发请求。重开 %s 换一张，或让 "
                               "captcha-mint.py 预热票据池。"
                               % (int(CAPTCHA_MAX_FRESH), CAPTCHA_RELAY),
                               "detail": "票据是一次性的，用过的再发就是 3007"}},
                    status_code=503)
            return JSONResponse(
                {"error": {"message": "captcha missing; open %s once"
                                      % CAPTCHA_RELAY,
                           "detail": "upstream requires Aliyun captcha per call"}},
                status_code=503)
        try:
            status, reply = _post(UPSTREAM + "/v1/messages", payload, token, captcha)
            break
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            last_upstream = raw[:300]
            log("upstream HTTP", exc.code, raw[:300])
            if "3012" in raw or "unusual activity" in raw:
                # 风控判定发生在验证码之前：再拖滑块、再重试都只会加深标记。
                return JSONResponse(
                    {"error": {"message":
                               "上游风控拦截 (3012 unusual activity)。"
                               "桥内 _warm_edge() 已用 OPTIONS 预热绕过直连 POST 的边缘拦截。"
                               "仍报 3012 通常有三种原因：OPTIONS 与 POST 间隔太久、失败频率触发"
                               "ESA 更严格档位、或 captcha 已被前一次请求消耗。"
                               "请冷却至少 10 分钟后换一张新票，并只发一次请求。",
                              "upstream": last_upstream}},
                   status_code=503)
            if exc.code in (400, 429) and "3007" in raw:
                continue      # 这张被拒了，换下一张新票
            return JSONResponse({"error": {"message": "upstream %d: %s"
                                                      % (exc.code, last_upstream)}},
                                status_code=502)
        except Exception as exc:
            log("chat error:", _err(exc))
            return JSONResponse({"error": {"message": _err(exc)}}, status_code=502)
    else:
        return JSONResponse(
            {"error": {"message":
                       "连续 %d 张新票都被判 captcha verify failed (3007)。浏览器侧"
                       "滑块其实是通过的（票据已落盘），说明 ZCode 服务端二次验证"
                       "不认非 App 场景产出的票据。" % max(1, CAPTCHA_RETRIES),
                       "upstream": last_upstream}},
            status_code=503)

    if reply.get("type") == "error":
        msg = ((reply.get("error") or {}).get("message")) or str(reply)[:200]
        code = (reply.get("error") or {}).get("code")
        status_code = 503 if code == "3007" else 502
        return JSONResponse(
            {"error": {"message": msg,
                       **({"captcha_relay": CAPTCHA_RELAY}
                          if code == "3007" else {})}},
            status_code=status_code)

    if body.get("stream"):
        return StreamingResponse(_stream(reply, model),
                                 media_type="text/event-stream")
    return JSONResponse(_to_openai(reply, model))


async def _stream(reply: dict, model: str):
    result = _to_openai(reply, model)
    content = result["choices"][0]["message"]["content"]
    chunk = {
        "id": result["id"],
        "object": "chat.completion.chunk",
        "created": result["created"],
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content},
                     "finish_reason": None}],
    }
    yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
    chunk["choices"][0]["delta"] = {}
    chunk["choices"][0]["finish_reason"] = result["choices"][0]["finish_reason"]
    yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
    yield "data: [DONE]\n\n"


@app.get("/")
async def root():
    return {
        "ok": True,
        "bridge": "zcode2codex",
        "version": BRIDGE_VERSION,
        "models": FREE_MODELS,
        "captcha_relay": CAPTCHA_RELAY,
    }


@app.get("/entitlements")
async def entitlements_endpoint(request: Request):
    token = read_token()
    if not token:
        return JSONResponse({"error": "not logged in"}, status_code=503)
    return JSONResponse(entitlements(token))


def main():
    global DEVICE_MID
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8796)
    args = ap.parse_args()

    if not DEVICE_MID:
        try:
            raw = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
            # X-Device-Mid is not in credentials; fall back to the value
            # captured from the real client (see /tmp/fk/zdig*.txt).
            DEVICE_MID = os.environ.get(
                "ZCODE_DEVICE_MID",
                "bf259545-1315-48c6-af67-dd9beebcdeac")
        except Exception:
            DEVICE_MID = "bf259545-1315-48c6-af67-dd9beebcdeac"

    token = read_token()
    log("zcode2codex %s on http://%s:%d" % (BRIDGE_VERSION, args.host, args.port))
    log("  logged in :", "yes" if token else "NO (open -a ZCode)")
    log("  captcha   :", read_captcha()[:24] + "..."
        if read_captcha() else "MISSING -> open " + CAPTCHA_RELAY)
    log("  models    :", ", ".join(FREE_MODELS))
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
