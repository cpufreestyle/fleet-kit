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

import asyncio
import base64
import json
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
import _platform
import _common
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

BRIDGE_VERSION = "0.1.0"
# The npm-installed CLI lives under a platform-tagged node directory on macOS;
# on Windows/Linux it is on PATH or pointed at by FLEET_QODERCLICN_PATH.
CLI_CANDIDATES = [
    os.environ.get("QODER_CLI_PATH") or "",
    _platform.cli_binary("qoderclicn",
                         unix_glob=[".local/node-*/bin/qoderclicn",
                                    ".local/share/pnpm/global/*/bin/qoderclicn"],
                         windows_names=["qoderclicn.cmd", "qoderclicn.exe"]),
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

app = _common.make_app("qoder2codex", BRIDGE_VERSION)

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


# `qoderclicn --list-models` 输出首行是字面量表头。老代码只过滤
# Available/Models/Model 三种大小写混合的拼写，而 CLI 1.1.x 实际吐的是大写
# "MODEL"，于是它被当成模型 id 泄漏进选择器（出现幽灵行 qoder/MODEL）。
# 按「首个 token 是否表头词」判定，大小写无关，也不会误伤 modelscope-x 这类
# 以合法前缀开头的 id。
# 分隔符只取空白和冒号：表头行总是"MODEL"、"Models:"、"MODEL - DESCRIPTION"
# 这种空白分写的形态；把 - 也算进来的话，model-router / id-chain 这种合法 id
# 会被当成表头整行丢掉。
_MODEL_HEADER_WORDS = {"model", "models", "available", "name", "id"}


def _is_header_line(line: str) -> bool:
    head = re.split(r"[\s:]+", line, maxsplit=1)[0].strip().lower()
    return head in _MODEL_HEADER_WORDS


def _parse_models(output: str) -> list[dict]:
    """解析 `qoderclicn --list-models` 输出，容错多种格式。"""
    models: list[dict] = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line or _is_header_line(line):
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


_IMAGE_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


def _extract_image_attachments(messages: list) -> tuple[list[str], Optional[str]]:
    """把 OpenAI 视觉消息里的图片落成临时文件，返回 (附件路径, 临时目录)。

    qoderclicn 只接受 --attachment 文件路径，所以 base64/URL 图片必须先落盘。
    同一张图片（按内容哈希）只附一次，避免多轮对话重复塞同一张图。
    """
    import hashlib
    import urllib.request

    seen: set[str] = set()
    paths: list[str] = []
    tmp_dir: Optional[str] = None

    def _save(raw: bytes, mime: str) -> None:
        nonlocal tmp_dir
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen:
            return
        seen.add(digest)
        if tmp_dir is None:
            tmp_dir = tempfile.mkdtemp(prefix="qoder_img_")
        path = os.path.join(tmp_dir, f"img_{len(paths)}{_IMAGE_EXT_BY_MIME.get(mime, '.png')}")
        with open(path, "wb") as fh:
            fh.write(raw)
        paths.append(path)

    def _from_url(url: str) -> None:
        if url.startswith("data:"):
            header, _, data = url.partition(",")
            mime = header[5:].split(";")[0]
            if "base64" not in header:
                return
            try:
                _save(base64.b64decode(data), mime)
            except Exception:
                pass
        elif url.startswith("http://") or url.startswith("https://"):
            try:
                with urllib.request.urlopen(url, timeout=20) as resp:
                    _save(resp.read(), resp.headers.get("Content-Type", ""))
            except Exception:
                pass

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = str(part.get("type") or "").lower()
            if ptype in ("image_url", "image"):
                node = part.get("image_url") or part.get("image") or {}
                url = node.get("url", "") if isinstance(node, dict) else str(node)
                if url:
                    _from_url(str(url))
            elif ptype == "input_image":
                source = part.get("source") or {}
                if isinstance(source, dict):
                    data = str(source.get("data") or source.get("base64") or "")
                    if data:
                        try:
                            _save(base64.b64decode(data), str(source.get("media_type") or ""))
                        except Exception:
                            pass
    return paths, tmp_dir


def _cleanup_tmp(tmp_dir: Optional[str]) -> None:
    if tmp_dir:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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


# "auto"/"default" (and an empty model) fall back to DEFAULT_MODEL; anything
# else keeps the qoder/ prefix stripped exactly once.
_strip_qoder = _common.make_model_remapper("qoder/")


def _clean_model(model: str) -> str:
    name = (_strip_qoder((model or "").strip()) or "").strip()
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
    attachments, tmp_dir = _extract_image_attachments(messages)
    if not body.strip():
        if attachments:
            body = "请描述附件图片的内容。"  # 纯图片请求（无文字）也放行
        else:
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
    for path in attachments:
        base_args += ["--attachment", path]

    if wants_stream:
        return StreamingResponse(_stream(body, base_args, model, payload, tmp_dir),
                                 media_type="text/event-stream")
    try:
        return JSONResponse(content=await _collect(body, base_args, model, payload))
    finally:
        _cleanup_tmp(tmp_dir)


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


async def _stream(body: str, base_args: list[str], model: str, payload: dict, tmp_dir=None):
    """把 qoderclicn 的 stream-json 事件转成 OpenAI SSE。"""
    chat_id = "chatcmpl-" + os.urandom(8).hex()
    created = int(time.time())

    def chunk(delta: dict, finish: Optional[str] = None) -> str:
        payload = {"id": chat_id, "object": "chat.completion.chunk", "created": created,
                   "model": model,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    try:
        yield chunk({"role": "assistant"})

        args = base_args + ["--output-format", "stream-json", body]
        proc = await asyncio.create_subprocess_exec(
            _cli(), *args, cwd=str(Path.home()),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        # stderr 必须持续抽干：管道缓冲写满后 CLI 会阻塞在 write 上，整个流
        # 跟着挂死；抽干的同时留住文本，进程异常退出时回报给调用方。
        stderr_buf = bytearray()

        async def _drain_stderr():
            assert proc.stderr is not None
            async for raw in proc.stderr:
                stderr_buf.extend(raw)

        pump = asyncio.ensure_future(_drain_stderr())
        try:
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
            if proc.returncode != 0:
                detail = stderr_buf.decode("utf-8", "replace").strip()[:400]
                yield chunk({"content": f"[qoder error] 退出码 {proc.returncode}: {detail}"})
        finally:
            pump.cancel()
        yield chunk({}, "stop")
        yield "data: [DONE]\n\n"
    except Exception as exc:
        # 流一旦开始，HTTP 状态码就无法改了；此时让异常把连接裸断掉，客户端
        # 只会看到 "socket closed unexpectedly"（且无从判断发生了什么）。
        # 把错误当成最后一个内容块推完，再正常收尾，客户端至少能拿到真相。
        yield chunk({"content": f"[bridge error] {type(exc).__name__}: {exc}"})
        yield chunk({}, "stop")
        yield "data: [DONE]\n\n"
    finally:
        _cleanup_tmp(tmp_dir)


def main():
    def banner(args) -> None:
        global API_KEY
        # The installer passes the key through the plist environment, but a
        # hand-run bridge uses --api-key. Without this the flag parsed cleanly
        # and was then ignored, so the bridge stayed open.
        if getattr(args, "api_key", ""):
            API_KEY = args.api_key
        print(f"qoder2codex v{BRIDGE_VERSION}")
        print(f"CLI : {[c for c in CLI_CANDIDATES if c and Path(c).exists()]}")
        print(f"Auth: {_auth_state()} key={'set' if API_KEY else 'OPEN'}")

    _common.serve(
        app, 8789,
        "Listen: http://%s:%s",
        log_level="warning",
        description="Qoder CN → OpenAI 兼容转换器",
        extra_args=[("--api-key", {"default": os.environ.get("QODER2CODEX_KEY", "")})],
        on_args=banner)


if __name__ == "__main__":
    main()
