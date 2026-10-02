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


## 根因 10：ZCode 路由 A 把整段历史丢成最后一句话

`zcode_bridge._cli_ask_all()` 的路由 A 每次都开一个全新 CLI session，
这是**有意的**（桥无状态，靠调用方每次带全量历史）。它也确实把历史走了一遍：

    msgs = []
    for m in (payload_messages or []):
        ...
        msgs.append(text)
    prompt = msgs[-1] if msgs else ""     # <-- 前面遍历的结果只用最后一个

于是遍历出来的历史全被丢掉。Codex 每一轮都重发完整对话，
所以**第一轮之后每一轮都是在无上下文的情况下回答**——模型看不见之前说过什么。
这也让上面那句「Codex sends full history every time」的注释失去意义：
既然每次新建 session，就必须把历史重放进这一次性的 prompt。

实测证据（`kit/tools/test_zcode_cli_prompt.py`）：修前 `_cli_ask_all` 递给 CLI 的
prompt 只有 `what is my name?`，`my name is Ada` 不在里面。

修复：新增 `_cli_prompt(turns)`，把整段对话摊平成一次性的 prompt。
不变量有两条，都写进测试了：
- **最后一轮原样收尾**，这样 `"reply exactly: X"` 这类指令仍然是模型要回答的东西；
- 超长历史**从最旧的开始丢**（`CLI_PROMPT_MAX_CHARS = 24000` 字符预算），
  长历史退化成「只带最近几轮」，而不是把 CLI 上下文撑爆。

顺带把同一 handler 里的 `asyncio.get_event_loop()` 改成 `get_running_loop()`：
这里是 `async def` handler，取运行中的 loop 才是正确 API，前者在 3.12+ 已标记废弃。

## 新守卫

- `test_zcode_cli_prompt.py`（5 条）：单轮原样、历史被重放、最后一轮原样收尾、
  超长历史按预算从最旧的丢、以及 `_cli_ask_all` 真的把历史递给 CLI
  （用假 backend 记录收到的 prompt）。反向对照已验证：把 `_cli_prompt(turns)`
  换回 `turns[-1][1]`，5 条全红；还原后全绿。


## 根因 11：预算测试压在真实墙钟上，整套测试会无缘无故变红

`tools/test_chat_budget.py` 测的是「一次被卡住的上游只该花一个有界的时间」，
而 gemini / antigravity 两座桥的预算是墙钟算术：`deadline - time.time()`。
于是测试里用了**真实** `time.sleep()` 去「消耗预算」，再去断言剩下的额度：

    def fake_a(...):
        seen.append(("code-assist", deadline and deadline - time.time()))
        time.sleep(1.0)                  # 真的睡一秒
        raise ...
    ...
    assert 8.0 < seen[1][1] <= 9.0       # 只剩 1 秒窗口
    assert elapsed < 2.5

问题：`fake_a` 睡了 1.0s，断言却要求剩余的 9s 落在 `(8.0, 9.0]` 这个**1 秒宽**的窗口里。
机器一忙（跑全套测试、并发 agent 同时在改文件、后台编译），从记账到真正调 B 只要多花 2 秒，
`seen[1][1]` 就跌破 8.0 —— 于是**代码完全正常，测试却红了**。

实测：单独跑 `tools/test_chat_budget.py` 21/21 全过；跑全套时偶发 1 failed。
同类隐患全文件有 7 处，容差从 `abs=0.05` 到默认 `approx()`（约 1e-6）不等，
其中 `test_gemini_a_slow_token_refresh_is_charged_to_the_budget` 用的是无容差默认值，
等于要求分秒不差。

为什么值得当核心问题修：一个会假红的套件，比没有测试更糟 —— 真回归会被当成「又抖了」而被忽略。

修复：加一个 `_FakeClock`（`time` 模块的替身，`sleep()` 只推进假时钟、其余属性委托给真模块），
把这些测试改成 `monkeypatch.setattr(<bridge>, "time", clock)`。
真实睡眠全部消失，断言从「窗口」变成**精确值**，比原来更强：

    - `assert 8.0 < seen[1][1] <= 9.0`   ->  `assert seen[1][1] == pytest.approx(9.0)`
    - `approx(1.0, abs=0.1)`             ->  `approx(1.0)`
    - `approx(2.0, abs=0.2)`             ->  `approx(2.0)`
    - `approx(2.0, abs=0.05)`            ->  `approx(2.0)`
    - `approx(1.0, abs=0.05)`            ->  `approx(1.0)`

`elapsed` 那类「不能挂住」的断言保留，但上界从 1.5/2.5/3.0/1.2 统一放到 5.0 —— 
假时钟下没有任何真实等待，5 秒仍然抓得到真挂死，只是不再被机器负载误伤。

`test_gemini_an_overrun_still_leaves_the_fallback_a_floor` 里留了一处真实 `time.sleep(0.7)`：
它断言的是常量下限（超预算后必然落到 `FALLBACK_FLOOR`），与精确耗时无关，不受负载影响。

验证：连续 10 套 `pytest tools -q` **238 passed / 0 failed**；单套耗时 17s -> 9.4s。
另外核对过测试数量没变（16 个测试函数、参数化后 21 个用例，与 `git show HEAD:` 一致，无一丢失）。

## 根因 12：`--sync-only` 从不刷新 `install.sh`，安装根目录里留着陈旧副本

`--sync-only` 的复制面只有四棵树加三个文件：

    copy_tree bridges/ tools/ docs/ opencodex/
    copy_file README.md requirements.txt uninstall.sh

`install.sh` 在 kit **根目录**，不在这四棵树里，脚本自己也从不拷贝自己，
所以 `runtime/install.sh` 是个没人维护的孤儿。并发 agent 把 install.sh 里的
`pick_agy_oauth()` 泛化成 `pick_optional()`（Qwen / Antigravity 两类上游密钥都不再伪造），
kit 那份是新的，runtime 那份还是 18:32 的旧版。

踩坑的方式很具体：`tools/test_install_dry_run.py` 用 `KIT = dirname(__file__)/..` 定位
`install.sh`，也就是**自己旁边那份**。于是：

    cd kit     && pytest tools -q   ->  238 passed
    cd runtime && pytest tools -q   ->  3 failed（全是 install_dry_run）

同一个套件、同一份代码，换个目录跑就红。而这两套测试本来就是同一份文件的两份拷贝
（sync 会把 `kit/tools/` 复制到 `runtime/tools/`），所以这是纯粹的副本漂移，不是真回归。

修复：在 sync 块里补一行 `copy_file "${KIT_DIR}/install.sh" "${FLEET_HOME}/install.sh"`，
dry-run 的信息行也补上 `install.sh`。`copy_file` 本身幂等（`cmp -s` 相同就跳过），
in-place 安装时 src 与 dst 是同一条路径，也不会自己覆盖自己。

验证：同步后 `diff -q kit/install.sh runtime/install.sh` 一致，两个根目录下
`pytest tools -q` 都是 238 passed。

## 根因 13：CatPaw 的 `/health` 要 10 秒才应答，冷启动看着像桥挂了

实测线上桥（pid 2368，跑的是 17:20 的代码）：

    curl -m 6  /health  ->  HTTP 000 after 6.0s    # 像是死了
    curl -m 30 /health  ->  HTTP 200 after 10.1s

handler 里顺序 ping 三个上游，每个 5s 超时：

    for base in (BASE, MCOPILOT, PUBLIC_BASE):
        st, body, _ = http_req(base + '/api/ping', headers={}, method='GET', timeout=5)

而这个网络里 `BASE`（catpaw.sankuai.com）和 `MCOPILOT` 都不可达，每次失败要等
~2.6s（等代理隧道放弃），三个 base 就是 ~10s。更糟的是 `get_token()` 接着又拿
同一个三个 base 去校验凭据，再花 ~7.8s。

**先说清楚影响面**（一开始我看窄了，纠正一下）：fleet 的三个探针
`verify_real_calls.py`、`fleet_probe.py`、`status_ui.py` 走的都是 `/v1/models`，
而 catpaw 的 `/v1/models` 是缓存优先的 `list_models()`，微秒级返回，
所以**从来没有哪座桥被它们误判过**。真正付代价的是人 —— 手动 curl `/health`
看起来就是桥挂了；以及刚重启那一下的第一个请求，因为 `get_token()` 的校验结果
会缓存 1500s。

修复分两步，都不改语义，只改顺序和并发：

1. `probe_reach()`：三个 ping 改成并发（各一个线程 + `join`），代价从「三个之和」
   变成「最慢那一个」；完整结果按 `REACH_TTL`（默认 20s）缓存，面板轮询不重复付费。
   实测：3.01s 冷、0.0007s 之后每次。
2. `get_token()` 的 base 顺序改为 `bases_in_order()`，仍然三个都会走、
   只改先后。两处提示：
   - 进程内记住上次成功的 base（`_TOKEN_BASE`），下次直接命中；
   - `/health` 里 `probe_reach()` 已经跑在 `get_token()` 前面，
     它探明的 200 host 直接作为首选，所以**重启后**也受益，
     不必等进程内记忆（进程内记忆重启即失忆，第一版就栽在这里：
     冷延迟只从 10.9s 变成 10.7s，因为记忆是空的）。

实测（重启后连续三次 curl）：

    call1=200 3.374s      # 冷：并发 ping 一个 3s + 命中可达 base 的 token 校验
    call2=200 0.0016s     # 命中 reach 缓存
    call3=200 0.0013s

从 10.9s 降到 3.37s。剩下的 3s 就是那个可达 base 自己的超时，属于必要成本。

## 新守卫

- `test_catpaw_health_latency.py`（3 条）：三个 ping 必须真的重叠（记录同时在飞的
  请求数）、总耗时必须小于最慢单个 ping 的三倍、以及两次调用之间只探一次（TTL 生效）；
  同时钉住输出形状仍是 `host -> [status, label]`。
- `test_catpaw_token_base_order.py`（5 条）：`bases_in_order()` 去重且保留三个、
  无记忆时就是文档顺序、成功过的 base 下次单独命中、只认另一个 base 时仍能走通链路、
  以及 reach 探明的 200 host 排最前。
  反向对照已验证：把 `bases_in_order()` 换回固定三元组、或去掉 reach 提示块，
  对应测试立刻红；还原后全绿，且还原后与备份字节一致。

## 根因 14：CatPaw 生成了桥密钥却从不校验

`install.sh` 会铸 `CATPAW2CODEX_KEY`、写进 `runtime/fleet.env`、
并在 `com.local.catpaw2codex.plist` 里导出 —— 而 `catpaw_bridge.py` 一次都没读它。
十二座桥全都调 `check_bridge_auth(request)`，只有这座 `BaseHTTPRequestHandler` 写的桥
压根没有鉴权。实测：不带任何头 `GET /v1/models` 返回 200，`POST /v1/chat/completions`
直接进去花运营者的美团额度。

影响范围说清楚：监听在 `127.0.0.1`，所以暴露面是同机进程和本机其他用户，不是公网 —— 
这正好是其他十二座桥已经接受并为之付成本的同一信任模型。问题在于这个密钥被生成、
被部署、然后被无视，运维会以为它起着作用。

修复：`BRIDGE_KEY = os.environ.get('CATPAW2CODEX_KEY') or ''`，
加 `H._check_key()` / `H._deny()`，契约与 `_common.check_bridge_auth` 一致 —— 
密钥未设置时保持开放，设置了就要求精确的 `Authorization: Bearer <key>`。
`/v1/models` 与 `/v1/chat/completions` 都要过；`/health` 仍然放行，
因为所有 FastAPI 同胞的 `/health` 都是放行的（它是存活探针，fleet 工具不带 key 调它）。

实测：无 key 401、错 key 401、对 key 200、`/health` 无 key 仍 200。

## 根因 15：CatPaw 每次 `/v1/models` 轮询都起一个后台刷新线程

`list_models()` 的缓存分支每次都 `threading.Thread(target=_bg_refresh).start()`，
而 `_bg_refresh()` 第一件事就是 `get_token()` —— 它要拿 `ST['lock']` 并走上游 host。
于是每一次探针轮询都留下一个线程，全堵在同一把锁上。

`/v1/models` 正是探针打的端点：`status_ui` 轮询它、`fleet_probe` 每几分钟跑一次、
`verify_real_calls` 按需跑。实测：连打 4 次 `/v1/models` 之后，
`/health` 要 10.7s，而它自己的两个组件单独量只有 3.7s（`probe_reach` 3.01s + `get_token` 0.66s）—— 
差额就是排在那些线程后面等 `ST['lock']`。

修复：`_spawn_bg_refresh()` 做 single-flight，同一时刻最多一个在飞，
跑完再把守卫打开（不会变成"只跑一次"）。刷新是幂等的、结果有缓存，一个就够。

实测：同样连打 4 次 `/v1/models`，`/health` 从 10.7s 降到 5.5s
（3.01s 探针 + 等那一个在飞刷新 + 自身 0.66s，基本对得上）。
没有继续深挖 `get_token()` 持锁做网络 I/O：那是认证路径的深层改动，风险不划算。

## 新守卫

- `test_catpaw_bridge_auth.py`（5 条）：密钥确实从环境读、`/v1/models` 无 key 401、
  错 key 401、对 key 200、`/health` 保持开放。反向对照：把 `_check_key` 改成永远返回 True，
  5 条立刻红。
- `test_catpaw_single_background_refresh.py`（2 条）：连打 10 次轮询只起一个刷新；
  刷新跑完之后守卫重新打开（不会永久锁死）。反向对照：把 `_spawn_bg_refresh()`
  换回每次新建线程，2 条立刻红。

## 根因 16：gemini 与 antigravity 也从不校验桥密钥（根因 14 的同类，共三座）

修完 catpaw 之后做了系统性排查，把「哪些桥真的在校验密钥」逐个查清。
`fleet_probe.py` 的注释写明了这个不变量：

    # local token each bridge checks, as found in its launchd plist
    KEY_ENV = {...}

`install.sh` 为**每一座**桥铸密钥、写进 `runtime/fleet.env`、并在各自 plist 导出。
但 13 座桥里有 3 座从来没读过自己的那份：catpaw（已修）、gemini、antigravity。
后两座与 catpaw 是同一形状（`BaseHTTPRequestHandler` + `do_GET`/`do_POST`），
所以是同一处疏漏被复制了三遍。

实测（修前）：

    gemini       GET  /v1/models  无 key -> 200（模型目录随便看）
    antigravity  GET  /v1/models  无 key -> 200
    antigravity  POST /v1/chat   无 key -> 502，耗时 1.77s

最后一条最值得说：502 说明请求**已经打到上游**才失败，
也就是未鉴权调用者能消耗运营者的 Google OAuth 刷新额度，而不是被就地拒绝。

修复与 catpaw 完全一致（`BRIDGE_KEY` + `_check_key`/`_deny`，契约同
`_common.check_bridge_auth`：未设置保持开放，设置则要求精确的 Bearer）。
`/v1/models` 与 `/v1/chat/completions` 都要过，`/health` 仍放行。
新增 `tools/test_http_bridge_auth.py` 用一个文件同时钉住这两座，
顺带也成为整座 `http.server` 家族的守卫。

安全性上说清楚影响面：三座桥都只听 `127.0.0.1`，所以暴露面是同机进程和本机其他用户，
不是公网 —— 与其他十座已经付这笔成本的桥同一信任模型。
问题在密钥被生成、被部署、然后被无视，运维会以为它起着作用。

修后实测：两座桥无 key / 错 key均 401、对 key 200、`/health` 无 key 仍 200；
`fleet_probe` 与 `verify_real_calls` 本来就带 key，无需改动，复测全绿。

## 根因 17：xhx 用量台账测试里硬编码日期，跨夜自动变红

`tools/test_xhx_usage_ledger.py` 的 `_row()` 默认 `day="2026-09-29"`，
而 `ledger.record()` 用 `datetime.now()` 打时间戳。于是：

- `test_a_call_is_counted_per_model` 用 `record()` 写当天行，却按 `day="2026-09-29"` 汇总；
- `test_a_corrupt_line_is_skipped_not_fatal` 写硬编码日期的行，`summarize()` 默认汇总今天。

9-29 当天两者恰好一致所以全绿；日期一翻页就红，与代码无关。
这与根因 11（测试压真实墙钟）是同一类：**套件为与代码无关的原因变红**，
而比没测试更糟的正是这种假红。

修复：加 `_today()`（取 `summarize()` 默认的同一天），`_row()` 的 day 改为默认 None -> 今天；
`test_only_todays_rows_are_summarised` 改成由今天推昨天，不再写死字面量。
另外全仓扫了一遍 `2026-` 日期字面量：其余命中全在 docstring 的溯源记录和
`free_models.json` 的数据字段里，没有逻辑依赖，不需要改。

## 新守卫

- `test_http_bridge_auth.py`（6 条，参数化覆盖 gemini + antigravity）：密钥确实从环境读、
  `/v1/models` 无 key 401、错 key 401、对 key 200、`/v1/chat/completions` 无 key 401、
  `/health` 保持开放。反向对照：两座桥的 `_check_key` 都改成永远返回 True，6 条立刻红。
- `test_xhx_usage_ledger.py` 现在与日期无关，任何一天跑都一样。

## 根因 18：装机脚本会把 Codex 默认模型钉到一个刚被跳过的 provider

`opencodex/setup-providers.sh` 里，stepfun provider 的注册是有条件的：

    if [ -n "${STEPFUN_PLAN_API_KEY:-}" ]; then
      run ocx provider add stepfun ...
    else
      echo "  (STEPFUN_PLAN_API_KEY not set; skipping stepfun plan api)"
    fi

而紧接着的默认模型钉定是**无条件**的，且硬编码回退值正是 stepfun 的模型：

    DEFAULT_MODEL="${FLEET_DEFAULT_MODEL:-stepfun/step-5-preview}"
    ... 写入 ~/.codex/config.toml 的 model = ...

于是在「既没设 `FLEET_DEFAULT_MODEL`、又没设 `STEPFUN_PLAN_API_KEY`」的机器上，
脚本一边打印 skipping stepfun plan api，一边把
`model = "stepfun/step-5-preview"` 写进 config.toml —— Codex 打开时停在一个
**没有注册**的 provider 上。行为级实测（dry-run + 临时 fleet.env + 一次性 HOME）：

    (STEPFUN_PLAN_API_KEY not set; skipping stepfun plan api)
    [dry-run] pin stepfun/step-5-preview as the default model in .../config.toml

本机之所以没暴露：`FLEET_DEFAULT_MODEL=trae/trae-step-5-preview` 且 key 有值，
两条分支都健康。这是**潜伏缺陷**，只在干净的新机器上发作。

修复：记录 stepfun 是否真的注册成功（`STEPFUN_REGISTERED`），
只有在「Operator 显式覆盖」或「stepfun 确实注册了」时才钉默认模型；
两者都不成立就告警并保持原样，而不是钉一个不存在的 provider。
Operator 的显式覆盖永远优先，不受 stepfun 条件影响。

## 根因 19：重装会静默丢掉 Operator 的默认模型覆盖

同一个区域里还挖出相关的第二个问题。`install.sh` 的 `emit_fleet_env()` 是
**从零重写** `fleet.env`，只从旧文件里抢救三行 Operator 自有的密钥：

    preserved="$(echo "$EXISTING" | grep -E '^(HOMEBREW_PYTHON|TOKENDANCE_API_KEY|STEPFUN_PLAN_API_KEY)=' || true)"

`FLEET_DEFAULT_MODEL` 不在其中，也不在新生成的清单里。于是任何一次重装，
Operator 设的默认模型就被静默丢掉，回落到上面那个硬编码兜底值 —— 
选择器默认模型悄悄变掉，没有任何提示。密钥被抢救、设置却不被抢救，说不通。

修复：把 `FLEET_DEFAULT_MODEL` 加进保留正则。`${preserved}` 追加在生成文件末尾，
source 时后者生效，所以保留行会正确覆盖。

顺带说明：dry-run 走 `emit_fleet_env > $TMPENV` 这条链，所以它同样看不到
`FLEET_DEFAULT_MODEL`。修好之后 dry-run 反而更诚实了 —— 它会告警而不是
打印一个真实安装不会执行的钉定动作。

## 新守卫

- `test_default_model_pin_gating.py`（4 条）：
  - 两个条件都没有时，**不得**出现 `pin stepfun/step-5-preview`（且仍打印 skipping）；
  - 有 `FLEET_DEFAULT_MODEL` 覆盖时，即使没有 stepfun key 也要钉该覆盖值；
  - 有 stepfun key 时，回退值照旧钉；
  - `emit_fleet_env` 的保留正则必须包含 `FLEET_DEFAULT_MODEL`。
  反向对照已验证：把 `DEFAULT_MODEL` 换回无条件、或把 `FLEET_DEFAULT_MODEL`
  从保留正则里去掉，对应测试立刻红，还原后全绿且字节一致。

## 根因 20：状态面板把一座健康的 fleet 报成全线阵亡

`tools/status_ui.py` 解析 fleet root 时，`--home` 和 `FLEET_HOME` 都没有给，
就回退到**一个**硬编码猜测 `~/fleet`；而它自己的 `--home` 帮助文本写的是
「default ~/FleetKit/runtime」—— 两者本来就不一致。

launchd 服务总是带 `--home`，所以只有**手工**跑 `status_ui.py --once` 会踩到。
而本机的实际安装在 `~/AI Shared/repo/FleetKit/runtime`，两个猜测位置都不存在，于是：

- 读不到 fleet.env，探测全部不带 Authorization 头；
- `models` 28、`probe_ok` **2**/12 —— 而带上正确的 `--home` 是 121、**12**/12；
- 12 条「HTTP 401 且 key 未读取到 - 登录后执行 bash ~/fleet/bridges/finish.sh <桥>」,
  每条都指向一个**不存在**的路径。一座健康的 fleet 看起来像死了。

修复三处：
1. `_default_home()` 按文档位置的顺序试（`~/FleetKit/runtime` 然后 `~/fleet`），
   取第一个真有 fleet.env 的。**每次调用时求值**而不是导入时算 —— 第一版写在模块层，
   结果 `HOME` 后来改了不生效，是测试抓出来的。
2. 读不到 fleet.env 时，告警说清补救办法（`--home DIR` / `FLEET_HOME` /
   plist 里登记的正确值），而不是只讲「key 未知」。
3. fleet.env 整体缺失时，不再每座桥重复一条「请登录」—— 第一条已经说清
   所有 key 都未知，重复 12 遍只会让人以为要去登录 12 次。

实测：不带 `--home` 时告警从 12 条降到 1 条且可操作；带 `--home` 时
12/12 探测通过、121 个模型，与真实状态一致。

## 新守卫

- `test_status_ui_default_home.py`（4 条）：文档位置有 fleet.env 时优先选它、
  旧位置 `~/fleet` 仍会试、两者都没有时告警必须提到 `--home`、
  显式 `--home` 永远优先。反向对照：把候选退回落单个 `~/fleet`，
  前两条立刻红，还原后全绿且字节一致。

## 根因 21：第 13 座桥 zcode 在三张运维表里根本不存在

跑 `verify_real_calls.py --json` 做全 fleet 权威核验时发现判定只有 12 行，
而 `install.sh` 的注册表里有 13 座桥。逐文件数 `zcode` 的出现次数，结论很干净：

    有 zcode：install.sh / fleet_probe.py / status.sh / finish.sh
    无 zcode：status_ui.py / verify_real_calls.py / catalog_filter.py

`status_ui.py` 的桥表上方还写着「Keep in sync with tools/status.sh,
bridges/finish.sh and deploy.sh」—— 这条同步要求对 zcode 是违反的。
规范定义（status.sh / finish.sh）里 zcode 是 `zcode2codex`，offset 13（端口 8800）。

后果是静默的，而且都在运维面上：

- 面板 `bridges` 显示 12 而不是 13，zcode 没有任何一行状态；
- `verify_real_calls.py` 从不核验 zcode，所以判定快照里**没有它的 verdict**；
- `catalog_filter.py` 的候选集来自面板（`bridges` 字段），zcode 不在其中，
  于是它既不会被判 available 也不会被判 unavailable —— 它的行能不能留在
  Codex 选择器里全凭运气，不靠任何判定。
- 注释里那句「Verified bridges are 12」也跟着过期。

**一座没人计的桥，就是一座坏了也没人知道的桥。**

修复：把 `("zcode", "zcode2codex", 13, ...)` 补进 `status_ui.py` 与
`verify_real_calls.py` 的桥表，并订正 `catalog_filter.py` 的过期注释。

实测：面板从 12 座变 **13 座**、`models` 121 变 **123**、`probe_ok` **13/13**，
zcode 有了自己的行；核验器判定从 12 行变 **13 行**，zcode 出现在表内
（当前判定 `BRIDGE_DOWN`，因为它自己的 captcha 通道是坏的 —— 这是已知外部阻塞，
但至少现在它被如实地计为一座坏桥，而不是一座不存在的桥）。

## 根因 22：核验器的聊天超时比某些桥自己的预算还短

`verify_real_calls.py` 用统一的 `PROBE_CHAT_TIMEOUT = 70` 探每一座桥。
但 zcode 走官方 CLI（`ZCODE_CLI_TIMEOUT` 默认 180s）且要先 mint 阿里云滑块
（最多 75s），也就是说它**正常工作时**首次调用就可能超过 70s。
探针比桥先放弃，读出来的就是 `BRIDGE_DOWN`（超时）而不是真实原因，
运营者看到「桥没连」而不是「上游风控/额度」—— 这正是本仓反复在修的那类误导。

修复：加 `PROBE_CHAT_TIMEOUT_OVERRIDE = {"zcode": 200}`（比桥自己的 180s 预算更长），
`chat()` 增加可选的 `timeout` 参数。**只有真有覆盖时才传这个 kwarg** —— 
第一版无条件传，结果弄坏了 10 个测试：它们用更窄的 lambda 冒充 `chat()`，
多一个关键字就 `TypeError`。保持原有 12 座桥的调用签名逐字节不变，
既修了超时也不破坏既有契约。

副作用（记录下来，不是缺陷）：核验器整套耗时从 96s 涨到 298s，
因为多了一座桥、而这座桥的探测本身就要跑满它的预算。探针是按需运行的，可接受。

## 新守卫

- `test_bridge_tables_agree.py`（3 条）：把 `status_ui.py`、`verify_real_calls.py`、
  `status.sh`、`finish.sh` 四处的 `(名字, label, offset)` 三元组钉成完全一致，
  并核对 `fleet_probe.py` 的端口等于 `PORT_BASE + offset`，
  以及 `("zcode", "zcode2codex", 13)` 在每一处都存在。
  反向对照：从 `status_ui.py` 删掉 zcode 行，三条立刻红。
- `test_probe_timeout_override.py`（2 条）：覆盖表里的每一项都必须比默认超时更长、
  且必须是探针认识的桥；以及**没有覆盖的桥必须继续用不带 kwarg 的历史调用签名**。
  反向对照：把覆盖表清空，立刻红。

## 根因 23：`fleet_chat_test.py` 是第四张漏掉 zcode 的桥表

修完根因 21 之后继续按同一把尺子量其余文件，发现 `tools/fleet_chat_test.py`
（全 fleet 聊天冒烟）也有一份自己的 `BRIDGE_NAMES`，同样只有 12 条、不含 zcode。
也就是说漏掉第 13 座桥的表一共是**四张**，不是三张：

    status_ui.py / verify_real_calls.py / catalog_filter.py / fleet_chat_test.py

这张表的后果最直接：`fleet_chat_test.py` 是给人在改动之后快速冒烟用的，
它不测 zcode，于是「改完跑一遍 fleet_chat_test 全绿」这句话对 zcode 是空的。

修复：补 `('zcode', 13, 'zcode/GLM-5.3-Flash')`，并把守卫测试扩到覆盖它。

守卫测试（`test_bridge_tables_agree.py`）现在同时钉住五处：
`status_ui.py`、`verify_real_calls.py`、`fleet_chat_test.py`、`status.sh`、`finish.sh`。
其中 `fleet_chat_test.py` 只有 `(名字, offset, 模型)` 三元组、没有 label，
所以测试分两层：先用 `(名字, offset)` 比对所有五处，
再对有 label 的那几张额外比 `(名字, label, offset)`。
参考基准取 `status.sh` —— 它和 `finish.sh` 一起构成这个仓里最早的规范定义。

反向对照：从 `fleet_chat_test.py` 删掉 zcode 行，守卫测试立刻红，
还原后全绿且字节一致。

顺带确认没有缺陷的两处：
- 每日签到链路是好的。LaunchAgent `com.local.fleet-checkin` 排在 09:00，
  当前是 09-30 00:59，所以面板 `checkin_ok_today: 0` 是**正确**的（今天还没到点）；
  状态文件里两个任务都记着 09-29 成功。之前看到 0 是因为 home 猜错，已在根因 20 修掉。
- 全仓扫 `TODO|FIXME|XXX|HACK|临时|待修`，唯一命中是 `mktemp` 模板里的 `XXXXXX`，
  没有作者留下而未处理的已知问题。

## 根因 24：codely `/v1/models` 把最常见的故障裸抛成 500，专门写的降级目录一次都没生效过

`list_models` 里本来就有一条完整的降级路径：上游拿不到目录时，返回
`FALLBACK_MODELS` 那 5 行静态目录，并在响应头 `X-Codely-Models-Fallback` 里
带上原因。问题在于这个 handler 的 `try` 只抓了 `HTTPException`：

    except HTTPException as e:
        detail = e.detail

而 `httpx.ConnectError`（以及它的兄弟 `ReadTimeout`、`ConnectTimeout`、
`ProxyError`）一个都不被接。codely 的上游 `codely-litellm.tuanjie.cn` 在公司
内网，VPN 掉线、主机不可达是**最常见**的故障，偏偏这一种直接从两层 `with`
里穿出去：

- 客户端拿到一个裸的 `500 Internal Server Error`；
- 日志里堆 traceback。实测 `codely.log` 累计 **291KB** 全是 `ConnectError`，
  其中请求行 `" 500 ` 共 **26** 条；
- 面板那一行 `{"ok": false, "http": 500, "count": 0, "models": []}`，
  `probe_ok` 从 13 掉到 12。运营者看到的是「目录挂了、模型也没了」，
  而真实原因只是网络到不了网关。

也就是说，作者写好的降级能力，因为少一个 `except`，在生产里**一次都没跑过**。

修复（`kit/bridges/codely/codely_bridge.py:415`）：补一条兜底，把异常折成和
上游错误码同一条分支的 `detail`，之后照旧走 `FALLBACK_MODELS` + 响应头：

    except Exception as e:  # noqa: BLE001 - an unreachable gateway must degrade
        detail = "%s: %s" % (type(e).__name__, str(e)[:200])

头值继续交给 handler 原有的 `_common.safe_header_value` 处理（上游报错文本
里的换行会让 h11 直接掐掉整个响应，这个坑在更早的根因里已经踩过一次）。

验证：

- 新测试 `test_models_route_degrades_when_the_gateway_is_unreachable`
  （`test_codely_recovery.py`）：把 `ConnectError` 注进 `list_models` 的上游调用，
  断言返回 **200**、5 行降级目录、`X-Codely-Models-Fallback` 头非空。两棵树各 6 passed。
- 反向对照：删掉这 8 行 → `1 failed, 5 passed`；还原 → `6 passed`，且字节一致还原。
- 实测「文件修好」到「真的生效」这一段：codely 进程 78542 起于 01:10:45，
  不早于 runtime 文件的 mtime 01:10:45；`codely.log` 里最后一条 `Uvicorn running`
  在第 4819 行，其后 **0 条 `" 500 `、0 条 traceback**。重启后带
  `CODELY2CODEX_KEY` 连打三次 `/v1/models`，145ms / 41ms / 33ms 全 200，
  而且这次返回的是真实上游目录（`is_alias: true` 那些字段）—— 网关这会儿通了，
  反过来证明之前那批 500 是内网可达性抖动，不是目录或模型真的没了。

一条值得记下来的教训：第一次反向对照跑错了命令，写成 `python test.py` 而不是
`python -m pytest test.py`。那个文件没有 `__main__` 守卫，脚本模式只定义函数
然后返回 0，看着像通过。**反向对照必须用和正式运行完全相同的命令**，
否则它验证的是自己。

**一个 `except` 之差，就是降级目录可用和全 fleet 少一座桥的差别。**

## 根因 25：chat 路由留着根因 24 的同款洞，「已经包在 except 里」这个结论下快了

根因 24 修 `/v1/models` 的时候，顺手把同一份文件里的 chat 路由也读了一遍，
当时的结论是「已经裹在 `try/except Exception` 里，不用动」。这个结论错了，
错在只看见了一个 `except` 存在，没看它裹住的范围。

chat 路由的真实形状是：内层那个 `except Exception` 只包着重试分支里的 header
构造，而 `get_gateway_key()`——真正去网关换 virtual key 的那次 HTTP 调用——
在任何一个 `try` 的外面。于是网关不可达时 `httpx.ConnectError` 从
`codely_bridge.py:481` 一路穿到 Starlette，客户端拿到裸的
`500 Internal Server Error` 加一段 traceback。

日志把这件事量化得很干净（`/tmp/fleet-logs/codely.log`，全文件 4844 行，
按路由拆那 26 条 `" 500 `）：

- `GET /v1/models` —— 25 条，根因 24 的现场；
- `POST /v1/chat/completions` —— 1 条。

只有 1 条，因为自动化探测只打目录和 `/health`，没人用 chat 路由打桥。但对人
来说这 1 条就是「我在写代码，桥突然 500 了」，原因只是 VPN 抖了一下。两个数字
的差别是探测覆盖率，不是缺陷严重程度。

修复分两步（`kit/bridges/codely/codely_bridge.py`）：

1. `306` 行提一个模块级助手，把「网关不可达」折成和根因 24 同一条
   `upstream_error_response` 分支，503 + 结构化信封：

       def _unreachable(exc: BaseException) -> JSONResponse:
           detail = "%s: %s" % (type(exc).__name__, str(exc)[:200])
           return _common.upstream_error_response(
               503, detail, "gateway", "codely_upstream_unreachable",
               message="codely gateway unreachable: " + detail)

2. chat 路由从 `481` 行 `key = await get_gateway_key()` 到 401 重试结束整段
   收进 `try`，`except HTTPException: raise` 保证既有语义优先，最后一条兜底
   接住其余一切：

       except HTTPException:
           raise  # whitelist 400s and mint-endpoint 401s keep their own shape
       except Exception as e:  # noqa: BLE001 - the company gateway drops routinely
           return _unreachable(e)

白名单 400 必须还是 400：模型不在白名单、请求体不合法，是调用方的错，不能因为
「顺手统一了上游故障」被抹成 503。这一条单独写成了测试。

验证：

- `test_codely_recovery.py` 新增两条（文件 250 行，两棵树各 8 passed）：
  `test_chat_route_degrades_when_the_gateway_is_unreachable` 断言 503 + 信封；
  `test_chat_route_keeps_the_whitelist_400_when_the_gateway_drops` 断言网关
  挂掉时白名单 400 原样返回。
- 反向对照：整段改回去 → `1 failed, 7 passed`；还原 → `8 passed`，字节一致。
- 「文件修好」到「真的生效」：同步 6 个文件，`launchctl kickstart -k` 重启 codely，
  进程起于 01:26:34，晚于 runtime 文件 mtime 01:26；带 `CODELY2CODEX_KEY` 打
  `/v1/models` 三次全 200，返回的是真实上游目录（网关这会儿是通的——反过来证明
  那批 500 是内网可达性抖动，不是目录或模型真的没了）。`codely.log` 最后一次
  `Uvicorn running` 在第 4843 行，其后 **0 条 `" 500 `、0 条 traceback、
  0 条 ConnectError**。两棵树各 279 passed。

**「有个 except」不等于「包住了对的那一段」。读覆盖范围，别数 except 的个数。**

## 根因 26：同一个洞不止 codely 有，fleet 里 9 座 FastAPI 桥一个守卫都没装

修完同一座桥的第二个洞，该问的是这一族在别处长什么样。答案是：会裸抛 500 的
只有 FastAPI 这一族 9 座桥（codely、lingxi、xhx、zcode、qwen、trae、qoder、
cline、workbuddy），而它们一处 `httpx.HTTPError` 异常处理器都没有。它们的上游
全是第三方 HTTP——公司内网、美团内网、要 captcha 的 mint 端点——没有一个敢
承诺「永远可达」，任何一处抖动都会变成裸 500 加 traceback。

先把不受影响的一族排除掉，省得下次重新查：antigravity、gemini、catpaw 是
`BaseHTTPRequestHandler` + `urllib`，`do_POST` 整体裹在宽 `except Exception` 里；
它们日志里的 traceback 是 `BrokenPipeError`（客户端断连，见
`antigravity.log:1263`），跟上游不可达是两回事，不会变成裸 500。

全 fleet 历史裸 500 的账（各 bridge log 汇总，截至本次会话）：

- `codely.log` 26 条（25 `GET /v1/models` + 1 `POST /v1/chat/completions`）；
- `lingxi.log:273` 1 条、`xhx.log:241` 1 条。

后两条不是上游洞，是根因 1 的 `NameError: StreamingResponse 未导入`，早已修掉
并由 `test_no_undefined_names.py`（作用域感知的未定义名审计）持续把着。本次会话
又独立跑了一遍同一把尺子扫全部 bridges 与 tools：**0 个未定义名**。把这 2 条刨开，
「上游不可达 → 裸 500」这一族全部集中在 codely 一座桥上——不是因为它特殊，
只是因为它的网关在内网、最常抖。

修复（`kit/bridges/_common.py`）：`make_app` 这个共享构造本来就在，守卫只需要
加在它里面——这一处修改当场覆盖 6 座已经在用 `make_app` 的桥（qoder、qwen、
lingxi、xhx、trae、zcode），剩下 2 处漂移（workbuddy、cline 自己写了
`app = FastAPI(...)`）收编回来，共 9 座：

    def install_upstream_guard(app: FastAPI) -> None:
        @app.exception_handler(httpx.HTTPError)
        async def _upstream_unreachable(request: Request, exc: httpx.HTTPError):
            detail = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            return upstream_error_response(
                503, detail, "upstream", "upstream_unreachable",
                message="upstream unreachable: " + detail)


    def make_app(title: str, version: str = "") -> FastAPI:
        """The app = FastAPI(...) line, identical in the FastAPI bridges."""
        if version:
            app = FastAPI(title=title, version=version)
        else:
            app = FastAPI(title=title)
        install_upstream_guard(app)
        return app

这里最关键的性质是**只兜没人接的情况**。Starlette 的类异常处理器只在异常穿透
路由函数之后才运行，所以路由自己已经表达过的失败一律优先——白名单 400、401
重试、降级 200，守卫一条都不会盖。workbuddy 和 cline 需要额外把 bridges 目录
插进 `sys.path` 才能 `import _common`。

验证：

- 新守卫 `kit/tools/test_upstream_unreachable_guard.py`（115 行，3 passed）：
  `test_unreachable_upstream_becomes_503_not_500` —— 两条路由注入
  `httpx.ConnectError`，都是 503 + 信封而非 500；
  `test_the_guard_leaves_the_route_own_decisions_alone` —— 路由自己抛的
  `HTTPException` 400 原样保留（守卫不许越过路由），`ValueError` 仍照旧逃逸
  （守卫不许把非上游异常吞成 503）；
  `test_every_bridge_app_is_built_through_common` —— 源扫描：bridges 下每个
  模块级 `app =` 都必须经过 `_common.make_app(` 或 `install_upstream_guard`，
  下限 9。这条是给第十座桥上的闸，谁再写 `app = FastAPI(...)` 测试立刻红。
- 反向对照：把 `cline/cline_bridge.py:147` 退回
  `app = FastAPI(title="cline2codex")` → 扫描守卫立刻红
  （`1 failed, 2 passed`）；还原 → 3 passed，字节一致。
- 生效链路：同步 6 个文件；重启 codely、cline、workbuddy、workbuddy-gpt 四座桥
  （标签就是这四个，没有 `-cn` 变体），进程均起于 01:26:34，晚于 mtime 01:26；
  codely 带 key `curl` 打 `/v1/models` → 200 真实目录；`cline2codex.log`、
  `workbuddy.log`、`workbuddy-gpt.log` 均正常起服务。两棵树各 **279 passed**
  （kit 与 runtime 同数：274 − 6 + 8 + 3）。

**一次修一个文件是补救，把重复的那一行收进公共构造才是止损；再挂一条源扫描测试，
第十一座桥就没机会重犯。**

---

## 根因 27：单文件改完没重启，进程跑的还是旧代码（xhx 晚 60 秒）

`/health` 绿不代表跑的是新代码。`runtime/bridges/xhx/usage_ledger.py` mtime
`22:10:36`，xhx 进程（pid 自然就是当时那个）启动于 `22:09:36`——代码比进程
**晚 60 秒**落盘，而进程只 `record/summarize/_trim` 常驻内存，账本照样写、
格式照样认，唯一差别是 `_trim` 的 2MB 封顶不会生效。这类事故 0 报错、0 告警，
只能靠「进程启动时间 vs 代码 mtime」对账。已用
`launchctl kickstart -k gui/501/com.local.xhx2codex` 重启修复（新进程
`01:43:28`，晚于代码）。

单文件晚于进程 60 秒只是警钟：真正的大面积事故是下一条——**共享模块**。

## 根因 28：共享模块改完只重启了 4 座桥，9 个 importer 里 5 座跑着旧守卫

`runtime/bridges/_common.py`（根因 26 的 `install_upstream_guard` 所在）
`01:26:22` 同步落地，当晚重启的只有 codely(1246)、cline(1248)、
workbuddy-cn(1251)、workbuddy-gpt(1253) 四座，进程起于 `01:26:34`，确认生效。
但 `import _common` 的一共 9 座桥，另外 5 座的进程都是**UTC+8 当晚早些时候**
起的，全部早于 `_common.py`：qoder(pid 50322, `18:38:22`)、qwen(77530,
`19:26:24`)、lingxi(69632, `21:48:16`)、trae(32265, `22:46:50`)、
zcode(40256, `22:57:18`)。也就是根因 26 的上游守卫在这 5 座里**根本没加载**，
它们的上游故障仍在报 500。workbuddy 本体没有进程，代码由 cn/gpt 两座经
`sys.path` 加载（见下），也算 importer。

只按 `bridges/<name>/` 自己的目录判断会漏掉两种加载形态，这正是本次工具要
解决的核心：

- **兄弟目录**：workbuddy-cn/gpt 的 `converter.py` 把 `bridges/workbuddy/`
  插进 `sys.path` 后 `from core import main`，命令行里根本不出现共享目录。
- **根级共享模块**：`bridges/_common.py`、`bridges/_platform.py` 被一批桥
  直接 import，改一个文件等于改一批桥。

fix：kickstart 5 座（antigravity/gemini/catpaw 判定未陈旧，不重启——它们
import 的是 `_platform`（mtime `09-28 18:15:44`），自身目录代码也都不晚于各自
进程）：

```bash
for label in qoder2codex qwen2codex lingxi2codex trae2codex zcode2codex; do
    launchctl kickstart -k gui/501/com.local.$label
done
```

并落地守卫工具 `kit/tools/bridge_freshness.py`：三种形态（目录自身 / 兄弟目录
经 sys.path / 根级共享 import）取最大 mtime 与进程启动时间比较，容忍 30 秒
时钟噪声；有陈旧退出码 1。写入/改动 bridges/ 后先跑它再收工。

验证：

- 新测试 `kit/tools/test_bridge_freshness.py`（14 passed），逐案复刻真实事故：
  代码晚于进程 60s 判 STALE（xhx 案）；`_common.py` 晚于 5 个 importer 进程、
  restarted 的 codely 判 OK（28 主案）；非 importer 不误报；`__pycache__`/
`auths`/`VERSION` 不算代码；无进程目录判 INFO 不失败；`run()` 退出码与
  `--json`；`ps` 坏行跳过；`resolve_home` 挑进程多的家目录。
  两棵树各 **293 passed**（kit 279 + 14，runtime 同数）。
- 反向对照：把 `newest_loaded` 里的 shared 归因抹掉
  （`shared = {}`）→ `test_shared_module_stales_every_importer` 立刻红
  （1 failed, 13 passed）→ 还原后字节一致（`cmp` 通过），14 passed。
- 误报收紧：antigravity 的 `os.path.expanduser('~/.gemini/...')` 曾被判成经
  gemini 目录加载；条件改为「引号包裹的桥名 + path 记号同行」后消除，5 座
  STALE 判定不变。
- 生效链路：同步 2 个文件；kickstart 5 座桥；实跑
  `runtime/tools/bridge_freshness.py`：重启前 `stale: lingxi, qoder, qwen,
  trae, zcode`（exit 1），重启后 `stale: none`（exit 0），5 座新进程起于
  `01:59:34~35`，晚于 `_common.py` 的 `01:26:22`。
- 抽查 5 座 `/health`：qoder `logged_in:true`（14 models）、lingxi
  `session_alive:true`、trae `ok:true`、zcode `logged_in:true`；qwen
  `ok:true` 但 `has_api_key:false`——已知外部缺 `QWEN_API_KEY`，非代码问题。

**健康检查绿的是路由新鲜，不是进程年龄；每次动 bridges/ 都以
`runtime/tools/bridge_freshness.py` 收尾——退出码 1 就是有人还在跑旧字节码，
kickstart 完再跑一次看到 `stale: none` 才算闭环。**
---

## 根因 29：BaseHTTP 服务连异常阶段都没有，一个坏 JSON 就断连

FastAPI 那一族有 Starlette 的异常处理器兜底（根因 26），`http.server` 这一族
什么都没有：`do_POST` 里任何一个异常逃出去，`socketserver` 打完 traceback 就把
socket 关掉——客户端看到的不是 500，是**连接被重置，没有状态行、没有 body、
连 error type 都没有**。重试、降级、展示，全都无从谈起。

gemini 与 antigravity 的 `do_POST` 在同一个姿势上裸奔：

    length = int(self.headers.get("Content-Length") or 0)
    payload = json.loads(self.rfile.read(length))

客户端发来一个非 JSON 的 body（改写 `tool_use` 请求体那一类操作就会这样），
`json.loads` 抛 `ValueError`，无人接收，连接断。catpaw 是这一族里唯一有意识
防过的三层：坏 JSON → 自有错误信封、认证失败 → 400、外加一层 catch-all。

第四座服务不在主桥清单里，是上一轮漏审计的
`kit/bridges/zcode/captcha-relay.py`：长驻 `http.server`，`do_POST` 把
captchaVerifyParam 写 `captcha.txt`，`OSError`（目录被清、磁盘满）一样逃逸
断连，而它是 zcode 会话的唯一续命入口。审计「有哪些 http.server 服务」不能
枚举桥目录，要用 `serve_forever` 扫全仓。

fix：守卫的唯一家园是**新文件 `kit/bridges/_basehttp.py`，只 `import json`**，
wrap handler 类的每个 `do_*`：逃逸的 `ValueError` → 400 `bad_request`，其他
→ 500 `bridge_error`，都经 `self._send` 发 JSON 信封；`_send` 自己失败（对端
早已挂断）静默。幂等标记 `wrapper._basehttp_guard = True`，重复安装不会二次
包装、二次发信封。路由自己的回答一律不受影响：正常返回的路径永远到不了这里。

为什么不住 `_common.py`：captcha-relay 由 Xcode 框架 Python 3.9 启动，
**没有 fastapi/httpx，只有标准库**，`import _common` 当场 ImportError。共享
代码放哪，标准是最弱的那个运行环境，不是最常见的那个。因此 `_common.py`
整体回退到同步前的 pristine 状态，`import json` 与函数全部撤掉，并用
`touch -r runtime/bridges/_common.py kit/bridges/_common.py` 对齐 mtime——9 座
FastAPI 桥因此零影响、零重启。三座桥改成 `import _basehttp` 加限定调用
`H = _basehttp.install_basehttp_guard(H)`；captcha-relay 补上桥同款的
`sys.path` bootstrap 后 `Handler = _basehttp.install_basehttp_guard(Handler)`。

**这一轮的教训比代码贵**：上一版把守卫放进 `_common.py`，三座桥写的是
`import _common` + 裸名调用 `install_basehttp_guard(H)`，`py_compile` 六个文件
全绿，一重启三座桥同时 `NameError`——`py_compile` 只查语法，根本不解析名字。
所以新增一条导入即包回归测试；扫描测试则只在「文件名是 `*_bridge.py` **或**
含 `serve_forever`」时才要求守卫，豁免 `lingxi/login_helper.py` 这种一次性
OAuth 回调助手（手动 `handle_request` 循环，不是长驻服务），同时让 captcha-relay
被 `serve_forever` 兜住。

验证：

- 新测试 `kit/tools/test_basehttp_guard.py`（7 passed）：
  `test_malformed_body_leaves_400_envelope`、
  `test_unexpected_error_leaves_500_envelope`、
  `test_normal_responses_pass_through`、
  `test_double_wrap_produces_one_envelope`、
  `test_send_failure_stays_silent`、
  `test_every_basehttp_handler_is_guarded`（源扫描）、
  `test_http_server_services_import_and_wrap`（spec 加载 antigravity / catpaw /
  gemini / captcha-relay 四座真实服务，断言 `do_POST._basehttp_guard` 为 True，
  专防 NameError 类回归）。两棵树各 **300 passed**（293 + 7），exit 0。
- 反向对照：拔掉 gemini 的守卫行 → 扫描 + 导入两条测试同时红（2 failed）→
  还原后 `cmp` 字节一致 → 7 passed。
- 生效链路：同步 6 个文件（`_basehttp.py`、三桥、captcha-relay、测试），`cmp`
  逐一一致；重启 4 座服务，health 全 OK——gemini `status:ok`
  account=google-one、antigravity google-antigravity（12 models）、catpaw
  `13661621468`、captcha-relay `ok`，新进程 `02:16:49`（pid
  54055/54057/54059/54061，其中 54061 跑 Xcode Python 3.9）。
- Xcode Python 3.9 下单独 `import captcha-relay` 成功、wrapped=True。
- `runtime/tools/bridge_freshness.py` → **`stale: none`**（zcode 主服务也一并
  kickstart，因工具把 captcha-relay.py 变更归到 zcode 组，新 pid 54852）。
- 实跑坏 body（带 key POST `not-json`，无 key 会先 401、走不到解析）：
  gemini:8794 与 antigravity:8797 → `400 {"error":{"message":"Expecting value: line 1 column 1 (char 0)","type":"bad_request"}}`；
  catpaw:8795 → `{"error":{"message":"bad json Expecting value..."}}`（其自有
  包装，守卫不越位）。happy path：gemini `/v1/models` 200 正常 JSON。

**http.server 里没有 Starlette 的异常阶段，不断连就得有信封；共享代码放哪要
看最弱的运行环境（Py3.9 仅标准库），`py_compile` 全绿不代表 import 也绿。**

补记（收尾）：`_basehttp.py` 模块 docstring 里原有一句「_common re-exports
install_basehttp_guard」——`_common.py` 回退后已成假话，改掉；随后重新同步
`_basehttp.py` 与本文档 2 个文件、restart 这 4 座服务，`bridge_freshness.py`
复跑回到 `stale: none`，health 复测全 OK（gemini google-one / catpaw 13661621468 /
antigravity 12 models / captcha-relay ok），catpaw 补测 `/v1/chat/completions`
→ `400 {"error":{"message":"bad json Expecting value: line 1 column 1 (char 0)"}}`。
两棵树复跑各 **300 passed**。

## 根因 30：antigravity 把 `metadata` 塞进 chat 请求体，每一条都被 Google 判 400

症状：antigravity（8797）`/health` 与 12 个模型全绿，却没有一次 chat 成功过；
gemini（8794）报错时，客户端在 `error.message` 位置读到的是一个对象而不是字符串。

实测（真实 token、同一账号、同一模型，只改请求体）：

    POST v1internal:loadCodeAssist    {metadata: {...}}   -> 200
    POST v1internal:generateContent   {model, request, metadata}
          -> 400 INVALID_ARGUMENT
             Invalid JSON payload received. Unknown name "metadata": Cannot find field.
    POST v1internal:generateContent   {model, request}
          -> 403 VALIDATION_REQUIRED  Verify your account to continue.

`kit/bridges/antigravity/antigravity_bridge.py` 的 `call_upstream()` 一直把
`'metadata': meta_for(ide)` 放进 generateContent 的请求体（`git log -S` 与
`git show HEAD:`：该行自文件第一个 commit `8e6f2bd7`「Antigravity 第十桥上线」
起就没变过，是先天洞）。`loadCodeAssist` 收这个字段，`generateContent` 不收：
Google 在看你模型之前就把整个请求体判掉。于是这座桥自 2026-09-26 诞生起 chat
100% 失败，而 `/health` 与模型列表两条自动检查一条都照绿——典型的「健康绿、
请求死」。gemini 桥的 `call_a` 本来就构造对了（`{'model': model, 'request': inner}`，
没有 metadata），这就是现成对照组，也解释了为什么 gemini 至少能把上游响应带回来。

修复：

- antigravity：`load_code_assist(ide=None)` 接受一个可选 IDE 身份，传了就在该
  身份下重新解析；chat 请求体改成 `{'model': model, 'request': inner}`，IDE 身份
  只在 `loadCodeAssist` 上生效。
- `call_upstream(model, msgs, stream, timeout=180, ide=None)` 的 `ide` 形参必须
  保留：`kit/tools/test_chat_budget.py` 有三个用例经它驱动 IDE fallback
  （`attempts[0][1] is None`、`ide='GEMINI_CLI'` 时返回 pong）。提前返回条件同时
  收紧成 `if ide is None and ST['project'] is not None`——否则命名 IDE 的重新解析
  会被缓存的项目号短路，fallback 就退化成刚刚失败的那一次请求。
- gemini：`do_POST` 错误分支把 `{'code_assist': ..., 'web': ...}` 整个 dict 塞进
  `error.message`，OpenAI 客户端读到对象而非字符串。改成
  `'; '.join('%s: %s' % (k, v) for k, v in err.items())`，明细沉到 `error.channels`，
  诊断信息不丢。

验证：

- 新测试 `kit/tools/test_codeassist_chat_payload.py`（4 passed）：chat 体不含
  `metadata` 而 loadCodeAssist 含、命名 IDE 仍重新解析、`ide` 形参契约、
  gemini 502 信封 `error.message` 是 str 且含两个渠道名（`channels` 仍是 dict）。
- 反向对照：把 `metadata` 加回 chat 体 → 前两个用例红 → 还原后全绿。
- `kit/tools/test_chat_budget.py` 原先钉的是旧信封形状
  （`set(body["error"]["message"]) == {"code_assist", "web"}`），等于把缺陷当契约
  钉死了；改为 `isinstance(..., str)` + `set(body["error"]["channels"]) == {...}`，
  两个文件合计 25 passed。
- 两棵树各 **327 passed**。kit 侧唯一失败是 `test_stepfun_image_shim.py` 的
  「503 状态对但响应体为空」，`git stash` 后在 HEAD 上同样红，与本次无关；
  runtime 侧两轮全量分别红在 workbuddy / catpaw 两个时序用例，单独跑各 4 passed、
  与本次两个文件组合跑 29 passed，属既有抖动。
- 同步 8 个文件（含新测试）→ `launchctl kickstart -k` antigravity 与 gemini →
  health 全 OK（antigravity `models:12`、gemini `status:ok`，新进程 02:41:25）→
  `runtime/tools/bridge_freshness.py` → **`stale: none`**。
- 实证（带 key，`claude-haiku-4-5@default`，`max_tokens:16`）：
  antigravity:8797 → 502，但**不再是 400 INVALID_ARGUMENT**，而是
  `HTTP 403 ... VALIDATION_REQUIRED "Verify your account to continue."`；
  gemini:8794 → `error.message` 现在是字符串
  （`"code_assist: HTTP 403 ...; web: SNlM0e not found (web session expired?)"`），
  `error.channels` 仍是 dict。

**外部阻塞（需要人做一次）**：同一个 Google 账号在 `v1internal:generateContent`
上返回 `VALIDATION_REQUIRED`，响应里带 `validation_url`。在浏览器打开它完成账号
校验，gemini 与 antigravity 两座桥的 403 才会解除；在此之前两桥的 chat 都走不到
「看模型」那一步。antigravity 桥把上游错误体截到 300 字符，URL 会被切掉，所以文档
里这个链接要绕开桥直连 Google 取完整值（该 URL 与会话绑定，过期需重取）。

**字段归属要按端点核对，不能因为相邻端点收就推广到所有端点；更要紧的是——一座桥
可以在 `/health` 与 `/v1/models` 全绿的同时，一次 chat 都没成功过，绿的不检查就
不算检查。**

## 根因 31：stepfun 图像桥把 loopback 转发交给环境代理，「上游不可达」长成了「上级拒绝」

症状：`kit/tools/test_stepfun_image_shim.py` 的
`test_an_unreachable_upstream_answers_503_not_a_bare_500` —— 状态码是对的（503），
响应体却是空的，`error.type` 不是 shim 自己那支的 `upstream_unreachable`。上游健康
时全绿，只有上游真的连不上才显形，而那正是这条用例唯一要守的分支。

实测：

    $ scutil --proxy | grep -E 'HTTPEnable|HTTPProxy|HTTPPort'
      HTTPEnable : 1
      HTTPProxy : 127.0.0.1
      HTTPPort  : 1082
    $ lsof -nP -iTCP:1082 -sTCP:LISTEN
      MacPacket 22012 a1-6 ... TCP 127.0.0.1:1082 (LISTEN)
    $ env | grep -i proxy     # exec shell 与 pytest 进程都没有 HTTP_PROXY

- httpx 0.28.1 默认信任环境：`_transport_for_url()` 对 `127.0.0.1` 与
  `example.com` 一样返回 `PROXY`。于是连一个 bind 后释放的死端口也拿不到
  ECONNREFUSED，请求被交给本机代理，代理回它自己的空 503。
- shim 对非 200 是原样中继（`Response(content=..., media_type=...)`），那个空 503
  就被当成 CC Switch 的回答转给客户端：调用看着像被上游拒了，而 shim 的
  「upstream unreachable」分支一次都没跑。
- httpx 0.28.1 的 `mounts` 里值为 `None` 表示「这个 pattern 直连」，且用户
  mounts 覆盖在 env proxy mounts 之上再排序，`_transport_for_url` 取首个匹配。

修复：`kit/tools/stepfun_image_shim.py` 给唯一那个 `httpx.AsyncClient` 挂上
loopback mounts —— `all://127.0.0.1` / `all://localhost` / `all://[::1]`，值 `None`。
上游永远在 loopback 上（CC Switch 15721），非 loopback 的上游仍尊重操作员的代理设置；
`all://::1` 不是合法 httpx pattern，带方括号的写法才是。

验证：

- 探针（设 `HTTP_PROXY` + upstream 指向 bind 后释放的死端口）：`proxy_hits []`，
  shim 回 `503 {"error":{"message":"upstream unreachable: ...","type":"upstream_unreachable"}}`。
- `_transport_for_url`：127.0.0.1 / localhost → `DIRECT`，example.com → `PROXY`；
  不加 mounts 时两者都是 `PROXY`（修复前 loopback 与外部 URL 同罪）。
- 新用例 `test_a_loopback_forward_never_inherits_the_ambient_http_proxy`：
  `_FakeProxy` 监听在本机并记录每次被请求，注入 `HTTP_PROXY` 后打 100 张图的请求
  → `proxy.seen == []`、响应体无 `proxied`、图片仍被 cap、上游收到 `POST`。
- 该文件用例 7 → 12，单跑 12 passed。

**「连本机」也是走网络：httpx 信任环境，于是「机器上有代理」和「上游不可达」会长成
同一个空 503。凡是上游在 loopback 上的服务，客户端都要显式声明直连。**

## 根因 32：两个「单文件必过」的全量红，泄漏的是一个没关的 uvicorn 和一个没 join 的线程

症状：`pytest -q tools` 偶发 1~2 failed，单文件跑必过；四轮全量给出 2 failed /
1 failed / 0 failed / 1 failed 四种结果（328~329 passed）。两条：

    test_catpaw_single_background_refresh.py::test_the_guard_reopens_once_the_refresh_finishes
        两次 list_models() 后 started == 2（守卫没挡住第二次）
    test_workbuddy_channel_retry.py::test_channel_budget_is_per_request_not_per_candidate
        len(sleeps) == 4148823，而不是 3

实测（第 4 轮全量，在 `len(sleeps) == 50` 时 faulthandler dump 全部线程栈）：

    Thread 0x000000017928f000 [asyncio_0] (most recent call first):
      File "kit/tools/test_workbuddy_channel_retry.py", line 114 in _sleep
      File ".../uvicorn/server.py", line 248 in main_loop
      File ".../uvicorn/server.py", line 107, in _serve
        await asyncio.sleep(0.1)

- 409 万次 sleep 不是桥在重试：是别的测试留下的 uvicorn 事件循环在按 0.1 秒心跳
  空转，而 `mock.patch.object(core.asyncio, "sleep", _sleep)` 打的是进程级
  `asyncio.sleep`——`core.asyncio` 就是那个模块对象，整个 worker 的每个 loop 都被
  记进了 `sleeps`。
- 那些循环的来源：`test_stepfun_image_shim.py` 的 `_Shim.__init__` 每实例化一次就起
  一个 `threading.Thread(target=server.run, daemon=True)`，而 uvicorn 的
  `main_loop` 只有 `should_exit` 被置位才返回。没人调 close，11 处调用 = 11 个跑到
  会话结束的空转 loop。
- catpaw 是独立的同类问题：`_spawn_bg_refresh()` 先 `_BG_RUNNING['on'] = True` 再
  `Thread.start()`，清 False 要等 `_bg_refresh()` 返回；用例 1 的 finally
  `release.set()` 之后立刻返回，残留线程的 finally 落进用例 2 的两次
  `list_models()` 之间把守卫清掉 → 第二个 refresh 启动。生产代码
  `_spawn_bg_refresh()` 本身没错，责任在测试的线程清理。

修复（都在测试侧）：

- `test_stepfun_image_shim.py`：`_Shim` 记下 thread 并新增 `close()`
  （`server.should_exit = True` + `join(timeout=10)`），新增 `site_factory` fixture
  统一回收所有实例。实测单实例：线程 delta 1 → 0，`is_alive()` False，无残留线程。
- `test_workbuddy_channel_retry.py`：`_sleep` 只统计**正在跑 `_collect()` 的那个
  事件循环**的调用（`_collect()` 一进来就记下 `get_running_loop()`），别的 loop
  直接 `await real_sleep(delay)` 按真实节奏放行。
- `test_catpaw_single_background_refresh.py`：finally 改 `_teardown()` ——
  `release.set()` 后轮询等 `_BG_RUNNING['on']` 变回 False（最多 10 秒）再恢复
  `cp._bg_refresh`，不允许线程跨用例。

验证：

- kit 树连续三轮全量：**344 passed / 344 passed / 344 passed，EXIT=0**。
- runtime 树（同步后）一轮全量：**344 passed，EXIT=0**。
- 单文件：catpaw + workbuddy 6 passed；stepfun 12 passed；uvicorn 泄漏清零。

**「单文件过、全量红」先看别的文件留下了什么：一个没关的 uvicorn 服务器、一个没
join 的线程，就能把同进程里另一条断言从 3 变成 409 万。进程级 mock
（`asyncio.sleep`、`time.sleep`）最危险——它按全局生效，测试必须声明自己只信哪一条
loop。**

## 根因 33：shim 的 launchd timer 从未部署，Codex 的 base_url 钉在一个没监听的端口上

症状：`~/.codex/config.toml`（line 14）`base_url = "http://127.0.0.1:15722/v1"`，而 15722
根本没人监听 —— Codex 走这个 provider 的每个请求都打到死端口；`tools/image_cap.py` 守的
StepFun Plan API 70 图封顶（第 71 张图回 400 images_too_many）实际从未生效过。

实测：

    $ launchctl list | grep stepfun        # 空，无 com.local.stepfun-image-cap
    $ lsof -nP -iTCP:15722 -sTCP:LISTEN   # 空
    $ grep base_url ~/.codex/config.toml   # http://127.0.0.1:15722/v1（shim 端口）

- `/tmp/fleet-logs/com.local.stepfun-image-cap.log` 最后一条是 03:01 的干净关停，而那轮跑的
  还是手工 15799 测试实例，不是 launchd。
- 部署路径：`kit/install.sh` 的 4d 节在**每次完整安装**里都无条件执行
  `bash ${FLEET_HOME}/tools/stepfun_image_shim.sh --home ${FLEET_HOME} install-timer`；
  但 runtime 一路只跑过 `--sync-only`，日志明确写着
  `sync-only: skipped virtualenv, services and opencodex wiring` —— services 段被跳过；
  shim 又是本会话才落盘的新文件，所以 timer 一次都没装过。
- `opencodex/setup-providers.sh` 装完 timer 还会跑 `tools/pin_shim_base_url.py`，把
  base_url 从 CC Switch 15721 改钉到 shim 15722（CC Switch 每次切 provider 会把
  config.toml 改回 15721，所以每轮 setup 都要重钉）。本次实测：钉点早已在 15722 上，
  `--dry-run` 报 `no change`，缺的只有服务。

修复：用项目自带命令按 install.sh 4d 的同路径补装：

    bash runtime/tools/stepfun_image_shim.sh --home runtime install-timer

验证：

- `launchctl list` → `59361 ... com.local.stepfun-image-cap`；`lsof -iTCP:15722` →
  Python 59361 LISTEN；日志 `Uvicorn running on http://127.0.0.1:15722` → 上游 15721。
- `GET /__image_cap/health` → `{"ok":true,"upstream":"http://127.0.0.1:15721","max_images":32}`。
- 经 shim `GET /v1/models` → 200 透传到 CC Switch；根因 31 的 loopback mounts 在 launchd
  环境下同样生效（launchd 无环境代理，直连也命中同一 mounts）。
- `pin_shim_base_url.py --dry-run` → `no change`，config.toml 一个字节都没动。

**「sync-only 只同步文件、不部署服务」：config.toml 的钉点（15722）和 launchd 的 timer 分属两套系统。新组件上线后必须在 runtime 补一次 install-timer（或跑完整 install.sh），否则钉点指向一个死端口，而 health 面板看不见它。**
## 根因 34：RC33 只修了一半 —— 重构留下的死代码被守卫抓住，新版 shim 却从没进过 runtime

症状：kit 全量测试从上一轮的「343 passed」变成 `1 failed, 343 passed`：

    tools/test_no_undefined_names.py::test_every_name_is_defined_somewhere
    -> kit/tools/pin_shim_base_url.py:154 'out' (function scope)

实测：

- RC33 为定时 re-pin 新增 `rewrite()`/`pin_once()`/`_atomic_write()` 后，把旧 `pin()`
  改写成了 5 行包装器（docstring -> `pin_once()` -> print -> `return 0`），但旧函数体
  原样留在 `return 0` 之后 13 行——不可达，却仍然被根因 1 的名字守卫扫到：
  第 154 行 `fh.writelines(out)` 引用的 `out` 只存在于 `rewrite()` 的函数作用域
  （line 66），在 `pin()` 里从未定义。修复前的备份 `/tmp/pin_backup.py`
  line 138-156 可逐行对照。
- 更关键的是另一半：kit 03:35 的新版 `stepfun_image_shim.py`（含
  `repin_codex_base_url()`/`start_repin_thread()`）从未同步进 runtime，而 launchd
  跑的是 `runtime/tools/stepfun_image_shim.py`，进程还是 02:39 的旧码——runtime
  侧一条 re-pin 线程都没有。与根因 27/28 同款「改完没同步、没重启」。

修复：

- 删掉 `pin()` 中 `return 0` 之后的不可达死代码，`pin()` 恢复为纯包装器。
- `cp -p` 同步 4 个文件进 runtime，`cmp` 全部 same：
  `kit/tools/pin_shim_base_url.py`、`kit/tools/test_pin_shim_base_url.py`、
  `kit/tools/stepfun_image_shim.py`、`kit/docs/stepfun2codex-runbook.md`。
- 重启：`launchctl kickstart -k gui/501/com.local.stepfun-image-cap`
  （不能用 `stepfun_image_shim.sh` 的 start/stop —— pidfile 那套是给 nohup 实例用的，
  会对 launchd 服务起第二个进程抢 15722 端口）。重启后实例 PID 85401（03:44），
  health ok。

验证：

- kit `354 passed`（33.27s），runtime `348 passed`（32.68s），两侧 EXIT=0。
- 进程级端到端（排除 `nohup env ...` 前缀不传 env 的方法论问题，改用 subprocess
  显式传 env）：`IMAGE_CAP_PORT=15799`、`IMAGE_CAP_PIN_CONFIG=<临时 config>`、
  `IMAGE_CAP_REPIN_INTERVAL=2`，8 秒后临时 config 的 custom `base_url` 从
  `127.0.0.1:15721` 变成 `127.0.0.1:15799`，日志出现
  `[repin] pinned custom base_url: 127.0.0.1:15721 -> 127.0.0.1:15799`；
  测试实例 terminate 后 rc=-15，端口释放。
- 生产实测（最有力的证据）：launchd 实例 03:44 重启时，CC Switch 已把
  `~/.codex/config.toml` 打回 15721，re-pin 线程第一轮就自动改钉回 15722 ——
  `/tmp/fleet-logs/com.local.stepfun-image-cap.log`：
  `[repin] pinned custom base_url: 127.0.0.1:15721 -> 127.0.0.1:15722`；
  随即一条真实请求过 shim：
  `[image-cap] model=stepfun/step-5-preview images=71 unique=71 kept=32 dup_dropped=0 cap_dropped=39`。

**「改完文件」不等于「修完」：跨 kit/runtime 的修复必须走完全套三步——同步（`cmp` same）、重启（`kickstart -k`）、复跑两侧测试。守卫测试只在你运行它的时候发声；runtime 进程跑着旧码这件事，没有任何测试看得见。**
## 根因 35：streaming 传输错误零 failover —— `_stream_upstream` 把「没发首字节」的网络错误也判了死刑

症状：`/v1/responses` 及所有 SSE 流式路径在**首字节尚未发出**时遇到传输层错误
（`httpx.ConnectError`/`ConnectTimeout`/`ReadTimeout`/SSL 等一切 `httpx.HTTPError`），
客户端只会收到一条 502 错误事件；账号池里明明还有 healthy 账号可以接管，却一个都不试。
非流式同路由 `_collect_with_pool` 传输错误即换号，流式却直接判死——同一路由两侧
failover 合同不一致。

实测：

- `core.py` `_stream_upstream()`（2850 行）的 `except httpx.HTTPError`（3006 行）：
  不看 `started`，一律 `yield _err_event(...)` + `return`，整轮候选循环直接放弃。
  只影响「还没发字节」的阶段——正是唯一还能安全换号的窗口，违背自身 docstring
  （2852-2858 行）：「account failover is deliberately limited to refresh errors,
  transport errors, non-200 responses, and SSE error events before the first
  event is released to the client」。
- 本应实现 failover 的三行（`_refresh_pin_async()`、`pool.mark_failure(...)`、
  `_log(...)`）写在 except 块**之后**，而 try 内所有出口都是 `break`/`continue`/`return`，
  except 块又以 `return` 收尾——三行是不可达死代码，其中 `_log` 行引用的 `exc`
  只在 except 内绑定。与根因 34 的孤儿代码同款形状，但名字守卫看不见：
  `exc` 在函数作用域里确实有定义。
- 复现测试 `kit/tools/test_workbuddy_stream_transport_failover.py`（3 用例；
  importlib 按文件加载 `core.py`，fake `httpx.AsyncClient` 按 Authorization 头
  分发 connect/mid_drop/ok）。修复前首用例 RED：账号 1 connect refused 后输出只有
  `data: {"error": {"message": "refused" ..., "code": 502}}`，账号 2（healthy）
  零机会；另 2 例（发字节后断流只产 1 条错误事件、空池仍报 cooling down）
  修复前后均 GREEN。

修复：

- except 块按 `started` 分流（3006 行起）：已发字节 → 「流中断」日志 + 错误事件 +
  `return`（首字节后重发会向客户端重复输出，只能结束）；未发字节 →
  `_refresh_pin_async()` + `pool.mark_failure(candidate.ref, "上游网络错误", 30)`
  + 「网络错误」日志 + `break` 跳出当前 attempt，候选循环换下一个账号，
  与 `_collect_with_pool` 的非流式 failover 同合约。
- 删除 except 块之后的 3 行孤儿代码，补 3 行中文注释说明 `started` 分线。
- `cp -p` 同步 `kit/bridges/workbuddy/core.py` 与测试文件进 runtime，`cmp` 均 same
  （同步前差异停在 core.py 3010 行——正是本次热修的那一行）。
- 重启共享该 `core.py` 的两个在跑桥：`launchctl kickstart -k`
  `gui/501/com.local.workbuddy2codex` 与 `...-gpt`。

验证：

- 单跑修复测试：修复前 `1 failed, 2 passed`，修复后 `3 passed`（0.83s），EXIT=0。
- kit 全量 `pytest -q tools`：`357 passed`（38.00s），EXIT=0；
  runtime 全量 `pytest -q tools`：`357 passed`（44.92s），EXIT=0。
- `bridge_freshness.py`：重启前 `stale: workbuddy-cn, workbuddy-gpt`（core.py 变更
  后两桥进程 +2h28m），kickstart 重启后 `stale: none`，EXIT=0。

**流式 failover 的分界线不是「报没报错」，而是「客户端见没见过字节」：首字节之前，传输错误和 502 响应等价，换号即可；首字节之后，任何重试都会重复输出，只能结束。**

