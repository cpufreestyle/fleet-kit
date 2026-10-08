"""The Anthropic gateway must translate, and must never invent a model.

What is checked here, and why each one earned a test:

* the request translation, because a tool_use block turned into plain text is
  a Claude Code that silently stops calling tools;
* the response translation, because finish_reason=length with an empty body
  is how a reasoning model looks like a dead one, and a content:[] message is
  what makes Claude Code drop the turn;
* the SSE event order, because Claude Code parses that grammar strictly and a
  missing content_block_stop leaves a spinner running forever;
* resolve(), because the picker is what the user feels: an alias whose target
  is down has to move to a model that answers and say so in a header, and an
  unknown id has to be refused, never quietly swapped;
* the routing tables, because a second copy of bridge ports or key env names
  is a drift bug waiting for the next new bridge.

No test here touches the network: the upstream is a fake response object
behind a fake opener, so the whole HTTP path runs in-process.
"""
import importlib.util
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

import anthropic_gateway as gw

KIT = os.path.abspath(os.path.dirname(__file__))


def _openai_reply(text="hi", tool_calls=None, finish="stop", usage=None):
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"id": "chatcmpl-1", "object": "chat.completion",
            "choices": [{"index": 0, "message": message,
                         "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 11, "completion_tokens": 5}}


class FakeHeaders:
    def __init__(self, mapping):
        self.mapping = dict(mapping)

    def get(self, key, default=None):
        for name, value in self.mapping.items():
            if name.lower() == key.lower():
                return value
        return default


class FakeResponse:
    def __init__(self, body=b"", content_type="application/json", lines=None,
                 status=200):
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.headers = FakeHeaders({"Content-Type": content_type})
        self.status = status
        self.lines = lines or []
        self.closed = False

    def read(self, *args):
        return self.body

    def close(self):
        self.closed = True

    def __iter__(self):
        return iter(self.lines)


class FakeOpener:
    """Stands in for the module OPENER: records the request, returns a reply."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []
        self.bodies = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        try:
            self.bodies.append(json.loads(request.data.decode("utf-8")))
        except Exception:
            self.bodies.append(None)
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def gateway(monkeypatch):
    """A live gateway process on loopback with a fake upstream behind it."""
    opener = FakeOpener(response=FakeResponse(
        body=json.dumps(_openai_reply()).encode("utf-8")))
    monkeypatch.setattr(gw, "OPENER", opener)
    monkeypatch.setattr(gw, "route_for",
                        lambda slug: {"url": "http://upstream.invalid/v1/chat/completions",
                                      "key": "k", "model": slug,
                                      "transport": "bridge", "provider": "fake"})
    server = gw.Server(("127.0.0.1", 0), gw.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % server.server_address[1], opener
    server.shutdown()
    server.server_close()


def _post(url, path, payload, headers=None):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url + path, data=body, method="POST",
                                     headers=headers or {})
    try:
        with urllib.request.build_opener(
                urllib.request.ProxyHandler({})).open(request, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8")), dict(exc.headers)


def _get(url, path):
    with urllib.request.build_opener(
            urllib.request.ProxyHandler({})).open(url + path, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _sse_lines(chunks):
    lines = []
    for chunk in chunks:
        lines.append(("data: " + json.dumps(chunk)).encode("utf-8"))
    return [line + b"\n" for line in lines]


_FALLBACK_SLUGS = (
    "stepfun/step-5-preview", "stepfun/step-3.7-flash",
    "workbuddy-gpt/hy4-preview", "workbuddy-gpt/gpt-6-astra",
    "workbuddy/glm-5.2", "workbuddy/hy3",
    "trae/Doubao-Seed-Evolving", "trae/seed-code-pro-0430",
    "qoder/Qwen3.8-Max", "qoder/Qwen3.8-Flash",
    "zcode/GLM-5.3", "zcode/GLM-5.3-Flash",
    "gemini/gemini-3-pro-preview", "catpaw/longcat-flash",
    "xhx/raccoon-19b265", "codely/codely-core", "codely/codely-air",
    "qwen/qwen3.8-flash", "lingxi/deepseek-flash",
    "cline-free/deepseek-v4.1-flash",
)


def _write_catalog(path, slugs):
    """rows as catalog_sort.py would leave them: priority ascending, slug as tiebreak."""
    rows = [{"slug": slug, "priority": i + 1,
             "display_name": slug.rpartition("/")[2]}
            for i, slug in enumerate(slugs)]
    path.write_text(json.dumps({"models": rows}), encoding="utf-8")


@pytest.fixture
def fleet_catalog(tmp_path, monkeypatch):
    """A catalog file of our own, so the live one cannot decide the verdict.

    catalog_rows() falls back to polling every bridge when the named file has
    fewer than MIN_CATALOG_ROWS rows, which turns a resolve() test into a
    loopback sweep whose answer depends on which bridges happen to be up.
    A file we wrote ourselves keeps the assertion about resolve() alone.
    """
    path = tmp_path / "cc-switch-model-catalog.json"
    monkeypatch.setattr(gw, "CATALOG_PATH", str(path))
    return path


# ------------------------------------------------------------ request translation
def _route(slug="workbuddy/glm-5.2"):
    return {"url": "http://127.0.0.1:1/v1/chat/completions", "key": "k",
            "model": slug, "transport": "bridge", "provider": "workbuddy"}


def test_system_and_messages_survive_translation():
    body = {"system": "be terse", "max_tokens": 64,
            "messages": [{"role": "user", "content": [{"type": "text",
                                                        "text": "hello"}]}]}
    out = gw.to_openai(body, _route())
    assert out["messages"][0] == {"role": "system", "content": "be terse"}
    assert out["messages"][1]["role"] == "user"
    assert out["messages"][1]["content"][0]["text"] == "hello"
    assert out["max_tokens"] == 64


def test_tool_use_becomes_tool_calls_and_tool_result_becomes_role_tool():
    body = {"max_tokens": 64, "messages": [
        {"role": "user", "content": "read a.py"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "reading"},
            {"type": "tool_use", "id": "toolu_1", "name": "read_file",
             "input": {"path": "a.py"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1",
             "content": "file body"}]}]}
    out = gw.to_openai(body, _route())
    # the user turn, then the assistant turn that owns the tool_calls, then the
    # tool answer: OpenAI pairs a role=tool message with the assistant turn
    # directly above it, so a tool_result may never be hoisted past its call
    assert [m["role"] for m in out["messages"]] == ["user", "assistant", "tool"]
    assistant = out["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "reading"
    call = assistant["tool_calls"][0]
    assert call["id"] == "toolu_1"
    assert call["type"] == "function"
    assert call["function"]["name"] == "read_file"
    assert json.loads(call["function"]["arguments"]) == {"path": "a.py"}
    assert out["messages"][2] == {"role": "tool", "tool_call_id": "toolu_1",
                                 "content": "file body"}


def test_image_block_becomes_a_data_url():
    body = {"max_tokens": 64, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64",
                                      "media_type": "image/png",
                                      "data": "QUJD"}}]}]}
    out = gw.to_openai(body, _route())
    part = out["messages"][0]["content"][0]
    assert part["type"] == "image_url"
    assert part["image_url"]["url"] == "data:image/png;base64,QUJD"


def test_tools_and_tool_choice_map_to_openai_shapes():
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    body = {"max_tokens": 64, "messages": [{"role": "user", "content": "go"}],
            "tools": [{"name": "read_file", "description": "read",
                       "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": "read_file"}}
    out = gw.to_openai(body, _route())
    assert out["tools"][0]["type"] == "function"
    assert out["tools"][0]["function"]["parameters"] == schema
    assert out["tool_choice"] == {"type": "function",
                                  "function": {"name": "read_file"}}
    body["tool_choice"] = {"type": "any"}
    assert gw.to_openai(body, _route())["tool_choice"] == "required"
    body["tool_choice"] = {"type": "none"}
    assert gw.to_openai(body, _route())["tool_choice"] == "none"


def test_stream_asks_for_usage_and_stop_sequences_map():
    body = {"max_tokens": 64, "stream": True, "stop_sequences": ["END"],
            "messages": [{"role": "user", "content": "go"}]}
    out = gw.to_openai(body, _route())
    assert out["stream"] is True
    assert out["stream_options"] == {"include_usage": True}
    assert out["stop"] == ["END"]


def test_absurd_max_tokens_is_clamped_not_forwarded():
    body = {"max_tokens": 10 ** 9, "messages": []}
    assert gw.to_openai(body, _route())["max_tokens"] == 32768
    # a deliberately tiny budget is still forwarded verbatim: only an absurd
    # one is capped, because a floor rewrites a call the client never made
    body = {"max_tokens": 1, "messages": []}
    assert gw.to_openai(body, _route())["max_tokens"] == 1
    body = {"messages": []}
    assert gw.to_openai(body, _route())["max_tokens"] == 1024


# ----------------------------------------------------------- response translation
def test_text_and_tool_use_blocks_and_usage():
    data = _openai_reply(text="done", tool_calls=[
        {"id": "call_1", "type": "function",
         "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}}],
        finish="tool_calls", usage={"prompt_tokens": 40, "completion_tokens": 12})
    out = gw.to_anthropic(data, "workbuddy/glm-5.2")
    assert [b["type"] for b in out["content"]] == ["text", "tool_use"]
    assert out["content"][0]["text"] == "done"
    assert out["content"][1]["input"] == {"path": "a.py"}
    assert out["stop_reason"] == "tool_use"
    assert out["usage"] == {"input_tokens": 40, "output_tokens": 12,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0}
    assert out["model"] == "workbuddy/glm-5.2"


@pytest.mark.parametrize("finish,expected", [
    ("stop", "end_turn"), ("length", "max_tokens"), ("max_tokens", "max_tokens"),
    ("tool_calls", "tool_use"), ("function_call", "tool_use"),
    ("content_filter", "refusal"), (None, "end_turn")])
def test_stop_reason_mapping(finish, expected):
    assert gw.stop_reason_for(finish, []) == expected


def test_empty_reasoning_only_body_still_yields_one_text_block():
    out = gw.to_anthropic(_openai_reply(text="", finish="length"), "m")
    assert out["content"] == [{"type": "text", "text": ""}]
    assert out["stop_reason"] == "max_tokens"


def test_unparseable_tool_arguments_are_kept_not_dropped():
    data = _openai_reply(tool_calls=[
        {"id": "c", "type": "function",
         "function": {"name": "f", "arguments": "not json"}}], finish="tool_calls")
    blocks = gw.to_anthropic(data, "m")["content"]
    assert blocks[-1]["type"] == "tool_use"
    assert "not json" in json.dumps(blocks[-1]["input"])


@pytest.mark.parametrize("body,expected", [
    ({"error": {"message": "nope"}}, "nope"),
    ({"choices": [{"message": {"content": "request blocked, resend"}}]},
     "request blocked"),
    (_openai_reply(text="all good"), None)])
def test_upstream_refusal_marker_is_an_error_even_at_200(body, expected):
    refusal = gw.upstream_refusal(body)
    if expected is None:
        assert refusal is None
    else:
        assert expected in refusal


# ----------------------------------------------------------------------- routing
def test_resolve_prefers_the_exact_catalog_slug(monkeypatch, fleet_catalog):
    monkeypatch.setattr(gw, "route_alive", lambda route, timeout=1.0: True)
    _write_catalog(fleet_catalog, _FALLBACK_SLUGS + ("trae/kimi-k2.7-code",))
    slug, note = gw.resolve("trae/kimi-k2.7-code")
    assert slug == "trae/kimi-k2.7-code"
    assert note == "catalog slug"


def test_resolve_alias_lands_on_its_target(monkeypatch, fleet_catalog):
    monkeypatch.setattr(gw, "route_alive", lambda route, timeout=1.0: True)
    _write_catalog(fleet_catalog, _FALLBACK_SLUGS)
    slug, note = gw.resolve("claude-opus-5")
    assert slug == gw.CLAUDE_ALIASES["claude-opus-5"]
    assert "alias" in note


def test_resolve_alias_falls_back_when_its_target_is_down(monkeypatch, fleet_catalog):
    target = gw.CLAUDE_ALIASES["claude-opus-5"]
    _write_catalog(fleet_catalog, _FALLBACK_SLUGS)

    def alive(route, timeout=1.0):
        return route["model"] != target

    monkeypatch.setattr(gw, "route_alive", alive)
    slug, note = gw.resolve("claude-opus-5")
    assert slug and slug != target
    assert "fell back" in note


def _load_catalog_sort():
    spec = importlib.util.spec_from_file_location(
        "catalog_sort", os.path.join(KIT, "catalog_sort.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_alias_targets_never_double_the_bridge_prefix():
    """An alias may not name a slug with the provider prefix pasted on twice.

    Measured 2026-10-02: every claude-sonnet slot pointed at
    trae/trae-seed-code-pro-0430 while the trae bridge lists
    trae/seed-code-pro-0430, and remap_model only ever strips the one
    prefix -- so each sonnet call reached upstream as a model that does
    not exist and came back 502 "the param is invalid". route_alive cannot
    catch this class: the port answers, the slug is not there. No bridge
    in the fleet exposes a doubled spelling (every live /v1/models read
    on 2026-10-02), so doubled means drift.
    """
    doubled = []
    for slot, target in gw.CLAUDE_ALIASES.items():
        provider, sep, name = target.partition("/")
        if sep and name.startswith(provider + "-"):
            doubled.append((slot, target))
    assert doubled == [], "alias doubles the prefix: %r" % (doubled,)


def test_alias_targets_are_models_catalog_sort_ranks_important():
    """Each alias slot may only name a model the sorter already ranks.

    PER_PROVIDER_IMPORTANT is written from each bridge's live /v1/models,
    so an alias whose target is absent from it names a model nobody has
    measured -- the same 502 the doubled prefix caused: the port answers
    and the slug does not exist. Membership is exact, not the substring
    match important_rank() uses, because the drifted
    trae-seed-code-pro-0430 contains seed-code-pro-0430 as a substring.
    """
    catalog_sort = _load_catalog_sort()
    missing = []
    for slot, target in gw.CLAUDE_ALIASES.items():
        provider, _sep, name = target.partition("/")
        known = catalog_sort.PER_PROVIDER_IMPORTANT.get(provider, ())
        if name not in known:
            missing.append((slot, target))
    assert missing == [], "alias target not ranked important: %r" % (missing,)


def test_resolve_unknown_claude_slot_lands_on_the_harbor(monkeypatch, fleet_catalog):
    monkeypatch.setattr(gw, "route_alive", lambda route, timeout=1.0: True)
    # the harbor is a *direct* upstream, so without its key route_for() answers
    # None and the fallback loop would pick whichever row happens to be live
    # -- that is a different assertion, not this one
    monkeypatch.setattr(gw, "fleet_env",
                        lambda path=None: {"STEPFUN_PLAN_API_KEY": "sk-test"})
    _write_catalog(fleet_catalog, _FALLBACK_SLUGS)
    slug, _note = gw.resolve("claude-sonnet-unknown-slot-probe")
    assert slug == gw.HARBOR
    assert _note.startswith("alias of %s" % gw.HARBOR)


def test_resolve_refuses_an_unknown_model(fleet_catalog):
    _write_catalog(fleet_catalog, _FALLBACK_SLUGS)
    slug, note = gw.resolve("no-such-model-42")
    assert slug is None
    assert "unknown model" in note


def test_resolve_bare_model_name_matches_a_catalog_slug(fleet_catalog):
    _write_catalog(fleet_catalog, _FALLBACK_SLUGS)
    slug, note = gw.resolve("step-5-preview")
    assert slug and slug.endswith("/step-5-preview")
    assert "bare model name" in note


def test_stepfun_routes_direct_with_the_plan_key(monkeypatch):
    monkeypatch.setattr(gw, "fleet_env",
                        lambda path=None: {"STEPFUN_PLAN_API_KEY": "sk-test"})
    route = gw.route_for("stepfun/step-5-preview")
    assert route["transport"] == "direct"
    assert route["url"] == "https://api.stepfun.com/step_plan/v1/chat/completions"
    assert route["key"] == "sk-test"
    assert route["model"] == "step-5-preview"


def test_stepfun_without_a_key_has_no_route(monkeypatch):
    monkeypatch.setattr(gw, "fleet_env", lambda path=None: {})
    assert gw.route_for("stepfun/step-5-preview") is None


def test_a_bridge_provider_uses_its_own_port(monkeypatch):
    monkeypatch.setattr(gw, "bridge_ports", lambda: {"workbuddy": 8787})
    monkeypatch.setattr(gw, "service_key", lambda provider: "bridge-key")
    route = gw.route_for("workbuddy/glm-5.2")
    assert route["transport"] == "bridge"
    assert route["url"] == "http://127.0.0.1:8787/v1/chat/completions"
    assert route["key"] == "bridge-key"
    assert route["model"] == "workbuddy/glm-5.2"


def test_a_provider_without_a_bridge_travels_the_ocx_gateway():
    route = gw.route_for("gpt-5.6-luna")
    assert route["transport"] == "gateway"
    assert route["url"] == gw.OCX_URL
    assert route["key"] == gw.OCX_KEY


def test_every_listed_model_has_a_route_and_is_ordered():
    payload = gw.models_payload()
    assert payload["data"], "the picker would open empty"
    rows = gw.catalog_rows()
    priorities = [r.get("priority", 10 ** 6) for r in rows]
    assert priorities == sorted(priorities), "strongest model is not first"
    listed = {m["id"] for m in payload["data"]}
    for slug in listed:
        # resolve(), not route_for(): a minted route has no provider prefix,
        # so route_for() would answer with the ocx fallback and call that a
        # route. What matters is that the id resolves to a real slug.
        target, note = gw.resolve(slug)
        assert target, slug + " is listed but resolves to nothing"


def test_every_minted_route_survives_the_desktop_picker_filter():
    """The whole point of minting: the app's id check must keep the row.

    anthropic_shaped() mirrors the app's Va() from app.asar 1.46388.4. If a
    digest ever lands on a vendor substring the row is published and dropped,
    which is the bug this whole mechanism exists to avoid.
    """
    for alias, slug in gw.alias_index().items():
        assert gw.anthropic_shaped(alias), alias + " would be dropped by the app"
        assert alias not in {r.get("slug") for r in gw.catalog_rows()}, \
            alias + " collides with a catalog slug"


def test_a_minted_route_resolves_back_to_the_row_it_was_minted_for():
    index = gw.alias_index()
    assert index, "nothing was minted"
    for alias, slug in index.items():
        target, note = gw.resolve(alias)
        assert target == slug, alias + " resolves to " + str(target)
        assert slug in note


def test_a_minted_route_is_stable_for_the_same_slug():
    """A client saves the id it picked; it must not move next restart."""
    entries = gw.catalog_entries()
    for slug, _display, _route, tier in entries:
        if gw.anthropic_shaped(slug):
            continue
        assert gw.route_alias(slug, tier) == gw.route_alias(slug, tier)
        # and it is derived from the slug, not from a counter
        assert gw.route_alias(slug, tier) != gw.route_alias(slug + "x", tier)


def test_every_catalog_row_is_visible_to_the_desktop_picker():
    """One way or another, each model must have an id the app will keep.

    This is the regression the whole alias mechanism guards: before it, 146
    catalog rows produced 5 picker rows.
    """
    listed = {m["id"] for m in gw.models_payload()["data"]}
    aliased = set(gw.alias_index().values())
    for slug in {r.get("slug") for r in gw.catalog_rows() if r.get("slug")}:
        assert slug in listed or slug in aliased, (
            slug + " has no id the desktop picker keeps")


def test_the_alias_digest_alphabet_cannot_spell_a_blacklisted_token():
    """'abab' is the one vendor token made only of hex letters.

    The minting alphabet leaves out a and b, so a digest can never contain it
    and be rejected for a reason that has nothing to do with the model.
    """
    assert "abab" in gw.VENDOR_ID_BLACKLIST
    assert not (set("ab") & set(gw._ALIAS_ALPHABET))
    for alias in gw.alias_index():
        digest = alias.rsplit("-", 1)[-1]
        assert set(digest) <= set(gw._ALIAS_ALPHABET)


def test_the_listing_offers_the_claude_alias_rows():
    """They are the only ids the desktop picker keeps.

    This test used to assert the opposite. Measured 2026-10-03 against app.asar
    1.46388.4: the gateway listed 134 rows, discovery logged "134 found", and
    the picker showed 5. The app filters the picker on the model id looking
    like an Anthropic model -- a vendor-name blacklist that drops glm, kimi,
    stepfun, qwen, deepseek, minimax and the rest -- and the family tier only
    buys a row through discovery, not through that filter. CLAUDE_ALIASES are
    Anthropic-shaped by construction and resolve() already honours them, so
    listing them is whatmakes the desktop picker show the fleet.
    """
    payload = gw.models_payload()
    ids = [m["id"] for m in payload["data"]]
    for alias in gw.CLAUDE_ALIASES:
        assert alias in ids, alias + " is a route the app will keep"
    # every alias row must be usable, not decoration
    for alias in gw.CLAUDE_ALIASES:
        slug, _note = gw.resolve(alias)
        assert slug, alias + " resolves to nothing"


def test_alias_rows_come_first_and_own_their_tier_default():
    """Where an alias exists for a tier, it must win that tier's default.

    Otherwise a bare tier name the app resolves still lands on an antigravity
    row instead of the fleet's route. mythos has no alias in the table, so its
    default stays a catalog row -- that is the one tier this does not cover.
    """
    payload = gw.models_payload()
    data = payload["data"]
    alias_tiers = {gw.alias_tier(a) for a in gw.CLAUDE_ALIASES}
    by_tier = {}
    for entry in data:
        by_tier.setdefault(entry["anthropic_family_tier"], []).append(entry)
    for tier, entries in by_tier.items():
        winners = [e for e in entries if e.get("is_family_default")]
        assert len(winners) == 1, "%s has %d defaults" % (tier, len(winners))
        if tier in alias_tiers:
            assert winners[0]["id"] in gw.CLAUDE_ALIASES, (
                "%s default is %s, not an alias" % (tier, winners[0]["id"]))
    # and the alias rows really do come first
    first_alias = next(i for i, e in enumerate(data)
                       if e["id"] in gw.CLAUDE_ALIASES)
    first_catalog = next(i for i, e in enumerate(data)
                         if e["id"] not in gw.CLAUDE_ALIASES)
    assert first_alias < first_catalog


def test_an_alias_row_names_the_model_it_routes_to():
    """A picker row that hides its target is a row nobody can audit."""
    data = {m["id"]: m for m in gw.models_payload()["data"]}
    for alias, target in gw.CLAUDE_ALIASES.items():
        assert target in data[alias]["display_name"]


def _fake_pool(monkeypatch):
    """Three rows over two providers, so a tier can be asserted exactly."""
    rows = [{"slug": "stepfun/step-5-preview", "priority": 1,
             "display_name": "step-5-preview"},
            {"slug": "workbuddy/glm-5.2", "priority": 2,
             "display_name": "glm-5.2"},
            {"slug": "workbuddy/glm-5.1", "priority": 3,
             "display_name": "glm-5.1"}]
    routes = {"stepfun/step-5-preview": {"provider": "stepfun",
                                       "transport": "direct"},
              "workbuddy/glm-5.2": {"provider": "workbuddy",
                                   "transport": "bridge"},
              "workbuddy/glm-5.1": {"provider": "workbuddy",
                                   "transport": "bridge"}}
    monkeypatch.setattr(gw, "catalog_rows", lambda: rows)
    monkeypatch.setattr(gw, "route_for", lambda slug: routes.get(slug))
    return rows


def test_family_tier_brackets_a_row_inside_its_own_provider():
    assert gw.family_tier(0, 1) == "opus"
    assert gw.family_tier(0, 22) == "opus"
    assert gw.family_tier(21, 22) == "mythos"
    assert gw.family_tier(11, 22) == "haiku"
    for rank in range(0, 40):
        assert gw.family_tier(rank, 40) in gw.FAMILY_TIERS


def test_every_listed_row_carries_a_tier_the_desktop_app_accepts(monkeypatch):
    """The desktop app drops a row whose id is not Anthropic-shaped unless the
    row carries anthropic_family_tier from the app's own tier list, so a row
    without one is a row the picker never shows.
    """
    _fake_pool(monkeypatch)
    data = gw.models_payload()["data"]
    assert data, "the picker would open empty"
    for entry in data:
        assert entry["anthropic_family_tier"] in gw.FAMILY_TIERS, entry["id"]


def test_each_tier_names_one_default_row(monkeypatch):
    """is_family_default is how a bare alias such as "opus" resolves, so a
    tier with no default is a tier the app cannot route.
    """
    _fake_pool(monkeypatch)
    data = gw.models_payload()["data"]
    winners = {}
    for entry in data:
        if entry.get("is_family_default"):
            winners.setdefault(entry["anthropic_family_tier"], []).append(entry["id"])
    for tier, ids in winners.items():
        assert len(ids) == 1, "%s has %d defaults: %s" % (tier, len(ids), ids)
    # the first row listed must own its tier, because the app resolves a bare
    # alias to the first entry it sees for that tier
    first = data[0]
    assert winners[first["anthropic_family_tier"]] == [first["id"]]



def test_a_hash_slug_is_listed_under_a_readable_name(monkeypatch):
    """xhx reports its models under build hashes (raccoon-19b265), and a
    picker that shows the hash is a picker nobody can choose from. The id
    stays the routable slug, so nothing that already references it moves.
    """
    rows = [{"slug": "xhx/raccoon-19b265", "priority": 1,
             "display_name": "raccoon-19b265"}]
    routes = {"xhx/raccoon-19b265": {"provider": "xhx",
                                       "transport": "bridge"}}
    monkeypatch.setattr(gw, "catalog_rows", lambda: rows)
    monkeypatch.setattr(gw, "route_for", lambda slug: routes.get(slug))
    entry = gw.models_payload()["data"][0]
    assert entry["id"] == "xhx/raccoon-19b265", "the routable id must not change"
    assert entry["display_name"] == "xhx/小浣熊Work-A"


def test_the_bridge_reported_name_beats_the_id_in_the_listing(monkeypatch):
    """xhx reports Raccoon-Work-260817-A next to raccoon-19b265, so the
    listing shows the name; a bridge that reports only an id still lists,
    with the id standing in for the missing name.
    """
    monkeypatch.setattr(gw, "bridge_ports", lambda: {"xhx": 8793})
    monkeypatch.setattr(gw, "service_key", lambda provider: "k")
    monkeypatch.setattr(gw, "DIRECT_UPSTREAMS", {})
    monkeypatch.setattr(gw, "_tcp_alive", lambda host, port, timeout=1.0: True)
    body = json.dumps({"data": [
        {"id": "raccoon-19b265", "name": "Raccoon-Work-260817-A"},
        {"id": "sn-glm-5-3-flash", "name": "GLM-5-3-Flash"},
        {"id": "sn-kimi-k3"}]}).encode()

    class Resp:
        def __init__(self, payload):
            self.body = payload

        def read(self, *args):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(gw.OPENER, "open",
                        lambda req, timeout=None: Resp(body))
    saved = dict(gw._BRIDGE_CATALOG)
    gw._BRIDGE_CATALOG["stamp"] = 0.0
    gw._BRIDGE_CATALOG["rows"] = []
    try:
        rows = gw.bridge_catalog_rows()
    finally:
        gw._BRIDGE_CATALOG.clear()
        gw._BRIDGE_CATALOG.update(saved)
    names = {r["slug"]: r["display_name"] for r in rows}
    assert names["xhx/raccoon-19b265"] == "Raccoon-Work-260817-A"
    assert names["xhx/sn-glm-5-3-flash"] == "GLM-5-3-Flash"
    assert names["xhx/sn-kimi-k3"] == "sn-kimi-k3", "no name reported: the id stands"


def test_every_raccoon_hash_in_the_pool_has_a_readable_name():
    """A new upstream build hash must not reach the picker unnamed.

    xhx lists whatever build the SenseTime app is on today, so the map is
    the only thing between a fresh raccoon-<hash> and an unreadable row.
    """
    unnamed = [row["slug"] for row in gw.catalog_rows()
               if (row.get("slug") or "").startswith("xhx/raccoon-")
               and row["slug"] not in gw.DISPLAY_NAMES]
    assert not unnamed, "unnamed raccoon build(s): %s" % unnamed

def test_key_table_does_not_drift_from_the_guard(monkeypatch):
    import default_model_guard as guard
    assert gw.KEY_ENV == guard.KEY_ENV
    for provider, port in guard.BRIDGE_PORTS.items():
        assert gw.bridge_ports().get(provider) == port, provider


# ------------------------------------------------------------------------ http
def test_get_models_and_health(gateway):
    url, _opener = gateway
    status, payload = _get(url, "/v1/models")
    assert status == 200
    # alias rows lead, because the desktop picker keeps only Anthropic-shaped
    # ids; the catalog still follows behind them in its own order
    assert payload["data"][0]["id"] in gw.CLAUDE_ALIASES
    listed = [m["id"] for m in payload["data"]]
    for row in gw.catalog_rows():
        assert row["slug"] in listed
    status, health = _get(url, "/health")
    assert status == 200
    assert health["models_listed"] >= 1
    assert "estimated" in health["count_tokens"]


def test_post_messages_returns_anthropic_shape(gateway):
    url, opener = gateway
    status, payload, headers = _post(url, "/v1/messages", {
        "model": "workbuddy/glm-5.2", "max_tokens": 32,
        "messages": [{"role": "user", "content": "hi"}]})
    assert status == 200, payload
    assert payload["type"] == "message"
    assert payload["content"][0]["type"] == "text"
    assert payload["stop_reason"] == "end_turn"
    assert headers.get("x-fleetkit-resolved-model") == "workbuddy/glm-5.2"
    sent = opener.bodies[-1]
    assert sent["model"] == "workbuddy/glm-5.2"
    assert sent["messages"][-1]["role"] == "user"


def test_post_messages_refuses_an_unknown_model(gateway):
    url, _opener = gateway
    status, payload, _headers = _post(url, "/v1/messages", {
        "model": "totally-made-up", "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}]})
    assert status == 404
    assert payload["error"]["type"] == "invalid_request_error"


def test_a_200_carrying_a_refusal_is_reported_as_an_error(gateway, monkeypatch):
    url, opener = gateway
    opener.response = FakeResponse(body=json.dumps(_openai_reply(
        text="Request blocked. Please send it again")).encode("utf-8"))
    status, payload, _headers = _post(url, "/v1/messages", {
        "model": "workbuddy/glm-5.2", "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}]})
    assert status == 502
    assert "refused" in payload["error"]["message"]


def test_an_upstream_401_becomes_an_anthropic_auth_error(gateway, monkeypatch):
    url, opener = gateway
    opener.error = urllib.error.HTTPError(
        "http://upstream.invalid", 401, "Unauthorized", None,
        None)
    status, payload, _headers = _post(url, "/v1/messages", {
        "model": "workbuddy/glm-5.2", "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}]})
    assert status == 401
    assert payload["error"]["type"] == "authentication_error"


def test_count_tokens_is_an_estimate_and_says_so(gateway):
    url, _opener = gateway
    status, payload, _headers = _post(url, "/v1/messages/count_tokens", {
        "model": "workbuddy/glm-5.2",
        "messages": [{"role": "user", "content": "a" * 400}]})
    assert status == 200
    assert payload["estimated"] is True
    assert 80 <= payload["input_tokens"] <= 120


def test_stream_events_arrive_in_the_anthropic_order(gateway, monkeypatch):
    url, opener = gateway
    opener.response = FakeResponse(
        content_type="text/event-stream",
        lines=_sse_lines([
            {"choices": [{"index": 0, "delta": {"content": "Hel"}}]},
            {"choices": [{"index": 0, "delta": {"content": "lo"}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{\"path\":"}}]}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": "\"a.py\"}"}}]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 9, "completion_tokens": 4}},
        ]) + [b"data: [DONE]\n", b"\n"])
    request = urllib.request.Request(
        url + "/v1/messages",
        data=json.dumps({"model": "workbuddy/glm-5.2", "max_tokens": 32,
                         "stream": True,
                         "messages": [{"role": "user", "content": "hi"}]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.build_opener(
            urllib.request.ProxyHandler({})).open(request, timeout=15) as resp:
        assert "text/event-stream" in resp.headers["Content-Type"]
        raw = resp.read().decode("utf-8")
    events = [line[len("data: "):] for line in raw.splitlines()
              if line.startswith("data: ")]
    kinds = [json.loads(e)["type"] for e in events]
    assert kinds[0] == "message_start"
    assert kinds[-1] == "message_stop"
    assert kinds.count("content_block_start") == 2
    assert kinds.count("content_block_stop") == 2
    assert kinds[-2] == "message_delta"
    assert kinds.index("content_block_stop") < kinds.index("message_delta")
    texts = [json.loads(e)["delta"]["text"] for e in events
             if json.loads(e)["type"] == "content_block_delta"
             and json.loads(e)["delta"]["type"] == "text_delta"]
    assert "".join(texts) == "Hello"
    partial = [json.loads(e)["delta"]["partial_json"] for e in events
               if json.loads(e)["type"] == "content_block_delta"
               and json.loads(e)["delta"]["type"] == "input_json_delta"]
    assert "".join(partial) == '{"path":"a.py"}'
    delta = json.loads(events[-2])
    assert delta["delta"]["stop_reason"] == "tool_use"


def test_a_json_reply_to_a_stream_request_becomes_one_shot_events(gateway,
                                                                 monkeypatch):
    url, opener = gateway
    opener.response = FakeResponse(body=json.dumps(
        _openai_reply(text="only json")).encode("utf-8"))
    request = urllib.request.Request(
        url + "/v1/messages",
        data=json.dumps({"model": "workbuddy/glm-5.2", "max_tokens": 32,
                         "stream": True,
                         "messages": [{"role": "user", "content": "hi"}]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.build_opener(
            urllib.request.ProxyHandler({})).open(request, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
    kinds = [json.loads(line[len("data: "):])["type"]
             for line in raw.splitlines() if line.startswith("data: ")]
    assert kinds[0] == "message_start"
    assert kinds[-1] == "message_stop"
    texts = [json.loads(line[len("data: "):])["delta"]["text"]
             for line in raw.splitlines() if line.startswith("data: ")
             and json.loads(line[len("data: "):])["type"] == "content_block_delta"]
    assert texts == ["only json"]


def test_a_token_protected_gateway_rejects_a_wrong_key(gateway, monkeypatch):
    monkeypatch.setenv("FLEET_ANTHROPIC_TOKEN", "s3cret")
    url, _opener = gateway
    status, payload, _headers = _post(url, "/v1/messages", {
        "model": "workbuddy/glm-5.2", "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}]},
        headers={"x-api-key": "wrong"})
    assert status == 401
    assert payload["error"]["type"] == "authentication_error"
    status, payload, _headers = _post(url, "/v1/messages", {
        "model": "workbuddy/glm-5.2", "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}]},
        headers={"x-api-key": "s3cret"})
    assert status == 200, payload


def test_the_wrapper_script_installs_the_service(gateway):
    path = os.path.join(KIT, "anthropic_gateway.sh")
    assert os.path.exists(path), "no control script next to the gateway"
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    for needle in ("platform.sh", "fleet_service_install", "8801",
                   "anthropic_gateway.py"):
        assert needle in src, needle + " missing from the control script"


def test_resolve_accepts_a_slug_a_foreign_catalog_write_hid(monkeypatch, tmp_path):
    # cc-switch rewrites the shared catalog with its own provider's models on
    # every start: one row left of 117, measured 2026-10-01. A slug whose
    # provider still answers must not 404 because of somebody else's write.
    monkeypatch.setattr(gw, "route_alive", lambda route, timeout=1.0: True)
    monkeypatch.setattr(gw, "bridge_catalog_rows", lambda: [])
    clobbered = tmp_path / "clobbered.json"
    clobbered.write_text(json.dumps({"models": [{"slug": "step-3.7-flash",
                                              "priority": 0}]}))
    monkeypatch.setattr(gw, "CATALOG_PATH", str(clobbered))
    slug, note = gw.resolve("workbuddy-gpt/hy4-preview")
    assert slug == "workbuddy-gpt/hy4-preview"
    assert "route derived" in note


def test_resolve_still_refuses_a_name_no_provider_claims(monkeypatch, tmp_path):
    monkeypatch.setattr(gw, "route_alive", lambda route, timeout=1.0: True)
    monkeypatch.setattr(gw, "bridge_catalog_rows", lambda: [])
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"models": []}))
    monkeypatch.setattr(gw, "CATALOG_PATH", str(empty))
    slug, note = gw.resolve("no-such-model-42")
    assert slug is None
    assert "unknown model" in note


def test_catalog_rows_falls_back_to_the_bridges_on_a_foreign_write(
        monkeypatch, tmp_path):
    clobbered = tmp_path / "clobbered.json"
    clobbered.write_text(json.dumps({"models": [{"slug": "step-3.7-flash",
                                              "priority": 0}]}))
    monkeypatch.setattr(gw, "CATALOG_PATH", str(clobbered))
    monkeypatch.setattr(gw, "bridge_ports", lambda: {"workbuddy-gpt": 8788})
    monkeypatch.setattr(gw, "service_key", lambda provider: "k")
    monkeypatch.setattr(gw, "DIRECT_UPSTREAMS", {})
    monkeypatch.setattr(gw, "_tcp_alive",
                        lambda host, port, timeout=1.0: True)
    seen = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self, *args):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_open(req, timeout=None):
        seen.append(req.full_url)
        return Resp(json.dumps({"data": [{"id": "hy4-preview"}]}).encode())

    monkeypatch.setattr(gw.OPENER, "open", fake_open)
    saved = dict(gw._BRIDGE_CATALOG)
    gw._BRIDGE_CATALOG["stamp"] = 0.0
    gw._BRIDGE_CATALOG["rows"] = []
    try:
        rows = gw.catalog_rows()
    finally:
        gw._BRIDGE_CATALOG.clear()
        gw._BRIDGE_CATALOG.update(saved)
    assert [r["slug"] for r in rows] == ["workbuddy-gpt/hy4-preview"]
    assert seen == ["http://127.0.0.1:8788/v1/models"]
