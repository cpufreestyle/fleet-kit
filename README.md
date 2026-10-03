# FleetKit — 11 桥反代理舰队一键部署包

[![ci](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

把一套「Codex → 本地反代理桥 → 各 AI 订阅服务」的舰队打包成可在其他 macOS 电脑
一键部署的 kit：10 座 OpenAI 兼容本地桥 + opencodex 集成 + 登录/验收/卸载脚本。

测试基线（2026-09-26）：5/10 桥实测 PASS，5 项失败均为用户侧条件，见「已知问题」。
真实调用基线（2026-09-26）：`tools/verify_real_calls.py` 用随机运算题核验，5/10 桥真实推理
（workbuddy、workbuddy-gpt、qoder、trae、xhx），其余 5 桥为登录门禁/会话失效/上游关停/需 VPN/Google 网络阻断，见「真实调用检测」。

## 镜像同步

GitHub 是本仓库的主 remote（origin）。当国际出口阻断、GitHub 不可达时，同一个
仓库同步推送到 Gitee 私有镜像，凭证已存在本机 ~/.git-credentials：

    git push gitee main      # 推到 Gitee 镜像 cpufreestyle/fleet-kit
    git push origin main     # 网络恢复后补推 GitHub

两个 remote 内容一致；git remote -v 里的 gitee 即镜像。2026-10-01 首次同步，
当时 main 领先 origin 5 个提交，全部历史已落在镜像上。

## 架构

    Codex ──▶ opencodex 代理 (127.0.0.1:10100)
                 │  [model_providers.*] allow-private-network
                 ▼
           15 座本地桥 (127.0.0.1:8787 .. 8803)
                 ▼
           WorkBuddy 国内版 / 海外版 / Qoder / 团结AI / Trae / 灵犀 / 小浣熊 / Gemini / CatPaw / TokenDance / Cline / ZCode / Kimi Code / MiniMax

| 桥 (name)        | 默认端口 | 上游服务            | 探针模型                  |
|------------------|---------|---------------------|---------------------------|
| workbuddy        | 8787    | WorkBuddy 国内版    | hy4-preview               |
| workbuddy-gpt    | 8788    | WorkBuddy 海外版    | gpt-6-astra / gpt-5.6     |
| qoder            | 8789    | Qoder CN            | auto                      |
| codely           | 8790    | 团结 AI (tuanjie)   | codely-core               |
| trae             | 8791    | Trae CN             | DeepSeek-V4-Pro 等        |
| lingxi           | 8792    | 灵犀 (LingXi)       | glm-5.3-flash 等          |
| xhx              | 8793    | 商汤小浣熊          | raccoon-*                 |
| gemini           | 8794    | Google Gemini       | gemini-3-pro-preview 等   |
| catpaw           | 8795    | CatPawAI (美团)     | glm-5.2 等                |
| antigravity      | 8797    | Google Antigravity  | claude-opus-4-8 / gemini-3.1-pro-preview 等 |
| qwen             | 8798    | 阿里 Qwen Cloud     | qwen3.8-flash / qwen3.8-max 等          |
| cline            | 8799    | Cline 免费池        | deepseek-v4.1-flash 等    |
| zcode            | 8800    | ZCode / 阿里云      | GLM-5.3 / GLM-5.3-Flash   |
| kimi             | 8802    | Kimi Code (Moonshot)| kimi-for-coding / k3      |
| minimax          | 8803    | MiniMax             | MiniMax-M3 / M2.7 等      |

Codex 里模型以 `桥名/模型` 出现，例如 `workbuddy/hy4-preview`。

## 项目命名与目录

项目名 **FleetKit**（十五桥反代理舰队）。git 源码与运行目录分离：

    <项目目录>/kit/       git 仓库（本文档所在）：改代码、git pull 都在这里
    <项目目录>/runtime/   运行根（FLEET_HOME）：13 座桥、fleet.env、logs、checkin

共 13 座桥 + 若干 timer（签到、ocx catalog 看门狗、可达性探测）全部指向
`<项目目录>/runtime/`，日志统一落在 `<项目目录>/runtime/logs/`。换机器或起第二套时用
`install.sh --home <目录>` 指定别的运行根。

日常命令（`<R>` = 运行根，例如本机 `<项目目录>/runtime`）：

    bash "<R>/tools/status.sh"                  # 13 座桥健康表 + ocx 状态
    bash "<R>/tools/checkin.sh" status          # 签到状态
    bash "<R>/bridges/finish.sh" <name>         # 某座桥登录后收尾
    bash "<R>/uninstall.sh"                     # 卸载

## 两座 WorkBuddy 桥

FleetKit 里有两座 WorkBuddy 桥，分别打国内版和海外版，端口固定：

| name | 端口 | 上游 | 目录 | 凭据来源 |
|------|------|------|------|----------|
| `workbuddy` | 8787 | 国内版 `copilot.tencent.com` | `runtime/bridges/workbuddy-cn/` | 文件账号池 `runtime/bridges/workbuddy-cn/auths/` |
| `workbuddy-gpt` | 8788 | 海外版 `www.workbuddy.ai` | `runtime/bridges/workbuddy-gpt/` | 桌面端 `~/.workbuddy-ai/local_storage`；池 `runtime/bridges/workbuddy-gpt/auths/` |

两座桥的代码几乎完全相同，共享实现放在 `bridges/workbuddy/`（`core.py` 承载 99% 逻辑，
外加 `netpin.py` DNS 固定和 6 个账号/签到/脱敏模块）。`workbuddy-cn/converter.py` 与
`workbuddy-gpt/converter.py` 只是几十行入口，靠 `WORKBUDDY_PROVIDER=cn|gpt` 选一套
provider 表（上游地址、模型名、是否 DNS 固定、uvicorn 参数等），`WORKBUDDY_BRIDGE_DIR`
让各自的 `auths/`、`bridge-settings.json`、`assets/` 仍落在自己的桥目录里。改 WorkBuddy
逻辑只改 `bridges/workbuddy/core.py`，两座桥同步生效；`install.sh` 会把
`workbuddy-cn` / `workbuddy-gpt` 里遗留的共享模块副本清掉（`prune_shared_workbuddy`）。

两座桥共用同一个本地 key `CODEBUDDY2OPENAI_KEY`（值在 `runtime/fleet.env`）。Codex 侧模型名带桥名前缀：
`workbuddy/<model>` 与 `workbuddy-gpt/<model>`（例如 `workbuddy/hy4-preview`、`workbuddy-gpt/hy4-preview`）。

海外版桥比国内版多两个环境变量（`install.sh` 已代设）：`WORKBUDDY_LOCAL_STORAGE` 指向桌面端登录态，
`WORKBUDDY_AUTH_POOL_DIR` 指向备用账号池。登录态失效时先确认海外版桌面 App 已登录；两座桥都可以把
auth JSON 丢进各自 `auths/` 目录，然后运行 `bash "<项目目录>/runtime/bridges/finish.sh" workbuddy`
（或 `workbuddy-gpt`）收尾。

## 跨平台：macOS / Windows / Linux

同一套脚本跑三个系统，服务托管交给 `tools/platform.sh` 自动选择后端：

| 系统 | 常驻桥 | 定时 timer | 服务定义目录 |
|------|--------|-----------|--------------|
| macOS | launchd plist（`launchctl bootstrap/kickstart`） | `StartInterval` / `StartCalendarInterval` | `~/Library/LaunchAgents` |
| Windows | 计划任务 `schtasks`（登录触发 + 重复间隔） | 计划任务 `TimeTrigger` / `CalendarTrigger` | `%LOCALAPPDATA%\FleetKit\services` |
| Linux | 独立进程 + `while true` 自重启包装脚本 | 同一包装脚本内 `sleep N` | `~/.local/share/FleetKit/services` |

Windows 需要 **Git Bash** 或 **PowerShell 7** 提供 `bash`（脚本本身是 bash），
并且 `schtasks` 必须可用（Windows 自带）。Linux 只需 `bash` + `python3` + `pkill`。

平台相关的上游标识（`"platform": "darwin"`、`platform=darwin-arm64` 之类）默认仍发
darwin，因为部分上游按客户端标识做风控；要改就设环境变量：

    export FLEET_CLIENT_PLATFORM=win32-x64   # zcode 的 app_version/platform
    export FLEET_PLATFORM_SHORT=win32        # trae / catpaw / codely 的 platform 字段

## 搬迁与路径含空格

FleetKit 设计为可整体搬走：`kit/`（源码）与 `runtime/`（运行根）两个目录一起拷贝到新机器或新位置，
然后跑 `bash "<新目录>/kit/install.sh --home <新目录>/runtime"` 重写服务定义
（所有服务的路径全部由 `--home` 决定；换机、跨平台、起第二套舰队都用这一条）。

注意：**项目路径含空格也能跑**（例如 `~/AI Shared/repo/FleetKit`）。相关约定：

- `runtime/fleet.env` 里每个值都必须用双引号包住，否则含空格的值会被 shell 拆词。
- 本文档所有命令都已给含空格的路径加了引号，可直接复制粘贴。
- `install.sh` 遇到空格路径只 warn、不退出（见 `install.sh` 的 FLEET_HOME 检查段）。
- `install.sh` 的默认安装目录仍是 `~/FleetKit/runtime`，含空格路径下行为一致。

## 环境要求

- macOS 12+ / Windows 10+ / Linux（服务后端见上表）
- python3 >= 3.9（install.sh 自动建 venv 并装依赖；Windows 用 `.venv\Scripts\python.exe`）
- node/npm（仅用于安装 opencodex：npm install -g @bitkyc08/opencodex）
- Windows：Git Bash（或 PowerShell + bash）与系统自带的 `schtasks`
- 各服务的账号/订阅（见「登录表」）

## 快速开始（3 步）

1. 取代码并安装（`<PRJ>` = 项目目录，各平台自选，例如 macOS `~/AI Shared/repo/FleetKit`、
   Windows `C:\\Users\\<你>\\FleetKit`）：

      # 方式 A：git clone（推荐，之后可 git pull 更新）
      mkdir -p "<PRJ>"
      git clone https://github.com/cpufreestyle/fleet-kit.git "<PRJ>/kit"

      # 方式 B：解包 tar
      cd ~ && tar xzf fleet-kit.tar.gz
      mkdir -p "<PRJ>"
      mv fleet-kit "<PRJ>/kit"

      bash "<PRJ>/kit/install.sh" --home "<PRJ>/runtime"

   Windows 在 Git Bash 里跑同一条命令；没有 Git Bash 时先装 Git for Windows。

2. 登录你想用的服务（见「登录表」），每个服务登录完成后运行：

       bash "<PRJ>/runtime/bridges/finish.sh" <name>

3. 验收：

       bash "<PRJ>/runtime/tools/status.sh"
       python3 "<PRJ>/runtime/tools/fleet_chat_test.py"

   如果装了状态面板（--with-ui），顺手打开 http://127.0.0.1:8796/ 看一眼。

在 Codex 中使用：确保 ocx 服务在跑（ocx service），模型选择器里选
`桥名/模型`（如 `workbuddy/hy4-preview`）。

## 安装选项

    install.sh [--home DIR] [--port-base N] [--with-opencodex|--no-opencodex]
               [--no-start] [--skip-deps] [--dry-run]
               [--with-checkin] [--with-ui] [--no-ocx-guard] [-h]

- `--home DIR`：安装根目录，默认 `~/FleetKit/runtime`。本仓库是 `kit/` 与
  `runtime/` 同级布局，所以示例命令一律显式写 `--home "<PRJ>/runtime"`；
  不传的话 dry-run 也会照着 `~/FleetKit` 报计划
- `--port-base N`：起始端口，13 座桥依次占用 N .. N+13（offset 9 留空），默认 8787
- `--with-checkin`：装每日 09:00 CST 签到 timer（xhx  SenseTime 积分 + workbuddy 桥存活确认）
- `--with-ui`：装本地状态面板 launchd 常驻服务，端口 N+9（默认 8796）
- `--no-ocx-guard`：关掉反代理模型 catalog 看门狗（默认随 opencodex 一起装）
- `--no-opencodex`：跳过 ocx provider 注册（之后可手动跑 bash "<PRJ>/runtime/opencodex/setup-providers.sh）"
- `--no-start`：只写文件和 plist，不启动桥
- `--skip-deps`：跳过 venv/依赖安装（用系统 python3）
- `--dry-run`：只打印计划，不落盘

换端口后记得同步测试脚本：python3 "<PRJ>/runtime/tools/fleet_chat_test.py" --port-base 9787。

### 高级：起第二套舰队

env 覆盖 launchd 目录/标签前缀/日志目录，即可与现有舰队并存：

    FLEET_LAUNCH_DIR=/tmp/f2/LaunchAgents FLEET_LABEL_PREFIX=com.localtest \
    FLEET_LOG_DIR=/tmp/f2/logs bash install.sh --home /tmp/f2 --port-base 9787

第二套同样可以加 `--with-checkin --with-ui`：签到 key 落在 /tmp/f2/fleet.env，状态面板
落 N+9（9787 → 9796），plist 带 `--home /tmp/f2`，读的是第二套的 fleet.env。

## 部署自动化（deploy.sh）

一条命令跑完整条流水线，适合新机首装或整套重装：

    bash deploy.sh                      # 装到 "<PRJ>/runtime，端口" 8787..8798

    bash deploy.sh --home /tmp/fleet-a --port-base 9687 \
        --with-checkin --with-ui --no-opencodex --smoke

流程：preflight（Darwin/python3/curl/launchctl + 端口占用）→ 可选 --update（git pull）
→ install.sh → 轮询 9 个端口（120s 超时）→ bridges/finish.sh 逐桥收尾
→ setup-providers.sh + 签到 timer → status.sh + 汇总。

| 选项 | 作用 |
|------|------|
| `--home DIR` | 安装根目录，默认 "<PRJ>/runtime" |
| `--port-base N` | 起始端口，默认 8787 |
| `--with-checkin` | 装每日 09:00 CST 签到 timer（xhx + workbuddy 两项任务）|
| `--with-ui` | 装本地状态面板 launchd 常驻服务（端口 N+9） |
| `--no-ocx-guard` | 跳过反代理模型 catalog 看门狗 timer（默认随 opencodex 装） |
| `--no-opencodex` | 跳过 ocx provider 注册 |
| `--smoke` | 每桥跑一次聊天冒烟（需要已登录） |
| `--update` | 安装前先 git pull 更新 kit |

退出语义：`0` = 该起的桥都就绪（个别没登录只算 warning）；`1` = preflight 失败或
**一座桥都没起来**。单座桥连不上（VPN、内网、地域封锁）不会中断部署，只在汇总里
列为 unreachable，其余桥照常收尾；之后用 `bash "<PRJ>/runtime/bridges/finish.sh" <name>` 补收。

## 自动签到（checkin.sh）

部分上游服务每日登录送积分/额度，过期不补。kit 内置一个 launchd 定时任务，
每天 09:00（CST）自动跑一遍，幂等：当天已签、或桌面端启动时已领，就直接跳过。

    bash "<PRJ>/runtime/tools/checkin.sh" status          # 今日是否签 + 余额
    bash "<PRJ>/runtime/tools/checkin.sh" run-now         # 立即跑全部任务
    bash "<PRJ>/runtime/tools/checkin.sh" run-now xhx     # 只跑指定任务
    bash "<PRJ>/runtime/tools/checkin.sh" install-timer   # 装每日 timer
    bash "<PRJ>/runtime/tools/checkin.sh" uninstall-timer # 卸掉 timer

安装时加一个开关即可（等价于装完再跑 install-timer）：

    bash install.sh --with-checkin

状态与日志位置（`--home DIR` 可改根目录，默认 "<PRJ>/runtime）："

    <home>/checkin/state.json     # 每任务：今日是否成功、余额、上次时间
    <home>/logs/checkin.log       # timer 运行日志

当前注册两个任务：

| 任务 | 说明 |
|------|------|
| `xhx` | 商汤小浣熊每日登录积分；凭据来自 `~/.box-agent/config/auth.json` |
| `workbuddy` | WorkBuddy Buddy 加油站。账号领取由桥内 `workbuddy_checkin.py` 在桥启动/每日运行时自动完成；本任务校验桥健康并在签到状态中汇总 |

WorkBuddy 的 dashboard 领取接口保持 cookie 鉴权，独立 timer 不复制凭据、不绕过
鉴权；它只确认账号池桥在线，领取仍由桥内幂等 worker 负责。

## 本地状态面板（status_ui）

零依赖的本地网页面板（Python 标准库单文件），用来看舰队整体运行情况：13 座桥的健康、
模型数、ocx、今日签到、日志尾巴、每座桥的真实调用判定，并且可以直接点按钮做签到 / 强制重签 /
重启单座桥 / 发起真实调用核验。

    bash tools/status_ui.sh start            # 启动（默认 http://127.0.0.1:8796/）
    bash tools/status_ui.sh stop
    bash tools/status_ui.sh install-timer    # launchd 常驻（KeepAlive + RunAtLoad）
    bash tools/status_ui.sh uninstall-timer

页面内容：每座桥一行，显示 launchd 状态 + 退出码、端口监听 pid、/v1/models 模型数与
探针延迟、key 的 md5 前 8 位、以及「真实调用」列（未核验 / REAL / GATE 等判定，来自
verify_real_calls.py 的最近一次快照）；下面依次是 ocx 状态、今日签到结果与余额、
「真实调用核验」面板（各桥判据明细 + 立即核验按钮）、各桥日志 tail。汇总卡片新增
「真实调用 REAL 座数」。核验是真实计费调用（随机运算题抗伪造，一轮约 3 分钟），只在
点「立即核验」时执行，结果原子落盘 <home>/real_calls.json，刷新面板不重复计费。
与余额、各桥日志 tail。提供签到 / 强制重签按钮、单桥重启按钮、10 秒自动刷新。

安装时装上：`bash install.sh --with-ui`（或 `bash deploy.sh --with-ui`）。面板端口是
PORT_BASE+9，默认 8796，与 13 座桥错开；端口若等于某个“实际桥端口”(PORT_BASE+offset) 会
拒绝启动（退出码 2）。配置读取优先级：命令行参数 > 进程环境 > `<home>/fleet.env` > 默认，
fleet.env 缺失时自动降级（桥显示 401、配置字段标 MISSING）而不是崩掉。

安全：只监听 127.0.0.1，不对外暴露；key 一律只显示 md5 前 8 位；日志接口只接受桥名
白名单，路径穿越会被挡掉。写操作只有 `/api/action/checkin` 与 `/api/action/restart/<name>`，
桥名不在白名单里直接返回 unknown bridge。

## 模型标注：免费状态 + 是否走客户端积分

`free-windows.json` 按各服务官网/官方定价页逐条标注每个模型的**免费状态与时段**，
以及**这次调用走不走客户端积分**（2026-09-29 补第二维）；`tools/free_models.py` 把它与
ocx live、Codex catalog 合并输出。改标注只改 JSON，不用动代码。

「走客户端积分」的口径（`legend.credits`）：

| credits | 含义 | 会不会把账号额度用光 |
|----------|------|----------------------|
| `client` | 消耗被反代理客户端账号内的点数/tokens/次数 | **会**，可扣完 |
| `limit` | 官方不计点，只占账号免费限额（限速/限次） | 不会，只会被限流 |
| `own` | 独立 API Key 余额 / 官方按量付费 / 原生订阅 | 不会，与反代理账号无关 |
| `unknown` | 官网未公示或当前不可达 | 无法核实 |

    python3 tools/free_models.py                          # 全量标注表（两维徽标 + 时段）
    python3 tools/free_models.py --credits client         # 只看会扣客户端积分的
    python3 tools/free_models.py --credits own            # 只看独立 Key / 原生订阅
    python3 tools/free_models.py --free-only              # 只看免费类
    python3 tools/free_models.py --provider qoder
    python3 tools/free_models.py --missing                # 选择器缺口报告
    python3 tools/free_models.py --json                   # 机器可读（状态面板同源）
    python3 tools/free_models.py --check-sources          # 官网来源可达性

状态面板（8796）的「模型标注」区块：免费徽标 + **客户端积分徽标** + **写代码徽标** + 时段 + 是否在选择器，
与 CLI 同源；积分单元格 hover 显示该行积分口径来源（如「codely 月度免费点数优先扣」）。
两个开关：「隐藏不可用」（探测即停或 verdict≠REAL 的 provider）、
「只看走客户端积分」（只列 `client` 行，方便排查额度耗尽）。

「写代码」列（2026-10-03 新增）：徽标来自 `code_model_bench.py snapshot` 的实测判定
（全过 / 部分 / NO_RUN / 断桥 / `?` = 无实测），hover 显示判定、实测日期与耗时；
快照落盘 `kit/code-capability.json`，单桥 snapshot 不清空其他模型的旧记录，超过
7 天（`stale_after_days`）标 stale 但不丢弃。实测方法与命令见
`docs/code-model-selection.md`。

### 哪些走客户端积分（2026-09-29 按官网/桥代码核实）

| 走客户端积分（`client`） | 积分形态 |
|--------------------------|----------|
| workbuddy / workbuddy-gpt | 每模型 credits 倍率（`auto` 为动态倍率），仪表盘可见余额与折扣倍率 |
| codely（团结AI） | 月度免费点数 + 充值点数，扣减时优先扣免费点数 |
| lingxi（灵犀） | 「灵力」额度，7 天滚动 + 5 小时 + 30 天窗口 |
| xhx（商汤小浣熊） | 每日登录积分（available / daily / reward points） |
| zcode | Start Plan 3,000,000 tokens/天 + Weekend Build 300,000,000 tokens 一次性 |

| 不走客户端积分 | 原因 |
|----------------|------|
| trae / qoder / cline（free 家族）/ gemini / antigravity | `limit`：官方不计点，只限速限次（Trae 免费档「限量使用」、Gemini 60 次/分 1000 次/天、Cline free 档 input/output 计费 0） |
| tokendance / stepfun | `own`：独立 API Key 按量计费余额，与反代理客户端账号无关 |
| openai（gpt-* 原生） | `own`：ChatGPT 原生订阅，按设计不进反代理 catalog |
| catpaw | `unknown`：需美团公司 VPN，当前不可达，无法核实 |

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
| CatPaw（美团） | 未明示 | 官网/App 未公示价格；provider 已注册（10 个模型在选择器），聊天需连美团内网/VPN |
| Antigravity | 登录免费带速率限制 | Google 账号登录即用；2026-06-18 起个人版 Code Assist 已关停，Antigravity 是 Google 保留的免费入口（12 个模型由 App 二进制提取） |
| Qwen Cloud（阿里国际站） | 免费额度 | 注册即赠 70M+ tokens（官网定价页标注）；「Qwen4 架构预览」= HF Qwen3.8-Flash-Next（开放权重、无邀请码），生产托管版 qwen3.8-flash 同架构、1M 上下文、官网明示支持 Codex |

### 选择器短名（short_aliases）

选择器原名是完整 slug（`xhx/xhx-sn-sensenova-6-8-flash-lite` 36 字符，面板里看不全）。
`tools/short_aliases.py` 用 ocx 别名把显示名压到 20 字符内（路由用的 slug 不变）：

    python3 tools/short_aliases.py            # 应用 + ocx sync（幂等，可反复跑）
    python3 tools/short_aliases.py --dry-run  # 只看映射
    # setup-providers.sh 已在所有 provider add 之后自动调用它

provider 别名：workbuddy→wb、workbuddy-gpt→wbg、codely→cdl、lingxi→lx、gemini→gem、
qoder→qdr、tokendance→tok、catpaw→cpw、antigravity→agy；模型名按词典压缩（deepseek→ds、flash→fl、preview→pv、
sensenova 直接去掉、gpt- 前缀去掉等）。效果示例：

| 原名 | 短名 |
|------|------|
| xhx/xhx-sn-sensenova-6-8-flash-lite | xhx/sn-6-8-fl-lite |
| trae/trae-DeepSeek-V4-Flash-Official | trae/ds-v4-fl-off |
| workbuddy-gpt/gpt-5.6-luna | wbg/gpt-5.6-luna |
| workbuddy/hy4-preview | wb/hy4-pv |
| catpaw/glm-5.2 | cpw/glm5.2 |

别名存在 opencodex 代理配置里，catalog 同步 / 重启都不丢；脚本会顺手清理「键写错」
的旧别名（连字符形式 vs 原生 id 的斜杠形式）。改词典改 `tools/short_aliases.py`
里的 TOKEN_MAP / PROVIDER_ALIAS 即可。

> **注意**：`ocx provider add <name> --force` 会把该 provider 的 `alias` 和
> `modelAliases` 两个键**整个清掉**，跑一次 setup 所有短名就全丢。所以
> `opencodex/setup-providers.sh` 在所有 `provider add` 之后、`ocx service restart`
> 之前自动重跑一次 `tools/short_aliases.py` 补回短名。手工加/改 provider 后也要同样
> 补跑，否则选择器名称会退回完整 slug。

### 默认模型

Codex 启动时选中的模型由 `~/.codex/config.toml` 的 `model` 键决定（值填 catalog slug）。
当前默认 `workbuddy/deepseek-v4-flash`（2026-10-03 起）：全机队 21 个模型
实测里唯一能稳定写出本仓库代码的路线，两道 FleetKit 真实代码题 8/8 + 7/7、
prose 0、单轮 7.5s，端到端过网关 10100 实测 `E2E_OK`。选型依据见
docs/code-model-selection.md。
港湾仍是 `stepfun/step-5-preview`：StepFun 阶跃星辰**官方 Plan API**
（`https://api.stepfun.com/step_plan/v1`），不经本地桥、直连官方，上下文 1M
（ocx 记 1000000），默认路线死时由守卫跳回它。此前默认走 trae 桥 8791 的
`trae/trae-step-5-preview`（选择器显示 `trae/step-5-pv`），该桥上游已挂
（401→502）后切到官方直连；tokendance 同款 `step-5-preview` 也因 key 401
不可用。接入细节与两个坑见 docs/stepfun2codex-runbook.md。
`setup-providers.sh` 在最后一次 `ocx sync` 之后重新 pin 这个键——CC Switch 和
`ocx provider add --force` 都会重写 config.toml，不 pin 默认模型会被打回。
改默认：`fleet.env` 里设 `FLEET_DEFAULT_MODEL=<slug>`，或直接手改 config.toml。

### 默认模型出问题，跳回 step-5-preview

默认模型 = `stepfun/step-5-preview`（下称港湾），其他模型出问题时按两层跳回：

1. **pin 时守卫**（`tools/default_model_guard.py`）：每次 `setup-providers.sh`
    pin 完默认模型后跑一遍，也可以手动
    `python3 tools/default_model_guard.py`（`--dry-run` 只看不改，
    `--json` 出机器可读结论）。它拿活配置里的 `model` 键，去**这条模型自己的
   路由**上打一次真实聊天（nonce `E2E_OK`，与 `fleet_probe.py` 同一约定）：
   stepfun 系走 image-cap shim 15722，其余走各自的桥端口。探到已死且当前默认
   不是港湾，就把 `model` 改钉回港湾并打印证据；港湾自己也死了就只报告、不改
   写——没有地方可跳。`fleet.env` 里的 `FLEET_DEFAULT_MODEL` 指着一条死路线
   时同样会告警：下次 setup 会把它 pin 上去。
2. **运行时 failover**（CC Switch 自己的 `auto_failover_enabled`）：会话中临时
   切到别的 provider 后它挂了，CC Switch 按熔断判据把请求转给 failover 队列里
   的 provider。港湾（StepFun）必须在 codex 的 failover 队列里，否则第 2 层为空转。
   守卫每次运行只读检查这两项（`proxy_config.auto_failover_enabled=1`、
   StepFun `in_failover_queue=1`，库在 `~/.cc-switch/cc-switch.db`），漂移时
   打印修复 SQL 并以退出码 1 报警；它**不写** CC Switch 的库——那是个 App 正开
   着的 55MB 数据库，改了不重启不生效，重启是人的决定。

为什么探路由而不是探 15721 网关：CC Switch 会把外来模型改写成当前 provider 的
模型再转出网关，一条死掉的默认从网关侧看永远是 200 step-5-preview——trae 路线
挂了那一周就是这么被漏掉的。

### 为什么有的模型不在选择器

- **tokendance**：95 个 live 模型已全部进入 catalog（`setup-providers.sh` 里原先的
  `ocx models selected tokendance --set step-5-preview` 会把 provider 收窄成 1 个，
  已改成 `--clear` 保持 all models，见已知问题第 6 条）。
- **openai 原生 7 个**（gpt-5.5 / 5.6 / 6.x）：ocx 内置（native），由 ChatGPT 账号直接
  管理，按设计不进反代理 catalog。
- **catpaw**：8795 静态兜底列得出 10 个模型（`longcat-flash`、`LongCat-2.0`、`glm-5v-turbo`、
  `glm-5.3-flashx`、`glm-5.2`、`glm-5.1`、`glm-5`、`MiniMax-M2.7`、`MiniMax-M2.5`、`deepseek-v3.2`），
  已注册进 ocx 并进入选择器（显示为 `cpw/xxx`）。但 `catpaw.sankuai.com` 是美团内网域，
  公网 NXDOMAIN，**聊天**必须连美团 VPN（否则 502 Tunnel 503）；连上后
  `launchctl kickstart -k gui/$(id -u)/com.local.catpaw2codex`。

- **antigravity**（2026-09-26 新增，第十桥）：8797，上游 Google Antigravity IDE 的
  language server（cloudcode-pa.googleapis.com），与 gemini 桥同源但用的是 Antigravity
  自有 OAuth client。**源码零硬编码**：`install.sh` 用 `bridges/antigravity/extract_client.py
  --verify` 从 `/Applications/Antigravity.app/Contents/Resources/bin/language_server` 提取并逐个
  实测 refresh_token 换 token，可用 pair 经 `fleet.env` → launchd 注入
  `ANTIGRAVITY_OAUTH_CLIENT_ID` / `ANTIGRAVITY_OAUTH_CLIENT_SECRET`（换机/换版本后重跑即可）。
  12 个模型名同样来自二进制 strings：claude-opus-4-8/4-6/4-5、
  claude-sonnet-4-5、claude-haiku-4-5、gemini-3.1/3-pro-preview、gemini-3-flash-preview、
  gemini-2.5-pro/flash、gpt-oss-120b/20b-maas。其中 claude 家族的 `@default`、`@2025xxxx`
  命名是文档记载，尚未实测，桥内已做「带后缀↔裸名」与「ANTIGRAVITY→GEMINI_CLI ide 元数据」
  两级回退。**注意**：本机网络到 `cloudcode-pa.googleapis.com` 不通（系统 DNS 给 fake-IP），
  真实聊天需代理/VPN 恢复后才能验证；`verify_real_calls.py` 里该桥基线因此与 gemini 同样预期待挂。
- **qwen**（2026-09-26 新增，第十一桥）：8798，上游阿里 Qwen Cloud 托管 API
  `https://maas.qwencloudapi.com/compatible-mode/v1`（OpenAI 兼容 + Anthropic Messages
  双协议），Bearer key 透传 + SSE 流式。**「Qwen4 邀请码」不存在**：chat.qwen.ai 公开只有
  qwen3.7-plus / qwen3.8-max / qwen3.8-omni-flash 三个模型，qwencloud.com Marketplace
  在售 9 个模型（Qwen3.8-Max、Qwen3.8-Flash、Qwen-Image-3.0-Pro、Wan3.0-Video、GLM-5.3、
  DeepSeek-V4-Flash、Kimi K3、TTS、ASR），均无 qwen4/preview/waitlist 入口。「Qwen4
  架构预览」对应 HF `Qwen/Qwen3.8-Flash-Next`（架构名 Qwen4ExpForConditionalGeneration，
  gated: false 开放权重；NVFP4 版约 124GB，无 DGX Spark 跑不动），**生产可用版是托管
  `qwen3.8-flash`**（同为 Qwen4 架构、1M 上下文[输入 991K / 输出 131K]、OpenAI+Anthropic
  双协议、官网明示支持 Codex）。无上游 key 时桥返回静态兜底目录（qwen3.8-flash /
  qwen3.8-max）且 chat 报 503 `qwen_key_missing`；到 qwencloud.com 注册拿 key 写入
  `fleet.env` 的 **`QWEN_API_KEY`**（不是本地桥 key `QWEN2CODEX_KEY`，两者别混用）后
  `bash bridges/finish.sh qwen` 透传上游全量目录。细节见 docs/qwen2codex-runbook.md。
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

    bash "<PRJ>/runtime/tools/ocx-catalog-guard.sh" status           # 桥模型数 + timer 状态
    bash "<PRJ>/runtime/tools/ocx-catalog-guard.sh" run              # 立即检查并自愈
    bash "<PRJ>/runtime/tools/ocx-catalog-guard.sh" install-timer
    bash "<PRJ>/runtime/tools/ocx-catalog-guard.sh" uninstall-timer

默认装 launchd 常驻（`StartInterval` 300s + `RunAtLoad`），随 opencodex 接线一起启用，
`--no-ocx-guard` 可关掉。日志：`~/Library/Logs/ocx-catalog-guard.log`。
阈值 `--min-models` 默认 60（11 桥齐全 70+，个别桥掉线不会误触发）。

注意：磁盘上的 catalog 修好之后，**已经在跑的 app 仍显示旧列表**，需要重启一次
Codex/ChatGPT（`ocx sync --restart-codex` 能自动做，但会结束进行中的会话）。
**默认只裁剪不可达模型。** FleetKit 约定「选择器里只留能连通的」：裸跑
`catalog-filter.sh` / `catalog_filter.py` 会按面板 `verify.real` 把不可达 provider
的模型摘掉。只要报告不动手，显式加 `--report-only`：

    bash tools/catalog-filter.sh --report-only
    python3 tools/catalog_filter.py --report-only

定时器同样生效：`install-timer` 装好后每 300s 跑一次裁剪，日志在
`~/Library/Logs/catalog-filter.log`。

## 不可用 provider 自动隐藏（catalog-filter）

`ocx-catalog-guard` 保证反代理模型「别消失」；但如果某个桥当前核验不是 REAL（端口通、能聊，真实调用核验没过），它仍会躺在选择器里，选中就报错。`catalog_filter.py` 补上另一半：按面板 `verify.real` 结果，把「有桥且非 REAL」的斜杠模型从 catalog 摘掉。

    python3 "<PRJ>/runtime/tools/catalog_filter.py" --dry-run
    python3 "<PRJ>/runtime/tools/catalog_filter.py"
    bash    "<PRJ>/runtime/tools/catalog-filter.sh" status|run|install-timer|uninstall-timer

同一脚本还顺手清掉「永远不会是聊天模型」的噪声行——只凭 slug 判定、不依赖面板状态：
TTS/语音、embedding/rerank、OCR/ASR、图像/视频生成（-i2v/-r2v/-t2v、seedream、happyhorse）、
web-search/web-reader 工具、computer_use_subagent、`-Official` 双列、占位行 `qoder/model`，以及与
在线同胞重复的 `-MMDD` 日期快照（deepseek-v4-flash-0731 旁边有 deepseek-v4-flash 才删，孤立的
qwen3-30b-a3b-instruct-2507 保留）。2026-09-26 起 cogevol（深度研究/PPT agent）与 spark-x2.5-1.7b/4b
（过小的 spark 模型）也归入噪声，选择器 154 → 149；`--no-hide-junk` 可整体关掉这层清理。

规则：
- 只删「有桥且非 REAL」的斜杠模型；tokendance / stepfun 永不动。
- 无斜杠前缀的「原生模型」没有桥可判定，默认保留；只有 --hide-native-when-pool-down 显式探测确认账号池不可用时才隐藏（见下）。
- 垃圾行清理先于桥判定执行（只看 slug），面板挂了也照删；`--no-hide-junk` 关闭。
- 权威源是 `http://127.0.0.1:8796/api/status` 的 `verify.real`（不是 `bridges[].probe.ok`）。
- 判据快照超过 24h（`--max-verify-age`）不删行，只清垃圾行（exit 6）：`verify_real_calls.py` 是按需跑的，快照隔天很正常，凭旧判据删行会把已恢复的桥一直藏掉。同理，面板不可达（3）或 real 为空（4）也一概不删。
- 写前做 byte 级 round-trip 校验：先探明文件正在用的序列化布局（缩进 / 行尾换行 / `ensure_ascii`），按同一布局回写；认不出的布局才拒绝落库（exit 5），并说明原因。2026-09-29 修：此前只认 `indent=2`+行尾换行，而在写的布局是 `indent=1` 无换行，于是每次写入都被拒且一声不响，包装脚本又把失败记成 `ok:`——一个从没真正过滤过的过滤器健康地跑了好几天。
- 所有本地 HTTP 调用（面板、原生池探测）都绕过系统代理：`urllib` 默认会走 macOS 系统代理，判据就成了代理的而不是桥的，代理一挂整棵过滤器跟着挂。
- REAL 但 catalog 缺失的桥会 `ocx sync` 补回（带 1h cooldown，避免和 guard 打架）。
- `--keep P[,P]` 临时保留某 provider；`--only P[,P]` 只处理指定 provider。

用 `catalog-filter.sh install-timer` 装 launchd 常驻（`com.local.catalog-filter`，`StartInterval` 300s + `RunAtLoad`），日志：`~/Library/Logs/catalog-filter.log`。

### 原生模型（无前缀行）与账号池

选择器里没有 `vendor/` 前缀的行（gpt-5.5 / gpt-5.6-* / gpt-6-* / step-3.7-flash）不是反代理模型，而是走本地账号池/网关的模型（配置里写 ocx 内置 openai provider `codexAccountMode: pool`；当前 `~/.codex/config.toml` 的 `model_provider=custom` 指向 CC Switch 网关 `http://127.0.0.1:15721`，由它路由）。它们没有桥，所以 `verify.real` 永远覆盖不到，`catalog_filter` 默认也不动它们。

账号池空了（未登录 ChatGPT、或 `~/.codex/auth.json` 里的 key 失效）时，这些行是选择器里最坏的一种失败：**看得见、选得动，一提交就 401**。`OpenAI account pool has no usable account credential` 就是这么来的。

`--hide-native-when-pool-down` 用一次最小请求探测 `config.toml` 里 `model_provider` 指向的网关（未设 `FLEET_PROXY_BASE` 时；默认 ocx `http://127.0.0.1:10100`）。请求体必须是 `input` 列表 + `stream:true`，否则网关在到达账号池之前就回 400（`Input must be a list` / `Stream must be set to true`），这个探测要看的 401 永远等不到；模型名按 catalog 里的原生行依次试，网关不认的名字（404）说明不了账号池的事，换下一个。只有明确读到「池无可用凭据」的 401 才判定不可用；其余任何结果（成功、其它 4xx/5xx、网关不可达）都按「可用」处理，探测失败不会误清空选择器。探测为不可用时隐藏这些行，池恢复后下个周期自动加回。

    bash "<PRJ>/runtime/tools/catalog-filter.sh" run --hide-native-when-pool-down --dry-run

`install-timer` 生成的 plist 默认已带 `--hide-native-when-pool-down`。2026-09-26 实测：池为空 → 8 个原生行隐藏，选择器 149 → 141，其余 141 个反代理模型不受影响。

想让原生模型真正可用，只能在 Codex 里重新登录 ChatGPT 账号（账号池属用户侧凭据，脚本不代办）：

    ocx account list openai   # 看账号池里到底还有没有账号


与 `ocx-catalog-guard` 互补：guard 在桥模型数 < 60 时 `ocx sync` 加回，filter 再剔除非 REAL。两者会互踩——filter 藏掉的行正好让 guard 计数掉到阈值以下，于是 `ocx sync` 把坏行全部加回来，下个周期再被藏掉，300s 一轮永远对打。所以 filter 每轮把「比舰队自报少多少斜杠行」写进 `~/.codex/.catalog-filter-hidden.json`（按 bridge 自己的模型数与 catalog 存活行数相减，不猜 slug），guard 读它：缺口能被 filter 解释就不补，`66 + 62 hidden = 128; not healing`；只有在岗桥的行也丢了（真被 CC Switch 冲掉）才 `ocx sync`。当前 REAL 桥：workbuddy、qoder、codely、trae、lingxi、xhx；连同 tokendance/stepfun 与原生行，清理后选择器 74 行（斜杠 66 + 原生 8），仍高于 guard 阈值但只剩 6 行余量，这道解释逻辑就是那时的安全带。

改完磁盘 catalog 后**需重启 Codex/ChatGPT** 才会刷新选择器。

## 登录表

| name | 登录方式 | 凭据位置 | 备注 |
|------|----------|----------|------|
| workbuddy | 国内版桌面 App 登录 | 账号池 `runtime/bridges/workbuddy-cn/auths/` | 上游 `copilot.tencent.com`（见「两座 WorkBuddy 桥」） |
| workbuddy-gpt | 海外版桌面 App 登录 | 桌面端 `~/.workbuddy-ai/local_storage`；池 `runtime/bridges/workbuddy-gpt/auths/` | 上游 `www.workbuddy.ai`；与 workbuddy 共用同一个本地 key |
| qoder | npm i -g @qodercn-ai/qoderclicn，按 CLI 流程登录 | ~/.qoder-cn/.auth/user | 桥以非交互模式调用 CLI |
| codely | 官方 CLI 设备码登录 | ~/.codely-cli/oauth_creds.json | 账号需先在网页端激活（见已知问题） |
| trae | Trae CN IDE 登录 | IDE 登录态；桥缓存到 ~/.trae2codex/creds.json | |
| lingxi | python3 "<PRJ>/runtime/bridges/lingxi/login_helper.py" 打开浏览器登录 | ~/.LingXi/auth.json | |
| xhx | 商汤小浣熊桌面 app 登录 | ~/.box-agent/config/auth.json | 桌面端会重写该文件，属正常 |
| gemini | gemini login，或 python3 "<PRJ>/runtime/bridges/gemini/extract_cookies.py" 导出 cookie | ~/.gemini/jetski-standalone-oauth-token 或 ~/.gemini2codex/cookies.txt | 账号需通过 Google 验证（见已知问题） |
| catpaw | CatPawAI 桌面 App 登录 | ~/Library/Application Support/CatPawAI/User/globalStorage/state.vscdb | 需美团内网/VPN（见已知问题） |
| antigravity | Antigravity 桌面 App 登录（或 gemini login） | ~/.gemini/jetski-standalone-oauth-token | 与 gemini 共用 token；需能连 cloudcode-pa.googleapis.com |
| qwen | qwencloud.com 控制台创建 API key，写入 `fleet.env` 的 `QWEN2CODEX_KEY` | `fleet.env`（无本地登录态） | 上游 maas.qwencloudapi.com；key 丢失可在控制台重建 |

（gemini 支持多账号池：`GEMINI_AUTH_DIR` + `POST /__gemini/accounts/*`，用法见 docs/gemini2codex-runbook.md「多账号池」一节。）

每个服务登录后运行对应的 bridges/finish.sh <name>：重启桥 → 等待 /v1/models →
列出模型 → 注入 Codex 模型目录 → ocx sync → 冒烟聊天一次。

## 真实调用检测（verify_real_calls）

「桥在监听」≠「真调上游」。`tools/verify_real_calls.py` 对每座桥发起一次**抗伪造**探测：
一次提问同时要求模型（a）复述随机暗号前四位、（b）计算 `随机A + 随机B`。罐头/镜像桥
无法给出正确运算（操作数每次随机），因此可靠区分「真实模型推理」与「伪装/透传 200」。
另读上游 usage 里的 `reasoning_tokens`/`credit` 作为佐证（mock 无此字段）。

    python3 tools/verify_real_calls.py            # 全舰队核验
    python3 tools/verify_real_calls.py --only workbuddy
    python3 tools/verify_real_calls.py --json     # 机器可读（供状态面板/汇总）

推理模型需较大 `max_tokens`：探针按 256 → 2048 → 4096 逐级抬配额，避开「思维链吃光额度→空内容」。

判据图例：

- REAL：随机运算题答对 = 真上游推理
- ECHO/MIRROR：复述暗号但算错 = 疑似透传，非真实推理
- CANNED/MOCK：极速+极短+答非所问 = 疑似罐头/镜像
- GATE：上游欢迎/登录门禁（需访问链接激活）
- AUTH_EXPIRED：401/403，session/key 失效（重跑 bridges/finish.sh）
- UPSTREAM_DOWN：502/503/504（代理/VPN 不通、上游关停、拒参）
- BRIDGE_DOWN：连接失败/超时

当前基线（2026-09-26 实测）：

| 桥 | 端口 | 模型 | 判定 | 说明 |
| --- | --- | --- | --- | --- |
| workbuddy | 8787 | hy4-preview | REAL | 运算正确 reason/credit 有值 |
| workbuddy-gpt | 8788 | gpt-6-astra | REAL | 运算正确 reason/credit 有值 |
| qoder | 8789 | DeepSeek-V4-Pro | REAL | 运算正确 |
| codely | 8790 | — | GATE | 上游欢迎/登录门禁（需访问链接激活） |
| trae | 8791 | trae/Doubao-Seed-Evolving | REAL | 运算正确 |
| lingxi | 8792 | — | AUTH_EXPIRED | session 失效，需重跑 finish |
| xhx | 8793 | xhx/raccoon-19b265 | REAL | 运算正确 |
| gemini | 8794 | — | UPSTREAM_DOWN | 上游 refresh/关停（502） |
| catpaw | 8795 | — | UPSTREAM_DOWN | 隧道不通（需 VPN） |
| antigravity | 8797 | claude-sonnet-4-5@20250929 | BRIDGE_DOWN | cloudcode-pa TLS 握手超时（凭据有效，纯网络） |

合计：REAL=5，其余 5 桥均为用户侧/外部条件。与 `fleet_chat_test.py`（存活/冒烟）互补：
前者问「真不真」，后者问「通不通」。

## 随附工具

- deploy.sh：一条命令跑完 preflight → 安装 → 等桥 → 逐桥收尾 → ocx → 签到 timer → 汇总
- bridges/finish.sh <name> [--home DIR] [--tries N] [--skip-chat]：单桥收尾（重启+验收+同步）
- tools/status.sh [--home DIR]：11 桥健康表（launchd/监听/模型数/key md5）+ ocx 状态 + 今日签到
- tools/fleet_chat_test.py [--port-base N]：全舰队 /v1/models + 聊天测试（只打印 key 的 md5）
- tools/verify_real_calls.py [--port-base N] [--only NAME] [--json]：真实调用核验（抗伪造运算题 + usage 佐证）
- tools/checkin.sh status|run-now|install-timer|uninstall-timer [--home DIR]：每日积分签到
- tools/status_ui.sh start|stop|install-timer|uninstall-timer [--home DIR]：状态面板（默认 127.0.0.1:8796）
- tools/status_ui.py [--port N] [--no-browser] [--once]：面板实现（stdlib 单文件；/api/status、/api/logs/<name>、/api/action/*（含 verify-real-calls））
- tools/ocx-catalog-guard.sh run|install-timer|uninstall-timer|status：反代理模型 catalog 看门狗（默认 300s）
- tools/catalog-filter.sh run|install-timer|uninstall-timer|status：按 verify.real 隐藏不可用桥模型＋按 slug 清理噪声行（默认 300s，与 ocx-catalog-guard 互补）
- tools/free_models.py [--free-only] [--credits client|limit|own|unknown] [--provider P] [--missing] [--json] [--check-sources]：模型标注：免费状态 + 是否走客户端积分（数据在仓库根 free-windows.json，状态面板同源）
- tools/short_aliases.py [--dry-run]：选择器短名（ocx 别名，路由不受影响）
- tools/checkin.py [--run-now|--status|--daemon]：签到实现（幂等，CST 记「今日」）
- opencodex/setup-providers.sh：重新注册 11 桥
- uninstall.sh [--home DIR] [--purge]：卸载 launchd 服务和 plist；--purge 连目录一起删

## 已知问题（2026-09-26 实测，均为用户侧/外部条件）

1. **tokendance（step-5 备选路线）**：API key 已失效——网关聊天端点返回
  401「API 密钥不存在」（/v1/models 列表端点是公开的，所以模型照样列得出）。
  默认模型当时已切到 StepFun 官方 Plan API 的 `stepfun/step-5-preview`，
  2026-10-03 起改为 `workbuddy/deepseek-v4-flash`（见「默认模型」一节）；
  key 重建后若想切回，改 `fleet.env` 的 `FLEET_DEFAULT_MODEL` 再跑一次
  `setup-providers.sh`。重建：tokendance.space 控制台重新生成 key，
  然后 `ocx provider add tokendance --adapter openai-chat --base-url
  https://tokendance.space/gateway/v1 --api-key <新key> --force` + `ocx sync`。
2. codely：上游网关对 chat 一律返回 400「欢迎使用Codely」onboarding 门禁。
   /v1/models 正常、key 有效；需登录 codely.tuanjie.cn 网页端完成首次激活。
3. gemini：502，且不是 token 过期——Google 已于 2026-06-18 关停 Code Assist
   individuals/AI Pro/Ultra 档（含 Gemini CLI 登录），该档本就是个人账号免费档
   （60 次/分、1000 次/天，AI Pro 只抬限额不单独计费）。需迁移 Antigravity 或改
   AI Studio key；详见 docs/free-models-runbook.md「Gemini 档位定性」。
   上游不可达时 chat 在 `GEMINI_CHAT_BUDGET`（默认 60s）内回 502，不再挂 6 分钟，
   客户端断开也不再伪装成 BRIDGE_DOWN。2026-09-29 修的是两层：预算要贯穿
   token 刷新／`loadCodeAssist`，并且要在 `socket.create_connection` 层按剩余时间
   给**每个解析地址**封顶——`cloudcode-pa.googleapis.com` 解析出 16 个地址
   （前 8 个 IPv6，本机 VPN 全部黑洞），不封的话一次调用就能吃满 16 份 timeout。
4. catpaw：需要美团内网/VPN，否则 catpaw.sankuai.com 不可达（Tunnel 503）。
   连上 VPN 后执行：launchctl kickstart -k gui/$(id -u)/com.local.catpaw2codex
5. **antigravity**（第十桥）：代码/凭据/catalog/ocx 注册全部就绪，12 个模型已进
   选择器（`agy/*`）。唯一阻塞是网络：本机 VPN 节点对
   `cloudcode-pa.googleapis.com` TLS 握手超时（`curl` http=000 rc=28），同节点
   `oauth2.googleapis.com` 可达且 refresh_token 实测刷新成功，所以不是账号问题。
   换能放行 Google 全域的节点后
   `launchctl kickstart -k gui/$(id -u)/com.local.antigravity2codex` 再
   `python3 tools/verify_real_calls.py --only antigravity`。细节见
   docs/antigravity2codex-runbook.md。上游挂起时 chat 在
   `ANTIGRAVITY_CHAT_BUDGET`（默认 60s）内收口，不会再拖到几分钟；连接层同样按
   剩余时间给每个解析地址封顶（16 个地址的域名不封就是 16 份 timeout）。
6. trae：桥 8791 上游曾挂（先 401 鉴权失效，后 502「param invalid」），已不是
  默认路线。经 CC Switch 对外表现为
  `503 所有供应商已熔断，无可用渠道`——看到这个报错先确认默认模型是不是又
 指回了死掉的桥；现默认为 stepfun 官方直连，实测 200。
7. **qwen**（第十一桥）：代码/catalog/ocx 注册全部就绪，8798 桥在跑。两个 key 别混：
   `QWEN2CODEX_KEY` 是**本地**桥 key（任意字符串），上游 Qwen Cloud key 要写
   `QWEN_API_KEY`——写错的话桥把本地 key 当上游 key 发出去，chat 全 401。
   （2026-09-29 起上游 401/403 时 `/v1/models` 直接回 401 +
   `upstream_auth_error`，不再返回假的静态目录；网络错误/5xx 仍给
   `qwen3.8-flash` / `qwen3.8-max` 兜底目录。）
   到 qwencloud.com 注册拿 key 写入 `runtime/fleet.env` 的 `QWEN_API_KEY`，
   再 `bash bridges/finish.sh qwen` 即透传上游全量目录。细节见
   docs/qwen2codex-runbook.md。
8. ~~cline（未接入）~~ **已接入（第十二桥，2026-09-26 21:20）**：`api.cline.bot`
   没有可用的 OpenAI 兼容推理端点（全局 auth 中间件把非白名单路径一律挡成 401），
   但本机 hub daemon 可以直连：`ws://127.0.0.1:25463/hub`，凭据在
   `~/.cline/data/locks/hub/production.json`。经 hub 驱动真实推理，
  4 个免费模型实测 200 + verify 判 REAL。详见 docs/cline2codex-runbook.md。

9. **zcode**（第十三桥，:8800）：智谱 Z.AI Coding Plan / ZCode。桥暴露
   `zcode/GLM-5.3` 与 `zcode/GLM-5.3-Flash`（Start Plan 3M / 5M tokens 每天，
   周末活动 300M tokens 一次性）。**当前只差一次 OAuth 登录**：ZCode.app 的登出
   流程会清掉 `zcodejwttoken`，之后桥返回 503 并在 `auth` 字段给出修复命令。恢复：

       node /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs login --no-browser

   在浏览器打开它打印的 URL 即可，JWT 落盘后桥无需重启（`read_token()` 每请求重读）。
   详见 docs/zcode2codex-runbook.md。

2026-09-26 fleet_chat_test 实测（10 桥）：5 桥 PASS（workbuddy、workbuddy-gpt、
qoder、trae、xhx）；5 项失败——codely 400 onboarding 门禁、lingxi 401 session
失效（`lingxi/deepseek-v4-flash`，需重跑 finish）、gemini 502（60s）、
catpaw 502（需 VPN）、antigravity 客户端 90s 超时（HTTP 无响应，服务端 Broken
pipe，根因同 gemini：cloudcode-pa 网络阻断）。除 lingxi 外均已记录在上方条目。
trae 本轮已恢复 200 PASS；当晚它曾报 401→502，step-5 路线改走 StepFun 官方
Plan API 后不受影响。

## 安全说明

- 13 个本地 key 只写在 <home>/fleet.env（权限 600，11 座桥 + tokundance + stepfun），仅本机使用，不要提交 git 或外发
- 所有桥只监听 127.0.0.1；gemini/catpaw/antigravity 三桥不校验本地 key（Authorization 只用于上游 Google/美团）
- 测试脚本只打印 key 的 md5，不打印明文
- 状态面板只监听 127.0.0.1；key 只显示 md5 前 8 位；日志接口走桥名白名单

## 目录结构

    <PRJ>/                  FleetKit 项目根
      kit/                    git 仓库（本文档所在）
        install.sh            一键安装（plist + venv + fleet.env + ocx 注册）
        deploy.sh             一键部署流水线（install + finish + 签到 timer + 汇总）
        uninstall.sh          卸载
        requirements.txt      Python 依赖
        README.md             本文
        bridges/              13 座桥源码 + finish.sh（含 cline/ 第十二桥、zcode/ 第十三桥）
                              _common.py 是 8 座 FastAPI 桥共享的外壳（鉴权/SSE/uvicorn 入口）
                              workbuddy/ 是两座 WorkBuddy 桥共享的实现（cn/gpt 只留入口）
        opencodex/            setup-providers.sh（ocx provider 注册）
        free-windows.json     模型标注数据（免费状态 + 时段 + 是否走客户端积分，改这里不改代码）
        tools/                status.sh / status_ui.sh / status_ui.py / ocx-catalog-guard.sh / checkin.sh / checkin.py / fleet_chat_test.py / verify_real_calls.py / fleet_split.py / free_models.py / short_aliases.py / catalog_filter.py / catalog-filter.sh（test_*.py 由 CI 跑 pytest）
        docs/                 各桥 runbook + stepfun/tokundance 官方 API runbook + 全量实测报告
      runtime/                运行根（install.sh --home 的默认值）
        fleet.env             桥 API key + FLEET_HOME/PORT_BASE（600，不入库）
        bridges/<name>/       13 座桥运行副本（登录态、state.json 都在这里）
        tools/ docs/ opencodex/   从 kit/ 复制来的运行副本
        checkin/              每日签到 timer 的状态目录
        logs/                 zcode 相关 launchd 服务的日志
        .venv/                install.sh 建的 Python 环境

## 可选：TokenDance

fleet.env 里取消注释 TOKENDANCE_API_KEY= 并填入 key，重跑
bash "<PRJ>/runtime/opencodex/setup-providers.sh" 即追加 tokendance provider
（https://tokendance.space/gateway/v1，模型 step-5-preview 等）。

## StepFun Plan API（默认模型上游）

默认模型 `stepfun/step-5-preview` 走 StepFun 官方 Plan API（订阅制，端点
`/step_plan/v1`），不是本地桥。`fleet.env` 里设 `STEPFUN_PLAN_API_KEY=<key>`，
重跑 setup-providers.sh 即注册/更新 provider；没有 key 时该步骤自动跳过。
详见 docs/stepfun2codex-runbook.md。

### StepFun 图片上限：image-cap shim

StepFun 的 Plan API 在 CC Switch 这一跳有 70 图上限（第 71 张回 400
`images_too_many`；`disable_response_storage = true` 让 Codex 每轮重发整段会话，
长会话必然撞上）。`tools/stepfun_image_shim.py` 架在 CC Switch（15721）**下面**的
透传层，监听 15722，转发前去重 + 截断到 32 张（`tools/image_cap.py` 是测量）。
链路：Codex → 15721(CC Switch) → 15722(shim) → `https://api.stepfun.com/step_plan/v1`，
shim 的上游是真正的上游而不是 CC Switch，两边绕不成环。

```bash
bash tools/stepfun_image_shim.sh status           # 看 URL / pid / 健康
bash tools/stepfun_image_shim.sh install-timer   # launchd 常驻
bash tools/stepfun_image_shim.sh uninstall-timer
python3 tools/pin_cc_switch_endpoint.py --dry-run --all-stepfun   # 预览会重指哪些行
```

`install.sh` 和 `opencodex/setup-providers.sh` 已自动接线（含 `--dry-run` 守卫）。
真正有效的杠杆是 CC Switch 自己的路由表：`tools/pin_cc_switch_endpoint.py` 改写
`~/.cc-switch/cc-switch.db` 里指向 StepFun 的 codex 行的转发目标。默认是 **sweep 模式**，
按转发目标判而不是按 provider 名判 —— 本机 `nv spark` 这个 codex provider 也转发到
StepFun，在 CC Switch UI 里选中它就直接绕过 shim，71 图照样 400（`--all-stepfun` 是同一个
开关，`IMAGE_CAP_CC_PIN_ALL=0` 收窄回只动命名那一行）。`tools/pin_shim_base_url.py`
（钉 `~/.codex/config.toml`）实测赢不了，降级为默认关闭的兜底。
**改完数据库不用重启 CC Switch.app**（10-01 实测推翻旧结论：进程自 09-29 未重启，
71 图请求仍被 shim 截断并返回 200，详见 runbook）。
实测：100 图经 launchd 全链路，`stepfun/step-5-preview` 上游只收 32，
`workbuddy/hy4-preview` 原样透传 100。详见 docs/stepfun2codex-runbook.md。

**并发闸门（10-01）**：间歇 `503 所有供应商已熔断` 的根因不在图片截断而在并发——
StepFun Plan 单账号并发上限 10，突发把第 11 个请求打成 429，CC Switch 计 4 次
失败即打开 codex 熔断（`circuit_failure_threshold=4`），之后所有请求 503，与是否
超并发无关。shim 内的计数闸门把同时上游请求压到 8，遇 429 退避重试 3 次，排队
最长 75s（压在 CC Switch 90s 首字节超时以下，超时本地回 429 不挂死）：
`IMAGE_CAP_MAX_INFLIGHT=8`、`IMAGE_CAP_QUEUE_TIMEOUT=75`、
`IMAGE_CAP_429_RETRIES=3`。观测看 `curl -s http://127.0.0.1:15722/__image_cap/health`
的 `concurrency` 块和 `stats.retried_429`。**kit 改完必须同步 runtime 副本再
`launchctl kickstart -k gui/501/com.local.stepfun-image-cap`**——launchd 跑的是
runtime 那份，health 里没有 `concurrency` 块就说明闸门没上线。详见
docs/stepfun2codex-runbook.md。

**卡死自愈（10-02）**：shim 偶发「进程活着、launchd `state = running`、事件循环
不应答」——请求全挂死但进程不退出，`KeepAlive` 只 relaunch 退出的进程，看不见这种
故障。现在两层看门狗：进程内 daemon 线程用裸 socket（不经过被监视的那个循环）探
`/__image_cap/health`，连续 `IMAGE_CAP_WATCHDOG_STRIKES`（默认 2）次失败就
`os.execv` 原地自重启，pid 不变；外部 launchd timer 每 30s 探活一次，连续失败后
`kickstart -k` 强杀重启——整进程 wedge 和端口被第二实例占用这两种内部线程看不见
的，只有它救得回。探活慢才指控（curl 超时 8s + 连续 2 次），strike 超 600s 作废，
探活一成功立刻清零，所以慢请求不会被误杀。`install-timer` 同时装 service 和
timer；`status` 报 watchdog 安装态和当前 strikes，health 的 `watchdog` 块看
`restarts` 涨没涨。详见 docs/stepfun2codex-runbook.md。

## 卸载

    bash "<PRJ>/runtime/uninstall.sh"            # 停服务并删 plist
    bash "<PRJ>/runtime/uninstall.sh" --purge    # 连 "<PRJ>/runtime" 目录一起删

注意：uninstall.sh 不动 ocx 的 provider 配置；如需清除，用 ocx 自带命令管理。

## 可达模型排在最前（catalog_sort.py）

Codex 选择器按 catalog 里的文件顺序渲染，所以一个死桥会把可用模型挤出首屏。
`catalog_sort.py` 按实测可达性重排：**可达 provider 在最前**（顺序取 `--order`），
其后是未测量过的，最后才是实测不可达的。

但置顶优先于可达性：LEAD_SLUGS（可用 FLEET_FIRST_MODEL 覆盖）固定排在全部行最前，
默认即 2026-10-03 双题实测满分的 workbuddy 三兄弟 deepseek-v4-flash、
deepseek-v4.1-flash、deepseek-v4-pro（依据见 docs/code-model-selection.md）；
DEFAULT_ORDER 也从同一天起把 workbuddy 调到 provider 顺序最前。

    python3 tools/catalog_sort.py --dry-run      # 只看新顺序
    python3 tools/catalog_sort.py                # 写入
    python3 tools/catalog_sort.py --drop-unreachable   # 直接删掉不可达的

可达性来自快照文件，默认 `$CODEX_HOME/fleet-reach.json`
（`--reach` 或 `FLEET_REACH_FILE` 可覆盖），形状：

    {"reachable": ["workbuddy", ...],
     "unreachable": ["qoder", ...],
     "measured_at": "2026-09-27T17:00:00+08:00"}

快照里没有的 provider 视为「未知」，排在可达之后、不可达之前，
所以新接入的 provider 不会被误伤。默认顺序可用 `FLEET_MODEL_ORDER` 覆盖。

改完记得 `ocx sync-cache` 把 `~/.codex/models_cache.json` 对齐
（Codex App 读的是那一份），否则 App 仍显示旧列表。

kit 内带一份当前实测快照 `tools/fleet-reach.json`，可直接复用：

    python3 tools/catalog_sort.py --reach tools/fleet-reach.json

## 让排序在 `ocx sync` 后依然生效

`ocx sync` 会从零重建 catalog，并把每个条目的 `priority` 重置为 5，
`catalog_sort.py` 排好的顺序会被整个冲掉。ocx 没有提供 hook，所以
FleetKit 用一个 wrapper 把真实 ocx 包起来：

    ~/.local/node-v22.20.0-darwin-arm64/bin/ocx        -> wrapper
    ~/.local/node-v22.20.0-darwin-arm64/bin/opencodex  -> wrapper
      （均在 <kit>/tools/fleet-sort-after-sync.sh）

wrapper 先按原样执行真实 ocx，只有子命令是 `sync` / `sync-cache` 且
退出码为 0 时，才补跑一次 `catalog_sort.py` 再 `ocx sync-cache`。
其它子命令（status / doctor / models ...）完全不受影响。

### 安装 / 卸载

    bash tools/fleet-sort-after-sync.sh --install
    bash tools/fleet-sort-after-sync.sh --uninstall

### 排错

排序没生效时，先手动跑一次看真实报错（wrapper 里输出被丢掉了）：

    python3 tools/catalog_sort.py --reach tools/fleet-reach.json

`--reach` 快照缺失或 provider 名单过旧都会让可达性判断失真，
更新 `tools/fleet-reach.json` 后重跑即可。

真实 ocx 路径可用 `FLEET_REAL_OCX` 覆盖，Python 用 `FLEET_PYTHON`，
快照用 `FLEET_REACH_FILE`。

## 可达性自动刷新（fleet_probe.py + launchd 定时器）

排序依赖一份可达性快照。手写的快照会过期：桥恢复上线或挂掉之后，
排序就不再反映真实情况。`tools/fleet_probe.py` 用真实调用重新测量每个桥，
只有返回预期 nonce 才算可达——只听端口、只列 /v1/models、或返回 canned
200 都不算，因为选择器是用户实际感知到的东西。

    bash tools/fleet-probe-install.sh install   # 装 launchd 定时器（默认 30 分钟）
    bash tools/fleet-probe-install.sh status    # 查看定时器与当前快照
    bash tools/fleet-probe-install.sh run       # 前台立即测一次
    bash tools/fleet-probe-install.sh uninstall

ocx sync 时 wrapper 发现快照超过 FLEET_REACH_MAX_AGE（默认 86400 秒）
只会去 kick 这个定时器，不会自己跑完整探测——一次全量探测要给每个桥发真实
请求、耗时几分钟，绝不能卡住 ocx sync。

### 采样策略

每个桥最多试 --tries 个模型（默认 6），跳过名字里带 image/tts/embed/ocr/
vision/vl 的。某个模型撞到限流不算桥不可用，会继续试下一个——这个坑是
实测踩到的：cline 前两个模型都是 429 cap，但第三个 z-ai/glm-5.3-flash
正常返回，只试前两个就会把活着的桥误判为不可达。

快照写到 ~/.codex/fleet-reach.json（catalog_sort.py 实际读取的那份）。

### 新电脑部署

`install.sh` 已自动装好这一套（`--no-reach` 可关掉）：

    bash install.sh            # [5c/6] fleet reachability probe
    bash install.sh --dry-run  # 只预览不落盘

顺序：装定时器 -> 立即探一次 -> 给 ocx 包 wrapper。手动补装：

    bash tools/fleet-probe-install.sh install
    bash tools/fleet-probe-install.sh run
    bash tools/fleet-sort-after-sync.sh --install
