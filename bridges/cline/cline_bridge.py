#!/usr/bin/env python3
"""cline2codex - 把本机 Cline 免费模型暴露成标准 OpenAI 兼容 API。

链路：Codex -> 本桥(:8799) -> Cline 本地 hub daemon(:25463 WebSocket) -> 免费模型

为什么走 hub 而不是 api.cline.bot：
  api.cline.bot 有全局 auth 中间件，非白名单路径一律 401（连不存在的路径也是同一条
  文案），且它要的下游 provider key 由运行时注册表经 hub 注入、二进制里无静态赋值。
  hub daemon 是本地 WebSocket 服务，discovery 文件里直接带 authToken，
  自己连上就能驱动真实推理。2026-09-26 实测 4/5 免费模型返回正常文本。

协议要点（逆向自 code-sidecar，全部实测）：
  * WS URL 与 authToken 来自 ~/.cline/data/locks/hub/production.json
  * authToken 走子协议，必须带前缀 cline-hub-auth.
  * 帧必须包一层: kind=command + envelope
  * client.register 需补全 6 个字段，缺一个就静默无响应
  * model 选择的字段名是 provider/model，不是 providerId/modelId

依赖：fastapi + uvicorn + websockets。用法：python3 cline_bridge.py [--port 8799]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import uvicorn
import websockets
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

BRIDGE_VERSION = "0.1.0"

HUB_DISCOVERY = Path.home() / ".cline" / "data" / "locks" / "hub" / "production.json"
HUB_AUTH_PREFIX = "cline-hub-auth."

# 上游免费模型。cline-free/solar-pro4 实测返回 model not found，已剔除。
FREE_MODELS = [
    "cline-free/deepseek-v4.1-flash",
    "cline-free/muse-spark-1.3-contributor",
    "z-ai/glm-5.3-flash",
    "poolside/laguna-s-2.1:free",
]
CATALOG_PREFIX = "cline/"

BRIDGE_KEY = os.environ.get("CLINE2CODEX_KEY") or ""
CALL_TIMEOUT = float(os.environ.get("CLINE_CALL_TIMEOUT") or "300")


def log(*args):
    print(*args, flush=True)


def read_hub() -> dict:
    """Read hub URL + authToken from the discovery file. Never raises."""
    try:
        return json.loads(HUB_DISCOVERY.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


def hub_ready() -> bool:
    d = read_hub()
    return bool(d.get("url")) and bool(d.get("authToken"))


app = FastAPI(title="cline2codex")


def check_auth(request: Request) -> None:
    """Local bridges are loopback-only and the key is not enforced.

    ocx discovers models with its own bearer token, so enforcing BRIDGE_KEY here
    makes discovery fail with 401 and the provider ends up with no models at all.
    Same policy as the gemini / catpaw / antigravity bridges.
    """
    return



class HubClient:
    """One WebSocket connection to the Cline hub daemon."""

    def __init__(self, url, token, client_id):
        self.url = url
        self.token = token
        self.client_id = client_id
        self.ws = None

    async def __aenter__(self):
        self.ws = await websockets.connect(
            self.url,
            subprotocols=[HUB_AUTH_PREFIX + self.token],
            origin="http://127.0.0.1",
            open_timeout=10,
            max_size=64 * 1024 * 1024,
        )
        await self.register()
        return self

    async def __aexit__(self, *exc):
        try:
            await self.ws.close()
        except Exception:
            pass

    async def _command(self, command, payload=None, sid=None, timeout=None):
        rid = str(uuid.uuid4())
        envelope = {
            "version": "v1",
            "command": command,
            "clientId": self.client_id,
            "requestId": rid,
        }
        if payload:
            envelope["payload"] = payload
        if sid:
            envelope["sessionId"] = sid
        await self.ws.send(json.dumps({"kind": "command", "envelope": envelope}))

        deadline = time.time() + (timeout or CALL_TIMEOUT)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("hub command %s timed out" % command)
            raw = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
            try:
                frame = json.loads(raw)
            except Exception:
                continue
            if frame.get("kind") != "reply":
                continue
            env = frame.get("envelope") or {}
            if env.get("requestId") != rid:
                continue
            return env

    async def register(self):
        # All six fields are required; a missing one is silently ignored by the hub.
        await self._command("client.register", {
            "clientId": self.client_id,
            "clientType": "core",
            "displayName": "cline2codex",
            "transport": "native",
            "actorKind": "client",
            "capabilities": [],
            "workspaceContext": {"workspaceRoot": "/tmp", "cwd": "/tmp"},
        }, timeout=20)

    async def run(self, prompt, model):
        # NOTE: field names are provider/model, NOT providerId/modelId.
        created = await self._command("session.create", {
            "workspaceRoot": "/tmp",
            "cwd": "/tmp",
            "modelSelection": {"provider": "cline", "model": model},
        }, timeout=30)
        session = (created.get("payload") or {}).get("session") or {}
        sid = session.get("sessionId")
        if not sid:
            raise RuntimeError("session.create returned no sessionId")
        # run.start == session.send_input; run.enqueue additionally needs the
        # durable run queue, so prefer run.start.
        return await self._command("run.start", {"prompt": prompt}, sid=sid)



def strip_prefix(model: str) -> str:
    """Codex sends cline/<id>; the hub wants the bare upstream id."""
    if model.startswith(CATALOG_PREFIX):
        return model[len(CATALOG_PREFIX):]
    return model


@app.get("/health")
async def health():
    hub = read_hub()
    return {
        "ok": True,
        "bridge": BRIDGE_VERSION,
        "hub_url": hub.get("url"),
        "hub_ready": hub_ready(),
        "models": len(FREE_MODELS),
    }


@app.get("/v1/models")
async def list_models(request: Request):
    check_auth(request)
    detail = ""
    if not hub_ready():
        detail = "hub daemon not running; start Cline once (open -a Cline)"
    # Bare upstream ids, no prefix: a prefixed id gets double-namespaced by ocx.
    return JSONResponse({
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": 0, "owned_by": "cline"}
            for m in FREE_MODELS
        ],
        **({"detail": detail} if detail else {}),
    })


def _to_openai(reply: dict, model: str) -> dict:
    result = (reply.get("payload") or {}).get("result") or {}
    text = result.get("text") or ""
    usage = result.get("usage") or {}
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop" if result.get("finishReason") == "completed" else "error",
        }],
        "usage": {
            "prompt_tokens": usage.get("inputTokens") or 0,
            "completion_tokens": usage.get("outputTokens") or 0,
            "total_tokens": (usage.get("inputTokens") or 0) + (usage.get("outputTokens") or 0),
        },
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    check_auth(request)
    try:
        body = await request.json()
    except Exception as exc:
        return JSONResponse({"error": {"message": "bad json: %s" % exc}}, status_code=400)

    model = strip_prefix(body.get("model") or FREE_MODELS[0])
    messages = body.get("messages") or []
    prompt = "\n\n".join(
        str(m.get("content")) for m in messages
        if isinstance(m, dict) and m.get("content")
    )
    if not prompt.strip():
        return JSONResponse({"error": {"message": "no prompt"}}, status_code=400)

    hub = read_hub()
    if not hub.get("url"):
        return JSONResponse(
            {"error": {"message": "Cline hub daemon unreachable; run: open -a Cline",
                       "detail": hub.get("error")}}, status_code=503)

    stream = bool(body.get("stream"))
    client_id = "cline2codex-" + uuid.uuid4().hex[:8]
    try:
        if stream:
            return StreamingResponse(
                _stream(client_id, prompt, model),
                media_type="text/event-stream",
            )
        async with HubClient(hub["url"], hub["authToken"], client_id) as hub_client:
            reply = await hub_client.run(prompt, model)
        return JSONResponse(_to_openai(reply, model))
    except Exception as exc:
        log("chat error:", type(exc).__name__, exc)
        return JSONResponse(
            {"error": {"message": "%s: %s" % (type(exc).__name__, exc)}},
            status_code=502,
        )


async def _stream(client_id: str, prompt: str, model: str):
    """The hub returns the whole turn in one reply, so emit it as one chunk."""
    hub = read_hub()
    try:
        async with HubClient(hub["url"], hub["authToken"], client_id) as hub_client:
            reply = await hub_client.run(prompt, model)
        result = (reply.get("payload") or {}).get("result") or {}
        text = result.get("text") or ""
        chunk = {
            "id": "chatcmpl-" + uuid.uuid4().hex[:24],
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        }
        yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
        chunk["choices"][0]["delta"] = {}
        chunk["choices"][0]["finish_reason"] = "stop"
        yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"
    except Exception as exc:
        yield "data: " + json.dumps({"error": {"message": str(exc)}}) + "\n\n"
        yield "data: [DONE]\n\n"


@app.get("/")
async def root():
    return {"ok": True, "bridge": "cline2codex", "version": BRIDGE_VERSION,
            "models": FREE_MODELS}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    hub = read_hub()
    log("cline2codex %s on http://%s:%d" % (BRIDGE_VERSION, args.host, args.port))
    log("  hub       :", hub.get("url") or ("unavailable (%s)" % hub.get("error")))
    log("  bridge key:", "set" if BRIDGE_KEY else "MISSING")
    log("  models    :", len(FREE_MODELS))
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

