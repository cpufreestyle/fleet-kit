# fleet-kit — 9 桥反代理舰队一键部署包

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

在 Codex 中使用：确保 ocx 服务在跑（ocx service），模型选择器里选
`桥名/模型`（如 `workbuddy/hy4-preview`）。

## 安装选项

    install.sh [--home DIR] [--port-base N] [--with-opencodex|--no-opencodex]
               [--no-start] [--skip-deps] [--dry-run] [-h]

- `--home DIR`：安装根目录，默认 ~/fleet
- `--port-base N`：起始端口，9 座桥依次占用 N .. N+8，默认 8787
- `--no-opencodex`：跳过 ocx provider 注册（之后可手动跑 bash ~/fleet/opencodex/setup-providers.sh）
- `--no-start`：只写文件和 plist，不启动桥
- `--skip-deps`：跳过 venv/依赖安装（用系统 python3）
- `--dry-run`：只打印计划，不落盘

换端口后记得同步测试脚本：python3 ~/fleet/tools/fleet_chat_test.py --port-base 9787。

### 高级：起第二套舰队

env 覆盖 launchd 目录/标签前缀/日志目录，即可与现有舰队并存：

    FLEET_LAUNCH_DIR=/tmp/f2/LaunchAgents FLEET_LABEL_PREFIX=com.localtest \
    FLEET_LOG_DIR=/tmp/f2/logs bash install.sh --home /tmp/f2 --port-base 9787

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

- bridges/finish.sh <name> [--home DIR] [--tries N] [--skip-chat]：单桥收尾（重启+验收+同步）
- tools/status.sh [--home DIR]：9 桥健康表（launchd/监听/模型数/key md5）+ ocx 状态
- tools/fleet_chat_test.py [--port-base N]：全舰队 /v1/models + 聊天测试（只打印 key 的 md5）
- opencodex/setup-providers.sh：重新注册 9 个 ocx provider（新机换端口后用）
- uninstall.sh [--home DIR] [--purge]：卸载 launchd 服务和 plist；--purge 连目录一起删

## 已知问题（2026-09-26 实测，均为用户侧/外部条件）

1. codely：上游网关对 chat 一律返回 400「欢迎使用Codely」onboarding 门禁。
   /v1/models 正常、key 有效；需登录 codely.tuanjie.cn 网页端完成首次激活。
2. gemini：403 VALI「Verify your account to continue.」——Google Code Assist
   账号验证门禁。需完成 Google 账号验证，或用「Get cookies.txt LOCALLY」
   导出 gemini.google.com cookie 覆盖 ~/.gemini2codex/cookies.txt。
3. catpaw：需要美团内网/VPN，否则 catpaw.sankuai.com 不可达（Tunnel 503）。
   连上 VPN 后执行：launchctl kickstart -k gui/$(id -u)/com.local.catpaw2codex

其余 6 桥（workbuddy、workbuddy-gpt、qoder、trae、lingxi、xhx）实测可直接用于 Codex。

## 安全说明

- 8 个本地 key 只写在 <home>/fleet.env（权限 600），仅本机使用，不要提交 git 或外发
- 所有桥只监听 127.0.0.1；gemini/catpaw 两桥不校验本地 key（Authorization 只用于上游 Google/美团）
- 测试脚本只打印 key 的 md5，不打印明文

## 目录结构

    fleet-kit/
      install.sh            一键安装（plist + venv + fleet.env + ocx 注册）
      uninstall.sh          卸载
      requirements.txt      Python 依赖
      README.md             本文
      bridges/              9 座桥源码 + finish.sh
      opencodex/            setup-providers.sh（ocx provider 注册）
      tools/                status.sh / fleet_chat_test.py / fleet_split.py
      docs/                 各桥 runbook + 全量实测报告

## 可选：TokenDance

fleet.env 里取消注释 TOKENDANCE_API_KEY= 并填入 key，重跑
bash ~/fleet/opencodex/setup-providers.sh 即追加 tokendance provider
（https://tokendance.space/gateway/v1，模型 step-5-preview 等）。

## 卸载

    bash ~/fleet/uninstall.sh            # 停服务并删 plist
    bash ~/fleet/uninstall.sh --purge    # 连 ~/fleet 目录一起删

注意：uninstall.sh 不动 ocx 的 provider 配置；如需清除，用 ocx 自带命令管理。
