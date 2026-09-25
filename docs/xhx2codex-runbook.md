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
