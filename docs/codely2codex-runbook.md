# codely2codex Runbook — Tuanjie AI（团结 AI）反代理到 Codex

日期：2026-09-25 | 状态：**链路已通，唯一卡点 = Tuanjie 账号 LiteLLM 额度 402**

## 架构（实测）
```
Codex 桌面/CLI → ocx(:10100) → codely2codex 桥(:8790) → https://codely-litellm.tuanjie.cn/v1
                     ↑                                        （LiteLLM，OpenAI 兼容）
              [model_providers.codely] config.toml (wire_api=chat) 用于官方 CLI 直连桥
```
- 桥：`~/codely2codex/codely_bridge.py`（FastAPI，复用 workbuddy2codex venv Python）
- LaunchAgent：`~/Library/LaunchAgents/com.local.codely2codex.plist`（KeepAlive，日志 `/tmp/codely-bridge.log`）
- 密钥：`CODELY2CODEX_KEY`（`~/.zshrc`；读取：`grep CODELY2CODEX_KEY ~/.zshrc | head -1 | sed -E 's/.*="([^"]+)".*/\1/'`）
- ocx provider：`codely`（openai-chat，base `http://127.0.0.1:8790/v1`，`allowPrivateNetwork:true`）
- 官方 CLI：`~/.codex/config.toml` `[model_providers.codely]` → `http://127.0.0.1:10100/v1` wire_api=responses（官方 CLI 已删 chat wire，走 ocx 转换；新加 provider 必须 `ocx restart` 才进路由表）
- Codex 目录：8 个 `codely/<model>` slug 已注入 `~/.codex/cc-switch-model-catalog.json`

## 8 个模型（官方 CLI `--cmd "/model list"` + 网关 /models 实测）
`codely-core`(默认) `codely-flash` `codely-air` `codely-basic` `codely-vl` `DeepSeek-V4.1-Flash` `GLM-5.3-FLASH` `KIMI-K3`
（DeepSeek-V4.1-Flash 网关标注 max_model_len 1048576）

## 逆向结论（来自 @unity-china/codely-cli 1.0.0-rc.60 bundle/gemini.js）
- CLI 已装：`npm i -g @unity-china/codely-cli --registry https://registry.npmmirror.com`（v1.0.0-rc.60，bin `codely`）
- 登录态文件：`~/.codely-cli/oauth_creds.json`（`access_token`/`refresh_token`/`cli_api_key`/`user_id`）——桥与官方 CLI 共享
- 设备码登录：`POST https://codely.tuanjie.cn/auth/device/initiate` `{provider:"unity",client_name:"codely-cli"}` → `verification_uri_complete`；`GET /auth/device/poll?auth_request_token=`；`POST /auth/device/exchange {authorization_code}`
- 刷新：`POST https://codely.tuanjie.cn/auth/refresh {refresh_token}`
- 虚拟密钥：`GET https://codely.tuanjie.cn/api/api-token/cli-api-key`（Bearer: access_token）→ `{cli_api_key:"sk-...",user_id,rpm,tpm}`
- 网关签名（所有 chat/completions 必须，缺了 401"请升级 Codely"）：
  `sk_core = HMAC-SHA256(HMAC-SHA256(BASE, "codely-signing-v1"), cli_api_key)`
  `BASE = hex 406f00f74768ba0cb0cd30f097ec6c2bdacb89c61a38b7dd140838bbd0e98018`
  `X-Codely-Signature: v1.<unix_ts>.<base64url(HMAC-SHA256(sk_core, "v1\n/v1/chat/completions\n<ts>"))>`
- 桥内已实现：签名 + 401 自动 refresh/重取密钥重试 + 模型名剥 `codely/` 前缀 + SSE 流式透传

## 当前账号状态
- 登录态：本机已有（user_id <你的 user_id>，org `<你的组织>`，`~/.codely-cli/oauth_creds.json` 18:41 生成）
- 调用返回 **402 budget_exceeded**：Current cost 4772.46 / Max budget -1.0（LiteLLM 虚拟密钥预算，账号级，所有模型均 402）
- 恢复条件：Tuanjie AI Credits 充值/组织额度到账后即可直接用

## 运维命令
```bash
# 重启桥
launchctl kickstart -k gui/$(id -u)/com.local.codely2codex
# 一键收尾（含验证）
~/codely2codex/finish_setup.sh
# 重新登录（设备码）
curl -X POST -H "Authorization: Bearer $KEY" http://127.0.0.1:8790/auth/device/start
curl -H "Authorization: Bearer $KEY" "http://127.0.0.1:8790/auth/device/check?auth_request_token=..."
# 重新注入目录
CODELY2CODEX_KEY=$KEY <venv>/python ~/codely2codex/inject_catalog.py
```

## 排坑记录
1. `ocx provider add codely` 默认拒绝 loopback → 需在 `~/.opencodex/config.json` 的 provider 条目加 `"allowPrivateNetwork": true`；add 失败会回滚配置，备份在 `~/.opencodex/config.json.invalid-*`
2. 新 provider 加进 config.json 后**必须 `ocx restart`**（否则运行中代理路由表不认，会错误落到 workbuddy provider，且不报错）
3. `ocx models provider codely on` 是启用开关（之前被默认关闭）
4. 盘上 `cli_api_key` 可能是老 CLI 写入的非 `sk-` 值（网关 401 "LiteLLM Virtual Key expected"），桥已改为只在 `sk-` 前缀时复用缓存，否则实时重拉
5. 桥的 LaunchAgent 必须显式设 `HOME=$HOME`（凭据在 `~/.codely-cli`）
6. Codex 桌面 app-server 仍在跑时模型列表不刷新：`ocx sync --restart-codex`（会中断活跃回合，未执行）
7. 已顺手修复：`~/.codex/config.toml` 里 workbuddy-hy4/qoder-hy4 两节被粘成一个表（duplicate key 导致官方 CLI 无法加载），已拆回两个正常表

## 未动/警告
- WorkBuddy 桥（8787 CN + 8788 GPT）与 qoder 桥（8789）保持运行，未受影响
- cc-switch 原生 custom 路由与 15721 代理按现状运行

## 2026-09-26 01:00 更新：门禁文案 402→400，models 直连真实 200
- 上游网关 codely-litellm.tuanjie.cn /v1/chat/completions 对全部 8 个模型一律 400「欢迎使用Codely, 访问 https://codely.tuanjie.cn/」（此前是 402 budget_exceeded，门禁文案已变）。
- /v1/models 直连真实 200（8 模型，key 有效）——models 通不代表 chat 通。
- 账号 web API 正常：/api/teams → <你的团队> has_key:true；/api/user/usage/summary → 剩余 <你的点数>。
- 官方 CLI（@unity-china/codely-cli bundle/gemini.js）走同网关同签名（BASE key 406f00f7…）同样被挡 → 与桥无关，纯账号 onboarding 门禁。
- 修复路径：用户登录 codely.tuanjie.cn 网页端完成首次激活。
