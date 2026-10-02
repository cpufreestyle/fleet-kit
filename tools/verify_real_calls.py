#!/usr/bin/env python3
"""FleetKit real-call verifier.

Proves whether each reverse-proxy bridge performs a *genuine* upstream model
call -- not just a listening port or a canned / mirrored HTTP 200.

Discriminator (hard to fake): one prompt asks the model to (a) echo the first
4 chars of a random nonce and (b) compute (random N + random ADD). A canned /
echo bridge cannot produce the correct arithmetic because the operands are
random per call. We also read provider usage (reasoning_tokens / credit) as an
extra authenticity signal -- a mock produces none of that.

Verdicts:
  REAL           HTTP 200 AND arithmetic correct -> genuine inference
  ECHO/MIRROR    echoes nonce but fails arithmetic -> passthrough, not real
  CANNED/MOCK    200 fast + short + fails both -> not a real upstream
  UNCLEAR        200 but incoherent/empty -> manual look
  GATE           upstream onboarding / login wall (400 welcome/URL)
  AUTH_EXPIRED   401, or a 403 that names no plan -> session or key invalid
  CHANNEL_BLOCKED 200 carrying a channel/key refusal -> not a reply
  PLAN_BLOCKED   403 naming plan / quota / permission -> login is fine and
                 the bridge works, that one model is simply off-plan
  UPSTREAM_DOWN  502/503/504 (tunnel blocked, shutdown, param rejected)
  BRIDGE_DOWN    connection refused / timeout
  STREAM_BROKEN  non-stream chat is REAL but stream=true fails -> the bridge
                  works for curl and is unusable for Codex, which always streams

Reasoning models need a bigger max_tokens: the probe escalates
256 -> 2048 -> 4096 when the content is empty because thinking ate the budget.

Keys are read from launchd plists (md5 only printed) or FLEET_*_KEY env vars.
Use --json for machine-readable output (UI / sync).

Sampling: one bridge can list a dozen models, only some of which work, so the
probe walks candidates until one genuinely answers instead of grading the bridge
on a single call. Measured 2026-09-29 with cands = cands[:4] and the verdict taken
from the last failure: cline serves the same 14 models every call, the 20:26 run
sampled 4 of them through the macOS system proxy, got InvalidProxyMessage in 0.01s
on all of them and filed the whole bridge UPSTREAM_DOWN -- while --only cline on the
same port answered REAL, code 200, 8.14s at 21:02. The candidate the BRIDGES table
names is now always tried first, the sample is wider, every attempt is recorded in
`attempted`, and the bridge itself no longer tunnels a loopback hub through that
proxy (bridges/cline/cline_bridge.py hub_proxy).

Plan-blocked models: a 403 whose body names the plan ("model not allowed on your
current plan") is not an expired session, so it no longer returns AUTH_EXPIRED
and no longer ends the walk early -- the next candidate gets its turn. Measured
2026-09-29 21:30 with --only lingxi on port 8792: deepseek-v4-flash -> 403 in
0.09s "model not allowed on your current plan", deepseek-flash -> 200 0.97s with
the arithmetic right, so the account is alive and only the declared model is
off-plan. Nothing was wrong with the bridge.
"""
import urllib.request, urllib.error, json, hashlib, time, glob, re, sys, os, random, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from upstream_errors import VERDICT_NOTE, is_error_body, matched_marker


NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PLACEHOLDERS = {"", "model", "auto", "default", "none", "test"}
MAX_CANDIDATES = 6
PLAN_BLOCK_MARKERS = ("model not allowed", "not allowed on your", "not in your plan",
                      "current plan", "quota", "insufficient", "permission",
                      "not permitted", "not entitled", "套餐", "配额", "无权限",
                      "未订阅", "无权访问",
                      # Kimi Code answers in English, and its phrasing names the
                      # subscription rather than the plan: Your current
                      # subscription does not have access to Kimi Code ...
                      # Upgrade your plan. No marker above matches that, so a
                      # lapsed plan read as an expired session -- the exact
                      # misreading the lingxi case above was fixed for.
                      "does not have access", "upgrade your plan",
                      "no active plan", "access_terminated")
# A bridge with no upstream key answers 503 with its own *_key_missing
# envelope before it ever dials the vendor. That is a local config gap, not an
# outage: reported as UPSTREAM_DOWN it sends the operator to the vendor status
# page for a key that was never set.
NO_KEY_MARKERS = ("key_missing",)
# Google's VALI gate (measured 2026-10-02 on gemini and antigravity, ports
# 8794/8797): the OAuth login still refreshes fine, but cloudcode-pa answers
# 403 "Verify your account to continue." with a validation_url only the
# account owner can clear in a browser. Both bridges wrap it in a 502 whose
# message now carries that link. Graded UPSTREAM_DOWN it sends the operator
# to a vendor status page for a gate that is not an outage; graded
# PLAN_BLOCKED it sends them to a pricing page. Neither is actionable.
VALI_MARKERS = ("validation_required", "verify your account",
                "account verification")
BUDGETS = [256, 2048, 4096]
PROBE_HTTP_TIMEOUT = 20
PROBE_CHAT_TIMEOUT = 70
# A few bridges carry a budget of their own that is larger than the probe:
# zcode drives the official CLI (ZCODE_CLI_TIMEOUT, default 180s) and mints an
# Aliyun captcha first (up to 75s). A 70s probe timeout would therefore report
# BRIDGE_DOWN (a timeout) for a bridge that is merely slow -- a different verdict
# from the truth, and one that hides the real reason. Give those bridges a probe
# that outlives their own budget.
PROBE_CHAT_TIMEOUT_OVERRIDE = {"zcode": 200}
STREAM_PROBE_TIMEOUT = 25

BRIDGES = [
    ("workbuddy",     "workbuddy2codex",      0, "hy4-preview",               "CODEBUDDY2OPENAI_KEY"),
    ("workbuddy-gpt", "workbuddy2codex-gpt",  1, "gpt-6-astra",               "CODEBUDDY2OPENAI_KEY"),
    ("qoder",         "qoder2codex",          2, "DeepSeek-V4-Pro",           "QODER2CODEX_KEY"),
    ("codely",        "codely2codex",         3, "codely-core",               "CODELY2CODEX_KEY"),
    ("trae",          "trae2codex",           4, "trae/Doubao-Seed-Evolving", "TRAE2CODEX_KEY"),
    ("lingxi",        "lingxi2codex",         5, "lingxi/deepseek-v4-flash",  "LINGXI2CODEX_KEY"),
    ("xhx",           "xhx2codex",            6, "xhx/raccoon-19b265",        "XHX2CODEX_KEY"),
    ("gemini",        "gemini2codex",         7, "gemini-2.5-flash",           "GEMINI2CODEX_KEY"),
    ("catpaw",        "catpaw2codex",          8, "glm-5.2",                   "CATPAW2CODEX_KEY"),
    ("antigravity",    "antigravity2codex",     10, "claude-opus-4-8@default",    "ANTIGRAVITY2CODEX_KEY"),
    ("qwen",           "qwen2codex",             11, "qwen3.8-flash",              "QWEN2CODEX_KEY"),
    ("cline",          "cline2codex",           12, "cline-free/deepseek-v4.1-flash", "CLINE2CODEX_KEY"),
    ("zcode",          "zcode2codex",           13, "zcode/GLM-5.3-Flash",       "ZCODE2CODEX_KEY"),
    ("kimi",           "kimi2codex",             15, "kimi/kimi-for-coding",     "KIMI2CODEX_KEY"),
    ("minimax",        "minimax2codex",          16, "minimax/MiniMax-M2.7",     "MINIMAX2CODEX_KEY"),
]


def plist_keys(prefix="com.local"):
    """{label: api key} per installed *2codex* service.

    Reads launchd plists on macOS and the generated .cmd / supervisor wrapper
    elsewhere (see fleet_platform.service_keys), so the tool works on Windows.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from fleet_platform import service_keys
    except Exception:
        return {}
    try:
        return index_keys(service_keys(), prefix)
    except Exception:
        return {}


def index_keys(full_keys, prefix="com.local"):
    """Index {full label: key} so bare-suffix lookups also resolve.

    The BRIDGES table below carries bare suffixes ("qwen2codex") while
    service_keys() returns full labels ("com.local.qwen2codex"). Looking the
    bare suffix up in that dict silently missed, so the prober called six
    bridges with no key at all and reported the key-enforcing ones
    AUTH_EXPIRED -- measured 2026-09-29, when qoder answered a normal chat 200
    with its real key minutes after the prober had told the operator to
    re-login. index_keys pins both spellings, the full label always winning:
    the bare entry exists only so the BRIDGES table resolves, never to shadow
    a label.
    """
    out = dict(full_keys)
    dot = prefix + "."
    for label, key in full_keys.items():
        if label.startswith(dot):
            out[label[len(dot):]] = key
    return out


def get_models(port, key):
    h = {"User-Agent": "fleet-verify/1.0"}
    if key:
        h["Authorization"] = "Bearer " + key
    r = NO_PROXY.open(urllib.request.Request("http://127.0.0.1:%d/v1/models" % port, headers=h),
                      timeout=PROBE_HTTP_TIMEOUT)
    data = json.loads(r.read().decode("utf-8", "replace"))
    return [m.get("id") for m in data.get("data", []) if m.get("id")]


def chat(port, model, key, content, max_tokens=2048, timeout=None):
    timeout = PROBE_CHAT_TIMEOUT if timeout is None else timeout
    payload = json.dumps({"model": model, "messages": [{"role": "user", "content": content}],
                          "max_tokens": max_tokens, "temperature": 0, "stream": False}).encode()
    h = {"Content-Type": "application/json", "User-Agent": "fleet-verify/1.0"}
    if key:
        h["Authorization"] = "Bearer " + key
    t0 = time.time()
    try:
        resp = NO_PROXY.open(urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % port,
                                                    data=payload, headers=h), timeout=PROBE_CHAT_TIMEOUT)
        out = json.loads(resp.read().decode("utf-8", "replace"))
        msg = out.get("choices", [{}])[0].get("message", {}) or {}
        rmodel = out.get("model") or out.get("choices", [{}])[0].get("model") or ""
        return dict(code=resp.status, secs=round(time.time() - t0, 2),
                    text=(msg.get("content") or "").strip(), rmodel=rmodel,
                    err="", usage=out.get("usage", {}) or {})
    except urllib.error.HTTPError as e:
        return dict(code=e.code, secs=round(time.time() - t0, 2), text="", rmodel="",
                    err=e.read().decode("utf-8", "replace")[:200].replace("\n", " "), usage={})
    except Exception as e:
        return dict(code=None, secs=round(time.time() - t0, 2), text="", rmodel="",
                    err=repr(e)[:160], usage={})


def stream_probe(port, model, key, content="嗨", max_tokens=32):
    """Ask the same model with stream=true and stop at the first data: chunk.

    Codex itself always streams, so a bridge that answers a buffered request but
    dies on stream=true is unusable in practice -- measured 2026-09-29, lingxi /
    xhx / codely built a StreamingResponse they never imported, so every
    streaming call came back HTTP 500 (lingxi 2.92s, codely 0.92s, xhx 2.07s)
    while /health, /v1/models and the non-stream arithmetic probe above all said
    REAL. Only the first SSE line is read: what matters is that the stream
    handshake and first block work, not how long the answer runs.
    """
    payload = json.dumps({"model": model, "messages": [{"role": "user", "content": content}],
                          "max_tokens": max_tokens, "temperature": 0, "stream": True}).encode()
    h = {"Content-Type": "application/json", "User-Agent": "fleet-verify/1.0",
         "Accept": "text/event-stream"}
    if key:
        h["Authorization"] = "Bearer " + key
    t0 = time.time()
    try:
        resp = NO_PROXY.open(urllib.request.Request(
            "http://127.0.0.1:%d/v1/chat/completions" % port, data=payload, headers=h),
            timeout=STREAM_PROBE_TIMEOUT)
        ctype = (resp.headers.get("Content-Type") or "")
        first = ""
        while True:
            line = resp.readline()
            if not line:
                break
            if line.startswith(b"data:"):
                first = line.decode("utf-8", "replace").strip()
                break
        if not first:
            return dict(code=resp.status, secs=round(time.time() - t0, 2),
                        ctype=ctype, data="", err="no data: block")
        if "[DONE]" in first and len(first) < 24:
            return dict(code=resp.status, secs=round(time.time() - t0, 2),
                        ctype=ctype, data="", err="first block is [DONE]")
        return dict(code=resp.status, secs=round(time.time() - t0, 2),
                    ctype=ctype, data=first[:200], err="")
    except urllib.error.HTTPError as e:
        return dict(code=e.code, secs=round(time.time() - t0, 2), ctype="", data="",
                    err=e.read().decode("utf-8", "replace")[:200].replace("\n", " "))
    except Exception as e:
        return dict(code=None, secs=round(time.time() - t0, 2), ctype="", data="",
                    err=repr(e)[:160])


def stream_failure(sp):
    """Short reason a stream probe did not prove SSE works."""
    if sp.get("code") != 200:
        return "HTTP %s" % sp.get("code")
    if sp.get("err"):
        return sp["err"][:40]
    return ""


def apply_stream_probe(row, port, model, key):
    """Grade the REAL bridge on stream=true, the only mode Codex uses.

    Runs once per REAL bridge (cheap, max_tokens=32) and only downgrades a REAL
    verdict -- it never promotes a failure, so a pass costs one extra probe.
    """
    sp = stream_probe(port, model, key)
    row["stream"] = sp
    ok = sp["code"] == 200 and bool(sp.get("data")) and not sp.get("err")
    row["stream_ok"] = bool(ok)
    if ok:
        return row
    why = stream_failure(sp) or "无数据块"
    row["verdict"] = "STREAM_BROKEN"
    row["note"] = ("非流式真实推理，但 stream=true %s" % why)
    return row


def make_probe():
    n = random.randint(2345, 9999)
    add = random.randint(137, 499)
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    nonce = "".join(random.choice(alphabet) for _ in range(7))
    prompt = ("只输出一行，格式为：暗号前四位 + 单个空格 + (%d+%d 的阿拉伯数字结果)。"
              "例如：A1B2 1234\n暗号：%s\n请严格只回复那一行，不要任何解释。" % (n, add, nonce))
    return prompt, n, add, nonce


def classify(code, secs, text, nonce, n, add, err):
    expected = n + add
    low = (text or "").lower()
    if code == 401:
        return "AUTH_EXPIRED", "session/key 失效(需重新登录)"
    if code == 403:
        el = (err or "").lower()
        if any(k in el for k in VALI_MARKERS):
            return "VERIFY_ACCOUNT", "上游要求浏览器验证账号(打开消息里的链接) " + (err or "")[:60]
        if any(k in el for k in PLAN_BLOCK_MARKERS):
            return "PLAN_BLOCKED", "模型/套餐受限(非登录态) " + (err or "")[:44]
        return "AUTH_EXPIRED", "403 " + (err or "")[:56]
    if code == 400:
        el = (err or "").lower()
        if any(k in el for k in ("welcome", "欢迎", "onboard", "register", "访问", "http")):
            return "GATE", "上游欢迎/登录门禁(需访问链接激活)"
        return "UPSTREAM_DOWN", "400 " + (err or "")[:56]
    if code in (502, 503, 504):
        el = (err or "").lower()
        if any(k in el for k in NO_KEY_MARKERS):
            return "NO_KEY", "桥未配上游 key（set 后重跑 finish.sh） " + (err or "")[:40]
        if any(k in el for k in ("invalidproxy", "proxyerror", "proxy tunnel")):
            return "UPSTREAM_DOWN", "本地代理隧道异常 " + (err or "")[:44]
        if any(k in el for k in VALI_MARKERS):
            return "VERIFY_ACCOUNT", "上游要求浏览器验证账号(打开消息里的链接) " + (err or "")[:60]
        return "UPSTREAM_DOWN", (err or "")[:56]
    if code is None:
        return "BRIDGE_DOWN", err
    if code != 200:
        return "UPSTREAM_DOWN", "HTTP %s %s" % (code, (err or "")[:46])
    # A channel refusal is a 200 with error text, so it would otherwise fall
    # through to the arithmetic test and be filed as UNCLEAR -- which reads
    # like a weak model rather than a bridge that cannot call home at all.
    if is_error_body(text):
        return "CHANNEL_BLOCKED", "%s (%s)" % (VERDICT_NOTE, matched_marker(text))
    arith_ok = str(expected) in (text or "")
    nonce_ok = nonce[:4].lower() in low
    if arith_ok:
        return "REAL", "运算正确(=真实推理) nonce=%s" % ("ok" if nonce_ok else "miss")
    if nonce_ok and not arith_ok:
        return "ECHO/MIRROR", "复述暗号但算错=疑似透传非真实推理"
    if not (text or ""):
        return "UNCLEAR", "200 但空内容"
    if secs < 0.6 and len(text) < 20:
        return "CANNED/MOCK", "极速+极短且答非所问=疑似罐头/镜像"
    return "UNCLEAR", "200 但未过运算判据(可能弱模型)"


def usage_signal(usage):
    parts = []
    det = usage.get("completion_tokens_details", {}) if isinstance(usage, dict) else {}
    rt = det.get("reasoning_tokens")
    if rt:
        parts.append("reason=%d" % rt)
    if usage.get("credit"):
        parts.append("credit=%s" % usage.get("credit"))
    return ("[" + " ".join(parts) + "]") if parts else ""


def verify_one(name, label, offset, preferred, keyenv, port_base, keys):
    port = port_base + offset
    key = keys.get(label) or os.environ.get(keyenv, "")
    row = dict(name=name, port=port, model="", code=None, secs=0.0, verdict="BRIDGE_DOWN",
               note="", kmd5=hashlib.md5(key.encode()).hexdigest()[:8] if key else "none",
               reply="", rmodel="", models=0, attempted=[])
    try:
        ids = get_models(port, key)
        row["models"] = len(ids)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:120]
        v, note = classify(e.code, 0, "", "", 0, 0, body)
        row["verdict"], row["note"], row["code"] = v, "models: " + note, e.code
        return row
    except Exception as e:
        msg = repr(e)
        hint = "上游超时(可能需VPN/CatPaw代理)" if ("Time" in msg or "timed out" in msg) else "桥未连接"
        row["note"] = "models ERR " + msg[:70] + " " + hint
        return row

    cands = []
    if preferred and preferred not in cands:
        cands.append(preferred)
    for mid in ids:
        if mid and mid.strip().lower() not in PLACEHOLDERS and mid not in cands:
            cands.append(mid)
    cands = cands[:MAX_CANDIDATES]
    attempted = row["attempted"]

    last = None
    for model in cands:
        attempt = dict(model=model, code=None, secs=0.0)
        attempted.append(attempt)
        for budget in BUDGETS:
            prompt, n, add, nonce = make_probe()
            # Only pass the kwarg when a bridge actually overrides it: every
            # other call site keeps the historical signature, so fakes that
            # patch chat() with a narrower lambda stay valid.
            override = PROBE_CHAT_TIMEOUT_OVERRIDE.get(name)
            if override is None:
                r = chat(port, model, key, prompt, max_tokens=budget)
            else:
                r = chat(port, model, key, prompt, max_tokens=budget,
                         timeout=override)
            last = (model, n, add, nonce, budget, r)
            attempt["code"], attempt["secs"] = r["code"], r["secs"]
            row["model"], row["code"], row["secs"], row["rmodel"] = model, r["code"], r["secs"], r["rmodel"]
            if r["code"] == 200:
                det = r["usage"].get("completion_tokens_details", {}) if isinstance(r["usage"], dict) else {}
                rt = det.get("reasoning_tokens") or 0
                if not r["text"] and rt and rt >= int(budget * 0.9) and budget < BUDGETS[-1]:
                    continue  # thinking ate the budget -> escalate
                v, note = classify(200, r["secs"], r["text"], nonce, n, add, r["err"])
                sig = usage_signal(r["usage"])
                row["verdict"], row["note"] = v, (note + (" " + sig if sig else "")).strip()
                row["reply"] = r["text"][:60].replace("\n", " ")
                if v == "REAL":
                    apply_stream_probe(row, port, model, key)
                return row
            if r["code"] in (401, 403):
                v, note = classify(r["code"], r["secs"], r["text"], nonce, n, add, r["err"])
                if v == "PLAN_BLOCKED":
                    break  # just this model is off-plan, the bridge may serve others
                row["verdict"], row["note"] = v, note
                return row
            if r["code"] == 400 and budget < BUDGETS[-1]:
                continue  # maybe max_tokens too tight -> escalate once/branch
            # 其他错误(502/timeout) 或 400 已到顶：换下一个候选
            break
    if last:
        model, n, add, nonce, budget, r = last
        v, note = classify(r["code"], r["secs"], r["text"], nonce, n, add, r["err"])
        row["model"], row["code"], row["secs"] = model, r["code"], r["secs"]
        if len(attempted) > 1:
            note += " (共试%d个模型)" % len(attempted)
        row["verdict"], row["note"] = v, note
        row["reply"] = (r["text"][:60].replace("\n", " ")) or (r["err"][:60])
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port-base", type=int, default=8787)
    ap.add_argument("--only", help="仅检测某一 bridge 名")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--label-prefix", default="com.local")
    a = ap.parse_args()

    keys = plist_keys(a.label_prefix)
    rows = [verify_one(nm, lb, off, pref, ke, a.port_base, keys)
            for (nm, lb, off, pref, ke) in BRIDGES if not a.only or nm == a.only]

    if a.json:
        print(json.dumps({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                          "port_base": a.port_base, "bridges": rows}, ensure_ascii=False, indent=2))
        return 0

    order = {"REAL": 0, "STREAM_BROKEN": 1, "ECHO/MIRROR": 1, "CANNED/MOCK": 2,
            "CHANNEL_BLOCKED": 3, "UNCLEAR": 3, "PLAN_BLOCKED": 4, "AUTH_EXPIRED": 5,
             "NO_KEY": 5, "VERIFY_ACCOUNT": 5, "UPSTREAM_DOWN": 6,
             "BRIDGE_DOWN": 7, "GATE": 8}
    print("FleetKit 真实调用检测  (port base %d)" % a.port_base)
    print("%-14s %-5s %-28s %-5s %-6s %-13s %s" % ("BRIDGE", "PORT", "MODEL", "HTTP", "LAT(s)", "VERDICT", "NOTE"))
    print("-" * 116)
    for r in rows:
        print("%-14s %-5d %-28s %-5s %-6s %-13s %s" % (
            r["name"], r["port"], (r["model"] or "-")[:28], str(r["code"]),
            "%.1f" % r["secs"], r["verdict"], (r["note"] or "")[:44]))
    print("-" * 116)
    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    summary = "  ".join("%s=%d" % (k, counts[k]) for k in sorted(counts, key=lambda x: order.get(x, 9)))
    real = [r["name"] for r in rows if r["verdict"] == "REAL"]
    print("合计: %s" % summary)
    print("真实可用上游: %s" % (", ".join(real) if real else "(无)"))
    sbroken = [r["name"] for r in rows if r.get("stream_ok") is False]
    if sbroken:
        print("流式失效(Codex 不可用): %s" % ", ".join(sbroken))
    print("判据: REAL=随机运算题答对(罐头/镜像无法伪造); STREAM_BROKEN=非流式可用但 stream=true 挂; 需重启 Codex 选择器才刷新列表。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
