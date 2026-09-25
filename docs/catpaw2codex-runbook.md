# CatPawAI (美团妙手) 反代理 Runbook

结论: 可以反代理, 模式与 lingxi/trae/wbb 相同 (HTTP API + 固定 auth header)。
两个前提: 1) 连美团内网/公司 VPN (模型服务在内网域); 2) 抓到聊天端点 (在远端 webview, 本地只有 conversation 管理)。

## CatPawAI 是什么

- macOS app: /Applications/CatPawAI.app, bundle com.catpaw.ide v1.101.0 (catpawVersion 2026.9.2)
- VS Code fork (Electron), 内置 catpaw 系列扩展; 产品页标题: 妙手 - 全场景 AI Agent 平台
- 本地账号: 13661621468 (userInfoId 100000048981, loginMethod legacy, scope sankuai)

## 域名地图 (2026-09-25 取证)

| 域 | 归属 | 当前可达 |
|---|---|---|
| catpaw.meituan.com | 公网 (升级/登录/tenant) | 200 (API 活) |
| catpaw.sankuai.com | 内网 AI 服务 | NXDOMAIN (公网 DNS) |
| copilot.sankuai.com | 内网补全服务 | NXDOMAIN |
| idekit.sankuai.com | 内网 IDE kit | NXDOMAIN |
| ssosv.sankuai.com/sson/auth/oidc/v1/token | 内网 SSO OIDC | NXDOMAIN |

## 认证链 (已确认)

- session 位置: ~/Library/Application Support/CatPawAI/User/globalStorage/state.vscdb
  key = catpaw.mt-authentication (JSON: accessToken + refreshToken)
- API 头: Catpaw-Auth: <accessToken> (来自扩展源码 _getCustomHeader)
- 续期: POST {base}/api/login/refreshToken, Catpaw-Auth: <refreshToken>
- 已知 API (catpaw core 扩展): /api/login/{accessToken,refreshToken,userInfo,qrcode,mobile,...}
  /api/tenant/config, /api/agent/conversation/{list,detail,delete}
- 当前状态: 公网侧 userInfo/refresh 均 401 auth failed (session 需内网/重新登录刷新)

## 聊天端点 (待抓)

- bg-agent 扩展只含 conversation 管理; 发消息/SSE 在 pod-process.html 加载的远端 UI 里
- 抓法 (连内网后): CatPawAI 里发一条消息, DevTools -> Network 找 catpaw.sankuai.com 的
  application/x-chat 或 SSE 请求; 或 catpaw.bg-agent showInBrowserWindow 打开的 webview F12
- 抓到后: export CATPAW_CHAT_PATH=/api/xxx 重启桥

## 桥

- 代码: ~/catpaw2codex/catpaw_bridge.py (8795 端口, 已编译)
- 已内置: state.vscdb token 读取, 双 base 自动刷新, Catpaw-Auth 头, OpenAI 兼容入口
- 环境变量: CATPAW_CHAT_PATH (端点), CATPAW_MODELS (默认 catpaw-agent,catpaw-pro,catpaw-lite), CATPAW_BASE
- 冒烟已过: /health 报 auth-error(符合现状), /v1/models 200, chat 501 TODO-capture

## 上线步骤 (内网就绪后)

1. 连公司 VPN, IDE 重新登录 (state.vscdb 刷新 session)
2. IDE 发消息 + DevTools 抓 chat 端点 -> 填 CATPAW_CHAT_PATH
3. python3 catpaw_bridge.py, curl 验证 non-stream + stream
4. ocx provider add catpaw --adapter openai-chat --base-url http://127.0.0.1:8795/v1 --api-key sk-local-catpaw --allow-private-network && ocx sync
5. 模型 catalog 条目确认, Codex 内 -m catpaw/catpaw-agent 直连

## 2026-09-26 01:00 更新：公网 host 探测与回滚说明
- 系统代理 127.0.0.1:1082（MacPacket）对 sankuai.com CONNECT 503 → 桥 502（URLerror Tunnel connect）。
- catpaw.sankuai.com 无公网 A 记录（CNAME inf.vip.sankuai.com 纯内网）。
- 公网 catpaw.meituan.com 有同一套 API（/api/agent/maas/model-types、/api/gpt/chat/completions），但 IDE bundle 证实只对 External 租户用公网 host；内部 SSO token 在公网网关报 passport auth failed / ssoid 不存在。
- 曾改 BASE 打公网：静态 models 能出、token ok 但 maas 401 → 已回滚（cp .bak-20260926 还原并重启，否则会破坏 VPN 时的可用性）。
- 结论：必须连美团 VPN。修复路径：连 VPN 后 launchctl kickstart -k gui/$(id -u)/com.local.catpaw2codex。
