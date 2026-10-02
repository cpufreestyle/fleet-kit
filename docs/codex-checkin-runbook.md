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

每行：状态 / 账号 / 登录 / 积分口径（free-windows.json）/ 积分值 / 来源 / 签到状态。
真数字来源：xhx 官方 points balance、workbuddy 两桥 /ui/checkin、zcode /entitlements
（一次性 100,000,000 tokens）；其余节点口径来自 free-windows.json，数值留空并写清原因。
status_ui 面板「节点积分 / 账号」就是这份数据（30s 缓存）。


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

#### MiniMax：仍然没有凭据，而且本来也没有余额接口

本机翻遍了也没有 MiniMax key：keychain 无条目、所有 `.env`（fleet.env、
.openclaw/.env、.omniroute/.env 等 13 份）无 MINIMAX 相关、shell history 无、
cc-switch provider 表里没有 MiniMax。`/Applications/MiniMax Agent.app`
（com.ai.wanjuan 0.10.4）装着但从未启动——没有 userData 目录，所以连
「从它自己存储里读」这条路都不存在。

而且平台本来就没有余额接口。这条结论有三个独立来源，逐条列清免得下次重查：

**来源一：MiniMax 开放平台 API 主机（无 key 探活实测）**

    api.minimaxi.com / api.minimax.io
      /v1/chat/completions   401 authorized_error（路由在，验签在）
      /v1/usage /v1/usages /v1/credits /v1/quota /v1/account   全部 404

    platform.minimaxi.com  同样九个路径全返回它自己的 404 页
    www.minimaxi.com/api/*  的 200 是 SPA 兜底页（返回 HTML 不是 JSON），不是接口

**来源二：MiniMax Agent 自己发布的 Safari 扩展（2026-10-02 逆）**

`/Applications/MiniMax Agent.app/Contents/PlugIns/MiniMax Agent Extension.appex/
Contents/Resources/popup.5eb990aa.js` 里能数出它全部的 web 路由：

    /v1/api/user/login/sms/send     POST  发验证码
    /v1/api/user/login/phone        POST  手机号登录，换 token
    /v1/api/user/renewal            POST  续 token
    /v1/api/user/account            DELETE 登出
    /v1/api/user/guide_status       GET
    /v1/api/user/toast              GET
    /v1/api/chat/msg                POST  聊天
    /v1/api/chat/msg_choice / msg_tts / voice_msg / retry_msg / feedback / stop_generating

鉴权是请求头 `token`（存在扩展自己的存储里），外加一个 `yy` 签名头——
MD5(unix + url + method + data + "oouiplugin")。基址只有两个：`https://hailuoai.com`
和它的预发 `https://hailuo-pre.xaminim.com`。

**整份路由表里没有任何 quota / balance / credits / usage / plan / vip 路径。**
唯一的账号路由 `/v1/api/user/account` 从代码看是登出（`ul.delete(e)`），不是查余额。

**来源三：对活主机按这份路由表逐条打**

    https://hailuoai.com/v1/api/user/account  DELETE → 401（路由在，要 token）
    https://hailuoai.com/v1/api/user/renewal  POST  → 400 {"statusInfo":{"code":2,
        "message":"请求异常，请检查请求参数","requestID":...}}（路由在，缺参数）
    https://hailuoai.com/v1/api/chat/msg       POST  → 401（路由在，要 token）
    https://hailuoai.com/v1/api/user/quota     GET   → 404
    https://hailuoai.com/v1/api/user/balance   GET   → 404
    https://hailuoai.com/v1/api/user/credits   GET   → 404
    hailuo-pre.xaminim.com 全部 SSL UNEXPECTED_EOF（预发不对公网开放）

三条来源指向同一件事：**MiniMax 不提供任何可编程的余额接口**，网页控制台上的数字
是登录态页面渲染的，没有对应 API。所以这一行永远只能是「key 或登录态还在吗」，
问不出剩多少——这不是没找到，是它不存在。

所以给了 key 也只能验证「key 还活着吗」。`plan_credits.py minimax` 会依次打
CN、海外两个 host 再判 401—— MiniMax 的 key 按区域签发，海外 key 在 CN host
上就是 401，旧代码会直接把好 key 判成死 key。

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
