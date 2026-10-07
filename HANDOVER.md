# FleetKit 交接文档（2026-10-07）

> 本文件是整套系统的**当前**交接入口，取代 `docs/2026-10-05-fleet-topology-handoff.md`（该文描述 10-05/06 状态，部分结论已作废，保留作历史对照）。
> 读完即可接管：日常操作、排障、扩容、迁移决策。
>
> 代码根：`/Users/a1-6/AI Shared/repo/FleetKit/`
> - `kit/` = git 仓库（源码唯一可信源）。本文件就在 `kit/HANDOVER.md`。
> - `runtime/` = 本机运行根 `FLEET_HOME`（桥副本、fleet.env、logs、签到状态），由 `install.sh` 从 kit 拷贝生成。
> - 改了 `kit/` 后必须同步到 `runtime/`，否则跑的是旧版。
>
> **AI 维护提示**：拓扑已数据化。改拓扑 = 改 `docs/fleet-topology.json` 再 `python3 tools/topology.py all`，**不要手改** html/md/canvas。本文档只讲"怎么接管"，拓扑细节以 `docs/fleet-topology.{md,html}` 为准。

---

## 1. 一句话现状

本地 LLM 舰队：把 17 个客户端订阅积分，经反代理桥转成 OpenAI/Anthropic 兼容端点，由一个网关统一调度，让 Codex / Claude Code / 其他 agent 用上这些模型。

三条客户端入口，第一跳都是 **StepFun 月配额**：

- **Codex** → `~/.codex/config.toml`（CC Switch 写）→ **opencodex 反代理 :10100** → `api.stepfun.com/step_plan/v1`。`~/.codex/auth.json` 里是 StepFun 月配额 key。
- **Claude Code** → `~/.claude/settings.json`（CC Switch 托管，BASE_URL=:15721）→ **FleetKit Anthropic 网关 :8801** → `route_for(slug)` 三分支。
- **其他 agent 客户端** → **FreeLLMAPI :3001** 的 auto 链（priority：月配额 → 订阅积分 → OpenRouter 免费）。

实测（2026-10-07）：:10100(144 模型) / :15721 / :8801(283 模型) / :3001 / :15722(图片 shim) / :8796(面板) 均在听；17 座桥全部在听；可达桥 = codely/lingxi/qoder/stepfun/trae/workbuddy/xhx。

---

## 2. 自 10-06 交接文档以来的变更（本轮已实机验证）

| 日期 | 变更 | 文件 | 影响 |
|---|---|---|---|
| 2026-10-06 | **qoder 桥补回 tool_calls**：上游 `qoderclicn` CLI 忽略 OpenAI `tools`，原本把 `<tool_call>` 漏成文本，ZCode/agentic 客户端卡死在 Thinking。现已在桥内解析并回吐结构化 tool_calls + 缓冲流式。 | `runtime/bridges/qoder/qoder_bridge.py` | qoder 进入可用（TRIAL 2 周） |
| 2026-10-06 | **StepFun 图片 shim 双路分流**：带 `/` 的舰队 slug（如 `workbuddy/x`）改发往 :10100 并剥掉 Authorization；`step*` 原生 id 仍发 StepFun。修复了"会话 pin 到 15722 → 404 model does not exist"的根因。注意：判前缀必须在 `rewrite_body()` 剥前缀**之前**读原始 model+body。 | `runtime/tools/stepfun_image_shim.py` | Codex 的 fleet 模型不再走错端口 |
| 2026-10-07 | **CC Switch 注册脚本**：把 opencodex 注册为 CC Switch 的 codex/claude provider（幂等，stdlib-only，写前备份 DB）。区分 `--client-url`(config.toml 用 :15721) 与 `--upstream`(endpoint 用 :10100)。 | `tools/register_cc_switch_provider.py` + `tools/add_cc_switch_opencodex.py` | 从 UI 一键切回 fleet 路由 |
| 2026-10-07 | **CC Switch 流式失败（已绕开）**：实测 Codex 的 `/v1/responses` 走 :15721 流式 → 502 "CC Switch local proxy failed" → 随即熔断"所有供应商已熔断"，连正常腿也变 503。结论：**Codex 客户端直连 :10100，CC Switch 只写配置不承载流式**。代码默认值已切回直连。 | `register_cc_switch_provider.py` 默认值 | 避免误报熔断 |
| 2026-10-07 | **拓扑数据化**：拓扑改为 `fleet-topology.json` 单一真相源，`topology.py` 提供 `check/refresh/render/all`。`refresh` 从 `fleet_probe` 快照同步桥状态，人工标注（免费/限额标签）不被覆盖。 | `kit/tools/topology.py` + `kit/docs/fleet-topology.{json,html,md}` | AI 易维护 |
| 2026-10-07 | **pin_fleet_route.py 加固**：除根键外，额外检测 `[model_providers.opencodex]` 表是否存在，避免"根键齐全但 provider 表被吞"导致线程 pinned 到该 provider 时加载失败。 | `tools/pin_fleet_route.py`（未提交，见 §6） | 看门狗更稳 |
| 2026-10-07 | **目录收窄**：tokendance（98 模型，401 不可用）移出目录，REAL 白名单按最新 verdict 刷新。 | `kit/` 多次提交 | 选择器更干净 |

---

## 3. 组件清单（当前在听）

| 组件 | 端口 | 服务 / 启动 | 说明 |
|---|---|---|---|
| opencodex 反代理 | :10100 | `com.opencodex.proxy`（bun） | 舰队原生入口；按 slug 前缀选桥；`defaultProvider=stepfun`；目录 `~/.codex/opencodex-catalog.json`(144 模型) |
| FleetKit Anthropic 网关 | :8801 | `com.local.fleet-anthropic` | Anthropic→OpenAI 转码 + `route_for(slug)` 三分支（step*直连 / 有桥走桥 / 其余回落 ocx） |
| FreeLLMAPI | :3001 | `com.local.freellmapi`（node v22） | 跨档降级路由层；DB `freellmapi/server/data/freeapi.db`(api_keys 17 / profile_models 193) |
| StepFun 图片 shim | :15722 | `com.local.stepfun-image-cap`（带 watchdog） | 图像去重/截断 + 双路分流（见 §2） |
| 状态面板 | :8796 | `com.local.fleet-ui` | 桥健康/模型标注；权威源 `/api/status` 的 `verify.real` |
| CC Switch | :15721 | app（非 launchd） | 客户端侧 failover 代理；写 codex/claude 两份配置。**DB 缓存内存，改库须重启 app 才可见** |
| 订阅积分桥 ×17 | :8787–8803 + :8805（8804 空） | `com.local.*2codex` | 每桥一个客户端订阅；`runtime/bridges/<name>/` |

`freellmapi` 的两个脚本（`register-opencodex-upstream.mjs` / `register-openrouter-free.mjs`）**必须用 node v22** 跑：`~/.local/node-v22.20.0-darwin-arm64/bin/node`，系统 node v26 会 ABI 不匹配。

---

## 4. 关键文件与角色

| 文件 | 角色 |
|---|---|
| `docs/fleet-topology.json` | **拓扑唯一真相源**（layers/nodes/edges/bridges/live）。改拓扑改它。 |
| `tools/topology.py` | `check`(数据完整性+漂移) / `refresh`(从 fleet_probe 同步 live) / `render`(生成 html+md) / `all`。 |
| `tools/fleet_probe.py` | 实测每座桥是否真应答（非仅端口在听），并对可达桥做 tool-call 探测，产出 `~/.codex/fleet-reach.json`。`--no-tools` 只测文本可达。 |
| `tools/register_cc_switch_provider.py` | 在 CC Switch 注册/刷新 opencodex 为 codex/claude provider（幂等，写前备份 DB）。`--dry-run` 预览。 |
| `tools/add_cc_switch_opencodex.py` | 同上，专给 claude app_type 注册 opencodex provider（未提交，见 §6）。 |
| `tools/pin_fleet_route.py` | 看门狗：config.toml 被 CC Switch 改写丢 fleet 路由时抢回。已加固检测 provider 表（未提交）。 |
| `runtime/tools/stepfun_image_shim.py` | :15722 分流器（舰队 slug→10100 剥 Authorization；step*→StepFun）。 |
| `runtime/bridges/qoder/qoder_bridge.py` | qoder 桥，已补 tool_calls 回吐。 |
| `~/.cc-switch/cc-switch.db` | CC Switch **活数据库**（1.6G，运行中）。改它必须重启 app。 |
| `freellmapi/server/data/freeapi.db` | 路由层真相源；`api_keys` / `profile_models`(真链) / `settings`(`routing_strategy`)。 |

---

## 5. 接管 / 日常操作

```bash
# cwd = <repo>/kit

# 1) 看拓扑（先看这个，最省事）
python3 tools/topology.py all          # 校验 + 刷新 live + 重渲染 html/md
open ../docs/fleet-topology.html        # 人类可视化

# 2) 测真实可达性（写 ~/.codex/fleet-reach.json）
python3 tools/fleet_probe.py
python3 tools/fleet_probe.py --no-tools # 只测文本可达

# 3) 在 CC Switch 里把 fleet 路由做成可选 provider（改库后重启 CC Switch app）
python3 tools/register_cc_switch_provider.py --app-type codex
python3 tools/register_cc_switch_provider.py --app-type claude

# 4) 路由层 key 健康（FreeLLMAPI）
sqlite3 ../freellmapi/server/data/freeapi.db 'select label,status from api_keys'

# 5) 加一座桥 = 加一个客户端产能（登录后自动入 auto 链）
bash ../runtime/bridges/finish.sh <name>

# 6) 对齐 OpenRouter 免费兜底档（定期跑，:free 会转付费）
OPENROUTER_API_KEY=<key> ~/.local/node-v22.20.0-darwin-arm64/bin/node \
  ../freellmapi/server/register-openrouter-free.mjs
```

客户端接入三常量：端点 `:3001/v1`（OpenAI）或 `:3001/v1/messages`（Anthropic）、统一密钥（dashboard Keys 页）、模型名（`stepfun/step-5-preview` 日常 / `auto` 自动降级）。
**坑**：Unify 硬开，`:free` 原始 ID 会被拒，免费档必须用 canonical ID（查 `getModelGroups()`）；免费档随时转付费，别手改，重跑脚本对齐。

---

## 6. Git 状态（交接时务必先读）

```
* main 4ee2239 [origin/main: ahead 25]
  远端：gitee（可达，推送前常需 rebase）/ origin=github（代理拦截，不可达）
未提交改动：
  M  tools/pin_fleet_route.py            # provider 表检测加固（建议提交）
  ?? tools/add_cc_switch_opencodex.py    # claude 侧注册脚本（建议提交或并入 register 脚本）
  ?? bridges/qoder/.mimosa/              # qoder 桥产物，按需
  ?? docs/backups/                       # 备份目录
  ?? docs/fleet-unified-topology-2026-10-06.png
stash@{0}: wip-other-20261006            # 别人的工作副本，勿动；确认后可丢
```

**行动项**：`pin_fleet_route.py` 与 `add_cc_switch_opencodex.py` 的改进应提交；`stash@{0}` 是他人未提交改动，交接后让本人确认再处理。
**推送**：只能推 gitee（github 被代理挡），推送前先 `git fetch gitee && git rebase gitee/main`（远端曾 strip model 前缀，rebase 时注意保留 shim 的"读原始 model 在 rewrite 之前"逻辑）。

---

## 7. 待办（按优先级）

1. **CC Switch failover 队列为空** → claude 一抖就误报"所有供应商已熔断"（:8801/3001/10100 其实通）。补队列或让 Claude Code 直连 :8801。唯一已知误报 503 来源。
2. **qwen / minimax 桥**：`fleet.env` 缺真实上游 key（`QWEN_API_KEY`/`MINIMAX_API_KEY`），补齐后 `finish.sh`。
3. **cline 桥**：session 失效，`finish.sh cline` 重登。
4. **antigravity / gemini 桥**：依赖国际网出口，等代理节点恢复。
5. **workbuddy-gpt / kimi-code**：账号池冷却中，等自愈。
6. **免费档定期重跑** `register-openrouter-free.mjs`（建议每周自动化）。
7. **`~/.cc-switch/` 1.6G**：确认不回滚后可清理（运行时别删）。
8. **提交 §6 的未提交改进**并清理 `stash@{0}`。

---

## 8. 能完全替代 FleetKit 吗？（最后一任提出的问题，结论在此）

**没有单一开源项目能开箱完全替代。** FleetKit 的核心价值是三件事的组合，通用网关只覆盖其中一两件：

| 候选 | 是否替代 | 说明 |
|---|---|---|
| **FreeLLMAPI（现役路由层）** | 部分 | 跨档降级 + 双协议 + 健康打分已调好。缺点：无 config-as-code、链真相在 DB。 |
| **LiteLLM（60k★）** | 最接近的长期候选 | 声明式 YAML（可 git 版本化）、`fallbacks` 原生跨模型降级、双端点齐全。但**它不消费订阅积分**——17 座桥仍要自己写；迁移预计 1-2 小时，可并行部署逐步切。 |
| **one-api（49k★）** | 否 | 只有渠道级重试（同模型换渠道），无跨模型降级。 |
| **zen-gate** | 否 | OpenAI 兼容 LLM 网关/路由，与现役 :10100/:3001 功能重叠，冗余。 |
| **OmniRouter（15★）** | 否 | 太年轻；强项是免费 web 档，不是路由自有桥。 |

**结论**：保留 FleetKit。若想让路由层更"工程化"，把 FreeLLMAPI 迁到 LiteLLM 是唯一有意义的方向，但**订阅积分桥这一层无法被任何网关替代**，必须继续维护。迁移前先把 FreeLLMAPI 的三张表（profile_models / settings / api_keys）导出成 YAML 存档。

---

## 9. 排障速查

| 现象 | 原因 / 处置 |
|---|---|
| 503 所有供应商已熔断（url 带 :15721） | CC Switch failover 队列空，与舰队无关；补队列或直连 :8801。 |
| 404 model does not exist（url 带 :15722） | 会话 pin 到图片 shim 而非 opencodex；shim 已分流但仍可能残留，重跑 `register_cc_switch_provider.py` 把 config 指回 :10100。 |
| agentic 客户端卡在 Thinking | 桥把 tool_calls 漏成文本（qoder 曾如此，已修）；用 `fleet_probe.py` 的 `agentic` 字段核对。 |
| 503 All models exhausted | auto 链全冷却；换具体 slug 或等 UTC 午夜。 |
| 免费模型 404 unavailable for free | `:free` 转付费，重跑 `register-openrouter-free.mjs`。 |
| not in the catalog（免费档） | 用了原始 `:free` ID，改用 canonical ID。 |
| 脚本 ABI / MODULE_VERSION 不匹配 | 用了系统 node v26，换 `~/.local/node-v22.20.0-darwin-arm64/bin/node`。 |
| config.toml 被改写 | 写者是 CC Switch；`pin_fleet_route.py` 抢回属设计行为。 |
| Codex 选择器没模型 | 会话没重启（目录只在起会话时读）。 |

---

## 10. 备份与回滚

| 备份 | 内容 |
|---|---|
| `freeapi.db.bak-before-*`（freellmapi） | 路由层各阶段快照，**改 DB 前必做** |
| `~/.cc-switch/`（含 cc-switch.db） | CC Switch 活数据库，**运行中勿删** |
| `~/.codex/config.toml` / `auth.json`、`~/.claude/settings.json` 的 `.bak-before-*` | 各客户端配置快照 |

回滚：停服务 → cp 对应备份 → 重启（`launchctl kickstart -k gui/$(id -u)/com.local.freellmapi`）。
