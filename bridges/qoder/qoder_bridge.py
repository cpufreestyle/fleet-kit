#!/usr/bin/env python3
"""qoder2codex — 把 Qoder（CN）订阅模型暴露成标准 OpenAI 兼容 API。

原理（官方 CLI 驱动）：
  * 复用官方 `qoderclicn`（@qodercn-ai/qoderclicn）的非交互模式：
      -p --output-format json            → 单次完整回答
      -p --output-format stream-json     → NDJSON 事件流（可转 SSE）
  * 登录态沿用 CLI 自己的凭据（~/.qoder-cn/.auth/user），不另存 token。
  * 只做协议转换：OpenAI chat/completions ⇄ Qoder CLI 文本问答。

依赖：fastapi + uvicorn。用法：python3 qoder_bridge.py [--port 8789]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

BRIDGE_VERSION = "0.1.0"
CLI_CANDIDATES = [
    os.environ.get("QODER_CLI_PATH") or "",
    str(Path.home() / ".local/node-v22.20.0-darwin-arm64/bin/qoderclicn"),
    shutil.which("qoderclicn") or "",
]
DEFAULT_MODEL = os.environ.get("QODER_DEFAULT_MODEL") or "auto"
FALLBACK_MODELS = [
    {"id": "auto", "object": "model", "created": 0, "owned_by": "qoder"},
    {"id": "performance", "object": "model", "created": 0, "owned_by": "qoder"},
    {"id": "lite", "object": "model", "created": 0, "owned_by": "qoder"},
]
CALL_TIMEOUT = int(os.environ.get("QODER_CALL_TIMEOUT") or "300")
API_KEY = os.environ.get("QODER2CODEX_KEY", "")

app = FastAPI(title="qoder2codex", version=BRIDGE_VERSION)

_models_cache: dict = {"models": FALLBACK_MODELS, "fetched_at": 0.0, "source": "fallback"}
_models_lock = threading.Lock()


def _check_auth(authorization: Optional[str], x_api_key: Optional[str]) -> None:
    if not API_KEY:
        return
    token = (authorization or "")[7:].strip() if (authorization or "").startswith("Bearer ") else (x_api_key or "")
    if token != API_KEY:
        raise HTTPException(status_code=401, detail={"error": {
            "message": "invalid api key", "type": "auth_error"}})


def _cli() -> str:
    for candidate in CLI_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    raise HTTPException(status_code=503, detail={"error": {
        "message": "qoderclicn 未安装：npm i -g @qodercn-ai/qoderclicn",
        "type": "cli_missing"}})


def _run_cli(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([_cli()] + args, capture_output=True, text=True,
                          timeout=timeout, cwd=str(Path.home()))


def _auth_state() -> dict:
    auth = Path.home() / ".qoder-cn/.auth/user"
    exists = auth.is_file()
    info: dict = {"logged_in": exists, "auth_file": str(auth)}
    try:
        status = Path.home() / ".qoder-cn/.qoder-app-status.json"
        if status.is_file():
            data = json.loads(status.read_text(encoding="utf-8"))
            info["ide_account"] = data.get("name")
            info["ide_logged_in"] = data.get("logged_in")
    except Exception:
        pass
    try:
        version = _run_cli(["--version"], timeout=30).stdout.strip()
        info["cli_version"] = version
    except Exception:
        pass
    return info


def _parse_models(output: str) -> list[dict]:
    """解析 `qoderclicn --list-models` 输出，容错多种格式。"""
    models: list[dict] = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("Available", "Models", "Model")):
            continue
        matched = re.match(r"^([A-Za-z0-9._\-/]+)\s*(?:[-–—:|]\s*(.*))?$", line)
        if not matched:
            continue
        model_id, label = matched.group(1), (matched.group(2) or "").strip()
        if model_id.lower() in {"name", "id"}:
            continue
        models.append({"id": model_id, "object": "model", "created": 0,
                       "owned_by": "qoder",
                       **({"description": label} if label else {})})
    return models


def _refresh_models(force: bool = False) -> list[dict]:
    with _models_lock:
        if not force and time.time() - _models_cache["fetched_at"] < 300 and _models_cache["models"]:
            return _models_cache["models"]
        try:
            proc = _run_cli(["--list-models"], timeout=90)
            parsed = _parse_models(proc.stdout)
            if parsed:
                _models_cache.update({"models": parsed, "fetched_at": time.time(),
                                      "source": "cli"})
            elif proc.stdout.strip():
                _models_cache.update({"models": FALLBACK_MODELS,
                                      "fetched_at": time.time(), "source": "fallback"})
        except Exception:
            if not _models_cache["models"]:
                _models_cache["models"] = FALLBACK_MODELS
        return _models_cache["models"]


def _messages_to_prompt(messages: list) -> tuple[str, str]:
    """返回 (system_prompt, 对话正文)。"""
    system_parts: list[str] = []
    turns: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        content = msg.get("content")
        if isinstance(content, list):
            content = "\n".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        content = str(content or "").strip()
        if not content:
            continue
        if role in ("system", "developer"):
            system_parts.append(content)
        elif role == "assistant":
            turns.append(f"Assistant: {content}")
        else:
            turns.append(f"User: {content}")
    return "\n\n".join(system_parts), "\n\n".join(turns)


def _extract_text(payload: dict) -> str:
    """从 CLI 的 json 输出里抽取回答文本（多格式容错）。"""
    for key in ("result", "response", "text", "output", "answer"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    message = payload.get("message")
    if isinstance(message, dict):
        for key in ("content", "text"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value
        blocks = message.get("content")
        if isinstance(blocks, list):
            texts = [b.get("text", "") for b in blocks if isinstance(b, dict)]
            if any(texts):
                return "".join(texts)
    if isinstance(payload.get("content"), str):
        return payload["content"]
    return ""


def _extract_usage(payload: dict) -> Optional[dict]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    completion = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    try:
        prompt, completion = int(prompt), int(completion)
    except (TypeError, ValueError):
        return None
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


def _clean_model(model: str) -> str:
    name = (model or "").strip()
    if name.lower().startswith("qoder/"):
        name = name.split("/", 1)[1]
    if not name or name.lower() in ("auto", "default"):
        name = DEFAULT_MODEL
    return name


@app.get("/health")
async def health():
    state = {"status": "ok", "version": BRIDGE_VERSION}
    try:
        state.update(_auth_state())
    except Exception as exc:
        state.update({"status": "degraded", "error": str(exc)[:200]})
    state["models_cached"] = len(_refresh_models())
    state["default_model"] = DEFAULT_MODEL
    return state


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": _refresh_models()}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request,
                           authorization: Optional[str] = Header(default=None),
                           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {exc}"}})

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required"}})

    model = _clean_model(str(payload.get("model") or ""))
    wants_stream = bool(payload.get("stream"))
    system_prompt, body = _messages_to_prompt(messages)
    if not body.strip():
        raise HTTPException(status_code=400, detail={"error": {"message": "empty prompt"}})

    base_args = [
        "-p",
        "--model", model,
        "--tools", "",                 # 禁用内置工具，只做文本补全
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--strict-mcp-config",       # 忽略用户 MCP 配置，避免 CLI 启动阶段卡死
        "--max-output-tokens", str(min(int(payload.get("max_tokens") or 4096), 32000)),
    ]
    if system_prompt:
        base_args += ["--append-system-prompt", system_prompt]

    if wants_stream:
        return StreamingResponse(_stream(body, base_args, model, payload),
                                 media_type="text/event-stream")
    return JSONResponse(content=await _collect(body, base_args, model, payload))


async def _collect(body: str, base_args: list[str], model: str, payload: dict) -> dict:
    args = base_args + ["--output-format", "json", body]
    try:
        proc = await asyncio.to_thread(
            subprocess.run, [_cli()] + args,
            capture_output=True, text=True, timeout=CALL_TIMEOUT, cwd=str(Path.home()))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail={"error": {
            "message": f"qoderclicn 超时（{CALL_TIMEOUT}s）", "type": "upstream_timeout"}})

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:400]
        if "Not logged in" in detail or "login" in detail.lower():
            raise HTTPException(status_code=503, detail={"error": {
                "message": "Qoder 未登录，请运行 qoderclicn login", "type": "auth_error"}})
        raise HTTPException(status_code=502, detail={"error": {
            "message": detail or f"qoderclicn 退出码 {proc.returncode}", "type": "upstream_error"}})

    parsed: dict = {}
    try:
        parsed = json.loads(proc.stdout)
    except Exception:
        parsed = {}
    text = _extract_text(parsed)
    if not text.strip():
        text = (proc.stdout or "").strip()
    if not text.strip():
        raise HTTPException(status_code=502, detail={"error": {
            "message": "Qoder 未返回内容", "type": "upstream_error"}})

    response = {
        "id": "chatcmpl-" + os.urandom(8).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
    }
    usage = _extract_usage(parsed)
    if usage:
        response["usage"] = usage
    return response


async def _stream(body: str, base_args: list[str], model: str, payload: dict):
    """把 qoderclicn 的 stream-json 事件转成 OpenAI SSE。"""
    chat_id = "chatcmpl-" + os.urandom(8).hex()
    created = int(time.time())

    def chunk(delta: dict, finish: Optional[str] = None) -> str:
        payload = {"id": chat_id, "object": "chat.completion.chunk", "created": created,
                   "model": model,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield chunk({"role": "assistant"})

    args = base_args + ["--output-format", "stream-json", body]
    proc = await asyncio.create_subprocess_exec(
        _cli(), *args, cwd=str(Path.home()),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        etype = event.get("type")
        if etype == "assistant":
            message = event.get("message") or {}
            blocks = message.get("content") or []
            for block in blocks if isinstance(blocks, list) else []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    yield chunk({"content": block["text"]})
        elif etype == "result":
            if event.get("is_error"):
                yield chunk({"content": f"[qoder error] {event.get('result', '')}"})
            break
    await proc.wait()
    yield chunk({}, "stop")
    yield "data: [DONE]\n\n"


def main():
    parser = argparse.ArgumentParser(description="Qoder CN → OpenAI 兼容转换器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8789)
    parser.add_argument("--api-key", default=os.environ.get("QODER2CODEX_KEY", ""))
    args = parser.parse_args()

    print(f"qoder2codex v{BRIDGE_VERSION}")
    print(f"CLI : {[c for c in CLI_CANDIDATES if c and Path(c).exists()]}")
    print(f"Auth: {_auth_state()}")
    print(f"Listen: http://{args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
