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


## 根因 8：Trae 桥找不到 IDE 登录态，多拼了一级 `Trae/`

`storage_candidates()` 把基准目录取成 `app_support_dirs("Trae")`，再在后面拼版本名：

    ~/Library/Application Support/Trae/Trae CN/User/globalStorage/storage.json

但各版本真实目录**就是** Application Support 下的直接子目录，中间没有 `Trae/` 这一层：

    ~/Library/Application Support/Trae CN/User/globalStorage/storage.json

候选列表最后按 `path.exists()` 过滤，于是 4 个桌面候选**全部**被过滤掉，
只剩凭据缓存 `~/.trae2codex/creds.json` 在兜底。新机器、缓存失效、
或者缓存里的 refresh 换不出新 token，都会报 HTTP 401「Trae login state not found」，
而用户其实一直在 IDE 里登录着。

同一处形状的镜像错误在 `read_desktop_auth()`：取 `product.json` 时从 `storage.json`
往上走 4 级，指望落在 `.app` 包里，实际落在 Application Support，
所以 `app_version` 永远是空串，每个请求都带着硬编码的兜底版本号。

修复：
- 新增 `app_support_root()` 返回 `app_support_dirs("Trae")[0]` 的**父目录**，
  版本名直接拼在它后面；
- `read_desktop_auth()` 改成按候选根列表找 `product.json`：
  先 `/Applications/<edition>.app/Contents/Resources/app/product.json`，
  再退回用户数据目录的祖先。

验证（`/tmp` 里的探针，非正式环境）：
- 修前 `storage_candidates()` 返回 `[cli-cn, cli]`；修后返回 `[Trae CN, TRAE SOLO CN, Trae]`；
- 把 `~/.trae2codex/creds.json` 挪开后，`resolve_credential()` 仍能解析出
  `edition=Trae CN, source=desktop`，并成功拉到 22 个模型；
- `read_desktop_auth()` 现在能读出 `app_version='3.3.104'`、`build='2.3.87416'`；
- 线上 `/health`：`logged_in=true, session_alive=true`，账号「用户6781982309」。

## 根因 9：`~/.trae-cn/trae-jwt-token` 不是可用登录源，只是装饰

这两个 CLI 路径一直挂在 `storage_candidates()` 里，但从来没被读过：

- payload 是 RS256 JWT，`data` 里只有 `id / tenant_id / type / user_id`，
  **没有 access token 也没有 refresh token**；
- 拿整个 JWT 当 Bearer 打 `get_detail_param`，实测返回 0 个模型。

也就是说它们是死代码：列在候选里，要么被 `source != "desktop"` 跳过，
要么读出来也没有 token 可用。直接删掉，并留注释说明为什么不列。

## 新守卫

- `test_trae_login_source_paths.py`（5 条）：临时目录伪造一套安装树，
  `mock.patch` 掉 `_platform.app_support_dirs` 与 `tb.CREDS_FILE`（隔离真实凭据缓存），
  钉住基准目录不是 `<root>/Trae`、拼出的路径不带多余的 `Trae/` 级、
  桌面安装能被发现、`resolve_credential()` 能直接从 IDE 登录态解析出凭据、
  以及不再宣传任何 `trae-jwt-token` 登录源。反向对照已验证：
  把 `.parent` 去掉复现缺陷，5 条全红；还原后全绿。

顺带把本仓 `exec` 踩过的坑记在这里，省下次重复：
- `exec` 的入参是 **JS**，不是 JSON；`tools.exec_command` 必须传对象；
- 带空格的路径（`~/.../AI Shared/repo/FleetKit`）不能 `cd`，
  所有命令都要写成绝对路径 + 整体加引号；
- 单个 cell 跑超过 30s 会拿到**空**输出，长任务要轮询；
- `launchctl kickstart -k` 后要用 `ps -eo pid,lstart,command` 对比文件 mtime 才算生效。

