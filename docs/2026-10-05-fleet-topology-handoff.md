# FleetKit LLM 舰队拓扑交接文档

> 最后更新：2026-10-06（初版 2026-10-05；10-06 的变更见文末「变更记录」）
>
> 本文是整套系统的唯一交接入口。读者不需要任何口头上下文：读完即可接管日常操作、排障、扩容与迁移。
> 可视化拓扑（含连线与三条真实路径）：`/Users/a1-6/.cursor/projects/empty-window/canvases/fleetkit-topology.canvas.tsx`
> 全客户端对比与统一入口拓扑（18 个客户端怎么接）：`/Users/a1-6/.cursor/projects/empty-window/canvases/all-clients-unified-topology.canvas.tsx`
> 代码根：`/Users/a1-6/AI Shared/repo/FleetKit`（kit=源码，runtime=本机部署实例）。

## 1. 一句话现状

三条客户端走三个不同的本地入口，但 **第一调度都是同一个东西：StepFun 月配额**。

- **Codex**：`~/.codex/config.toml`（由 CC Switch 的「StepFun」provider 写入）→ **opencodex :10100**
  （`defaultProvider=stepfun`）→ `api.stepfun.com/step_plan/v1`。`~/.codex/auth.json` 里放的就是 StepFun 月配额 key。
- **Claude Code**：`~/.claude/settings.json`（被 CC Switch 代理托管，BASE_URL=:15721）→ **FleetKit Anthropic 网关 :8801**
  → 它的 `route_for(slug)` 三分支：`step*` 直连官方月配额 / 有桥的 provider 走桥 / 其余回落 opencodex。
- **其他 agent 客户端**：**FreeLLMAPI :3001** 的 auto 链，策略 `priority`，按
  `priority 1-4` 月配额 → `priority 5-185` 订阅积分 → `priority 188-199` OpenRouter 免费档 依次降级。

底层产能：**15 座 FleetKit 反代理桥(:8787-8803)** 把各家客户端订阅积分转成 OpenAI 兼容端点，
外加 **opencodex :10100**（71 个 slug 的舰队原生网关）与 **OpenRouter 免费档**。
当前实测：15 座桥全部在监听，FreeLLMAPI 17 把 key 全部 healthy，auto 链 193 行。

## 2. 拓扑图

```
[客户端]                [本地入口]                      [产能档位]                        [上游]
Codex CLI/桌面 ────▶ opencodex :10100 ──────┬────▶ 档0 StepFun 月配额 ────▶ api.stepfun.com/step_plan/v1
Claude Code ───────▶ CC Switch :15721      │  ├──▶ 档1 订阅积分桥 ×15 ────▶ 127.0.0.1:8787-8803（15 家订阅）
                      └→ 网关 :8801 ─────────┤  └──▶ 档2 OpenRouter 免费 ────▶ openrouter.ai :free
其他 agent 客户端 ──▶ FreeLLMAPI :3001 ─────┘
```

入口各自的行为差异：opencodex 有 `compactionRouting`（压缩走便宜模型省配额）和子代理分档；
:8801 做 Anthropic→OpenAI 协议转码并按 slug 选上游；:3001 做跨档降级与 key 轮换。

## 3. 组件清单

| 组件 | 地址 | 服务(launchd) | 说明 |
|---|---|---|---|
| FreeLLMAPI | http://127.0.0.1:3001 | `com.local.freellmapi` | 路由层。OpenAI `/v1` + Anthropic `/v1/messages`。DB: `freellmapi/server/data/freeapi.db` |
| FleetKit Anthropic 网关 | http://127.0.0.1:8801 | `com.local.fleet-anthropic` | Anthropic 协议转码 + `route_for(slug)` 三分支路由；Claude Code 经 CC Switch 代理过来 |
| opencodex 网关 | http://127.0.0.1:10100 | `com.opencodex.proxy` | 舰队原生入口；目录 `~/.codex/opencodex-catalog.json`；子代理 `~/.codex/agents/ocx-*.toml`；`defaultProvider=stepfun` |
| FleetKit 桥 ×15 | :8787-8803 | `com.local.*2codex` | 每桥一个客户端订阅(积分)。`runtime/bridges/<name>/` |
| StepFun 月配额 | api.stepfun.com/step_plan/v1 | 外部官方端点 | opencodex 的 `stepfun` provider；FleetKit 侧由 `STEPFUN_PLAN_API_KEY` 供给 |
| OpenRouter 免费档 | openrouter.ai | 外部 | FreeLLMAPI 一路 key(id=32)，9 个在用模型挂链尾 |
| StepFun 镜像 shim | :15722 | `com.local.stepfun-image-cap`（带 watchdog） | step-5-preview 图像能力补偿，保留运行 |
| 状态面板 | :8796 | `com.local.fleet-ui` | 桥健康/日志（302 跳转属正常） |
| CC Switch | :15721 | app（非 launchd） | 客户端侧 failover 代理；写 codex/claude 两份配置。**其 claude failover 队列为空 -> 下游一抖就报「所有供应商已熔断」，而 8801/3001/10100 其实都通** |

## 4. 关键文件

| 文件 | 作用 |
|---|---|
| `~/.codex/config.toml` | 由 CC Switch 的「StepFun」provider 写入：`model_provider=opencodex`、`model=step-5-preview`、catalog=`~/.codex/opencodex-catalog.json`。残留的 `[model_providers.custom]->15721` 是历史键。**fleet 路由键(openai_base_url / experimental_realtime_ws_base_url / [model_providers.opencodex])必须保留**，否则 FleetKit 看门狗会抢配置 |
| `~/.codex/auth.json` | StepFun 月配额 key（`UIZMg...`，64 位）。Codex 直连 opencodex 用的就是它；**不是** FreeLLMAPI 统一密钥 |
| `~/.claude/settings.json` | 被 CC Switch 代理托管：`ANTHROPIC_BASE_URL=http://127.0.0.1:15721`、`PROXY_MANAGED`；CC Switch 库里 claude 当前选中项是 FleetKit(8801, `ANTHROPIC_MODEL=stepfun/step-5-preview`)。注意 8801 的 `sonnet` 别名映射是坏的(指向 trae 502)，已绕开 |
| `~/.codex/opencodex-catalog.json` | Codex 当前真正的模型选择器数据源（约 1.9MB，随 `ocx sync` 更新） |
| `~/.codex/freellmapi-catalog.json` | **遗留物**：10-05 切 FreeLLMAPI 直连时的选择器源，Codex 已不再读它，可留作对照 |
| `freellmapi/server/data/freeapi.db` | 路由层唯一真相源：`api_keys`(17 把)、`profile_models`(193 行)、`settings` |
| `freellmapi/server/register-opencodex-upstream.mjs` | 把 opencodex 注册为 FreeLLMAPI 的一路 key（幂等） |
| `freellmapi/server/reorder-stepfun-plan-first.py` | 把月配额模型排到 auto 链首 + 切 `priority` 策略，带 `--dry-run` |
| `freellmapi/server/register-openrouter-free.mjs` | 对齐 OpenRouter 免费兜底档：拉实时免费清单、摘掉已转付费的、补新的。幂等，可反复跑 |
| `freellmapi/server/ops/2026-10-05-fleetkit-bootstrap/` | 早期一次性脚本已归档（链重建/目录生成/CC Switch 接入/密钥重加密），附 README 说明每个现在还能不能用。原 /tmp/freellmapi-shortcut/ 已无用 |
| `freellmapi/接入配方.md` | 客户端接入配方（三类客户端三行常量） |

## 5. 客户端接入（往后所有新客户端）

三个常量：端点 `http://127.0.0.1:3001/v1`（或 :3001 的 Anthropic 口）/ 统一密钥（dashboard Keys 页查看）/ 模型名。
模型名首选 `stepfun/step-5-preview`（日常）或 `auto`（自动跨档降级）。

**坑一：Unify 是硬开着的**，`:free` 这种原始 ID 会被拒（not in the catalog），免费档必须用 canonical ID：
`nemotron-3-nano-30b-reasoning` / `north-mini-code` / `nemotron-3-super-120b` / `nemotron-3.5-lightning` / `poolside-laguna-xs-2.1` / `poolside-laguna-s-2.1` / `gemma-4-26b-a4b` / `gemma-4-31b` / `ling-3.0-flash-sante`
（查任意模型的 canonical ID 用 `getModelGroups()`）。

**坑二：免费档会转付费**。OpenRouter 的 `:free` 随时可能 404「unavailable for free」，
服务也会把非目录模型级联摘链。别手改，重跑 `register-openrouter-free.mjs` 对齐。

完整配方见 `freellmapi/接入配方.md`。

**统一入口结论（2026-10-06 定）**：机器上 18 个 agent 客户端不需要各配一套路由 —— 能自填端点的一律指 :3001
（OpenAI 口 /v1、Anthropic 口 /v1/messages），三档降级在 :3001 背后；绑订阅的客户端（Antigravity / CatPawAI / Gemini.app /
Trae CN / Qoder CN / CodeBuddy CN / Claude.app）不接消费，继续当产能方。逐客户端的可填性与协议对比见 unified canvas。

## 6. 日常操作

```bash
# 以下命令默认 cwd = <repo>/kit
# 看路由层各 key 健康状态
sqlite3 ../freellmapi/server/data/freeapi.db 'select label,status from api_keys'
# 加一座桥 = 加一个客户端产能（登录后自动入 auto 链和目录，客户端零改动）
bash ../runtime/bridges/finish.sh <name>
# 重建 Codex 目录（只收可路由模型）
python3 ../freellmapi/server/ops/2026-10-05-fleetkit-bootstrap/build-catalog.py
# 重启路由层（直接改 DB 后必须做）
launchctl kickstart -k gui/$(id -u)/com.local.freellmapi
# 把月配额模型排到 auto 链首（同时把策略切成 priority）
python3 ../freellmapi/server/reorder-stepfun-plan-first.py --dry-run
python3 ../freellmapi/server/reorder-stepfun-plan-first.py
# 对齐 OpenRouter 免费兜底档（拉实时清单，摘掉已转付费的，可反复跑）
OPENROUTER_API_KEY=<key> ~/.local/node-v22.20.0-darwin-arm64/bin/node ../freellmapi/server/register-openrouter-free.mjs
```

**freellmapi 的两个新脚本必须用 node v22 跑**（服务本身是 v22 编译的 better-sqlite3，用系统 v26 会报 ABI 不匹配）。路径：`~/.local/node-v22.20.0-darwin-arm64/bin/node`。

统一密钥轮换：dashboard → regenerate，然后同步各客户端。

## 7. 待办（按优先级）

1. **CC Switch failover 队列**：`claude` 0 条、`codex` 仅 StepFun 1 条。要么给 claude 队列补 backup provider，要么让 Claude Code 直连 8801 绕开它。**这是目前唯一已知的误报 503 来源。**
2. **zcode 桥（:8800）**：captcha 票据过期失效（ZCode 限 75 秒），直连返回「captcha 票据不在新鲜期内」；自动铸造被关（ZCODE_CAPTCHA_AUTOMINT=0）。人工开 http://127.0.0.1:8910/（实测在监听）换一张票即恢复，或让 captcha-mint.py 预热票据池。恢复后 GLM-5.3 / GLM-5.3-Flash 两个模型自动入链。
3. **qwen / minimax 桥**：`fleet.env` 缺真实上游 key（QWEN_API_KEY / MINIMAX_API_KEY），补齐后 `finish.sh <name>`。
4. **cline 桥**：session 失效，`bash runtime/bridges/finish.sh cline` 重登。
5. **antigravity / gemini 桥**：依赖国际网出口，等 MacPacket 代理(:1082) 节点恢复。
6. **workbuddy-gpt / kimi-code**：账号池冷却中，等自愈。
7. **免费档定期重跑**：建议每周跑一次 `register-openrouter-free.mjs`（可建自动化）。目录里另有 13 家免费 provider（Google AI Studio / Groq / Cerebras / GitHub Models / Cohere…）仍未注册 key，愿意折腾可以继续扩这一档。
8. **OmniRouter 评估**：已 clone 在 `~/AI Shared/repo/omnirouter` 未部署；若要免费游客档产能再上（构建 + PORT=8081 + 注册为 FreeLLMAPI 的一路 key）。
9. **`~/.cc-switch/` 1.6G**：确认不回滚后可清理（它还在运行，别在运行时删）。

## 8. 路由器选型结论（给下一任）

需求铁律：必须消费订阅积分（反代理）+ 跨客户端自动降级 + 客户端接入极简。据此评估：

| 候选 | 结论 |
|---|---|
| **FreeLLMAPI（现役）** | 可用且已调好。弱点：默认策略 `balanced` 成本盲（只按可靠性/速度/智能打分，`server/src/services/scoring.ts:51`），付费档位不受手动顺序保护 —— 已切 `priority`；catalog 月度同步其实是成功的（`catalog_applied_version=2026.10.05`、368 模型，初版写 fetch failed 有误）；链有两套表（`profile_models` 才是真链，`fallback_config` 是历史遗留，已踩过） |
| **LiteLLM（60k★）** | **更合适的长期选择**：声明式 YAML（config-as-code，无 DB 手术、可 git 版本化）；`router_settings.fallbacks` 原生跨模型降级；60k★ 社区；OpenAI+Anthropic 双端点齐全。代价：Python 依赖稍重、降级链要手写（无动态健康打分） |
| new-api（49k★） | 不满足：只有渠道级重试（同模型换渠道），无跨模型降级 |
| OmniRouter（15★、27 天） | 不满足：太年轻；强项是免费 web 档而非路由自有桥 |

**建议的下一步（可选迁移，不动现状）**：LiteLLM 并行部署 → 每座桥作为一个 model group、`stepfun/step-5-preview` 为主模型、其余按质量写 fallbacks → Codex/Claude Code 逐个切 → 验证后退役 FreeLLMAPI。预计 1-2 小时，随时可回滚。
**过渡期最低要求**：把 `profile_models` 的顺序、`settings` 里的 `routing_strategy`、`api_keys` 三张表导出成 YAML 存档，否则 FreeLLMAPI 一坏就说不清当时怎么配的。

## 8.5 为什么统一入口是 :3001，不是 opencodex 或 CC Switch

**opencodex :10100 —— 留给 Codex 专用，不做统一入口**
- 协议不全：20 个 provider 的 adapter 全是 `openai-chat`(19) / `openai-responses`(1)，**没有一个 anthropic 适配器**，
  所以 ZCode、Claude Code 这类讲 Anthropic Messages 的客户端根本接不进来。
- 没有跨档降级：`configRebaseProvenance.deletedTopLevelKeys=["combos"]` 说明它原来的 combos（组合/回退）已被删，
  现在没有任何 fallback 配置；某个 slug 的上游抖了就是抖了，不会自动落到别的档。
- 它的强项恰好只有 Codex 用得上：`defaultProvider=stepfun`（月配额第一跳）、
  `compactionRouting -> codely-core`（上下文压缩专门走便宜模型省配额）、`subagentModels` 子代理分档、71 个短名 slug。
- 也没有免费档产能（OpenRouter 那条路只在 :3001 有）。

**CC Switch :15721 —— 降级为配置写入器，不再是故障点**
- 它是 macOS app（`/Applications/CC Switch.app`），而多平台规则要求 win/mac/linux 严格分开，
  拿它当入口等于把整个舰队绑死在 macOS 上。
- failover 队列是空的（`claude` 0 条、`codex` 仅 StepFun 1 条），没有降级能力，
  「所有供应商已熔断」这个误报就是从这里来的。
- 它不做路由、不做健康打分，只是"转发到当前选中的 provider"，加一跳没有任何收益，
  而且是 GUI，没有 API 给其他客户端调。

**:3001 能当统一入口的原因**：同端口两种协议（OpenAI `/v1` + Anthropic `/v1/messages`）；
背后是三档链 + 健康打分 + 冷却 + 429 降位，真有降级能力；免费档只有它有；
17 把 key 全部 healthy；launchd 常驻且跨平台。

**代价（已知并接受）**：Codex 改走 :3001 会失去 compaction 省钱与子代理分档，
所以 Codex 保持直连 opencodex；`:3001` 重启会影响所有客户端，重启要避开使用时段。

## 8.6 本轮收窄：四个客户端的接线与实测（2026-10-06）

只收窄 **Codex / Hermes / Claude Code / ZCode** 四个，其余客户端不动。

| 客户端 | 接到 | 协议/配置 | 实测 |
|---|---|---|---|
| Codex CLI/桌面 | :3001 | @@BT@@model_provider="custom"@@BT@@ + @@BT@@[model_providers.custom]@@BT@@（base_url :3001/v1, wire_api=responses）；model=@@BT@@stepfun/step-5-preview@@BT@@；catalog=@@BT@@freellmapi-catalog.json@@BT@@；auth.json 换统一密钥 | @@BT@@codex exec@@BT@@ 返回 ok，无 Unknown model 警告 |
| Hermes Agent CLI | :3001 | config.yaml 的 @@BT@@custom_providers@@BT@@ 加 name=@@BT@@freellmapi@@BT@@（base_url http://127.0.0.1:3001/v1 + 统一密钥，api_mode=chat_completions），@@BT@@model.provider=freellmapi@@BT@@、@@BT@@model.default=stepfun/step-5-preview@@BT@@，@@BT@@fallback_model@@BT@@ 指向同 provider 的 @@BT@@auto@@BT@@ | @@BT@@hermes -z@@BT@@ 返回 ok（改之前完全不可用） |
| Claude Code | :3001 | settings.json：@@BT@@ANTHROPIC_BASE_URL=http://127.0.0.1:3001@@BT@@、@@BT@@ANTHROPIC_AUTH_TOKEN=统一密钥@@BT@@、@@BT@@ANTHROPIC_MODEL=stepfun/step-5-preview@@BT@@、@@BT@@ANTHROPIC_SMALL_FAST_MODEL=auto@@BT@@ | @@BT@@claude -p@@BT@@ 返回 ok |
| ZCode | 接不了 | app 内置 20 个 provider 模板的 baseUrl 全部写死、无自定义入口、无用户级配置文件 | 只能当产能方（zcode 桥 :8800） |

**踩过的坑（都记下来省得重复踩）**
- Hermes 原来 provider 名是 @@BT@@nvidia@@BT@@，撞上内置 provider 名，于是它去找 @@BT@@NVIDIA_API_KEY@@BT@@ 环境变量、完全忽略 @@BT@@custom_providers@@BT@@ 里写的 api_key -> 报 "no API key was found"。**自定义 provider 别用内置名**。
- Codex 的 @@BT@@model_catalog_json@@BT@@ 对 schema 很严：少 @@BT@@supported_reasoning_levels@@BT@@ 或 @@BT@@priority@@BT@@ 都直接拒。现成生成办法见 @@BT@@/tmp/build_catalog3.py@@BT@@（以 @@BT~~/.codex/opencodex-catalog.json~~@@BT@@ 第一条为模板，逐条覆盖 slug/display_name/description/context_window/priority，priority 取 profile_models 的真实链序）。
- Codex 也可不要 catalog（删掉 @@BT~~model_catalog_json~~@@BT~~ 那行），它会自己拉 @@BT~~/v1/models~~@@BT~~，但会警告 "Unknown model ... fallback metadata"，性能可能降级，不推荐。

**副作用（已知并接受）**
- Codex 离开 opencodex 后失去 @@BT~~compactionRouting~~BT~~（压缩走 codely-core 省月配额）和子代理分档；换来的是三档自动降级。要省配额就把 @@BT~~model~~BT~~ 改回 opencodex 路由。
- @@BT~~~/.codex/auth.json~~BT~~ 的 key 从 StepFun 月配额 key 换成 FreeLLMAPI 统一密钥；StepFun 月配额 key 仍在 @@BT~~runtime/fleet.env~~BT~~ 和 opencodex 里用，没丢。
- CC Switch 里 codex 当前选中项仍是「StepFun」provider，**在 CC Switch 里点切换会把 config.toml 写回 opencodex 路由**，届时重跑本节接线即可。

## 9. 多平台规则

Windows / macOS / Linux 严格分开部署：各自 `runtime/`、各自 `install.sh`，禁止共用 runtime。
新机器步骤（10-06 版）：

1. 平台 `install.sh` 部署 FleetKit
2. 部署 FreeLLMAPI（用 node v22）
3. 跑 `register-opencodex-upstream.mjs`（把 opencodex 注册成一路 key）
4. 跑 `reorder-stepfun-plan-first.py`（月配额排链首 + 切 `priority`）
5. 按接入配方指客户端（`接入配方.md`）
6. 若要用免费兜底档：注册 OpenRouter key 后跑 `register-openrouter-free.mjs`，之后每周重跑
7. `finish.sh` 逐步加桥

局域网客户端需把 `freellmapi/server/.env` 的 `HOST=127.0.0.1` 改为 `0.0.0.0`（注意：统一密钥是唯一防线）。

## 10. 备份与回滚

| 备份 | 内容 |
|---|---|
| `freeapi.db.bak-before-*`（collapse/reencrypt/rewire/bridges/stepfun-first/openrouter 各一份） | 路由层 DB 各阶段快照，**改 DB 前必做** |
| `~/.codex/config.toml.bak-before-freellmapi-direct-*` | 10-05 切 FreeLLMAPI 直连前的 config（现已被 CC Switch 改写回 opencodex 路径，留作对照） |
| `~/.codex/auth.json.bak-before-*` | auth 变更前状态 |
| `~/.claude/settings.json.bak-before-*` | Claude Code 各阶段配置 |
| `~/.cc-switch/`（1.6G，含 cc-switch.db） | **CC Switch 仍在运行，这是它的活数据库**，不是历史存档 |

回滚任一状态：停服务 → cp 对应备份回位 → 重启服务。
回滚路由层到某个时间点：`cp freeapi.db.bak-before-xxx freeapi.db`，然后 `launchctl kickstart -k gui/$(id -u)/com.local.freellmapi`。

## 11. 排障速查

| 现象 | 原因/处置 |
|---|---|
| 503 所有供应商已熔断（url 带 :15721） | CC Switch 自己的 failover 队列空，与舰队无关；此刻 8801/3001/10100 都是通的。补队列或直连 8801 |
| 503 All models exhausted | auto 链全在冷却；换具体 slug 或等 UTC 午夜重置 |
| 某免费模型 404 unavailable for free | 该 `:free` 已转付费。重跑 `register-openrouter-free.mjs` 按实时清单重新对齐 |
| 调免费档报 not in the catalog | 用了原始 `:free` ID；Unify 硬开，必须用 canonical ID（见第 5 节） |
| 免费模型返回 empty completion | reasoning 模型把 token 全用在思考上；加大 `max_tokens` 再试 |
| 某 pinned 模型 404/502 | 对应桥死了（缺 key/验证码/代理）；auto 不受影响，pinned 需手动换 |
| 脚本报 ABI / MODULE_VERSION 不匹配 | 用了系统 node v26，换 `~/.local/node-v22.20.0-darwin-arm64/bin/node` |
| 401 | 密钥不对 = 轮换后没同步 |
| Codex 选择器没有模型 | 会话没重启（目录只在起会话时读） |
| config.toml 被改写 | 写者是 CC Switch（切 provider 时重写）；看门狗 `pin_fleet_route.py` 抢回 fleet 路由属设计行为 |

## 12. 变更记录

**2026-10-06（本次，全部实机验证）**

- 核实 Codex/Claude Code 的真实入口：Codex 走 opencodex 直连（不经 :3001），`~/.codex/auth.json` 里是 StepFun 月配额 key；Claude Code 被 CC Switch 代理托管在 :15721。**修正了初版「CC Switch 已退役」的错误结论。**
- 把 FreeLLMAPI 的 4 个月配额模型从 `priority 134-137` 提到 `priority 1-4`，策略从 `balanced` 切到 `priority`。实测：改前 `auto` 命中 `deepseek-v4-pro`，改后命中 `step-5-preview`。
- 新增脚本 `freellmapi/server/reorder-stepfun-plan-first.py`、`freellmapi/server/register-openrouter-free.mjs`。
- 注册 OpenRouter 免费档（`api_keys` id=32），9 个仍在免费的模型挂 `priority 188-199`，实测 `north-mini-code` 与 `ling-3.0-flash-sante` 通；该 key 已被健康检查转 healthy。
- 发现并记录 Unify 硬开导致必须用 canonical ID；发现免费档会转付费（当天 12 个里 5 个 404），服务会自动级联摘链（197 行掉到 191 行）。
- 更新拓扑图（四列）与档位表格，产出可视化拓扑 `fleetkit-topology.canvas.tsx`。

**2026-10-05（初版）**：基于 vLLM + LiteLLM 链路的旧描述、CC Switch 接入与退役尝试、模型命名规范化（`xhx/raccoon-*` 改成可读名）。该日结论中关于 CC Switch 已退役、catalog 同步失败、Codex 指向 :3001 三项已作废。
