import importlib.util
import json
import os
import tempfile
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location("checkin", "tools/checkin.py")
checkin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkin)


def health_handler(request):
        assert request.url.path == "/health"
        assert request.headers.get("authorization") == "Bearer test-key"
        return httpx.Response(200, json={"status": "ok"})


def test_workbuddy_health(monkeypatch):
    monkeypatch.setattr(checkin, "WORKBUDDY_HEALTH_URL", "http://127.0.0.1:1/health")
    monkeypatch.setattr(checkin, "WORKBUDDY_KEY", "test-key")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(health_handler)) as ac:
            return await checkin.task_workbuddy(ac)
    import asyncio
    result = asyncio.run(run())
    assert result["ok"] is True
    assert "自动签到服务在线" in result["detail"]

def test_task_registry_contains_both_tasks():
    assert set(checkin.TASKS) == {"xhx", "workbuddy"}
