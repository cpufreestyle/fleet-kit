# FleetKit 参考：桥清单 / 登录表 / 判据

配套 [SKILL.md](SKILL.md)。默认 shell 为 PowerShell 7（`pwsh`）。

## 桥清单（默认 `--port-base 8787`）

| 桥 | 偏移 | 端口 | 上游服务 | 凭据环境变量 |
|---|---|---|---|---|
| workbuddy | 0 | 8787 | WorkBuddy 国内版 `copilot.tencent.com` | `CODEBUDDY2OPENAI_KEY` |
| workbuddy-gpt | 1 | 8788 | WorkBuddy 海外版 `www.workbuddy.ai` | `CODEBUDDY2OPENAI_KEY` |
| qoder | 2 | 8789 | Qoder CN | `QODER2CODEX_KEY` |
| codely | 3 | 8790 | 团结 AI (tuanjie) | `CODELY2CODEX_KEY` |
| trae | 4 | 8791 | Trae CN | `TRAE2CODEX_KEY` |
| lingxi | 5 | 8792 | 灵犀 LingXi | `LINGXI2CODEX_KEY` |
| xhx | 6 | 8793 | 商汤小浣熊 | `XHX2CODEX_KEY` |
| gemini | 7 | 8794 | Google Gemini | `GEMINI2CODEX_KEY` |
| catpaw | 8 | 8795 | CatPawAI（美团内网） | `CATPAW2CODEX_KEY` |
| （UI 面板） | 9 | 8796 | 本地状态面板 | — |
| antigravity | 10 | 8797 | Google Antigravity | `ANTIGRAVITY2CODEX_KEY` |
| qwen | 11 | 8798 | 阿里 Qwen Cloud | `QWEN2CODEX_KEY` |
| cline | 12 | 8799 | Cline hub | `CLINE2CODEX_KEY` |
| zcode | 13 | 8800 | 智谱 Z.AI Coding Plan | `ZCODE2CODEX_KEY` |

`tokendance` / `stepfun` 不是本地桥，是直连 provider（无端口）。

## 登录表

| name | 登录方式 | 凭据位置 |
|---|---|---|
| workbuddy | 国内版桌面 App 登录 | 账号池 `<R>/bridges/workbuddy-cn/auths/` |
| workbuddy-gpt | 海外版桌面 App 登录 | `~/.workbuddy-ai/local_storage`；池 `<R>/bridges/workbuddy-gpt/auths/` |
| qoder | `npm i -g @qodercn-ai/qoderclicn` 后按 CLI 流程登录 | `~/.qoder-cn/.auth/user` |
| codely | 官方 CLI 设备码登录（账号需先在网页端激活） | `~/.codely-cli/oauth_creds.json` |
| trae | Trae CN IDE 登录 | IDE 登录态，桥缓存到 `~/.trae2codex/creds.json` |
| lingxi | `python3 "<R>/bridges/lingxi/login_helper.py"` 开浏览器登录 | `~/.LingXi/auth.json` |
| xhx | 商汤小浣熊桌面 App 登录 | `~/.box-agent/config/auth.json` |
| gemini | `gemini login` 或 `bridges/gemini/extract_cookies.py` 导 cookie | `~/.gemini/jetski-standalone-oauth-token` / `~/.gemini2codex/cookies.txt` |
| catpaw | CatPawAI 桌面 App 登录（需美团内网/VPN） | `<App Support>/CatPawAI/User/globalStorage/state.vscdb` |
| antigravity | Antigravity 桌面 App 登录（或 `gemini login`） | `~/.gemini/jetski-standalone-oauth-token` |
| qwen | qwencloud.com 控制台创建 API key | 写入 `<R>/fleet.env` 的 `QWEN2CODEX_KEY` |
| cline | Cline 桌面端登录 | `~/.cline/data/locks/hub/production.json` |
| zcode | `node .../zcode.cjs login --no-browser`，浏览器打开打印的 URL | JWT 落盘后桥无需重启 |

每登录一个立刻：`bash "<R>/bridges/finish.sh" <name>`（Windows 用 pwsh + Git Bash 调，见 SKILL.md）。

## 真实调用判据（verify_real_calls.py）

| 判定 | 含义 | 处置 |
|---|---|---|
| REAL | 随机运算题答对 = 真上游推理 | 可用 |
| ECHO/MIRROR | 复述暗号但算错 = 疑似透传 | 排查桥实现 |
| CANNED/MOCK | 极速+极短+答非所问 | 疑似罐头/镜像 |
| GATE | 上游欢迎/登录门禁 | 网页端激活账号 |
| AUTH_EXPIRED | 401/403，session/key 失效 | 重跑 `finish.sh <name>` |
| UPSTREAM_DOWN | 502/503/504 | VPN / 上游关停 / 拒参 |
| BRIDGE_DOWN | 连接失败或超时 | 桥没起，看 `<R>/logs` 与 status.sh |

探针按 256 → 2048 → 4096 逐级抬 `max_tokens`，避免推理模型思维链吃光额度导致空内容。

## 目录与环境变量

```
<KIT>/           git 仓库：install.sh / deploy.sh / uninstall.sh / bridges / tools / opencodex
<R>/             运行根 = --home：fleet.env、bridges/<name>/、tools/、logs/、checkin/、.venv/
```

`fleet.env` 关键键：`PORT_BASE`、`FLEET_HOME`、`FLEET_OS`、`FLEET_DEFAULT_MODEL`、各 `*_KEY`。
改端口后同步测试脚本：`--port-base` 要一致。

## 常用命令（pwsh）

```powershell
python "<KIT>/.codebuddy/skills/fleetkit-deploy/scripts/acceptance.py" --home "<R>" --json
python "<KIT>/tools/fleet_default_model.py" --status            # 默认模型 + 探测
python "<KIT>/tools/fleet_default_model.py" --guard --dry-run   # 谁坏了（只读）
python "<R>/tools/verify_real_calls.py" --only workbuddy
ocx provider list
ocx models live
ocx sync                       # 重建 catalog 并刷新 models 缓存
```

安全：13 个本地 key 只在 `<R>/fleet.env`（600）；所有桥只监听 127.0.0.1；
测试脚本与面板只打印 key 的 md5 前 8 位。
