# WorkBuddy 海外版 GPT 模型反代理接入 Codex — Runbook

日期：2026-09-25 · 状态：已验证可用（Codex UI 侧需重启桌面 App）
维护代码：`~/wbb-global/converter.py`（原件备份 `converter.py.bak-prepin`）

## 链路

```
Codex（桌面/CLI）
  └─ provider workbuddy-hy4  base_url=http://127.0.0.1:10100/v1  wire_api=responses
       └─ OpenCodex (ocx)  :10100   provider workbuddy-gpt → http://127.0.0.1:8788/v1
            └─ 全球桥 wbb-global :8788  (BACKEND=https://www.workbuddy.ai)
                 └─ 43.160.158.125:443（DoH 固定 IP，直连，TLS SNI=www.workbuddy.ai）
                      └─ gpt-5.3-codex / gpt-6-astra / gpt-5.6-{sol,terra,luna} / ...
```

国内链路（独立进程，未改动）：`workbuddy` provider → `:8787` → copilot.tencent.com → hy4-preview 等。

## 组件与路径

| 组件 | 路径 / 标识 |
| --- | --- |
| 全球桥代码 | `~/wbb-global/`（converter.py / account_pool.py / dashboard.py …） |
| 全球桥 LaunchAgent | `~/Library/LaunchAgents/com.local.workbuddy2codex-gpt.plist`（KeepAlive=true） |
| 全球桥日志 | `/tmp/wbb-global-launchd.log` |
| 国际账号凭据 | `~/wbb-global/auths/workbuddy-<账号hash>.json`（<你的 WorkBuddy 账号>） |
| ocx 配置 | `~/.opencodex/config.json`（providers: workbuddy→8787、workbuddy-gpt→8788） |
| Codex 配置 | `~/.codex/config.toml`（`[model_providers.workbuddy-hy4]`） |
| 模型目录 | `~/.codex/cc-switch-model-catalog.json`（34 项，含 14 个 `workbuddy-gpt/*`） |

Bridge API Key：`grep CODEBUDDY2OPENAI_KEY ~/.zshrc`（44 位，与 plist 内一致）。

## 打过的 4 个补丁（converter.py）

1. **DNS 固定（fake-ip → 真实 IP）**：MacPacket TUN 把 `www.workbuddy.ai` 解析成 `198.18.x`（不可达）。用腾讯 DoH 解析真实 IP `43.160.158.125`，在 `socket.getaddrinfo` 层钉死；每 600s 刷新，网络错误时立即后台刷新；静态兜底列表 + TCP 443 探活后才生效。
   - 证据：系统 DNS `198.18.0.120`；DoH `43.160.158.125`；TLS 证书 `CN=workbuddy.ai / Tencent (Shenzhen)`。
2. **禁用 macOS 系统代理**：`urllib.request.getproxies()` 返回 `http://127.0.0.1:1082`，httpx（`trust_env=True`）会走它对海外后端返回 503。
   - 证据：`httpx.ProxyError: 503 Service Unavailable` → 强制所有 `httpx.Client/AsyncClient` `trust_env=False`，DoH 查询改用无代理 opener。
3. **uvicorn 强制 asyncio 事件循环**：uvloop 的 TLS 握手被海外上游直接 EOF。
   - 证据：uvloop 下 `httpx.ConnectError: ''`（3/3 失败），asyncio 下 3/3 成功；故 `uvicorn.run(..., loop="asyncio")`。
   - 注意：`httpx AsyncClient(timeout=…)` 任何客户端都受影响，换 loop 是根因修复。
4. **补 system prompt**：海外后端安全策略要求首条消息是 system，否则 `400 {"code":11128,"msg":"first message is not system prompt"}`；客户端以 user 开头时自动注入占位 system。

## 冒烟结果（2026-09-25 20:48–20:59，全部 14 个海外模型）

| 模型 | 结果 |
| --- | --- |
| gpt-5.3-codex | 200（PONG-OK / FINAL-OK） |
| gpt-6-astra | 200 |
| gpt-5.6-sol / terra / luna | 200（各自回显） |
| gpt-5.5 / gpt-5.4 | 200 |
| hy4-preview / hy3 | 200 |
| glm-5.3 / glm-5.2 | 200 |
| kimi-k3 / kimi-k2.6 | 200 |
| gemini-3.5-flash | 200（曾瞬时 429 限流，60s 后恢复） |

端到端（ocx `/v1/responses`）：`workbuddy-gpt/gpt-5.3-codex` → 200 `FINAL-OK`；国内 `workbuddy/hy4-preview` → 200 `CN-FINAL-OK`（无回归）。
`ocx observe logs` 显示修复前请求被错误路由成 `workbuddy/workbuddy-gpt/…`（打到国内桥），修复后正确为 `workbuddy-gpt/…`。

## 常用运维命令

```bash
# 桥健康（比 wbb status 可靠）
curl -s -H "Authorization: Bearer $(grep CODEBUDDY2OPENAI_KEY ~/.zshrc | head -1 | sed -E 's/.*="([^"]+)".*/\1/')" http://127.0.0.1:8788/health

# 重启全球桥（KeepAlive=true，崩溃会自动拉起）
launchctl kickstart -k gui/$(id -u)/com.local.workbuddy2codex-gpt

# 直连桥自测
curl -s -X POST http://127.0.0.1:8788/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.3-codex","messages":[{"role":"user","content":"Reply with exactly: PONG-OK"}],"max_tokens":50}'

# 经 ocx 自测（Codex 实际走的路径）
curl -s -X POST http://127.0.0.1:10100/v1/responses -H 'Content-Type: application/json' \
  -d '{"model":"workbuddy-gpt/gpt-5.3-codex","input":"hi","stream":false}'

# 看 ocx 路由与状态
ocx observe logs | tail -20
ocx provider list --json
```

## 注意事项

- **上游 429 会连锁冷却账号 60s**：某个模型触发限流后，短时间内其它请求会拿到 `503 账号池冷却`。测试时逐个做、失败后等 60s。
- **真实 IP 可能轮换**：TTL 60s；DoH 刷新兜底 + 静态 IP 兜底都在代码里，通常无需人工干预。
- **Codex 桌面 App 选择方式**：当前 app-server（PID 5211）启动于 20:25:57，晚于 provider/目录最后一次写入（20:25:50），理论上已加载 `workbuddy-gpt/*`；若模型选择器未显示，则完全退出并重开桌面 App。使用时 Provider 选 `workbuddy-hy4`，模型选 `workbuddy-gpt/*`（如 `workbuddy-gpt/gpt-5.3-codex`）。
- `cc-switch` 若重写 `cc-switch-model-catalog.json`，14 个 `workbuddy-gpt/*` 条目需重新注入。

## 2026-09-25 21:03 追加：Codex 官方 CLI 直连验证

```bash
/Applications/ChatGPT.app/Contents/Resources/codex exec --skip-git-repo-check \
  -c model_provider=workbuddy-hy4 -c model_reasoning_effort=low \
  -m "workbuddy-gpt/gpt-5.3-codex" "Reply with exactly: CLI-OK"
# → codex: CLI-OK   （codex-cli 0.155.0-alpha.16，config.toml 里的 workbuddy-hy4 provider）
```

说明：该测试不依赖 curl/ocx 手工调用，而是 Codex 自己的客户端实现按 `[model_providers.workbuddy-hy4]` 发起请求，等价于桌面 App 的选择效果；剩余人工步骤仅有「完全退出并重开桌面 App」刷新模型选择器。
