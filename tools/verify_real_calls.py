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
  AUTH_EXPIRED   401/403 session or key invalid
  UPSTREAM_DOWN  502/503/504 (tunnel blocked, shutdown, param rejected)
  BRIDGE_DOWN    connection refused / timeout

Reasoning models need a bigger max_tokens: the probe escalates
256 -> 2048 -> 4096 when the content is empty because thinking ate the budget.

Keys are read from launchd plists (md5 only printed) or FLEET_*_KEY env vars.
Use --json for machine-readable output (UI / sync).
"""
import urllib.request, urllib.error, json, hashlib, time, glob, re, sys, os, random, argparse

NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PLACEHOLDERS = {"", "model", "auto", "default", "none", "test"}
BUDGETS = [256, 2048, 4096]
PROBE_HTTP_TIMEOUT = 20
PROBE_CHAT_TIMEOUT = 70

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
]


def plist_keys(prefix="com.local"):
    out = {}
    for p in glob.glob(os.path.expanduser("~/Library/LaunchAgents/%s.*2codex*.plist" % prefix)):
        name = p.split("/")[-1].replace(prefix + ".", "").replace(".plist", "")
        try:
            raw = open(p, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        m = re.search(r"<key>([A-Z0-9_]*(?:KEY|TOKEN)[A-Z0-9_]*)</key>\s*<string>([^<]+)</string>", raw)
        if m:
            out[name] = m.group(2)
    return out


def get_models(port, key):
    h = {"User-Agent": "fleet-verify/1.0"}
    if key:
        h["Authorization"] = "Bearer " + key
    r = NO_PROXY.open(urllib.request.Request("http://127.0.0.1:%d/v1/models" % port, headers=h),
                      timeout=PROBE_HTTP_TIMEOUT)
    data = json.loads(r.read().decode("utf-8", "replace"))
    return [m.get("id") for m in data.get("data", []) if m.get("id")]


def chat(port, model, key, content, max_tokens=2048):
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
    if code in (401, 403):
        return "AUTH_EXPIRED", "session/key 失效(需重新登录)"
    if code == 400:
        el = (err or "").lower()
        if any(k in el for k in ("welcome", "欢迎", "onboard", "register", "访问", "http")):
            return "GATE", "上游欢迎/登录门禁(需访问链接激活)"
        return "UPSTREAM_DOWN", "400 " + (err or "")[:56]
    if code in (502, 503, 504):
        return "UPSTREAM_DOWN", (err or "")[:56]
    if code is None:
        return "BRIDGE_DOWN", err
    if code != 200:
        return "UPSTREAM_DOWN", "HTTP %s %s" % (code, (err or "")[:46])
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
               reply="", rmodel="", models=0)
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
    if preferred in ids:
        cands.append(preferred)
    for mid in ids:
        if mid and mid.strip().lower() not in PLACEHOLDERS and mid not in cands:
            cands.append(mid)
    cands = cands[:4]

    last = None
    for model in cands:
        for budget in BUDGETS:
            prompt, n, add, nonce = make_probe()
            r = chat(port, model, key, prompt, max_tokens=budget)
            last = (model, n, add, nonce, budget, r)
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
                return row
            if r["code"] in (401, 403):
                v, note = classify(r["code"], r["secs"], r["text"], nonce, n, add, r["err"])
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

    order = {"REAL": 0, "ECHO/MIRROR": 1, "CANNED/MOCK": 2, "UNCLEAR": 3,
             "AUTH_EXPIRED": 4, "UPSTREAM_DOWN": 5, "BRIDGE_DOWN": 6, "GATE": 7}
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
    print("判据: REAL=随机运算题答对(罐头/镜像无法伪造); 需重启 Codex 选择器才刷新列表。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
