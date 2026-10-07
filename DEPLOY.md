# FleetKit 部署指南（其他电脑照此文件即可部署）

本文是**从零到可用**的唯一入口。细节、原理、排错都在
[README.md](README.md) 与 [docs/](docs/)，本文只保留一条最短路径。

## 0. 前提

所有平台的共同依赖：`python3 >= 3.9`（install.sh 自动建 venv 并装依赖）、
`curl`；可选 `node/npm`（仅 opencodex 网关需要，`npm install -g @bitkyc08/opencodex`）。
三个系统的服务后端互不通用，**每台机器各自装一套**，详见下面分平台步骤。

## 1. 取代码

```bash
PRJ="$HOME/FleetKit"                     # 换成任意目录，路径可含空格
mkdir -p "$PRJ"
git clone https://gitee.com/cpufreestyle/fleet-kit.git "$PRJ/kit"
# GitHub 主源（可达时优先）：git clone https://github.com/cpufreestyle/fleet-kit.git "$PRJ/kit"
```

`kit/` 是 git 仓库（改代码、git pull 都在这里），`runtime/` 是运行根（install.sh 生成，
登录态 / 账号池 / 日志都在里面）。**两个目录不要合并**，也不要把 runtime 提交进 git。

## 2. 分平台安装

三条路线跑的是同一套 `install.sh` / `deploy.sh`，区别只在服务后端：
macOS 用 launchd、Windows 用 PowerShell 常驻 + 计划任务、Linux 用 detached
自重启包装脚本。每条路线的命令都可以整段复制。

### macOS（12+）

```bash
PRJ="$HOME/FleetKit"
cd "$PRJ/kit"
bash deploy.sh --home "$PRJ/runtime" --with-checkin --with-ui
```

服务定义落在 `~/Library/LaunchAgents/com.local.*.plist`（可用
`launchctl print gui/$(id -u)/<label>` 看状态），timer 用 launchd 的
`StartInterval` / `StartCalendarInterval`（每天 09:00 签到按本地时钟触发）。
需要 `launchctl`（macOS 自带）。重启单座桥：

```bash
launchctl kickstart -k gui/$(id -u)/com.local.<name>2codex
```

### Windows（10+）

需要 **Git for Windows**（提供 bash；PowerShell 7 更佳，5.1 也能跑）。
脚本本身是 bash，在 Git Bash / PowerShell 里跑同一条命令：

```powershell
$PRJ = "C:\Users\<你>\FleetKit"
cd "$PRJ\kit"
bash deploy.sh --home "$PRJ\runtime" --with-checkin --with-ui
```

服务后端：install.sh 为每座桥生成一个隐藏的 PowerShell 常驻 supervisor
（`%LOCALAPPDATA%\FleetKit\services\<label>-super.ps1` + pid 文件），timer 走
系统计划任务（schtasks，最短间隔 60s；每日 09:00 签到用 CalendarTrigger）。
注意：

- venv 布局是 `.venv\Scripts\python.exe`（不是 posix 的 bin/）；
- 服务重启不通过 launchctl，由 `tools/platform.sh` 的 Windows 分支自动处理；
- **每台 Windows 机器各自一套 runtime**，不要和 macOS/Linux 共用目录（登录态、
  plist、schtasks 都是平台私有的）。

### Linux

需要 `bash` + `python3` + `pkill`（`procps` 包；`setsid` 有则用，没有则退回 nohup）。

```bash
PRJ="$HOME/FleetKit"
cd "$PRJ/kit"
bash deploy.sh --home "$PRJ/runtime" --with-checkin --with-ui
```

服务后端：每座桥一个 `~/.local/share/FleetKit/services/<label>.sh` 自重启包装
（内层 `while true` + `trap`，退出自动拉起），timer 是同一个包装里 `sleep N` 循环
（每日任务等价 86400s 间隔，按脚本首次启动时间起算，**不是**绝对 09:00——需要
绝对时刻请用 systemd timer 或 cron 自己包一层）。

### 三平台通用注意

- `fleet.env` 里记录了 `FLEET_OS`（本套安装用的后端），后续 `status.sh` /
  `finish.sh` / `uninstall.sh` 都读它而不是重新探测——拷 runtime 到另一台机器
  前先想清楚后端是否匹配；
- Windows 与 macOS/Linux 的 runtime **必须分开**（登录态文件、服务定义、venv 布局
  都是平台私有的），跨平台只分发 `kit/` 源码；
- 第二套舰队并存：`FLEET_SERVICE_DIR` / `FLEET_LOG_DIR` / `FLEET_LABEL_PREFIX`
  三个环境变量 + `--home` + `--port-base` 全套换掉即可（README「高级」一节）。

## 3. deploy.sh 流水线（三平台同一条命令跑完）

上一节的分平台命令最终都执行同一套 `deploy.sh` 流水线：preflight（平台/依赖/端口
占用）→ install.sh（建 venv、生成 `runtime/fleet.env`、写服务定义并启动 13 座桥）
→ 等 120s 端口就绪 → 逐桥收尾 → opencodex provider 注册 → 签到 timer →
状态面板 → 汇总报告。

退出码 0 = 该起的桥都就绪（个别没登录只算 warning）；1 = preflight 失败或一座桥都没起。
单座桥连不上不会中断部署，之后单独补（见 §4）。

装完即有：

| 组件 | 端口 / 位置 | 作用 |
|------|------------|------|
| 13 座反代理桥 | 8787..8803（8796 留给面板） | 客户端订阅 → OpenAI 兼容端点 |
| opencodex 网关 | 127.0.0.1:10100 | Codex 模型选择器的统一入口 |
| 状态面板 | http://127.0.0.1:8796/ | 健康表 / 签到 / 真实调用核验 / 日志 |
| 签到 timer | 每天 09:00 CST | xhx / workbuddy 自动领积分 |

## 4. 登录你想用的服务

每座桥对应一个客户端订阅。登录方式速查（完整表格见 README「登录表」）：

| 桥 | 登录方式 | 凭据落点 |
|----|----------|----------|
| workbuddy | 国内版桌面 App 登录 | `runtime/bridges/workbuddy-cn/auths/` |
| workbuddy-gpt | 海外版桌面 App 登录 | `~/.workbuddy-ai/local_storage` |
| qoder | `npm i -g @qodercn-ai/qoderclicn` 后按 CLI 登录 | `~/.qoder-cn/.auth/user` |
| codely | 官方 CLI 设备码登录（需网页端先激活） | `~/.codely-cli/oauth_creds.json` |
| trae | Trae CN IDE 登录 | `~/.trae2codex/creds.json` |
| lingxi | `python3 runtime/bridges/lingxi/login_helper.py` | `~/.LingXi/auth.json` |
| xhx | 商汤小浣熊桌面 App 登录 | `~/.box-agent/config/auth.json` |
| cline | Cline 桌面 App 登录（hub daemon 直连） | `~/.cline/data/locks/hub/production.json` |
| zcode | `node /Applications/ZCode.app/.../zcode.cjs login --no-browser` | JWT 落盘即生效 |
| gemini / antigravity | `gemini login` / Antigravity App 登录 | `~/.gemini/jetski-standalone-oauth-token` |
| catpaw | CatPawAI 桌面 App 登录（需美团 VPN） | CatPawAI `state.vscdb` |
| qwen | qwencloud.com 控制台创建 key，写入 `fleet.env` 的 `QWEN_API_KEY` | `fleet.env` |

每个服务登录完成后收尾（重启桥 → 列模型 → 注入 Codex catalog → 冒烟一次）：

```bash
bash "$PRJ/runtime/bridges/finish.sh" <name>
```

## 5. 填上游 key（可选，按需）

`runtime/fleet.env`（权限 600，不入 git）里这些键是**上游凭据**，不填对应桥保持降级：

```bash
QWEN_API_KEY=...            # qwencloud.com 控制台；与本地桥 key QWEN2CODEX_KEY 是两回事
TOKENDANCE_API_KEY=...      # tokendance.space 控制台
STEPFUN_PLAN_API_KEY=...    # StepFun 官方 Plan API（默认模型港湾 stepfun/step-5-preview 的上游）
FLEET_DEFAULT_MODEL=workbuddy/deepseek-v4-flash
```

改完后 `cd "$PRJ/kit" && bash opencodex/setup-providers.sh --env-file "$PRJ/runtime/fleet.env"`
重新注册 provider 并 pin 默认模型。

## 6. 验收

```bash
bash "$PRJ/runtime/tools/status.sh"                 # 13 座桥健康表 + ocx
python3 "$PRJ/runtime/tools/fleet_chat_test.py"      # 全舰队冒烟聊天
python3 "$PRJ/runtime/tools/verify_real_calls.py"    # 真实推理核验（抗伪造运算题）
```

然后打开 Codex（模型选择器选 `桥名/模型`，如 `workbuddy/hy4-preview`），
浏览器开 http://127.0.0.1:8796/ 看面板。已装 timer 的机器三件事自动完成：
签到（09:00）、ocx catalog 看门狗（300s，防模型列表被冲掉）、可达性探测（30min，
驱动「可达模型排最前」）。

## 7. 常见问题速查

| 现象 | 处置 |
|------|------|
| 某桥 `finish.sh` 报登录失效 | 重新登录该客户端 → 重跑 `finish.sh <name>` |
| 503 所有供应商已熔断（url 带 :15721） | CC Switch failover 队列空；确认默认模型没指到死桥，Codex 直连 :10100 不受影响 |
| Codex 选择器没模型 | 会话没重启（目录只在起会话时读）；或 `ocx-catalog-guard.sh run` 自愈后重启 Codex |
| 503 qwen_key_missing | `fleet.env` 补 `QWEN_API_KEY` → `finish.sh qwen` |
| gemini / antigravity 502 / TLS 超时 | 需国际出口；出口恢复后重启该桥（macOS `launchctl kickstart -k gui/$(id -u)/com.local.<name>2codex`，Windows/Linux 重跑 `install.sh --home ... --no-opencodex` 或直接 kill 掉 supervisor/wrapper 让自重启拉起） |
| catpaw 502 Tunnel | 连美团 VPN 后重启该桥 |
| 换机 / 路径变了 | 整体拷 `kit/` + `runtime/`，重跑 `install.sh --home <新目录>/runtime` 重写路径 |

更多排错见 README「已知问题」与 `docs/*-runbook.md`。

## 8. 卸载

```bash
bash "$PRJ/runtime/uninstall.sh"            # 停服务并删服务定义
bash "$PRJ/runtime/uninstall.sh" --purge    # 连 runtime 目录一起删
```

uninstall 不动 opencodex 的 provider 配置；需要清就用 ocx 自带命令。
