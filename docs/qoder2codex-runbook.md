# Qoder（CN）模型反代理接入 Codex — Runbook

日期：2026-09-25 · 状态：桥已就绪，等待 `qoderclicn login` 授权
思路：复用官方 `qoderclicn` CLI（@qodercn-ai/qoderclicn）的非交互模式驱动订阅模型，
      本地桥把它包装成 OpenAI 兼容 API，再挂到已有 OpenCodex(ocx) → Codex 链路。

## 链路

```
Codex  ─ provider qoder-hy4 → 127.0.0.1:10100/v1 (ocx, wire_api=responses)
       └ ocx provider qoder (adapter openai-chat) → 127.0.0.1:8789/v1
            └ qoder2codex 本地桥（本目录 Python FastAPI）
                 └ qoderclicn -p --output-format json|stream-json  （官方 CLI，自带登录态）
                      └ gateway.qoder.com.cn → 订阅模型（auto / performance / lite …）
```

## 组件

| 组件 | 路径 / 标识 |
| --- | --- |
| 桥代码 | `~/qoder-bridge/qoder_bridge.py` |
| 目录注入脚本 | `~/qoder-bridge/inject_catalog.py` |
| 桥 LaunchAgent | `~/Library/LaunchAgents/com.local.qoder2codex.plist`（8789，KeepAlive） |
| 桥日志 | `/tmp/qoder-bridge.log` |
| CLI | `~/.local/node-v22.20.0-darwin-arm64/bin/qoderclicn` → `@qodercn-ai/qoderclicn` (1.1.63) |
| CLI 登录态 | `~/.qoder-cn/.auth/user`（CLI 自己管理，桥不存 token） |
| CLI 登录流程 | `~/Library/LaunchAgents/com.local.qoderclicn-login.plist` → `/tmp/qoder-login.log` |
| 桥 API Key | `~/.zshrc` 的 `QODER2CODEX_KEY` |

## 关键设计

- **不碰 IDE 凭据**：Qoder IDE 的登录态在 Electron `safeStorage` 加密的
  `secret://aicoding.auth.userInfo` 里（读不出来），CLI 也不共享它。所以走 CLI 自己的
  设备码登录（`qoderclicn login`，浏览器点一下），凭据由官方 CLI 保管。
- **纯文本补全**：请求带 `--tools "" --permission-mode bypassPermissions`，禁用内置工具，
  只取模型回答，避免 agent 行为与权限提示。
- **两种输出**：`--output-format json`（非流式，聚合）与 `stream-json`（流式，转 OpenAI SSE）。
- **模型发现**：`qoderclicn --list-models` 解析后缓存（5 分钟）， `/v1/models` 暴露。

## 运维命令

```bash
# 健康（含 CLI 登录态）
curl -s -H "Authorization: Bearer $(grep QODER2CODEX_KEY ~/.zshrc | head -1 | sed -E 's/.*="([^"]+)".*/\1/')" \
  http://127.0.0.1:8789/health

# 模型列表
qoderclicn --list-models

# 直连桥自测
curl -s -X POST http://127.0.0.1:8789/v1/chat/completions \
  -H "Authorization: Bearer $QODER2CODEX_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qoder/auto","messages":[{"role":"user","content":"Reply with exactly: QODER-OK"}]}'

# 注入 Codex 模型目录
QODER2CODEX_KEY=$QODER2CODEX_KEY python3 ~/qoder-bridge/inject_catalog.py

# 经 ocx 自测（Codex 实际走的路径）
curl -s -X POST http://127.0.0.1:10100/v1/responses -H 'Content-Type: application/json' \
  -d '{"model":"qoder/auto","input":"Reply with exactly: QODER-OK","stream":false}'
```

## 待办 / 注意事项

- 登录：`launchctl kickstart -k gui/$(id -u)/com.local.qoderclicn-login` 会打印设备码 URL，
  浏览器授权后 CLI 自动落盘 `~/.qoder-cn/.auth/user`。
- ocx 若在 provider 添加前就已启动，需 `ocx restart` 才能正确路由 `qoder/*`。
- 每次请求都是独立 CLI 进程（无会话连续性），Codex 侧会把完整历史一起发过来，因此无状态也可用。
- CLI 单次调用有耗时（秒级到几十秒），桥默认超时 300s。

## 2026-09-26 01:00 更新：MCP 卡死根因与修复
- 现象：流式 chat 正常（只等首事件），非流式 subprocess.run 等到 300s 超时。
- 根因：qoderclicn 每次调用都加载用户 MCP 配置（~/.qoder/mcp.json、~/.qoder-cn/mcp.json），卡在 MCP issues detected 后挂起。
- 修复：~/qoder-bridge/qoder_bridge.py base_args 增加 --strict-mcp-config（无 --mcp-config = 不加载任何 MCP server），CLI 6.7s 正常返回 JSON。备份 qoder_bridge.py.bak-20260926。launchctl kickstart -k gui/$(id -u)/com.local.qoder2codex 生效。修复后实测 chat 200 / 5.8s。
