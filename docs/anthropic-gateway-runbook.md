# Anthropic 网关 Runbook：Claude Code（CLI / 桌面）接入 FleetKit 模型池

2026-10-01 落地并实测。目标：让 Claude Code 直接用上 FleetKit 全部 121 个模型，
而不是某一个平台的几个模型。

## 链路

    Claude Code (CLI 或桌面版，读 ~/.claude/settings.json)
      -> http://127.0.0.1:8801  FleetKit Anthropic 网关 (launchd: com.local.fleet-anthropic)
        -> 各平台桥 8787..8800 / stepfun 直连 api.stepfun.com / ocx 网关 10100

网关只依赖 Python 标准库，跨平台（macOS launchd 由 platform.sh 安装，Linux/Windows 直接
`python3 kit/tools/anthropic_gateway.py --host 127.0.0.1 --port 8801` 即可）。

## 组件与端口

| 端口 | 服务 | 说明 |
| --- | --- | --- |
| 8801 | anthropic_gateway.py | Anthropic Messages API 兼容网关，本 runbook 主角 |
| 8787..8800 | 各平台桥 | workbuddy / workbuddy-gpt / trae / xhx / lingxi / gemini / catpaw / cline / codely / antigravity / qoder / zcode |
| 10100 | ocx 网关 | 7 个裸 gpt 型号（gpt-6-astra 等）转发到厂商 |
| 直连 | api.stepfun.com/step_plan | StepFun Plan API，唯一非桥 provider |
| 8796 | status_ui.py | FleetKit 状态面板 |
| 15721 | cc-switch 代理 | 接管 Codex/Claude 配置用的本地代理（见下文「两种形态」） |

## 日常操作

    bash tools/anthropic_gateway.sh install     # 装 launchd 服务并启动
    bash tools/anthropic_gateway.sh status      # launchd 状态 + /health
    bash tools/anthropic_gateway.sh stop|start
    bash tools/anthropic_gateway.sh uninstall
    bash tools/anthropic_gateway.sh check <slug>...   # 真实 E2E，单次约 30s，请分批
    bash tools/anthropic_gateway.sh print-config --home DIR

    curl --noproxy '*' -s http://127.0.0.1:8801/health
    # 实测 2026-10-01: models_listed=117, catalog_rows=117, 14 个 provider 全部 key:true up:true

日志：`/tmp/fleet-logs/com.local.fleet-anthropic.log`。

## 模型与别名

`/v1/models` 列出全部 117 个模型，按各平台最强在前排序。Claude Code 只会发它自己的
模型名，网关用 `CLAUDE_ALIASES` 翻译（`kit/tools/anthropic_gateway.py` 顶部）：

| Claude Code 的模型名 | 落到 |
| --- | --- |
| claude-opus-5 / claude-opus-4-8 | workbuddy-gpt/hy4-preview |
| claude-sonnet-5 / claude-sonnet-4-5 / claude-3-5-sonnet-latest | trae/seed-code-pro-0430（200k 上下文的代码专用池） |
| claude-haiku-4-5 / claude-3-5-haiku-latest | workbuddy/glm-5.2 |
| claude-fable-5 | workbuddy-gpt/gpt-5.6-luna |
| 其它 claude-* 名字 | HARBOR = stepfun/step-5-preview（picker 不能答不如给默认） |

解析结果写在响应头 `x-fleetkit-resolved-model` / `x-fleetkit-resolve-note`，
别名目标宕机时按 note 说明回退到第一个能答的目录行。

另外两条兜底（2026-10-01 新增，均带测试）：

1. 目录文件被别的工具改写时（见故障排查），少于 20 行就现场拉各桥的 `/v1/models`
   重建目录，缓存 60 秒；
2. 目录里没有某一行、但模型名的 provider 前缀确实能路由且端口活着，就直接放行，
   不再 404（`no-such-model-42` 这种没有 provider 认领的仍然拒绝）。

### 看得懂的名字（display_name，2026-10-02）

picker 里给人看的那一列是 `display_name`，模型 id 保持路由原样不动（配置、别名、
路由表全不受影响），改名的两层规则：

1. 桥自己报了人类名字就用它的：xhx 桥从上游目录带回了 `name` 字段
   （`Raccoon-Work-260817-A`、`GLM-5-3-Flash` 这种），网关优先展示；
2. 上游名字本身也是构建哈希的，用网关顶部的 `DISPLAY_NAMES` 表：xhx 的三个
   Raccoon 构建号显示成看得懂的名字——

| id（路由用，不变） | display_name（picker 显示） |
| --- | --- |
| `xhx/raccoon-19b265` | `xhx/小浣熊Work-A` |
| `xhx/raccoon-405a1c` | `xhx/小浣熊Work-B` |
| `xhx/raccoon-8c4485` | `xhx/小浣熊Work` |

上游哪天换新构建号，`test_every_raccoon_hash_in_the_pool_has_a_readable_name`
会先失败，提醒往表里补一行——新哈希不会裸奔进 picker。

## Claude Code 接入：两种形态

### A. 直连（当前生效，推荐）

`~/.claude/settings.json` 的 env 直接指向网关，Claude Code CLI 和桌面版都读这个文件：

    ANTHROPIC_BASE_URL=http://127.0.0.1:8801
    ANTHROPIC_AUTH_TOKEN=dummy            # 网关仅监听 loopback，不校验 token
    ANTHROPIC_MODEL=stepfun/step-5-preview
    ANTHROPIC_DEFAULT_OPUS_MODEL=workbuddy-gpt/hy4-preview
    ANTHROPIC_DEFAULT_SONNET_MODEL=stepfun/step-5-preview
    ANTHROPIC_DEFAULT_HAIKU_MODEL=workbuddy/glm-5.2
    ANTHROPIC_DEFAULT_FABLE_MODEL=workbuddy-gpt/gpt-5.6-luna

sonnet 位故意不走 trae（trae 上游全线 502），指到 stepfun/step-5-preview。

### B. 经 cc-switch（要用量统计/失败切换时再开）

CC Switch 的 provider 表里已注册 `FleetKit`（id `fc41d7fa-8fba-4739-9cc4-1502ae29a6fe`，
app_type `claude` 与 `claude-desktop` 各一行，sort_index=0 排最前，DB 里 is_current=1）：

- claude：env 指向 8801，四个 slot 同上；
- claude-desktop：`claudeDesktopMode=proxy` + `claudeDesktopModelRoutes`（8 个 claude-* 名字
  映射到上表同样的目标），给 Claude 桌面 App 的 3P profile 用。

**坑（2026-10-01 实测）**：cc-switch 代理真正路由到哪个 provider 只认 UI 点击。
启动时若检测到上次异常退出，它按 `proxy_live_backup` 里保存的 Live 配置恢复，
把代理当前 provider 拉回备份里那个（本次是 StepFun，其 token 已 401），
直接改 DB 的 `is_current` 不切换代理路由。所以：

- 想要「直连」：保持 cc-switch 的 claude 代理开关关闭（当前已关），用形态 A；
- 想要「走代理」：在 CC Switch 里打开 claude 代理开关，再点一下 FleetKit，
  之后 settings.json 会被改写回 `http://127.0.0.1:15721`，代理按 FleetKit 的 env 转发到 8801。
- 改 DB 前先备份：`cp ~/.cc-switch/cc-switch.db ~/.cc-switch/cc-switch.db.bak-<ts>`
  （本次备份：`cc-switch.db.bak-before-fleetkit-anthropic-20261001-230121`）。

### C. 桌面 App 的 3P profile（`claude_desktop_bridge.py`，2026-10-02）

桌面版**不读** `~/.claude/settings.json` 的 env。它把自己的一套推理配置放在
`configLibrary` 里，`_meta.json` 的 `appliedId` 决定当前用哪一份。这台机器上位置是：

    ~/Library/Application Support/Claude-3p/configLibrary/<entry-id>.json

跨平台规则（读自 app.asar 1.32352.1 的 `gf()`）：`CLAUDE_USER_DATA_DIR` 优先；
win32 走 `%LOCALAPPDATA%\\Claude-3p`；其余平台是 `<app userData>-3p`，
macOS 即 `~/Library/Application Support/Claude-3p`，Linux 即 `~/.config/Claude-3p`。
条目 id 必须匹配 `/^[a-f0-9-]{36}$/`，本工具默认用端口编码：8801 →
`00000000-0000-4000-8000-000000088010`（与 cc-switch 15721 的 `...157210` 同风俗）。

    python3 tools/claude_desktop_bridge.py --dry-run apply   # 只看不写
    python3 tools/claude_desktop_bridge.py apply             # 写入并 applied
    python3 tools/claude_desktop_bridge.py status            # 谁生效 + 网关答不答
    python3 tools/claude_desktop_bridge.py off               # 退回上一个条目
    python3 tools/claude_desktop_bridge.py remove            # 删掉本工具写的条目

写盘前自动备份 `.bak-<时间戳>`，cc-switch 条目原样保留，随时 `off` 切回去。
**改完要重启 Claude.app**，配置是启动时读的。

#### 为什么原来只有 4 个模型

桌面版的模型发现是 `GET {base}/v1/models?limit=1000`，然后按这个规则过滤
（同样读自 app.asar，`il()` 与 `rl`）：

    rl   = ["sonnet","opus","haiku","fable","mythos"]
    jye  = /ark-code|astron|command-r|deepseek|doubao|gemini|gemma|glm|gpt|grok|
           hermes|hy3|kimi|lfm|\bling\b|llama|longcat|mimo|minimax|mistral|
           mixtral|moonshot|nemotron|openai|phi-|qianfan|qwen|tc-code|\bunic\b|
           yi-|stepfun|step-3|seed-|bytedance|hunyuan|granite|amazon\.nova|
           nova-|devstral|ministral|ernie|codex|arcee|trinity|abab|phi\d|\bk2\.|
           \bm2\.|jamba|arctic|solar|mercury|zamba|kat-coder|\bds-|dpsk/
    保留 = !jye.test(id) && (id 命中 ^(sonnet|opus|haiku|fable|mythos)-?版本$ 
                             || id 含 claude/opus/sonnet/haiku/fable/mythos/anthropic)
           || 该行带 anthropic_family_tier

也就是说：id 长得不像 Anthropic 的行会被整行丢掉。`stepfun/step-5-preview`、
`workbuddy/glm-5.2` 全部命中 `jye`，所以 121 行只剩 4 个 claude-*。
解法是网关给每一行打 `anthropic_family_tier` + 首行 `is_family_default`
（`anthropic_gateway.py` 的 `models_payload()`）：

    curl -s 'http://127.0.0.1:8801/v1/models?limit=1000' | python3 -c \
      "import sys,json;d=json.load(sys.stdin);r=d['data'][0];print(r)"
    {"type": "model", "id": "antigravity/claude-opus-4-8@default",
     "display_name": "claude-opus-4-8@default", "provider": "antigravity",
     "transport": "bridge", "anthropic_family_tier": "opus",
     "is_family_default": true}

    # 121 行全部带 tier：opus 35 / mythos 29 / fable 20 / sonnet 20 / haiku 17

tier 名取自应用自己的 `rl`，取值是该行在**自己 provider 内**的强弱档位
（`family_tier()`）：单模型 provider 即它的 opus，22 模型 provider 的末位才是
mythos，不会把弱模型包装成强模型。`is_family_default` 是 tier 内第一名，
裸别名（`opus`/`sonnet`）就靠它解析。


**顺带一提排序**：claude_desktop_bridge 的 picker 顺序就是网关 `/v1/models` 的顺序，
而网关在目录被 cc-switch 压成 1 行时会回落到「各桥 strongest-first 轮询」
（见下方故障排查第一条）。所以 cc-switch 启动过之后，桌面 picker 的顺序是轮询序而不是
目录序；121 个模型一个不少，只是第一屏不再是 stepfun 打头。要恢复目录序就把
`cc-switch-model-catalog.json` 的 .bak 拷回去（`catalog_sort.py` 每次写盘都留）。

**条目里故意不写 `inferenceModels`**：应用文档原话是「模型列表让发现变得不必要时
就跳过发现」，写上 4 个模型就等于关掉发现，picker 又退回 4 行。

## 实测记录（2026-10-01，全部真实输出）


    # 非流式
    curl --noproxy '*' -s -i -X POST http://127.0.0.1:8801/v1/messages \
      -H 'Content-Type: application/json' -H 'anthropic-version: 2023-06-01' \
      -d '{"model":"claude-opus-5","max_tokens":64,"messages":[{"role":"user","content":"Reply exactly: E2E_OK"}]}'
    -> HTTP/1.1 200 OK
       x-fleetkit-resolved-model: workbuddy-gpt/hy4-preview
       {"content":[{"type":"text","text":"E2E_OK"}],"stop_reason":"end_turn"}

    # 流式：message_start -> ping -> content_block_start/delta/stop -> message_delta/stop 全序列
    # count_tokens: {"input_tokens": 2, "estimated": true, ...}  （chars/4 估算，无 tokenizer）
    # 四 slot 直连全部 E2E_OK：hy4-preview / step-5-preview / glm-5.2 / gpt-5.6-luna

注意 step-5-preview 是小预算敏感模型：`max_tokens=48` 时 48 个 token 全花在推理上，
正文为空且 `stop_reason=max_tokens`；预算 >=128 即正常返回。Claude Code 自身预算不受影响，
自己写脚本调它时请给 >=128。

### 2026-10-02：桌面 App 3P profile 打通，picker 从 4 个变 121 个

    $ python3 tools/claude_desktop_bridge.py apply
    backup        : .../Claude-3p/configLibrary/_meta.json.bak-20261002-122455
    applied       : 00000000-0000-4000-8000-000000088010

    $ python3 tools/claude_desktop_bridge.py status
      * 00000000-0000-4000-8000-000000088010  FleetKit  http://127.0.0.1:8801
        00000000-0000-4000-8000-000000157210  CC Switch  http://127.0.0.1:15721/claude-desktop
    gateway       : http://127.0.0.1:8801 -> 121 models

    $ curl -s 'http://127.0.0.1:8801/v1/models?limit=1000' | ...
    rows 121 | {'fable': 20, 'haiku': 17, 'mythos': 29, 'opus': 35, 'sonnet': 20}
    defaults ['antigravity/claude-opus-4-8@default', 'cline-free/muse-spark-1.3-contributor',
              'codely/codely-core', 'codely/codely-flash', 'gemini/gemini-2.5-flash']

    $ curl -X POST http://127.0.0.1:8801/v1/messages ... step-5-preview
    {"type": "message", "model": "stepfun/step-5-preview",
     "content": [{"type": "text", "text": "Hi"}], "stop_reason": "end_turn"}   http=200

网关重启过一次（`launchctl kickstart -k gui/$(id -u)/com.local.fleet-anthropic`）才让
tier 标签生效；旧进程里跑的是没有 tier 的旧代码。

## 故障排查

    lsof -nP -iTCP:8801 -sTCP:LISTEN          # 网关在不在
    curl --noproxy '*' -s http://127.0.0.1:8801/health
    bash tools/anthropic_gateway.sh check <slug>   # 单个约 30s，分批跑
    launchctl kickstart -k gui/$(id -u)/com.local.fleet-anthropic   # 重启服务

- **401 `team not allowed to access model ... alias-only-proxy-models`**：OCX/NIM 侧
  team 权限，该 team 只能走代理别名。换模型或换 team，不是网关问题。
- **11128 `Illegal API invocation from an unapproved channel`**：上游渠道校验失败。
  网关的 `upstream_refusal` 会把它识别成错误返回（不会当成功吐出来），换 provider 重试。
- **trae 整个 provider 上游全线故障**（与本网关无关的存量问题）：
  `{"error":{"message":"We're sorry, the param is invalid. Please try with a valid param.","type":"trae_upstream_error"}}`，
  HTTP 502。打 trae 桥 8791 任何模型、任何 max_tokens 都一样。后果：`claude-sonnet-5`
  等别名实际不可用；桌面 sonnet 位已改指 stepfun/step-5-preview。等 trae 恢复后可以把
  `CLAUDE_ALIASES` 的 sonnet 行改回去。
- **`/v1/models` 突然只剩 1 个模型 / 直连模型 id 404**：cc-switch 每次启动会把共享目录
  `~/.codex/cc-switch-model-catalog.json` 改写成一个模型（它当前 provider 的）。
  网关已能自愈（见上），要立刻恢复 Codex 选择器就copy 回去：
  `cp ~/.codex/cc-switch-model-catalog.json.bak-20261001-211006 ~/.codex/cc-switch-model-catalog.json`
  （该文件由 `tools/catalog_sort.py` 每次写入时留 .bak，保留 5 份）。

## 回滚

- Claude Code 配置：`~/.claude/settings.json.bak-before-fleetkit-*`（含 hooks/permissions 的原样备份）。
- cc-switch：`~/.cc-switch/cc-switch.db.bak-before-fleetkit-anthropic-20261001-230121`
  （app 退出后覆盖回去即可；config.json 同步改回了 FleetKit current）。
- 网关本身：`bash tools/anthropic_gateway.sh uninstall`。

## 测试

    cd kit/tools
    ../../runtime/.venv/bin/python -m pytest test_anthropic_gateway.py -q   # 48 passed
    ../../runtime/.venv/bin/python -m pytest test_claude_desktop_bridge.py -q  # 20 passed
    ../../runtime/.venv/bin/python -m pytest test_bridge_tables_agree.py \
      test_upstream_errors.py test_pin_fleet_route.py test_stepfun_shim_wiring.py \
      test_no_undefined_names.py -q                                        # 57 passed
