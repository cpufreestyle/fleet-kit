---
name: fleetkit-deploy
description: Deploy and accept a FleetKit bridge fleet — 13 local reverse-proxy bridges plus opencodex wiring — and diagnose the resulting Codex model picker. Use when the user installs, deploys, re-installs or verifies FleetKit (install.sh, deploy.sh, bridges/finish.sh, tools/status.sh), when a bridge needs re-login, or when Codex reports "502 Provider unreachable" on http://127.0.0.1:10100/v1/responses.
---

# FleetKit 部署与验收

一套 Codex → opencodex 代理（127.0.0.1:10100）→ 13 座本地反代理桥 → 各 AI 订阅服务的舰队。
kit（git 源码）与 runtime（运行根）分离：所有服务路径由 `--home` 决定。

## Shell 约定：默认 PowerShell 7

**所有命令默认在 PowerShell 7（`pwsh`）里执行**，代码块标 ```powershell 的都用 pwsh 跑。

- 没装 pwsh：`winget install -e --id Microsoft.PowerShell`，装完重开终端（`preflight.py` 会报 `pwsh: WARN`）。
- 5.1 只作兜底：没有 `&&`、部分 cmdlet 行为不同，遇到怪错先确认 `$PSVersionTable.PSVersion` ≥ 7。
- macOS/Linux 直接用 bash，无需 pwsh。
- Windows 上 `.sh` 脚本（install/deploy/finish/status）仍是 bash，通过 pwsh 调 Git Bash：

```powershell
pwsh -NoProfile -Command "& 'C:\Program Files\Git\bin\bash.exe' -lc \"bash 'D:/kit/deploy.sh' --home 'D:/kit/runtime'\""
```

`preflight.py` / `acceptance.py` 是 Python，pwsh 里直接 `python <script>` 即可，不需要 bash。

## 快速开始

`<KIT>` = 仓库根（含 `install.sh`），`<R>` = 运行根。

```powershell
# 1) 部署前体检
python "<KIT>/.codebuddy/skills/fleetkit-deploy/scripts/preflight.py" --home "<R>"

# 2) 部署：preflight → install → 等端口 → 逐桥收尾 → ocx → 汇总（幂等，可重跑）
pwsh -NoProfile -Command "& 'C:\Program Files\Git\bin\bash.exe' -lc \"bash '<KIT>/deploy.sh' --home '<R>' --with-ui --with-checkin\""

# 3) 验收（端口 + /v1/models + 最小聊天）
python "<KIT>/.codebuddy/skills/fleetkit-deploy/scripts/acceptance.py" --home "<R>" --chat
```

## 部署流程

1. **体检**：`preflight.py --home "<R>"`。硬阻塞先解决（端口占用、缺 Git Bash、无 ocx 只能跳过接线）。
2. **安装**：`bash "<KIT>/install.sh" --home "<R>" [--port-base 8787] [--with-ui] [--with-checkin] [--dry-run]`。
   一条龙用 `deploy.sh`（等价 + 等端口 + 逐桥 finish + ocx 接线）。
3. **登录**：按 [REFERENCE.md](REFERENCE.md)「登录表」逐个服务登录。
4. **收尾**：每登录一个就跑 `bash "<R>/bridges/finish.sh" <name> [--tries 5] [--skip-chat]`
   （重启桥 → 等 `/v1/models` → 注入 catalog → `ocx sync` → 冒烟）。
5. **接线**：`bash "<R>/opencodex/setup-providers.sh" --env-file "<R>/fleet.env"`（重跑会补短名并 pin 默认模型）。
6. **刷新选择器**：磁盘 catalog 改完**必须重启 Codex/ChatGPT** 才生效。
7. **默认模型**：跑 `fleet_default_model.py --guard`（见下），`setup-providers.sh` 结尾会自动跑一次。

## 默认模型与自动回退

规则：**Codex 默认模型恒为 `step-5-preview`**（StepFun 官方 Plan API 直连，不经本地桥）。
任何 provider 探测失败，就把默认模型跳回它，并把坏 provider 的模型行从 catalog 摘掉，
避免选中即 502。判定用「每 provider 采样多个模型」（单个模型可能只是没开 Responses API）。

```powershell
python "<KIT>/.codebuddy/skills/fleetkit-deploy/scripts/fleet_default_model.py" --check           # 当前默认模型 + 是否安全
python "<KIT>/.codebuddy/skills/fleetkit-deploy/scripts/fleet_default_model.py" --guard --dry-run # 只报告，不改文件
python "<KIT>/.codebuddy/skills/fleetkit-deploy/scripts/fleet_default_model.py" --guard           # 摘掉必挂桥模型（自动 .bak 备份）
python "<KIT>/.codebuddy/skills/fleetkit-deploy/scripts/fleet_default_model.py" --apply-default   # 把推荐安全模型写进 config.toml
```

默认模型恒为可达的 StepFun 模型（`step-5-preview` 或 `stepfun/*`）。
`--guard` 按前缀保留安全模型（默认 `stepfun` / `step-` / `gpt`，可用 `--safe-prefix` 改），
把路由到本地桥、当前必 502 的模型（qoder/*、cline/*、workbuddy/* …）从 catalog 摘掉。
回退目标自己不通时脚本退出 2 并拒绝改动。`ocx sync` 会把被摘模型加回来。
改完 catalog/config 都要**重启 Codex/ChatGPT** 才生效。

## 验收清单

| 检查 | 命令 | 通过标准 |
|---|---|---|
| 桥存活 + 模型数 | `acceptance.py --home "<R>"` | 13 座桥端口通、`/v1/models` 非空 |
| 端到端聊天 | `acceptance.py --home "<R>" --chat` | 每桥 chat 列 = 200 |
| 真调上游 | `acceptance.py --home "<R>" --real` | REAL 列 = `REAL`（抗伪造运算题，耗时数分钟且计费） |
| 全舰队健康表 | `bash "<R>/tools/status.sh" --home "<R>"` | AGENT=loaded、LISTEN=up |
| 面板 | http://127.0.0.1:8796/ | 与 CLI 同源 |

未登录 / 需 VPN / 上游关停的桥**不算部署失败**，列为 unreachable，之后单独补 `finish.sh`。

## 平台差异

| 系统 | 服务后端 | 额外要求 |
|---|---|---|
| macOS | launchd plist | `launchctl`；bash 直跑 |
| Windows | `schtasks` | pwsh + **Git Bash**（可能装在 D:，按注册表/git.exe 定位）；路径含空格必须整体加引号 |
| Linux | 自重启包装脚本 | `bash` + `pkill` |

Windows 陷阱：

- PATH 里的 `C:\Windows\system32\bash.exe` 是 **WSL 启动器**，不是 Git Bash——用它跑 install.sh
  会落进 WSL 且拿不到 `schtasks`。所以 `preflight.py` 从不信任 PATH 上的 `bash`。
- Git for Windows **不一定在 `C:\Program Files`**。定位顺序：注册表
  `HKLM|HKCU:\SOFTWARE\GitForWindows\InstallPath` → 从 `git.exe` 反推根目录（`<root>\cmd\git.exe`
  → `<root>`）→ 拼 `bin\bash.exe` / `usr\bin\bash.exe` → 最后才是 `C:\Program Files\Git` 候选。
  只查 C: 会把已装在 D: 的 Git 误判成缺失，白白重装。
- pwsh 同理：装完在用户 PATH 生效前，按
  `%LOCALAPPDATA%\Programs\PowerShell-7\pwsh.exe` 兜底查找。

路径含空格可跑：`fleet.env` 每个值用双引号包住；`install.sh` 遇到空格只 warn 不退出。
换机 / 起第二套：`--home <新目录>` 重跑 install.sh，第二套另配 `--port-base`。

## 常见阻塞

- **`502 Provider unreachable`（url 10100/v1/responses）**：代理没挂，是选中模型路由到的桥没在监听。
  跑 `acceptance.py` 确认哪些端口死；全死通常是 runtime 没装在这台机器上（无 `fleet.env`、服务目录空）。
  应急：`fleet_default_model.py --guard`（自动跳回 `step-5-preview` 并隐藏坏 provider），
  或手动选 `stepfun/*`。
- **401 / AUTH_EXPIRED**：会话或 key 失效 → 重跑 `bridges/finish.sh <name>`。
- **GATE / onboarding**：上游要网页端首次激活（codely 典型）。
- **UPSTREAM_DOWN（502/503）**：需 VPN 或上游已关停（catpaw 内网、gemini 关停、antigravity 网络阻断）。
- **端口占用**：preflight 报 busy → 换 `--port-base` 或先 `bash "<旧R>/uninstall.sh" --home "<旧R>"`。

判据图例与登录表见 [REFERENCE.md](REFERENCE.md)。

## 脚本

- `scripts/preflight.py --home DIR [--port-base N] [--kit DIR] [--json]`：bash/python3/curl/ocx/pwsh、端口区间、目录可写性、空格路径、已有 runtime。退出 `0` 就绪、`1` 硬阻塞。
- `scripts/acceptance.py --home DIR [--port-base N] [--chat] [--real] [--json] [--timeout S]`：逐桥探测端口与 `/v1/models`；`--chat` 打最小聊天，`--real` 调 `tools/verify_real_calls.py`。退出 `0` 全通、`2` 部分通、`3` 全不通。
- `scripts/fleet_default_model.py [--catalog PATH] [--check] [--guard] [--dry-run] [--apply-default] [--list] [--json]`：读取 `~/.codex/opencodex-catalog.json` 与 `config.toml`，报告当前默认模型是否安全；`--guard` 把路由到本地桥、当前必 502 的模型摘掉（写 `.bak` 备份），`--apply-default` 把推荐安全模型写进 `config.toml`。退出 `0` 安全、`1` 默认不安全。
