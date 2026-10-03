# codex-checkin Runbook — 订阅平台自动签到

日期：2026-09-25 | 状态：**已上线**（小浣熊任务实测 OK，LaunchAgent 每日 09:00 自动执行）

## 架构
```
launchd(com.local.fleet-checkin)  每日 09:00 + RunAtLoad
   -> "$FLEET_HOME/../runtime/tools/checkin.py" --daemon
        解释器：workbuddy2codex venv Python（fleet.env 的 $FLEET_PYTHON）
        状态：CODEX_CHECKIN_HOME="$FLEET_HOME/../runtime/checkin/state.json"
        日志："$FLEET_HOME/../runtime/logs/checkin.log"
```
- 装机开关：`bash install.sh --with-checkin`（或 `bash deploy.sh --with-checkin` 一次到位）
- 日常命令：`bash $FLEET_HOME/tools/checkin.sh status|run-now|install-timer|uninstall-timer`
  - 都支持 `--home DIR` 换根目录；等价于 `tools/checkin.py --status|--run-now|--daemon`
- 卸载：`bash $FLEET_HOME/uninstall.sh --purge`（SUFFIXES 里含 `fleet-checkin`，timer 一起摘）
- 幂等：同一自然日（北京时间）已成功则跳过；桌面端当天启动过也会先领掉，脚本会记录“已发放过”

## 迁移说明（2026-09-26）

签到原先是独立服务 `~/codex-checkin`（label `com.local.codex-checkin`，状态在 `~/.codex-checkin/`），
已随 FleetKit 整体迁入 runtime。**旧 label 与旧路径均已废弃**，勿再按旧路径操作：

| 项 | 旧（已废弃） | 新（当前） |
| --- | --- | --- |
| launchd label | `com.local.codex-checkin` | `com.local.fleet-checkin` |
| 脚本 | `~/codex-checkin/checkin.py` | `~/FLEET_HOME/tools/checkin.py` |
| 状态 | `~/.codex-checkin/state.json` | `$FLEET_HOME/checkin/state.json` |
| 日志 | `/tmp/codex-checkin.log` | `$FLEET_HOME/logs/checkin.log` |

`~/.codex-checkin/` 是搬迁前的历史残留（state.json + checkin.log），新服务已不读取，
确认无需回溯后可以手工删除。kit 早期曾设想自建 `fleet-checkin` 与 live 服务并存，
现已合并为**单一服务**，不存在两个 timer。

### xhx — 商汤小浣熊 每日登录积分
- 契约（逆向自官方桌面端 app.asar）：`POST https://xiaohuanxiong.com/api/web/desktop/v1/login/points/grant`
  - 头：`Authorization: Bearer <access_token>`、`X-Client-Platform: desktop-macos`、`X-Client-Version: v1.0.28`
  - 无 body；响应 `data.granted=true` = 本次新发放；`false` = 今日已发放（桌面端启动时也会调，天然幂等）
- token 来自 `~/.box-agent/config/auth.json`；过期走单次轮换刷新（立即落盘），刷新失败重读盘（桌面端可能已重同步）
- 签到后顺带拉 `GET /api/web/points/v1/balance` 记账（available_points 等）

### workbuddy / workbuddy-gpt — Buddy 加油站真签到（2026-10-01）
- 经桥自己的端点，不另存凭证：GET / 领 dashboard session cookie →
  GET /ui/checkin（活动 + 每账号 today_checked_in / credit / streak_days）→
  POST /ui/checkin/claim（无 ref = 整个账号池领取）
- 端点仍要求本机 + Content-Type: application/json + Origin 匹配，见桥内 _check_dashboard_management
- **两座桥端点路径相同、后端不同**：国内 copilot.tencent.com、海外 www.workbuddy.ai。
  实测 2026-10-01：海外桥拿国内端点问海外账号 → 401 Authorization Required，面板只能显示
  「状态读取失败」。已改为按 _PROVIDER["backend"] 传后端，海外随即读到本期活动
  （Buddy 加油站 season 1、每日 100 credits）
- 海外 POST /v2/billing/meter/daily-checkin 目前回 400：应用里这条路要带 X-Device-Token
  （Turing Shield，见 "WorkBuddy AI.app" 的 app.asar），本工具不伪造该头，所以海外节点报
  「待领取」而不是假装领到；桌面端启动时会自行领取

### 其余 12 个节点 — 无每日签到端点（已逐个核实）
qoder / codely / trae / lingxi / cline / qwen / gemini / catpaw / antigravity / zcode / stepfun / tokendance
- 上游没有每日签到接口，记为 na：既不算成功也不算失败，--run-now 退出码不因此变红
- codely 的 LiteLLM /key/info、/user/info 实测 nginx 403，也没有余额可读
- 每个节点仍然读出账号与积分，让 15 个节点在面板里都有带数字的一行

## 节点积分查看（tools/node_credits.py）
stdlib（urllib），可单独跑，不进任何 venv：

    python3 tools/node_credits.py                 # 全机队一张表
    python3 tools/node_credits.py --json          # 机器可读
    python3 tools/node_credits.py --node zcode    # 单节点

每行：状态 / 账号 / 套餐 plan / 登录 / 积分口径（free-windows.json）/ 积分值 / 来源 / 签到状态。

真数字来源：xhx 官方 points balance、workbuddy 两桥 /ui/checkin、zcode /entitlements
（一次性 100,000,000 tokens）；其余节点口径来自 free-windows.json，数值留空并写清原因。
status_ui 面板「节点积分 / 账号」就是这份数据（30s 缓存）。

#### 2026-10-03：每行补上「套餐 plan」与到期日

面板「节点积分 / 账号」从 8 列变 9 列，账号后面多一列 `套餐 plan`：套餐叫什么名字，
以及带到期日的套餐什么时候到期。名字不只是给人看的——同一个桥换个套餐，模型池可能整个
变掉，而到期日不显示出来，token 会在会话中间悄悄死掉。

套餐名按节点来源不同：

- `workbuddy` / `workbuddy-gpt`：Buddy 加油站 activity_name（本期活动名）
- `trae`：bridge /health 的 `edition`，并附 `expires_at_ms` 折算的 UTC+8 到期日
- `kimi-code` / `minimax`：/health account_pool 每账号的 `points_plan`（与积分同一处）
- `zcode`：/entitlements 的 `plan`（实测 ZCode Trust Build，本节点 /health 无 plan 字段）
- `gemini` / `antigravity`：Google Code Assist `currentTier.id`（账号 403 时为空）

CLI 表格同样多一列，列宽 16，超长名字按 16 字符截断。

#### 2026-10-03（下午）：积分板块重排为全宽，剩余积分与套餐余额一眼可见

之前「节点积分 / 账号」挤在 `grid2` 半宽栅格里，9 列小字很难找到数值列。现在：

- 板块移出半宽栅格，升级为**全宽**「节点积分 / 套餐余额」面板，排在桥表格下面
- 9 列重排为：节点 / **剩余积分**（放大加粗、千分位）/ 口径徽章 / 套餐 plan / 账号 /
  状态 / 登录 / 签到 / 来源与备注
- 有数值的节点排前面；标题行给出汇总 `共 N 节点 · X 家有剩余数值`
- 「只看有剩余数值」复选框，勾上即时筛选，自动刷新后保持勾选状态

口径徽章配色沿用免费模型表四档语义（client=绿、limit=蓝、own=黄、unknown=灰），
新增 subscription=紫。

### 两个编程套餐账号（Kimi Code / MiniMax，2026-10-01 加入）
它们不是桥：机队没有为它们跑桥进程，所以每一行仍是同样三个问题——账号活着吗、哪个 key、
平台说还剩多少。调用由 `tools/plan_credits.py` 直接问平台，`node_credits.py` 只负责渲染：

    kimi-code   GET  https://api.kimi.com/coding/v1/usages
                # coding 套餐的窗口余额，一次 GET，不花钱
    minimax     POST https://api.minimaxi.com/v1/chat/completions（max_tokens=1）
                # MiniMax 没有余额接口：1-token 调用既验证 key 又真的扣一点，扣减以控制台为准

key 来源：环境变量 `KIMI_CODING_API_KEY` / `MINIMAX_API_KEY`；Kimi 在没有环境变量时回落到
cc-switch 里 claude 的 default provider（那份本来就是 Kimi coding key）。401/403 一律显示
「key 被拒」并附平台原文——把死 key 渲染成 0 credits，是套餐被悄无声息取消的方式。

当前实测（2026-10-01）：cc-switch 里那份 Kimi key 已失效，`GET /coding/v1/usages -> HTTP 401
The API Key appears to be invalid or may have expired.`；MiniMax 本机没有任何凭据，行里直说
「未配置 key」且不发起调用。

#### 2026-10-02：Kimi Code 找到真 key，答案是「没有套餐」

key 不用再找。Kimi 桌面版自己把 coding key 存在

    ~/Library/Application Support/kimi-desktop/daimon-share/daimon/kimi-code-key.json
    （结构 {"v":2,"keys":[{"userId":...,"apiKey":"sk-kimi-...","keyId":...}]}）

`plan_credits.py` 现在会自己读这份文件，所以本机直接
`python3 tools/plan_credits.py kimi` 就能出结果，不用 `export` 任何东西。
（cc-switch claude default 那个 65 位无前缀串不是 key，实测仍 401。）

实测输出（真实返回，未删减）：

    kimi key from ~/Library/Application Support/kimi-desktop/daimon-share/daimon/kimi-code-key.json
    { "platform": "kimi-code", "http": 200, "auth": "Authorization",
      "body": {},
      "plan": {"user_level": 10, "user_level_name": "Free",
               "goods_version": 0, "status": "USER_STATUS_NORMAL"},
      "subscription": "terminated",
      "reason": { "http": 403, "body": { "error": {
        "type": "access_terminated_error",
        "message": "Your current subscription does not have access to Kimi
                    Code right now. Upgrade your plan to keep coding with
                    Kimi Code: https://www.kimi.com/code/#pricing" } } } }

the key works but this plan is not active -- ...
renew the plan, then this reports real windows again

结论：这个 Kimi 账号**当前没有生效的 Kimi Code 套餐**（Free 档、goods_version 0），
所以 `/coding/v1/usages` 返回 `{}`——不是接口不对，是没套餐可报。计费页在
https://www.kimi.com/code/#pricing 。

顺带定死的两件事：

- `/coding/v1/usages` 是对的路由（它的兄弟 `/coding/v1/usage` 返回 404
  `resource_not_found_error`），Bearer 和 `x-api-key` 两种头都收。
- 「key 有效但套餐停了」是 403 `access_terminated_error`，和「key 死了」的
  401 是两件事。旧代码把 403 一律说成 dead key，会把一个好 key 说成死的；
  现在这种情况单独走退出码 4，并把平台给的续费地址原样打出来。

#### MiniMax：余额接口是有的，之前找错地方了（2026-10-03 更正）

**先更正一个错误结论。** 10-02 那轮写下「MiniMax 不提供任何可编程的余额接口」，
这是错的。当时只在开放平台和 Agent 扩展里找，而真正的路由在官网域的 www 主机上。

MiniMax 自己的 FAQ 写明了这个接口（platform.minimaxi.com/docs/token-plan/faq.md，
「如何查看 Token Plan 用量」的方式二）：

    curl --location 'https://www.minimax.cn/v1/token_plan/remains' \
      --header 'Authorization: Bearer <API Key>' \
      --header 'Content-Type: application/json'

实测（无 key）：

    www.minimax.cn    /v1/token_plan/remains -> 200
      {"base_resp":{"status_code":1004,
       "status_msg":"login fail: Please carry the API secret key in the
       'Authorization' field of the request header"}}
    www.minimaxi.com  /v1/token_plan/remains -> 200 同上
    api.minimax.cn / api.minimaxi.com       -> 200 同上（这三个域都接）
    www.minimax.io    -> SSL EOF（不对公网开放）
    platform.minimax.cn / platform.minimaxi.com -> 404（那是文档站）

**为什么之前全部找错：** 路由在 `www.minimax.cn`，不在 `api.minimax*.com`。
前三轮把 api 域、hailuoai.com、平台文档站、营销站 /backend/ 前缀、Agent 扩展
17 条路由全扫了一遍，唯独没扫官网 www 域——文档里给的示例 URL 就在那里。

**它要的是订阅 Key，不是按量计费 API Key。** FAQ 原话：订阅 Key 用于 Token Plan
套餐内额度和已购积分，与普通按量计费 API Key 相互独立、不能混用。所以拿一把
pay-as-you-go key 打这个接口只会得到 login fail，那不是 key 死了。

套餐档位（同页）：Plus ¥49/月、Max ¥119/月、Ultra ¥469/月；1000 积分 = ¥7；
套餐内额度受 5 小时固定窗口和周窗口控制，未用完不结转；超出部分由已购积分兜。

本机凭据状态：没有任何 MiniMax key，subscription key 也没有。翻查范围——
keychain、13 份 .env、shell history、cc-switch provider 表、Chrome/Edge/Safari
四个 cookie 库、26367 个文件里的 MINIMAX* 变量名（18 个，全是脚本占位符）、
fleet.env、kit/bridges/ 下所有桥、以及 MiniMax Agent 自己的 appex 容器
（WebKit ITP 库 0 行，证明它一个页面都没加载过）。所以这一行现在是
「有接口、没 key」。

使用方法（拿到订阅 Key 之后）：

    export MINIMAX_API_KEY=<订阅 Key>
    python3 tools/plan_credits.py minimax     # 先打 /v1/token_plan/remains

`plan_credits.py minimax` 现在会先打这个文档接口，读到了就报套餐额度；
读不到（按量 key 会被拒）才退回 1-token 聊天调用验证 key 是否活着。

#### 2026-10-01 记录（已被上一节取代）

以下为 10-01 的原始记录，保留作对照：

    拿到新 key 之后：

    export KIMI_CODING_API_KEY=<key>
    export MINIMAX_API_KEY=<key>        # 海外平台：plan_credits.py minimax --base https://api.minimax.io
    python3 tools/plan_credits.py all   # 两边额度一次打出


### 桥调用会不会扣积分（2026-10-02 实测）
用户此前提问「用小浣熊的模型，积分没少」。实测：经机队网关打 Kimi / MiniMax 模型，
调用全部正常返回，但四个能看到数字的节点计数一个都没动：

    调用                                    结果   workbuddy   workbuddy-gpt   xhx
    xhx/xhx-sn-kimi-k3（Kimi 模型）         200     100         100             7824 → 7824
    workbuddy/minimax-m3（MiniMax 模型）    200     100         100             7824 → 7824
    catpaw/MiniMax-M2.7（MiniMax 模型）     502     -           -               -（catpaw 上游需 VPN）

结论：这些桥消耗的是平台侧的限额/授权，不是面板上那个客户端积分（xhx 自己的 notes 也
写明「llm/v2 调用不结算积分」）。积分要动，只能走官方客户端或平台自己的结算接口。
所以「调用了模型但积分没少」是预期行为，不是桥把调用吞了。

Kimi Code 的官方接口是另一回事：`/coding/v1/usages` 能直接读到套餐余额（见上一节），
那份 key 换成有效的之后，`plan_credits.py kimi` 就是官方口径的额度调用。

## 使用方法
```bash
VENV=~/.local/node-v22.20.0-darwin-arm64/lib/node_modules/workbuddy2codex/.venv/bin/python
$VENV "$FLEET_HOME/../runtime/tools/checkin.py" --status                  # 看今日是否已签、余额
$VENV "$FLEET_HOME/../runtime/tools/checkin.py" --run-now                 # 手动签到（全部任务）
$VENV "$FLEET_HOME/../runtime/tools/checkin.py" --run-now xhx --force     # 强制某任务
tail -5 "$FLEET_HOME/../runtime/logs/checkin.log"                         # launchd 执行日志
launchctl kickstart -k gui/$(id -u)/com.local.fleet-checkin         # 手动触发一次守护任务

```

## 排坑
1. 小浣熊 refresh_token 单次轮换——脚本刷新成功后立即原子写回 auth.json（0600）；失败时重读盘拿桌面端新同步的 token
2. 桌面 app 启动时会自行调用同一 grant 接口，所以“自动签到”的实际价值是：用户当天没开 app 时也能领到
3. 时间按 Asia/Shanghai 记“今日”，与平台结算一致
4. 新平台扩展：在 `TASKS` 注册表加 `{"desc":..., "fn": async (client) -> dict}`，返回 ok/detail/available_points 即可
