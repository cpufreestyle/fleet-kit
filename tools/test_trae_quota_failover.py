"""A quota-dead Trae account must not take the whole fleet down with it.

Measured 2026-10-02 on this Mac:

  * Three Trae editions are installed (Trae CN / TRAE SOLO CN / international
    Trae). The CN account (用户6781982309) answers every one of its 22 models
    with "Your requests have exceeded the quota.", while the international
    account (Q Micheal) has its own quota but coresg-normal.trae.ai is behind
    the TLS wall.
  * With the pre-fix code the bridge pinned its cached CN credential: one
    exhausted account meant 22 dead models even though a second credential
    sat in the pool, and a dead gateway raised instead of failing over.

These tests pin the pool ordering, the cooldown TTL, the quota classifier and
the stream-head peek that lets a streaming request switch accounts before the
first byte reaches the client.
"""
import asyncio
import importlib.util
import os
import sys

import pytest

BRIDGES = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "bridges"))
sys.path.insert(0, BRIDGES)
sys.path.insert(0, os.path.join(BRIDGES, "trae"))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BRIDGES, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


trae = _load("trae_bridge", os.path.join("trae", "trae_bridge.py"))


def _cred(account, region, edition):
    # 真实 storage.json 里 userId 是数字串，account.username 才可能是中文昵称
    # 真实 token 是 ASCII；测试若把中文账号名拼进 token，httpx 编码 HTTP 头时会
    # 抛 UnicodeEncodeError，那是测试自己的问题，不是桥的行为。
    slug = "".join(ch for ch in account if ch.isascii() and ch.isalnum())
    return {"account": account, "region": region, "edition": edition,
            "access_token": "t-" + slug,
            "user_id": "".join(ch for ch in account if ch.isdigit()),
            "expires_at_ms": 4_000_000_000_000.0}


class _FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now


def _fake_candidates(monkeypatch, creds):
    by_edition = {c["edition"]: c for c in creds}
    monkeypatch.setattr(trae, "storage_candidates",
                        lambda: [{"edition": c["edition"], "source": "desktop"} for c in creds])
    monkeypatch.setattr(trae, "read_desktop_auth", lambda cand: by_edition[cand["edition"]])


def _done(value):
    """An awaitable stand-in for a monkeypatched async bridge function."""
    async def _coro():
        return value
    return _coro()


async def _fake_directory(cred):
    region = cred.get("region") or "cn"
    if region == "cn":
        return {"seed-code-pro-0430": {"id": "seed-code-pro-0430",
                                       "function": "solo_work_remote",
                                       "name": "Seed-Code-Pro",
                                       "context_window": 200000}}
    return {"claude-sonnet-4-5": {"id": "claude-sonnet-4-5",
                                  "function": "solo_work_lite",
                                  "name": "Claude Sonnet 4.5",
                                  "context_window": 200000}}


@pytest.fixture(autouse=True)
def _clean_state():
    # The cooldown map and the catalog cache are module state; a leak from one
    # test would silently reorder the pool for the next one.
    trae._quota_dead.clear()
    trae._catalog["models"], trae._catalog["ts"] = {}, 0.0
    yield
    trae._quota_dead.clear()
    trae._catalog["models"], trae._catalog["ts"] = {}, 0.0


def test_pool_orders_alive_first_and_dedupes(monkeypatch):
    cn = _cred("用户6781982309", "cn", "Trae CN")
    intl = _cred("Q Micheal", "ai", "Trae")
    monkeypatch.setattr(trae, "load_store", lambda: {"credential": dict(cn)})
    _fake_candidates(monkeypatch, [cn, dict(cn), intl])  # same account twice on purpose
    pool = asyncio.run(trae.credential_pool())
    assert [trae.cred_ident(c) for c in pool] == ["cn:用户6781982309", "ai:Q Micheal"]


def test_quota_dead_credential_sinks_to_the_bottom(monkeypatch):
    cn = _cred("用户6781982309", "cn", "Trae CN")
    intl = _cred("Q Micheal", "ai", "Trae")
    monkeypatch.setattr(trae, "load_store", lambda: {"credential": dict(cn)})
    _fake_candidates(monkeypatch, [cn, intl])
    trae.mark_quota_dead(cn)
    pool = asyncio.run(trae.credential_pool())
    assert trae.cred_ident(pool[0]) == "ai:Q Micheal"
    # The cooled account stays in the pool as the last resort.
    assert trae.cred_ident(pool[-1]) == "cn:用户6781982309"


def test_cooldown_expires(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(trae.time, "time", clock.time)
    monkeypatch.setattr(trae, "QUOTA_COOLDOWN", 600.0)
    cred = _cred("cn-acct", "cn", "Trae CN")
    trae.mark_quota_dead(cred)
    assert trae.is_quota_dead(cred)
    clock.now += 599
    assert trae.is_quota_dead(cred)
    clock.now += 2
    assert not trae.is_quota_dead(cred)


def test_quota_classifier():
    assert trae.is_quota_error("Your requests have exceeded the quota.")
    assert trae.is_quota_error("HTTP 429: rate limit reached")
    assert not trae.is_quota_error("invalid json body")
    assert not trae.is_quota_error("trae upstream 500: internal")
    assert not trae.is_quota_error("")


def _sse(*events):
    out = ""
    for name, data in events:
        out += f"event: {name}\ndata: {data}\n\n"
    return out.encode()


async def _ait(chunks):
    for c in chunks:
        yield c


def test_peek_detects_quota_on_the_stream_head():
    body = _sse(("request_wait_in_queue", "{}"),
                ("error", '{"message": "Your requests have exceeded the quota."}'))
    prefix, msg = asyncio.run(trae._peek_quota(_ait([body])))
    assert prefix == b""
    assert "exceeded the quota" in msg


def test_peek_replays_a_healthy_head_intact():
    head = _sse(("request_wait_in_queue", "{}"), ("output", '{"response": "po"}'))
    tail = _sse(("output", '{"response": "ng"}'), ("done", '{"finish_reason": "stop"}'))
    prefix, msg = asyncio.run(trae._peek_quota(_ait([head, tail])))
    assert msg is None
    # Everything read before the decision must be replayed byte for byte.
    assert prefix == head
    assert b"ng" in tail


def test_peek_passes_a_non_quota_error_through():
    body = _sse(("error", '{"message": "model not supported"}'))
    prefix, msg = asyncio.run(trae._peek_quota(_ait([body])))
    assert msg is None
    assert prefix == body


def test_peek_survives_a_truncated_head():
    # A head cut mid-JSON must not decide; more bytes arrive next.
    prefix, msg = asyncio.run(trae._peek_quota(_ait([b"event: error\ndata: {\"mess"])))
    assert msg is None
    assert prefix.startswith(b"event: error")


def test_replay_response_streams_prefix_then_rest():
    class _Resp:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    resp = _Resp()
    replay = trae._ReplayResponse(resp, b"HEAD", _ait([b"A", b"B"]))

    async def drain():
        return b"".join([c async for c in replay.aiter_bytes()])

    assert asyncio.run(drain()) == b"HEADAB"
    asyncio.run(replay.aclose())
    assert resp.closed


def test_region_first_puts_the_matching_edition_up_front():
    cn = _cred("用户6781982309", "cn", "Trae CN")
    intl = _cred("Q Micheal", "ai", "Trae")
    pool = [cn, intl]
    assert trae.region_first(pool, "ai") == [intl, cn]
    assert trae.region_first(pool, "cn") == [cn, intl]
    # No tag (or an unknown one) must keep the pool order untouched.
    assert trae.region_first(pool, "") == pool
    assert trae.region_first(pool, None) == pool
    assert trae.region_first(pool, "sg") == pool


def test_catalog_merges_every_edition_and_tags_region(monkeypatch):
    cn = _cred("用户6781982309", "cn", "Trae CN")
    intl = _cred("Q Micheal", "ai", "Trae")
    monkeypatch.setattr(trae, "credential_pool",
                        lambda allow_refresh=True: _done([cn, intl]))
    monkeypatch.setattr(trae, "fetch_directory", _fake_directory)
    trae._catalog["models"], trae._catalog["ts"] = {}, 0.0
    catalog = asyncio.run(trae.get_catalog(force=True))
    assert set(catalog) == {"seed-code-pro-0430", "claude-sonnet-4-5"}
    assert catalog["seed-code-pro-0430"]["region"] == "cn"
    assert catalog["claude-sonnet-4-5"]["region"] == "ai"
    # An unreachable edition must not wipe the models another one answered.
    trae._catalog["models"], trae._catalog["ts"] = {}, 0.0
    monkeypatch.setattr(trae, "credential_pool",
                        lambda allow_refresh=True: _done([intl]))
    catalog = asyncio.run(trae.get_catalog(force=True))
    assert set(catalog) == {"claude-sonnet-4-5"}



def test_a_chinese_user_id_cannot_break_the_request_headers():
    """中文昵称混进 HTTP 头只会让 httpx 抛 UnicodeEncodeError，请求根本发不出。"""
    cred = _cred("用户6781982309", "cn", "Trae CN")
    cred["user_id"] = cred["account"]          # storage 被写坏时的极端形态
    headers = trae.trae_headers(cred, {})
    assert headers["x-uid"] == "6781982309"
    for key, value in headers.items():
        key.encode("ascii")
        value.encode("ascii")

def test_chat_502_names_every_credential_reason(monkeypatch):
    import httpx
    from fastapi.testclient import TestClient
    import _common

    cn = _cred("用户6781982309", "cn", "Trae CN")
    intl = _cred("Q Micheal", "ai", "Trae")
    trae.check_bridge_auth = _common.make_auth_checker("")
    monkeypatch.setattr(trae, "credential_pool",
                        lambda allow_refresh=True: _done([cn, intl]))
    monkeypatch.setattr(trae, "get_catalog",
                        lambda force=False: _done({
                            "seed-code-pro-0430": {"id": "seed-code-pro-0430",
                                                   "function": "solo_work_remote",
                                                   "name": "Seed-Code-Pro",
                                                   "region": "cn"}}))

    def handler(request):
        if "trae-api-cn" in str(request.url):
            return httpx.Response(
                200, content=b'event: error\n'
                             b'data: {"message": "Your requests have exceeded the quota."}\n\n')
        raise httpx.ConnectError("ProxyError 503 Service Unavailable",
                                 request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(trae, "client", lambda: client)
    r = TestClient(trae.app).post(
        "/v1/chat/completions",
        json={"model": "trae/seed-code-pro-0430",
              "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502
    detail = r.json()["error"]["message"]
    assert "配额耗尽" in detail and "用户6781982309" in detail
    assert "网络不可达" in detail and "Q Micheal" in detail
