"""Image-generation models must not be advertised as chat models.

Measured 2026-09-29 21:30: GET /v1/models on lingxi2codex returns
["deepseek-flash", "glm-5.3-flash", "gpt-image-2.5-sunburst"] straight from the
upstream /models reply, which carries only id/object/owned_by -- no capability
field. Asking gpt-image-2.5-sunburst for a completion is answered 502 in 0.09s
with {"error":"invalid upstream url"}, so the bridge can never serve it; yet it
sits in the model list exactly like the two chat models. Once lingxi verifies
REAL the catalog filter restores every row it owns, so that dead row would reach
the picker as if it worked.

WorkBuddy already splits these off by supported_endpoint_types
(bridges/workbuddy/core.py:1294). The shared helper gives gateways that report
nothing a name-based fallback, and refuses to hide a whole catalogue -- one
wrongly hidden model is worse than one broken row.
"""
import asyncio
import importlib.util
import os
import sys

import pytest

BRIDGES = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "bridges"))
sys.path.insert(0, BRIDGES)

import _common

spec = importlib.util.spec_from_file_location(
    "lingxi_bridge", os.path.join(BRIDGES, "lingxi", "lingxi_bridge.py"))
lingxi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lingxi)


@pytest.mark.parametrize("mid", ["gpt-image-2.5-sunburst", "seedream-4.0",
                                 "doubao-seededit-3.0", "qwen-t2i", "wanx-i2i",
                                 "GPT-Image-2"])
def test_image_models_are_dropped(mid):
    ids = ["glm-5.3-flash", mid, "deepseek-flash"]
    assert _common.drop_image_models(ids) == ["glm-5.3-flash", "deepseek-flash"]


def test_chat_ids_survive_untouched():
    ids = ["glm-5.3-flash", "deepseek-flash", "lingxi/deepseek-v4-pro"]
    assert _common.drop_image_models(ids) == ids


def test_an_all_image_list_is_kept_rather_than_hidden():
    ids = ["gpt-image-1", "seedream-4.0"]
    assert _common.drop_image_models(ids) == ids


def test_empty_list_stays_empty():
    assert _common.drop_image_models([]) == []


def _fake_upstream(monkeypatch, ids):
    class Reply:
        status_code = 200

        def json(self):
            return {"data": [{"id": i, "object": "model", "owned_by": "lingxi"}
                             for i in ids]}

    async def resolve_auth():
        return {"token": "fake-token"}

    async def authed(method, path, auth, **kw):
        assert path == lingxi.MODELS_PATH
        return Reply()

    monkeypatch.setattr(lingxi, "resolve_auth", resolve_auth)
    monkeypatch.setattr(lingxi, "authed", authed)
    lingxi._cache["models"], lingxi._cache["ts"] = [], 0.0


def test_lingxi_model_list_leaves_the_image_model_behind(monkeypatch):
    _fake_upstream(monkeypatch, ["deepseek-flash", "glm-5.3-flash",
                                 "gpt-image-2.5-sunburst"])
    assert asyncio.run(lingxi.get_models(force=True)) == [
        "deepseek-flash", "glm-5.3-flash"]


def test_lingxi_health_and_v1_models_agree_on_the_filtered_list(monkeypatch):
    _fake_upstream(monkeypatch, ["deepseek-flash", "gpt-image-2.5-sunburst"])
    seen = asyncio.run(lingxi.get_models(force=True))
    assert seen == ["deepseek-flash"]
