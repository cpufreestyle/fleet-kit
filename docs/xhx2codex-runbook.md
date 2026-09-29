# xhx2codex Runbook — 商汤小浣熊（SenseTime Raccoon）反代理到 Codex

日期：2026-09-25 | 状态：**全链路已通**（桥 / ocx / 官方 CLI 三处实测 200）

## 架构（实测）
```
Codex 官方 CLI/桌面 → ocx(:10100) → xhx2codex 桥(:8793) → https://xiaohuanxiong.com/api/web/llm/v2
                          ↑
              [model_providers.xhx] config.toml (wire_api=responses) 用于官方 CLI 直连桥
```
- 桥：`~/xhx2codex/xhx_bridge.py`（FastAPI，复用 workbuddy2codex venv Python）
- LaunchAgent：`~/Library/LaunchAgents/com.local.xhx2codex.plist`（KeepAlive，日志 `/tmp/xhx-bridge.log`）
- 密钥：`XHX2CODEX_KEY`（`~/.zshrc`）
- ocx provider：`xhx`（openai-chat，base `http://127.0.0.1:8793/v1`，`allowPrivateNetwork:true`）
- 官方 CLI：`~/.codex/config.toml` `[model_providers.xhx]` → `http://127.0.0.1:10100/v1` wire_api=responses
- Codex 目录：9 个 `xhx/xhx-<model>` slug（ocx 归一化后的双前缀形态；桥 revamp 循环剥前缀，两种形态都能用）

## 学习来源（逆向自官方桌面端 /Applications/商汤小浣熊.app）
- app.asar（@electron/asar 解包）内 `build/electron/main/*.js` 与 desktop-renderer chunk：
  - 登录态：`~/.box-agent/config/auth.json`（与桌面端共享；桌面端启动时会清除并重同步，文件权限 0600）
  - 刷新：`POST https://xiaohuanxiong.com/api/web/auth/v1/refresh` body `{"refresh_token":"<rt>"}`
    → `{code:0, data:{access_token, refresh_token}}`
  - 模型目录：`GET /api/web/llm/v2/model_catalog` → `data.categories[].models[]`（name/description/params.context_window/max_tokens）
  - 聊天：`POST /api/web/llm/v2/chat/completions`（OpenAI 兼容，含 reasoning_content、SSE 标准 chunk）
  - 鉴权：`Authorization: Bearer <access_token>`（access ~3h，refresh 轮转型）
- 账号：RaccoonJoshua（nation_code 86），office_identity=personal

## 9 个模型（model_catalog 实测）
| slug | 名称 | ctx | max_tokens | 倍率 |
|---|---|---|---|---|
| `raccoon-8c4485` | Raccoon-Work（默认 agent 模型，vis=false 仍可调） | 1M | 100k | 1 |
| `raccoon-19b265` | Raccoon-Work-260817-A | 1M | 100k | 1 |
| `raccoon-405a1c` | Raccoon-Work-260817-B | 1M | 100k | 1 |
| `sn-glm-5-3-flash` | GLM-5.3-Flash | 1M | 100k | 0.2 |
| `sn-deepseek-v4-1-flash` | DeepSeek-V4.1-Flash | 1M | 100k | 0.25 |
| `sn-glm-5-3` | GLM-5.3 | 1M | 100k | 0.75 |
| `sn-kimi-k3` | Kimi-K3 | 1M | 100k | 1 |
| `sn-sensenova-6-8-flash` | SenseNova-6.8-Flash | 256k | 64k | 0.5 |
| `sn-sensenova-6-8-flash-lite` | SenseNova-6.8-Flash-Lite | 256k | 64k | 0.5 |

## 排坑记录（重要）
1. **refresh_token 单次轮换**：每次刷新都换发新 RT，旧 RT 立即失效（code 200822 refresh_conflict）。
   桥的 `refresh_token()` 成功后立即原子写回 auth.json（保留 office_identity 等字段，0600 权限）；
   若 401 时刷新冲突，会重新读盘拿桌面端可能已重同步的新 token。
   **不要在桥外手动调 refresh 而不落盘**——会把轮换链烧断（本次排查时烧过一次，靠重启桌面 app 自动重同步恢复）。
2. 桌面 app 启动时会**删除并重写** `~/.box-agent/config/auth.json`；桥不缓存 token，每次请求读盘。
3. 模型多为推理模型：`max_tokens` 给太小（如 64）会全耗在 reasoning 上、content 为空、finish_reason=length；
   验证时给 1024。
4. `ocx provider add xhx` 后需手动给 `~/.opencodex/config.json` 的 xhx 条目加 `"allowPrivateNetwork": true`；
   新 provider 必须 `ocx restart` + `ocx models provider xhx on` + `ocx sync`。
5. 官方 CLI 对 cc-switch 目录schema 要求严（supported_reasoning_levels / shell_type 等），
   inject_catalog 后必须跑一次 `ocx sync` 归一化，否则 CLI 解析失败。
6. Codex 目录里最终是 ocx 的双前缀 slug（`xhx/xhx-*`），桥的 remap 循环剥前缀所以兼容。

## 运维命令
```bash
launchctl kickstart -k gui/$(id -u)/com.local.xhx2codex        # 重启桥
curl -s http://127.0.0.1:8793/health                            # 健康检查
~/xhx2codex/finish_setup.sh                           # 一键收尾（含验证）
codex exec -c model_provider=xhx -m "xhx/xhx-sn-glm-5-3-flash" "..."   # 官方 CLI 使用
```

## 未动/警告
- 商汤小浣熊桌面 app 当前在后台运行（保持 token 自动同步；若桥偶发 401 可重开 app）
- workbuddy（8787/8788）、qoder（8789）、codely（8790）、trae（8791）、lingxi（8792）桥保持运行

## 计费真相（2026-09-29 实测）：这条链路不扣积分

**结论**：`llm/v2/chat/completions` 返回真实 `usage`，但**不清算积分**。

实测（都打真实上游，不是罐头回）：

| 测试 | 模型（计费倍率） | tokens | 积分变化 |
|---|---|---|---|
| 直连上游 | raccoon-19b265（×1） | 117 | Δ 0 |
| 直连上游 | raccoon-405a1c（×1） | 520 | Δ 0 |
| 经桥调用 | raccoon-405a1c（×1） | 528 | Δ 0（3 分钟、5.5 分钟后复查仍是 9283/976/8307） |
| 连打 3 次 | raccoon-405a1c（×1） | ~520×3 | Δ 0，5.5 分钟后仍 0 |

`GET /api/web/points/v1/balance` 的五个字段（`available_points / daily_points /
monthly_points / reward_points / topup_points`）一个都没动。

**为什么**：`model_catalog` 带每模型计费信息，平台是有计费体系的：

| 模型 | billing_multiplier | 有效倍率 | 状态 |
|---|---|---|---|
| raccoon-8c4485 / 19b265 / 405a1c | 1 | 1 | normal（全价） |
| sn-sensenova-6-8-flash / -lite | 0.5 | **0** | limited_free，限免至 10/31 |
| sn-glm-5-3-flash | 0.2 | 0.1 | discount，限时折扣 9/30 止 |
| sn-glm-5-3 | 0.75 | 0.75 | normal |
| sn-deepseek-v4-1-flash | 0.25 | 0.25 | normal |
| sn-kimi-k3 | 1 | 1 | normal |

但桥打的是 `/api/web/llm/v2/chat/completions`，官方桌面端聊天走的是**另一套 API 面**
（`/api/web/office/v3/sessions` → `.../external/messages`，端点从
`/Applications/商汤小浣熊.app/Contents/Resources/app.asar` 扒到），积分在 office/v3 那条链路上结。
`~/.box-agent/config/auth.json` 本身就是桌面端 agent runtime 的登录态，所以这不是伪造支付状态，
只是不在计量面上——上游任何时候可以改。

**余额自己在掉**：实测约 0.5 分/分钟（≈700 分/天），来源是桌面端自身消耗（它启动时领每日积分）。
每日发放约 1000 分，`checkin.py` 的 `xhx` 任务负责补签。

## 本地用量账本（积分不动，账要清）

桥每次调用往 `<FLEET_HOME>/xhx-usage.jsonl` 追加一行（`XHX_USAGE_FILE` 可覆盖）：

```json
{"ts":"2026-09-29T21:00:00","model":"raccoon-405a1c","stream":false,"secs":8.0,
 "prompt_tokens":20,"completion_tokens":500,"total_tokens":520,"reasoning_tokens":0}
```

- 流式调用只有在客户端带 `stream_options.include_usage` 时上游才回 usage；没回也计数，token 记 0
- 文件超过 2MB 自动裁到 newest-fit（`usage_ledger.MAX_BYTES` / `KEEP_LINES`），写失败不影响桥
- 状态面板「小浣熊用量」一栏按模型汇总今日次数/token，`status_ui.py --once` 可直接看 JSON
- 计数包含**所有**经过桥的调用：fleet_probe / verify_real_calls 的探测调用也算，所以数字比纯 Codex 用量略高
- 想让 Codex 用量真的走积分，得把桥改造成 office/v3 会话流程；目前刻意没做（见下）

## 渠道校验失败要能被发现

上游拒绝可以**用 HTTP 200 装着错误文本**回来（workbuddy 的 `11128 unapproved channel`
就是这样，读起来像一句回答）。`tools/upstream_errors.py` 收了这类签名，
`fleet_probe.py`（可达性 → 选择器排序）和 `verify_real_calls.py`（REAL 判定 → catalog_filter
能否隐藏该 provider）都先过它：

- 探测侧：命中的调用判不可达，理由是 `upstream refused: <marker>`
- 核验侧：verdict 记 `CHANNEL_BLOCKED`（状态面板红色），不会被算成"弱模型"的 UNCLEAR

新增签名往 `MARKERS` 里加，并补 `tools/test_upstream_errors.py` 的用例。

- 流式 500 / 渠道校验（11128）死循环 / Trae 登录态找不到：见 [2026-09-29-stream-500-and-channel-retry.md](./2026-09-29-stream-500-and-channel-retry.md)
