# lingxi2codex Runbook — 灵犀（LingXi）官方 SaaS 反代理到 Codex

日期：2026-09-25 | 状态：**全链路已通**（桥 / ocx / 官方 CLI 三处实测 200）

## 架构（实测）
```
Codex 官方 CLI/桌面 → ocx(:10100) → lingxi2codex 桥(:8792) → https://lingxi.regaing.com/api/v1
                          ↑
              [model_providers.lingxi] config.toml (wire_api=responses) 用于官方 CLI 直连桥
```
- 桥：`~/lingxi2codex/lingxi_bridge.py`（OpenAI 兼容透传 + auth 刷新）
- LaunchAgent：`~/Library/LaunchAgents/com.local.lingxi2codex.plist`（KeepAlive，日志 `/tmp/lingxi-bridge.log`）
- 密钥：`LINGXI2CODEX_KEY`（`~/.zshrc`）
- 登录助手：`~/lingxi2codex/login_helper.py`（已用过并成功；一次性 LaunchAgent `com.local.lingxi2codex-login` 已完成使命，可删）
- ocx provider：`lingxi`（openai-chat，base `http://127.0.0.1:8792/v1`，`allowPrivateNetwork:true`）
- 官方 CLI：`~/.codex/config.toml` `[model_providers.lingxi]` → `http://127.0.0.1:10100/v1` wire_api=responses
- Codex 目录：`lingxi/lingxi-deepseek-flash`、`lingxi/lingxi-glm-5.3-flash`（ocx 归一化双前缀 slug；桥循环剥前缀）

## 学习来源（开源项目 @lingxi-agent/core@0.9.6）
- npm 包 dist/index.cjs（4276 行级 bundle）逆PLES出的契约：
  - 登录态 `~/.LingXi/auth.json`：`{token, refresh, name, baseURL}`（CLI `/login` 浏览器流程：
    开 `https://lingxi.regaing.com/login?client=cli&port=8062`，回调 `127.0.0.1:8062/?token=...&refresh=...&name=...`）
  - API 基座 `{baseURL}/api/v1`（默认 `https://lingxi.regaing.com/api/v1`），OpenAI 兼容
  - 鉴权 `Authorization: Bearer <token>`；探活 `GET /users/me`
  - 刷新 `POST /auth/refresh {refresh_token}` → `{access_token, refresh_token}`（桥 401 时自动刷新重试）
  - 模型：`GET /models`（订阅动态下发）；官方默认 deepseek-v4-flash/pro
- CLI 也装了：`npm i -g @lingxi-agent/core`（`lingxi` REPL，交互式；/login /plan /tenant）

## 订阅实测可用模型（登录后 /models 返回）
- `deepseek-flash`（默认快模型）、`glm-5.3-flash`、`gpt-image-2.5-sunburst`（生图，未入 Codex 目录）
- 文档里的"官方默认" `deepseek-v4-flash` / `deepseek-v4-pro` 在当前账号套餐**之外**：
  2026-09-29 实测 403 `model not allowed on your current plan`。它们是桥的 `FALLBACK_MODELS`
  （上游 /models 拉不到时的兜底），所以兜底期间下发的两个 id 也打不通，运维时以
  `deepseek-flash` / `glm-5.3-flash` 为准。
- 注意：模型是**推理模型**，max_output_tokens 太小（48）会全耗在 reasoning、正文为空；
  Codex 侧用 1024 正常返回。

## 排坑记录
1. 前序模型据 `ExchangeToken` 的 `TokenExpireAt` 为过去时间误判"账号会话已死"——实际 token 可用，
   401 是瞬时的；判活要看桥 `/health` 的 `session_alive`（真打 get_detail_param）。
2. ocx 会把桥返回的带前缀 id 再加一层 provider 前缀 → 双前缀 slug（`lingxi/lingxi-*`）；
   桥 `remap_model` 已改循环剥前缀，两种 slug 形态都能路由。
3. 官方 CLI 对 cc-switch 目录 schema 要求严，inject 后需 `ocx sync` 归一化。
4. 登录 LaunchAgent（一次性）已完成登录，plist 可删：`launchctl bootout gui/$(id -u)/com.local.lingxi2codex-login`。
5. **403 不等于登录过期**。2026-09-29 实测：声明模型 `deepseek-v4-flash` 打过去 0.09s 返回
   `403 {"error":"model not allowed on your current plan: deepseek-v4-flash"}`——这是**模型级套餐限制**，
   账号会话是好的（`/health` 的 `session_alive=true`，`deepseek-flash` 200 0.97s 运算题答对）。
   核验工具 `verify_real_calls.py` 原来把 401/403 合并成同一个 AUTH_EXPIRED 并立即返回，
   于是整个桥被误判、模型行被目录过滤掉。现已拆出 `PLAN_BLOCKED`（403 且 body 带 plan/quota/
   permission 措辞）并继续试下一个候选模型；见到 `PLAN_BLOCKED` 就换
   `lingxi/deepseek-flash` 或 `lingxi/glm-5.3-flash`，不必重新登录。
6. `gpt-image-2.5-sunburst` 是生图模型，`/chat/completions` 必然 502（`invalid upstream url`）。
   上游 `/models` 不带能力字段，所以桥侧按命名关键词过滤
   （`_common.drop_image_models`），不再当对话模型下发；若整个列表都命中关键词则原样返回
   ——宁可留一个坏行，也不隐藏整张表。

## 运维命令
```bash
launchctl kickstart -k gui/$(id -u)/com.local.lingxi2codex      # 重启桥
curl -s http://127.0.0.1:8792/health                             # 健康检查
~/lingxi2codex/login_helper.py                         # 重新登录（浏览器）
codex exec -c model_provider=lingxi -m "lingxi/lingxi-deepseek-flash" "..."   # 官方 CLI
```
