#!/usr/bin/env python3
"""Ground-truth benchmark: which FleetKit model writes FleetKit code best.

Vibes are not evidence. This tool sends a real coding task to a model, pulls
the fenced code block out of the reply, runs it against a real testsuite, and
counts passing checks -- so a model wins by compiling and passing asserts,
not by sounding confident, and any extra prose is measured as wasted output.

Everything it needs is embedded: both task prompts, both testsuites, both
reference answers (each verified to pass its own suite), and the grader. That
makes the grader itself testable -- the offline subcommand grades known good
and known garbage fixtures with no network at all.

Subcommands:
  offline   grade the built-in fixtures; expect OFFLINE_SELFTEST PASS
  grade     grade a saved raw model response file (TASK with --task 1 or 2)
  live      ask a live model and score it (--bridge NAME, or the gateway)

The conclusion this file backs is written up in kit/docs/code-model-
selection.md: which model FleetKit should use to edit FleetKit itself.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
RUNTIME_VENV = os.path.join(REPO, "runtime", ".venv", "bin", "python")
RUNTIME_ENV = os.path.join(REPO, "runtime", "fleet.env")
DEFAULT_GATEWAY = "http://127.0.0.1:8801"

# Bridge ports and fleet.env key names; mirrors default_model_guard.py so the
# two tools never disagree about where a provider lives.
BRIDGE_PORTS = {
    "workbuddy": 8787, "workbuddy-gpt": 8788, "qoder": 8789,
    "codely": 8790, "trae": 8791, "lingxi": 8792, "xhx": 8793,
    "gemini": 8794, "catpaw": 8795, "antigravity": 8797,
    "qwen": 8798, "cline": 8799, "zcode": 8800,
    "kimi-code": 8802, "minimax": 8803,
}
KEY_ENV = {
    "workbuddy": "CODEBUDDY2OPENAI_KEY", "workbuddy-gpt": "CODEBUDDY2OPENAI_KEY",
    "qoder": "QODER2CODEX_KEY", "codely": "CODELY2CODEX_KEY",
    "trae": "TRAE2CODEX_KEY", "lingxi": "LINGXI2CODEX_KEY",
    "xhx": "XHX2CODEX_KEY", "gemini": "GEMINI2CODEX_KEY",
    "catpaw": "CATPAW2CODEX_KEY", "antigravity": "ANTIGRAVITY2CODEX_KEY",
    "qwen": "QWEN2CODEX_KEY", "cline": "CLINE2CODEX_KEY",
    "zcode": "ZCODE2CODEX_KEY", "kimi-code": "KIMI2CODEX_KEY",
    "minimax": "MINIMAX2CODEX_KEY",
}

# Backslash-free source: the three regexes that need a backslash build it from
# chr(92), and embedded newlines use chr(10), so this file carries no literal
# backslash to trip over when the content is copied or patched.
NL = chr(10)
BS = chr(92)
METRIC_RE = re.compile(BS + "|HTTP:[0-9]+ T:[0-9.]+" + BS + "s*$")
FENCE_RE = re.compile("```(?:python)?" + BS + "s*" + BS + "n(.*?)```", re.S)
SCORE_RE = re.compile("^SCORE (" + BS + "d+)/(" + BS + "d+)")

# loopback calls must never leak out through the macOS system proxy
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# --- task prompts (verbatim from the ground-truth bench) ---
TASK1_PROMPT = """
你是 FleetKit 项目(Python 桥接网关)的代码助手。下面这个函数属于免费时段解析模块，有 4 个缺陷：
1) 输入为 None 或空字符串时要抛 ValueError
2) 格式必须是 HH:MM-HH:MM，否则抛 ValueError
3) 小时或分钟越界时要抛 ValueError
4) 跨午夜窗口(start 大于 end)要交换两者顺序

请修复，只输出修复后的完整函数代码(放在一个 ```python 代码块中)，不要任何解释文字：

```python
def parse_window(s):
    a, b = s.split("-")
    def m(x):
        h, mm = x.split(":")
        return int(h) * 60 + int(mm)
    return m(a), m(b)
```
"""

TASK2_PROMPT = '''
你是 FleetKit 桥接网关的代码助手。请实现下面这个函数，这是网关最核心的请求体转换逻辑：

def convert_anthropic_to_openai(body):
    """把 Anthropic /v1/messages 请求体转成 OpenAI chat.completions 请求体。
    规则：
    1. body["system"] 可能是字符串或 content block 列表(list of {"type":"text","text":...})，
       两种情况都要合并成第一条 {"role":"system","content":...} message
    2. messages 里 content 为字符串时保持；为 list 时把所有 text block 用换行拼接；
       遇到 {"type":"tool_use","id":...,"name":...,"input":...} 要转成
       {"role":"assistant","content":"","tool_calls":[{"id":..., "type":"function",
        "function":{"name":..., "arguments": json.dumps(input, ensure_ascii=False)}}]}
    3. max_tokens 缺省时取 1024
    4. 返回 dict 必须含 model/messages/max_tokens 三个键
    """
    pass

只输出实现后的完整代码(一个 ```python 代码块，含 import)，不要解释：
'''

# --- testsuites (expected newlines built with chr(10), no literal backslash) ---
TASK1_TESTS = """
def run():
    cases = [
        ("07:00-09:30", (420, 570), None),
        ("22:00-02:00", (120, 1320), None),
        ("", None, ValueError),
        ("7-9", None, ValueError),
        ("24:00-01:00", None, ValueError),
        ("10:70-11:00", None, ValueError),
        (None, None, ValueError),
        ("00:00-23:59", (0, 1439), None),
    ]
    ok = 0
    for arg, want, exc in cases:
        try:
            got = parse_window(arg)
            if exc is None and got == want:
                ok += 1
            else:
                print("FAIL", repr(arg), "got", got, "want", want)
        except Exception as e:
            if exc is not None and isinstance(e, exc):
                ok += 1
            else:
                print("FAIL", repr(arg), "raised", type(e).__name__, e)
    print("SCORE %d/%d" % (ok, len(cases)))


run()
"""

TASK2_TESTS = """
def run():
    import json
    body = {
        "model": "workbuddy/hy4-preview",
        "system": [{"type": "text", "text": "你是助手"}, {"type": "text", "text": "保持简洁"}],
        "max_tokens": 4096,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "第一行"}, {"type": "text", "text": "第二行"}]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "让我查一下"},
                {"type": "tool_use", "id": "call_1", "name": "get_status", "input": {"bridge": "trae"}},
            ]},
            {"role": "user", "content": "继续"},
        ],
    }
    out = convert_anthropic_to_openai(body)
    ok = 0
    n = 6

    def chk(name, cond):
        nonlocal ok
        if cond:
            ok += 1
        else:
            print("FAIL", name)

    chk("keys", set(["model", "messages", "max_tokens"]).issubset(out.keys()))
    chk("system_merged", out["messages"][0]["role"] == "system" and out["messages"][0]["content"] == "你是助手" + chr(10) + "保持简洁")
    chk("content_join", out["messages"][1]["role"] == "user" and out["messages"][1]["content"] == "第一行" + chr(10) + "第二行")
    tc = out["messages"][2].get("tool_calls") or []
    chk("tool_calls", len(tc) == 1 and tc[0]["id"] == "call_1" and tc[0]["type"] == "function")
    fn = tc[0]["function"] if tc else {}
    chk("function_shape", fn.get("name") == "get_status" and json.loads(fn.get("arguments", "{}")) == {"bridge": "trae"})
    chk("max_tokens", out["max_tokens"] == 4096)
    b2 = convert_anthropic_to_openai({"model": "m", "system": "sys", "messages": [{"role": "user", "content": "hi"}]})
    chk("system_str_and_default", b2["messages"][0]["content"] == "sys" and b2["max_tokens"] == 1024)
    print("SCORE %d/%d" % (ok, n + 1))


run()
"""

TASKS = {
    1: {"name": "parse_window", "prompt": TASK1_PROMPT, "tests": TASK1_TESTS, "max_tokens": 1024, "total": 8},
    2: {"name": "convert_anthropic_to_openai", "prompt": TASK2_PROMPT, "tests": TASK2_TESTS, "max_tokens": 1536, "total": 7},
}

# --- reference answers (each verified to pass its own testsuite) ---
CORRECT_R1 = """
def parse_window(s):
    if not isinstance(s, str) or not s:
        raise ValueError("window must be a non-empty HH:MM-HH:MM string")
    parts = s.split("-")
    if len(parts) != 2:
        raise ValueError("expected exactly one dash")

    def to_minutes(x):
        if x.count(":") != 1:
            raise ValueError("expected HH:MM")
        hh, mm = x.split(":")
        if not (hh.isdigit() and mm.isdigit()):
            raise ValueError("hours and minutes must be digits")
        hh = int(hh)
        mm = int(mm)
        if hh > 23 or mm > 59 or hh < 0 or mm < 0:
            raise ValueError("hours or minutes out of range")
        return hh * 60 + mm
    start = to_minutes(parts[0])
    end = to_minutes(parts[1])
    if start > end:
        start, end = end, start
    return start, end
"""

CORRECT_R2 = """
import json


def _join_text_blocks(blocks):
    parts = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
    return chr(10).join(parts)


def _system_to_text(system):
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return _join_text_blocks(system)
    return ""


def convert_anthropic_to_openai(body):
    messages = []
    sys_text = _system_to_text(body.get("system"))
    if sys_text:
        messages.append({"role": "system", "content": sys_text})
    for msg in body.get("messages", []):
        role = msg.get("role")
        content = msg.get("content")
        tool_calls = []
        if isinstance(content, list):
            text_parts = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    text_parts.append(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    tool_calls.append({
                        "id": b.get("id"),
                        "type": "function",
                        "function": {
                            "name": b.get("name"),
                            "arguments": json.dumps(b.get("input", {}), ensure_ascii=False),
                        },
                    })
            text = chr(10).join(text_parts)
        elif content is None:
            text = ""
        else:
            text = content
        new_msg = {"role": role, "content": text}
        if tool_calls:
            new_msg["tool_calls"] = tool_calls
        messages.append(new_msg)
    return {
        "model": body.get("model"),
        "messages": messages,
        "max_tokens": body.get("max_tokens", 1024),
    }
"""


def fenced(code):
    return "```python" + NL + code + NL + "```"


def extract_text(raw):
    text = METRIC_RE.sub("", raw.strip())
    try:
        data = json.loads(text)
    except Exception:
        return text
    if isinstance(data, dict):
        content = data.get("content")
        if isinstance(content, list):
            joined = "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
            if joined or content:
                return joined
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") or {}
            if "content" in msg:
                return msg.get("content") or ""
    return text


def extract_code(text):
    m = FENCE_RE.search(text)
    if m:
        return m.group(1), True
    return text, False


def _bench_python():
    override = os.environ.get("FLEET_BENCH_PYTHON")
    if override:
        return override
    if os.path.exists(RUNTIME_VENV):
        return RUNTIME_VENV
    return sys.executable


def _parse_scores(out):
    score = None
    failures = []
    for line in out.splitlines():
        stripped = line.strip()
        m = SCORE_RE.match(stripped)
        if m:
            score = (int(m.group(1)), int(m.group(2)))
        elif stripped.startswith("FAIL") or stripped.startswith("ERR"):
            failures.append(stripped)
    return score, failures


def run_suite(code, task_id):
    task = TASKS[task_id]
    source = code + NL + task["tests"]
    handle = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
    try:
        handle.write(source)
        handle.close()
        try:
            proc = subprocess.run([_bench_python(), handle.name], capture_output=True, text=True, timeout=30)
            out = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            out = "ERR timeout running testsuite"
    finally:
        os.unlink(handle.name)
    return _parse_scores(out)


def grade_response(raw, task_id):
    task = TASKS[task_id]
    text = extract_text(raw)
    match = FENCE_RE.search(text)
    code, matched = extract_code(text)
    if matched:
        prose = len(text) - len(match.group(0))
    else:
        prose = len(text)
    if not matched:
        return {"task": task["name"], "score": None, "total": task["total"],
                "no_runnable": True, "score_str": None, "prose": prose,
                "failures": [], "note": "no fenced code block found"}
    score, failures = run_suite(code, task_id)
    return {"task": task["name"], "score": None if score is None else list(score),
            "total": task["total"], "no_runnable": False,
            "score_str": None if score is None else "%d/%d" % score,
            "prose": prose, "failures": failures[:3], "note": None}


def grade(kind, task_id):
    if kind == "good":
        code = CORRECT_R1 if task_id == 1 else CORRECT_R2
        return grade_response(fenced(code), task_id)
    return grade_response("cannot handle this request", task_id)


def _fmt_row(res):
    if res["no_runnable"]:
        return "%s NO_RUNNABLE prose=%d" % (res["task"], res["prose"])
    if res.get("score_str"):
        return "%s score=%s prose=%d fails=%d" % (res["task"], res["score_str"], res["prose"], len(res["failures"]))
    return "%s RUN_ERROR prose=%d fails=%s" % (res["task"], res["prose"], res["failures"][:1])


def run_offline():
    rows = []
    good1 = grade("good", 1)
    good2 = grade("good", 2)
    garb1 = grade("garbage", 1)
    rows.append(("R1 good", _fmt_row(good1)))
    rows.append(("R2 good", _fmt_row(good2)))
    rows.append(("R1 garbage", _fmt_row(garb1)))
    ok = (good1.get("score_str") == "8/8" and good1["prose"] == 0
          and good2.get("score_str") == "7/7" and good2["prose"] == 0
          and garb1["no_runnable"] and garb1["score"] is None)
    return ok, rows


def _read_env_file():
    env = {}
    if os.path.exists(RUNTIME_ENV):
        with open(RUNTIME_ENV, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip(chr(34)).strip(chr(39))
    return env


def bridge_key(name, env):
    key_env = KEY_ENV.get(name, "")
    if not key_env:
        return ""
    return env.get(key_env) or os.environ.get(key_env) or ""


def _post(url, payload, headers, timeout):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    resp = OPENER.open(request, timeout=timeout)
    return resp.read().decode("utf-8", "replace")


def ask_gateway(model, prompt, max_tokens, timeout):
    url = os.environ.get("FLEET_GATEWAY_URL", DEFAULT_GATEWAY).rstrip("/") + "/v1/messages"
    token = os.environ.get("FLEET_ANTHROPIC_TOKEN") or os.environ.get("FLEET_GATEWAY_KEY") or ""
    headers = {"Content-Type": "application/json", "x-api-key": token,
               "anthropic-version": "2023-06-01", "x-fleetkit-resolved-model": model}
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]}
    return _post(url, body, headers, timeout)


def ask_bridge(name, model, prompt, max_tokens, timeout):
    port = BRIDGE_PORTS.get(name)
    if not port:
        raise ValueError("unknown bridge: " + name)
    env = _read_env_file()
    url = "http://127.0.0.1:%d/v1/chat/completions" % port
    key = bridge_key(name, env)
    headers = {"Content-Type": "application/json", "User-Agent": "fleet-bench/1.0"}
    if key:
        headers["Authorization"] = "Bearer " + key
    body = {"model": model, "max_tokens": max_tokens, "stream": False,
            "messages": [{"role": "user", "content": prompt}]}
    return _post(url, body, headers, timeout)


def benchmark_model(model, bridge=None, timeout=60):
    results = {"model": model, "bridge": bridge, "reachable": False, "smoke": None, "tasks": []}
    steps = [("smoke", "Reply exactly: OK", 16, None),
             ("task1", TASKS[1]["prompt"], TASKS[1]["max_tokens"], 1),
             ("task2", TASKS[2]["prompt"], TASKS[2]["max_tokens"], 2)]
    started = time.time()
    for label, prompt, mt, task_id in steps:
        begin = time.time()
        try:
            if bridge:
                raw = ask_bridge(bridge, model, prompt, mt, timeout)
            else:
                raw = ask_gateway(model, prompt, mt, timeout)
        except Exception as exc:
            results["error"] = label + ": " + repr(exc)[:160]
            break
        seconds = round(time.time() - begin, 1)
        if label == "smoke":
            results["reachable"] = True
            results["smoke"] = seconds
        else:
            res = grade_response(raw, task_id)
            res["seconds"] = seconds
            results["tasks"].append(res)
    results["total_seconds"] = round(time.time() - started, 1)
    return results


def _fmt_live(res):
    header = "%-34s %-11s reach=%s smoke=%ss total=%ss" % (
        res["model"], res["bridge"] or "gateway", res["reachable"], res["smoke"], res["total_seconds"])
    lines = [header]
    if res.get("error"):
        lines.append("    ERROR " + res["error"])
    for task in res["tasks"]:
        verdict = task.get("score_str") or ("NO_RUNNABLE" if task["no_runnable"] else "RUN_ERROR")
        lines.append("    %-24s %-8s prose=%-3d %ss%s" % (
            task["task"], verdict, task["prose"], task.get("seconds"),
            "" if not task["failures"] else "  fails=" + ";".join(task["failures"])[:120]))
    return NL.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Which FleetKit model writes FleetKit code best.")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("offline")
    grader = sub.add_parser("grade")
    grader.add_argument("rawfile")
    grader.add_argument("--task", type=int, choices=[1, 2], default=1)
    runner = sub.add_parser("live")
    runner.add_argument("models", nargs="+")
    runner.add_argument("--bridge")
    runner.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args(argv)

    if args.cmd == "offline":
        ok, rows = run_offline()
        for name, line in rows:
            print("%-12s %s" % (name, line))
        print("OFFLINE_SELFTEST PASS" if ok else "OFFLINE_SELFTEST FAIL")
        return 0 if ok else 1

    if args.cmd == "grade":
        with open(args.rawfile, encoding="utf-8", errors="ignore") as fh:
            raw = fh.read()
        res = grade_response(raw, args.task)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("score_str") == "%d/%d" % (res["total"], res["total"]) else 1

    if args.cmd == "live":
        for model in args.models:
            print(_fmt_live(benchmark_model(model, bridge=args.bridge, timeout=args.timeout)))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
