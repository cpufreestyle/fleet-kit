# 2026-09-29 修复记录：Codex 一提交就 500 + WorkBuddy 渠道校验死循环

症状（用户报告）：
- Codex 里选 `lingxi/`、`xhx/`、`codely/` 的模型，一提交就报错；手动 curl 同一个端口却像是好的。
- WorkBuddy 桥隔一段时间就整体不可用，面板提示「登录态失效，请重新登录」，重新登录也没用。
- 但 `tools/verify_real_calls.py`（真实调用检测）与面板全绿，全都写 REAL。

## 根因 1：三座桥构造 `StreamingResponse`，却从没 import 它

`bridges/lingxi/lingxi_bridge.py`、`bridges/xhx/xhx_bridge.py`、`bridges/codely/codely_bridge.py`
的 `chat_completions` 里 `stream` 分支都写了：

    return StreamingResponse(gen(), media_type="text/event-stream")

而文件头部只 import 了 `JSONResponse, Response`。于是每一次流式请求都在 handler 里抛
`NameError: name 'StreamingResponse' is not defined`，FastAPI 转成 HTTP 500。

实测（同一端口、同一 key、同一模型，只有 `stream` 不同）：

    stream=False  -> 200  运算正确  REAL
    stream=True   -> 500  lingxi 2.92s / codely 0.92s / xhx 2.07s
    trae（有 import）-> 200 SSE

**Codex 只走流式**，所以这三座桥在 Codex 里是 100% 不可用，而 `/health`、`/v1/models`、
非流式 chat 三条自动化检查一条都照绿，`py_compile` 也拦不住（NameError 是运行期才炸）。

修复：三处都改成 `from fastapi.responses import JSONResponse, Response, StreamingResponse`。

## 根因 2：探测盲区 —— 检测脚本只发 `stream: False`

`tools/verify_real_calls.py` 的 `chat()` 把 `"stream": False` 写死在 payload 里，
因此它结构上不可能发现根因 1。补两件事：

1. `stream_probe(port, model, key)`：同样题目、`stream=True`、`max_tokens=32`，
   **只读到第一个 `data:` 行为止**（验证 SSE 握手与首块，不比速度），socket 超时单独用
   `STREAM_PROBE_TIMEOUT = 25`，不占非流式的 70s 预算。
2. `apply_stream_probe(row, port, model, key)`：只在拿到 REAL 之后调用一次，
   失败则把 verdict 降级为 **`STREAM_BROKEN`**，note 形如
   `非流式真实推理，但 stream=true HTTP 500`；它只降级、不提前，成本就是每轮多发一次 32 token 请求。

顺带把 `status_ui.py` 的 `VERDICT_RANK` / `VERDICT_KIND` 与页面 JS 的 `VF_KIND`
补上同一判据（漏一个的后果是那一行渲染成 idle，直接从面板上消失）。

验证（假桥：非流式真答、流式 500）：

    ./runtime/.venv/bin/python tools/verify_real_calls.py --port-base 9123 --only workbuddy
    # workbuddy  9123  hy4-preview   200   STREAM_BROKEN 非流式真实推理，但 stream=true HTTP 500
    # 流式失效(Codex 不可用): workbuddy

## 根因 3：`_ChannelRetry` 这个类在仓库里根本不存在

`bridges/workbuddy/core.py` 在渠道重试逻辑里 `raise _ChannelRetry()` 又 `except _ChannelRetry`，
但**全仓库没有任何地方定义过它**。后果是上游任何一次渠道校验拒绝
（错误码 `11128` / `"unapproved channel"`）都会变成未捕获的 `NameError`。

同时 `channel_tries = 0` 写在 `for candidate in candidates:` 循环**内部**，
而 except 分支又是 `candidates.insert(0, candidate)` 把当前候选塞回队首 ——
每次重进循环计数器都被重置成 0，三度重试的预算永远用不完，变成无限重发。

修复：
- 在 `_collect_with_pool` 之前补上 `class _ChannelRetry(Exception)`；
- `channel_tries = 0` 提到函数作用域，与 `_stream_with_pool` 的契约一致（一次请求一个 3 次预算）。

## 根因 4：`11128` 被当成「登录态失效」，误冷却 600 秒

`_account_failure()` 先判 `status in {401, 403}`，再判 `11128 / unapproved channel`，于是一次上游临时渠道校验被记成 token 过期：冷却 600 秒（应为 20 秒），面板还提示重新登录。

修复：把渠道分支挪到最前面并加注释说明优先级 —— 403 不等于登录过期。

## 根因 5（运维坑）：补丁落盘了，但线上进程没重启

`bridges/workbuddy/core.py` 21:45 修正落盘，而两个 WorkBuddy 进程都是 **17:20:38** 起的，
一直在跑修正前的代码。`--sync-only` 只搬文件，不重启服务，所以「文件已修」≠「已生效」。

判断方法（比看日志快）：

    ls -l runtime/bridges/workbuddy/core.py          # 补丁落盘时间
    ps -eo pid,lstart,command | grep converter        # 进程启动时间
    # 进程早于文件 => 必须 launchctl kickstart -k gui/501/com.local.<label>

本次重启：`workbuddy2codex`、`workbuddy2codex-gpt`、`xhx2codex`、`fleet-ui`。
重启后 workbuddy 复测 REAL + 流式 200。

## 新守卫（`kit/tools/`，均随 `pytest tools -q` 跑）

- `test_no_undefined_names.py`：作用域感知的未定义名审计，遍历 `bridges/`、`tools/`、`opencodex/`，
  按 locals → enclosing → module → builtins 解析名字。就是它抓住的根因 1。
- `test_verify_stream_probe.py`：钉住 `STREAM_BROKEN` 判据、SSE 首块判定、
  `stream=True` 真的发出去了（payload/超时都断言），以及 Python 地图与页面 JS 判据一致。
- `test_workbuddy_channel_retry.py`：假 `httpx.AsyncClient` + 打桩 `asyncio.sleep`，
  钉住异常类存在、3 次退避 `[1.5, 3.0, 4.5]`、`mark_failure("上游渠道校验未通过", 20)`、403 上抛、
  预算按请求而非按候选、普通 429 原样透出。

另：`xhx2codex` 此次同步到带 `usage_ledger` 计量的新版（`_metered` 包住 SSE 中继，流式也记账）。

## 根因 6：`_stream_upstream` 的 `break` 把「原地重发」变成了「换号重试」

流式路径的渠道校验分支原来这样收尾：

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", url, ...) as response:
            if response.status_code != 200:
                if _is_channel_error(error) and channel_attempts < 3:
                    channel_attempts += 1
                    channel_retry = True
                    _log(f"... retry {channel_attempts}/3 | {model_name}")
                    await asyncio.sleep(1.5 * channel_attempts)
                    break

`break` 在两层 `async with` 之内、`for attempt in range(4)` 之中。Python 的 `break`
会先退出两个 `async with`（干净关掉 response 和 client），然后退出的是**最内层 for**，
不是「再试一次」。单独一个最小复现就能证实：

    for attempt in range(4):
        async with CM("client"):
            async with CM("stream"):
                if attempt == 0 and stmt == "break":
                    break
    # break    -> 轨迹止于 attempt 0，attempt 1 从未进入
    # continue -> attempt 1 正常进入

后果与日志完全相反：日志写着 `retry 1/3`，实际只发一次请求就换下一个账号。
而 `channel_retry = True` 这一行是死代码 —— `break` 已经离场，没有谁再读它。
`_is_channel_error()` 的文档字符串写着 "Transient and self-clearing, so a resend is
worth trying"，说的正是原地重发。

单账号池（`-gpt` 变体的常态）下，一次自愈型 11128 的直接结果是：
1 次请求而非 3 次、`mark_failure` 一次都不记、客户端立刻拿到 502。

修复：`break` 改成 `continue`（上下文管理器照样会关），删掉死代码 `channel_retry = True`，
并留注释说明这个坑 —— 同样的形状下次很容易再写错。

## 根因 7：流式路径的重试预算按账号计，非流式按请求计

`channel_attempts = 0` 写在 `for candidate in candidates:` 循环体里，等于每个账号各发
一份 3 次预算。非流式的 `_collect_with_pool` 早就把 `channel_tries` 提到了函数作用域
（一次请求一个预算），`test_workbuddy_channel_retry.py` 里
`test_channel_budget_is_per_request_not_per_candidate` 明确钉住了这个契约，
而那条测试的文档字符串还写着「the same contract `_stream_with_pool` already had」。

也就是说流式路径一直是那个契约的例外。10 个账号的池子遇到持续 11128，
一次用户请求会打 40 次上游、睡 90 秒。

修复：把 `channel_attempts = 0` 提到 `for candidate` 之前，与非流式对齐。
顺带补一条：重试预算耗尽的渠道错误不要再对同一账号重打 —— `mark_failure` 会写盘并
累计 `failures` 计数，重复调用只是浪费，所以这种情形 `break` 换下一个账号。

## 新守卫

- `test_workbuddy_stream_channel_retry.py`（3 条）：假 `httpx.AsyncClient` 记下每个 attempt
  实际使用的 `Authorization`，从而直接断言「重发的是同一个账号」；
  钉住 4 次请求 `["Bearer A"] * 4`、退避 `[1.5, 3.0, 4.5]`、冷却 `("上游渠道校验未通过", 20)`、
  双账号下预算是请求级而非账号级、以及 11128 后紧跟干净 SSE 时能恢复输出。
  反向对照已验证：把两个缺陷重新注入，3 条全红；还原后全绿。

