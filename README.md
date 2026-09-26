# fleet-kit — 9 桥反代理舰队一键部署包

[![ci](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

把一套「Codex → 本地反代理桥 → 各 AI 订阅服务」的舰队打包成可在其他 macOS 电脑
一键部署的 kit：9 座 OpenAI 兼容本地桥 + opencodex 集成 + 登录/验收/卸载脚本。

测试基线（2026-09-26）：6/9 桥实测 PASS，3 项失败均为用户侧条件，见「已知问题」。

## 架构

    Codex ──▶ opencodex 代理 (127.0.0.1:10100)
                 │  [model_providers.*] allow-private-network
                 ▼
           9 座本地桥 (127.0.0.1:8787 .. 8795)
                 ▼
           WorkBuddy 海外版 / Qoder / 团结AI / Trae / 灵犀 / 小浣熊 / Gemini / CatPaw / TokenDance

| 桥 (name)        | 默认端口 | 上游服务            | 探针模型                  |
|------------------|---------|---------------------|---------------------------|
| workbuddy        | 8787    | WorkBuddy 海外版    | hy4-preview               |
| workbuddy-gpt    | 8788    | WorkBuddy 海外版    | gpt-6-astra / gpt-5.6     |
| qoder            | 8789    | Qoder CN            | auto                      |
| codely           | 8790    | 团结 AI (tuanjie)   | codely-core               |
| trae             | 8791    | Trae CN             | DeepSeek-V4-Pro 等        |
| lingxi           | 8792    | 灵犀 (LingXi)       | glm-5.3-flash 等          |
| xhx              | 8793    | 商汤小浣熊          | raccoon-*                 |
| gemini           | 8794    | Google Gemini       | gemini-3-pro-preview 等   |
| catpaw           | 8795    | CatPawAI (美团)     | glm-5.2 等                |

Codex 里模型以 `桥名/模型` 出现，例如 `workbuddy/hy4-preview`。

## 环境要求

- macOS（launchd 托管；其他平台可手动运行 bridges/ 里的脚本）
- python3 >= 3.9（install.sh 自动建 venv 并装依赖，见 requirements.txt）
- node/npm（仅用于安装 opencodex：npm install -g @bitkyc08/opencodex）
- 各服务的账号/订阅（见「登录表」）

## 快速开始（3 步）

1. 解包并安装：

       cd ~ && tar xzf fleet-kit.tar.gz && cd fleet-kit
       bash install.sh

2. 登录你想用的服务（见「登录表」），每个服务登录完成后运行：

       bash ~/fleet/bridges/finish.sh <name>

3. 验收：

       bash ~/fleet/tools/status.sh
       python3 ~/fleet/tools/fleet_chat_test.py

   如果装了状态面板（--with-ui），顺手打开 http://127.0.0.1:8796/ 看一眼。

在 Codex 中使用：确保 ocx 服务在跑（ocx service），模型选择器里选
`桥名/模型`（如 `workbuddy/hy4-preview`）。

## 安装选项

    install.sh [--home DIR] [--port-base N] [--with-opencodex|--no-opencodex]
               [--no-start] [--skip-deps] [--dry-run]
               [--with-checkin] [--with-ui] [--no-ocx-guard] [-h]

- `--home DIR`：安装根目录，默认 ~/fleet
- `--port-base N`：起始端口，9 座桥依次占用 N .. N+8，默认 8787
- `--with-checkin`：装每日 09:00 CST 签到 timer（当前只有 xhx 任务）
- `--with-ui`：装本地状态面板 launchd 常驻服务，端口 N+9（默认 8796）
- `--no-ocx-guard`：关掉反代理模型 catalog 看门狗（默认随 opencodex 一起装）
- `--no-opencodex`：跳过 ocx provider 注册（之后可手动跑 bash ~/fleet/opencodex/setup-providers.sh）
- `--no-start`：只写文件和 plist，不启动桥
- `--skip-deps`：跳过 venv/依赖安装（用系统 python3）
- `--dry-run`：只打印计划，不落盘

换端口后记得同步测试脚本：python3 ~/fleet/tools/fleet_chat_test.py --port-base 9787。

### 高级：起第二套舰队

env 覆盖 launchd 目录/标签前缀/日志目录，即可与现有舰队并存：

    FLEET_LAUNCH_DIR=/tmp/f2/LaunchAgents FLEET_LABEL_PREFIX=com.localtest \
    FLEET_LOG_DIR=/tmp/f2/logs bash install.sh --home /tmp/f2 --port-base 9787

第二套同样可以加 `--with-checkin --with-ui`：签到 key 落在 /tmp/f2/fleet.env，状态面板
落 N+9（9787 → 9796），plist 带 `--home /tmp/f2`，读的是第二套的 fleet.env。

## 部署自动化（deploy.sh）

一条命令跑完整条流水线，适合新机首装或整套重装：

    bash deploy.sh                      # 装到 ~/fleet，端口 8787..8795

    bash deploy.sh --home /tmp/fleet-a --port-base 9687 \
        --with-checkin --with-ui --no-opencodex --smoke

流程：preflight（Darwin/python3/curl/launchctl + 端口占用）→ 可选 --update（git pull）
→ install.sh → 轮询 9 个端口（120s 超时）→ bridges/finish.sh 逐桥收尾
→ setup-providers.sh + 签到 timer → status.sh + 汇总。

| 选项 | 作用 |
|------|------|
| `--home DIR` | 安装根目录，默认 ~/fleet |
| `--port-base N` | 起始端口，默认 8787 |
| `--with-checkin` | 装每日 09:00 CST 签到 timer |
| `--with-ui` | 装本地状态面板 launchd 常驻服务（端口 N+9） |
| `--no-ocx-guard` | 跳过反代理模型 catalog 看门狗 timer（默认随 opencodex 装） |
| `--no-opencodex` | 跳过 ocx provider 注册 |
| `--smoke` | 每桥跑一次聊天冒烟（需要已登录） |
| `--update` | 安装前先 git pull 更新 kit |

退出语义：`0` = 该起的桥都就绪（个别没登录只算 warning）；`1` = preflight 失败或
**一座桥都没起来**。单座桥连不上（VPN、内网、地域封锁）不会中断部署，只在汇总里
列为 unreachable，其余桥照常收尾；之后用 `bash ~/fleet/bridges/finish.sh <name>` 补收。

## 自动签到（checkin.sh）

部分上游服务每日登录送积分/额度，过期不补。kit 内置一个 launchd 定时任务，
每天 09:00（CST）自动跑一遍，幂等：当天已签、或桌面端启动时已领，就直接跳过。

    bash ~/fleet/tools/checkin.sh status          # 今日是否签 + 余额
    bash ~/fleet/tools/checkin.sh run-now         # 立即跑全部任务
    bash ~/fleet/tools/checkin.sh run-now xhx     # 只跑指定任务
    bash ~/fleet/tools/checkin.sh install-timer   # 装每日 timer
    bash ~/fleet/tools/checkin.sh uninstall-timer # 卸掉 timer

安装时加一个开关即可（等价于装完再跑 install-timer）：

    bash install.sh --with-checkin

状态与日志位置（`--home DIR` 可改根目录，默认 ~/fleet）：

    <home>/checkin/state.json     # 每任务：今日是否成功、余额、上次时间
    <home>/logs/checkin.log       # timer 运行日志

当前注册的任务只有 `xhx`（商汤小浣熊每日登录积分，凭据 ~/.box-agent/config/auth.json）。
workbuddy 的签到是 workbuddy2codex 桥内的账号池模块（account_pool.py /
workbuddy_checkin.py），跟着桥自己的节奏跑，不归 checkin.sh 管。

## 本地状态面板（status_ui）

零依赖的本地网页面板（Python 标准库单文件），用来看舰队整体运行情况：9 座桥的健康、
模型数、ocx、今日签到、日志尾巴，并且可以直接点按钮做签到 / 强制重签 / 重启单座桥。

    bash tools/status_ui.sh start            # 启动（默认 http://127.0.0.1:8796/）
    bash tools/status_ui.sh stop
    bash tools/status_ui.sh install-timer    # launchd 常驻（KeepAlive + RunAtLoad）
    bash tools/status_ui.sh uninstall-timer

页面内容：每座桥一行，显示 launchd 状态 + 退出码、端口监听 pid、/v1/models 模型数与
探针延迟、key 的 md5 前 8 位、可用 / 未登录 chips；下面依次是 ocx 状态、今日签到结果
与余额、各桥日志 tail。提供签到 / 强制重签按钮、单桥重启按钮、10 秒自动刷新。

安装时装上：`bash install.sh --with-ui`（或 `bash deploy.sh --with-ui`）。面板端口是
PORT_BASE+9，默认 8796，与 9 座桥错开；端口若落在 [PORT_BASE, PORT_BASE+9) 区间内会
拒绝启动（退出码 2）。配置读取优先级：命令行参数 > 进程环境 > `<home>/fleet.env` > 默认，
fleet.env 缺失时自动降级（桥显示 401、配置字段标 MISSING）而不是崩掉。

安全：只监听 127.0.0.1，不对外暴露；key 一律只显示 md5 前 8 位；日志接口只接受桥名
白名单，路径穿越会被挡掉。写操作只有 `/api/action/checkin` 与 `/api/action/restart/<name>`，
桥名不在白名单里直接返回 unknown bridge。

## 免费模型标注（官网信息 + 时段）

`free-windows.json` 按各服务官网/官方定价页逐条标注每个模型的免费状态与时段
（2026-09-26 抓取）；`tools/free_models.py` 把它与 ocx live、Codex catalog 合并输出。
改标注只改 JSON，不用动代码。

    python3 tools/free_models.py                 # 全量标注表（188 个模型）
    python3 tools/free_models.py --free-only     # 只看免费类
    python3 tools/free_models.py --provider qoder
    python3 tools/free_models.py --missing       # 选择器缺口报告
    python3 tools/free_models.py --json          # 机器可读（状态面板同源）
    python3 tools/free_models.py --check-sources # 官网来源可达性

状态面板（8796）新增「免费模型标注」区块：徽标 + 时段 + 是否在选择器，与 CLI 同源。

### 官网标注结果（2026-09-26 抓取）

| 服务 | 免费状态 | 时段 / 限额（官网要点） |
|------|----------|--------------------------|
| Trae 国内版（trae.cn） | 免费档长期 | 免费计划 ¥0：所有功能均可免费使用、2 个并发云任务 |
| Trae 国际版（trae.ai） | 免费档长期 | Free $0：仅 Auto 模式、限量使用、每月 5000 次补全、2 并发云任务；Pro $20/月起全模型 |
| CodeBuddy 国内版 | 限时免费个人版 | Hy4 preview 限免两周：2026-09-10 → 2026-09-23（已结束） |
| WorkBuddy 海外版 | 限免 + Free 计划 | Hy4 preview 限免两周：2026-08-28 → 2026-09-23（已结束） |
| 团结AI Codely | 免费额度 | 每月「月度免费点数」优先扣；Lite/Pro/Max 每 5 小时 + 每周限额，Ultra 无 5 小时窗口 |
| 灵犀 LingXi | 登录免费 | 7 天滚动 + 5 小时 + 30 天窗口额度；加量包 30 天有效 |
| Qoder | 试用 2 周 | 新用户 2 周 Pro 试用（全 Pro 功能），到期降 Free（Community）计划 |
| TokenDance | 按量 + 峰谷 | DeepSeek V4 官方端点高峰 = 周一至周五 09:00–12:00、14:00–18:00；千帆/阿里云端点（仅 deepseek-v4-flash-0731）高峰 = 每天 08:00–22:00；其余空闲时段 5 折 |
| 商汤小浣熊 | 未明示 | 官网 SPA 未公示价格，以登录后控制台为准 |
| Gemini（Code Assist） | 订阅内含 | 免费档月度限额 + 本机 Google AI Pro 会员；当前桥 502 需重新登录 |
| CatPaw（美团） | 不可达 | 需公司 VPN，无法核实 |

### 选择器短名（short_aliases）

选择器原名是完整 slug（`xhx/xhx-sn-sensenova-6-8-flash-lite` 36 字符，面板里看不全）。
`tools/short_aliases.py` 用 ocx 别名把显示名压到 20 字符内（路由用的 slug 不变）：

    python3 tools/short_aliases.py            # 应用 + ocx sync（幂等，可反复跑）
    python3 tools/short_aliases.py --dry-run  # 只看映射

provider 别名：workbuddy→wb、workbuddy-gpt→wbg、codely→cdl、lingxi→lx、gemini→gem、
qoder→qdr、tokendance→tok；模型名按词典压缩（deepseek→ds、flash→fl、preview→pv、
sensenova 直接去掉、gpt- 前缀去掉等）。效果示例：

| 原名 | 短名 |
|------|------|
| xhx/xhx-sn-sensenova-6-8-flash-lite | xhx/sn-6-8-fl-lite |
| trae/trae-DeepSeek-V4-Flash-Official | trae/ds-v4-fl-off |
| workbuddy-gpt/gpt-5.6-luna | wbg/gpt-5.6-luna |
| workbuddy/hy4-preview | wb/hy4-pv |

别名存在 opencodex 代理配置里，catalog 同步 / 重启都不丢；脚本会顺手清理「键写错」
的旧别名（连字符形式 vs 原生 id 的斜杠形式）。改词典改 `tools/short_aliases.py`
里的 TOKEN_MAP / PROVIDER_ALIAS 即可。

### 为什么有的模型不在选择器

- **tokendance**：ocx 里只 selected 了 `step-5-preview`，其余 94 个 live 模型没进 catalog。
  要更多：`ocx models selected tokendance --set <id,id>` 然后 `ocx sync`。
- **openai 原生 7 个**（gpt-5.5 / 5.6 / 6.x）：ocx 内置（native），由 ChatGPT 账号直接
  管理，按设计不进反代理 catalog。
- **catpaw**：需美团 VPN，8795 不可达 → live = 0。
- **qoder**（2026-09-26 修复）：桥（8789）一直有 15 个模型，但从未注册进 ocx
  （live = 0）。已执行 `ocx provider add qoder --adapter openai-chat --base-url
  http://127.0.0.1:8789/v1 --api-key <plist 里的 QODER2CODEX_KEY> --allow-private-network`，14 个模型进入 catalog；已 `ocx service restart` 让代理加载，qoder 聊天经代理实测 OK。`opencodex/setup-providers.sh` 本就包含 qoder（本机当初漏注册），已加 plist key 回退。
- 选择器里模型名**不带** provider 前缀（显示 `hy4-preview` 而不是
  `workbuddy/hy4-preview`）；catalog 的 `slug` 字段才带前缀。

## Codex 模型目录看门狗（ocx-catalog-guard）

Codex 的模型选择器读的是 `model_catalog_json` 指向的那个 catalog 文件，而 opencodex 是把
反代理模型**追加**进去的。只要有个第三方 provider 切换器（典型是 CC Switch）重新生成这个
catalog，追加的模型就被冲掉——表现为「重启一下 app，反代理的模型全没法选了」。

`ocx ensure` 治不了：它发现 `config.toml` 的 `model_provider` 被外部占用时会直接跳过注入
（实测 2026-09-26：把 catalog 抹成 1 个模型后跑 `ocx ensure`，反代理模型数仍是 0）。
只有 `ocx sync` 会同时刷新 catalog 和 models 缓存。

kit 因此带一个看门狗，数 catalog 里带 `/` 的桥模型，少于阈值就自动 `ocx sync`：

    bash ~/fleet/tools/ocx-catalog-guard.sh status           # 桥模型数 + timer 状态
    bash ~/fleet/tools/ocx-catalog-guard.sh run              # 立即检查并自愈
    bash ~/fleet/tools/ocx-catalog-guard.sh install-timer
    bash ~/fleet/tools/ocx-catalog-guard.sh uninstall-timer

默认装 launchd 常驻（`StartInterval` 300s + `RunAtLoad`），随 opencodex 接线一起启用，
`--no-ocx-guard` 可关掉。日志：`~/Library/Logs/ocx-catalog-guard.log`。
阈值 `--min-models` 默认 60（9 桥齐全约 72，个别桥掉线不会误触发）。

注意：磁盘上的 catalog 修好之后，**已经在跑的 app 仍显示旧列表**，需要重启一次
Codex/ChatGPT（`ocx sync --restart-codex` 能自动做，但会结束进行中的会话）。

## 登录表

| name | 登录方式 | 凭据位置 | 备注 |
|------|----------|----------|------|
| workbuddy | WorkBuddy 海外版桌面 App 登录 | ~/.workbuddy/local_storage（或把 auth JSON 放进 ~/fleet/bridges/workbuddy2codex/auths） | 桥自动读取桌面端登录态 |
| workbuddy-gpt | 同上；第二账号的 auth JSON 放进 ~/fleet/auths-gpt | 同左 | 与 workbuddy 共用同一个本地 key |
| qoder | npm i -g @qodercn-ai/qoderclicn，按 CLI 流程登录 | ~/.qoder-cn/.auth/user | 桥以非交互模式调用 CLI |
| codely | 官方 CLI 设备码登录 | ~/.codely-cli/oauth_creds.json | 账号需先在网页端激活（见已知问题） |
| trae | Trae CN IDE 登录 | IDE 登录态；桥缓存到 ~/.trae2codex/creds.json | |
| lingxi | python3 ~/fleet/bridges/lingxi/login_helper.py 打开浏览器登录 | ~/.LingXi/auth.json | |
| xhx | 商汤小浣熊桌面 app 登录 | ~/.box-agent/config/auth.json | 桌面端会重写该文件，属正常 |
| gemini | gemini login，或 python3 ~/fleet/bridges/gemini/extract_cookies.py 导出 cookie | ~/.gemini/jetski-standalone-oauth-token 或 ~/.gemini2codex/cookies.txt | 账号需通过 Google 验证（见已知问题） |
| catpaw | CatPawAI 桌面 App 登录 | ~/Library/Application Support/CatPawAI/User/globalStorage/state.vscdb | 需美团内网/VPN（见已知问题） |

每个服务登录后运行对应的 bridges/finish.sh <name>：重启桥 → 等待 /v1/models →
列出模型 → 注入 Codex 模型目录 → ocx sync → 冒烟聊天一次。

## 随附工具

- deploy.sh：一条命令跑完 preflight → 安装 → 等桥 → 逐桥收尾 → ocx → 签到 timer → 汇总
- bridges/finish.sh <name> [--home DIR] [--tries N] [--skip-chat]：单桥收尾（重启+验收+同步）
- tools/status.sh [--home DIR]：9 桥健康表（launchd/监听/模型数/key md5）+ ocx 状态 + 今日签到
- tools/fleet_chat_test.py [--port-base N]：全舰队 /v1/models + 聊天测试（只打印 key 的 md5）
- tools/checkin.sh status|run-now|install-timer|uninstall-timer [--home DIR]：每日积分签到
- tools/status_ui.sh start|stop|install-timer|uninstall-timer [--home DIR]：状态面板（默认 127.0.0.1:8796）
- tools/status_ui.py [--port N] [--no-browser] [--once]：面板实现（stdlib 单文件；/api/status、/api/logs/<name>、/api/action/*）
- tools/ocx-catalog-guard.sh run|install-timer|uninstall-timer|status：反代理模型 catalog 看门狗（默认 300s）
- tools/free_models.py [--free-only] [--provider P] [--missing] [--json] [--check-sources]：免费模型标注（数据在仓库根 free-windows.json，状态面板同源）
- tools/short_aliases.py [--dry-run]：选择器短名（ocx 别名，路由不受影响）
- tools/checkin.py [--run-now|--status|--daemon]：签到实现（幂等，CST 记「今日」）
- opencodex/setup-providers.sh：重新注册 9 个 ocx provider（新机换端口后用）
- uninstall.sh [--home DIR] [--purge]：卸载 launchd 服务和 plist；--purge 连目录一起删

## 已知问题（2026-09-26 实测，均为用户侧/外部条件）

1. **tokendance（影响默认模型）**：API key 已失效——网关聊天端点返回
   401「API 密钥不存在」（/v1/models 列表端点是公开的，所以模型照样列得出）。
   表现为默认模型 `step-5-preview` 不可用。需到 tokendance.space 控制台重建 key，
   然后 `ocx provider add tokendance --adapter openai-chat --base-url
   https://tokendance.space/gateway/v1 --api-key <新key> --force` + `ocx sync`。
2. codely：上游网关对 chat 一律返回 400「欢迎使用Codely」onboarding 门禁。
   /v1/models 正常、key 有效；需登录 codely.tuanjie.cn 网页端完成首次激活。
3. gemini：502，且不是 token 过期——Google 已于 2026-06-18 关停 Code Assist
   individuals/AI Pro/Ultra 档（含 Gemini CLI 登录），该档本就是个人账号免费档
   （60 次/分、1000 次/天，AI Pro 只抬限额不单独计费）。需迁移 Antigravity 或改
   AI Studio key；详见 docs/free-models-runbook.md「Gemini 档位定性」。
4. catpaw：需要美团内网/VPN，否则 catpaw.sankuai.com 不可达（Tunnel 503）。
   连上 VPN 后执行：launchctl kickstart -k gui/$(id -u)/com.local.catpaw2codex

2026-09-26 fleet_chat_test 实测：6 桥 PASS（workbuddy、workbuddy-gpt、qoder、trae、
lingxi、xhx），codely 400 / gemini 502 / catpaw VPN 三项失败均为上述用户侧条件。

## 安全说明

- 8 个本地 key 只写在 <home>/fleet.env（权限 600），仅本机使用，不要提交 git 或外发
- 所有桥只监听 127.0.0.1；gemini/catpaw 两桥不校验本地 key（Authorization 只用于上游 Google/美团）
- 测试脚本只打印 key 的 md5，不打印明文
- 状态面板只监听 127.0.0.1；key 只显示 md5 前 8 位；日志接口走桥名白名单

## 目录结构

    fleet-kit/
      install.sh            一键安装（plist + venv + fleet.env + ocx 注册）
      deploy.sh             一键部署流水线（install + finish + 签到 timer + 汇总）
      uninstall.sh          卸载
      requirements.txt      Python 依赖
      README.md             本文
      bridges/              9 座桥源码 + finish.sh
      opencodex/            setup-providers.sh（ocx provider 注册）
      free-windows.json     免费模型标注数据（官网信息 + 时段，改这里不改代码）
      tools/                status.sh / status_ui.sh / status_ui.py / ocx-catalog-guard.sh / checkin.sh / checkin.py / fleet_chat_test.py / fleet_split.py / free_models.py / short_aliases.py
      docs/                 各桥 runbook + 全量实测报告

## 可选：TokenDance

fleet.env 里取消注释 TOKENDANCE_API_KEY= 并填入 key，重跑
bash ~/fleet/opencodex/setup-providers.sh 即追加 tokendance provider
（https://tokendance.space/gateway/v1，模型 step-5-preview 等）。

## 卸载

    bash ~/fleet/uninstall.sh            # 停服务并删 plist
    bash ~/fleet/uninstall.sh --purge    # 连 ~/fleet 目录一起删

注意：uninstall.sh 不动 ocx 的 provider 配置；如需清除，用 ocx 自带命令管理。
