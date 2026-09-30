# 2026-09-30 诊断记录：opencodex 仪表盘 Failed to fetch（Codex 内置浏览器侧封锁）

## 症状（用户报告）
- 网关仪表盘「更新 opencodex」对话框红色 Failed to fetch（频道 latest）。
- 附带现象：仪表盘看不到 fleet 模型，数据卡片异常。

## 结论
问题在 Codex 内置浏览器（进程名 ChatGPT.exe）的客户端网络/权限层；opencodex、网关、InfiniSynapse 桥接均正常，本次未做任何改动。

## 证据
1. 网关健康：bun pid 36408 在跑；/api/update/check?tag=latest 无凭证返回 401 JSON（服务端有应答），非挂死。
2. 干净浏览器全绿：无头 Chromium 打开 http://127.0.0.1:10100/v1，页面自身全部 API 200；页面上下文请求 update/check?tag=latest 返回 200 already_latest（2.73.0 已是最新）；控制台仅有 HTTP 401 探针错误，无 TypeError: Failed to fetch。仪表盘完整渲染（在线 / v2.73.0 / 14 提供方 / Token 948.8万）。
3. TCP 层实锤：无头 Chromium 对 10100 保持多条 ESTABLISHED 轮询连接；ChatGPT.exe 全部 13 个进程 18 秒内对 10100 零连接。内置浏览器里请求未离开浏览器即被拦截，fetch() 抛 TypeError，UI 才显示 Failed to fetch。
4. fleet 模型未丢：/api/models 含 fleet 条目，combo/fleetcore → codely-core 组合在线，usage.jsonl 流量持续；「看不到 fleet 模型」是同一封锁的症状，服务端侧一直正常。

## 处置
- 立即可用：Edge/Chrome 打开 http://127.0.0.1:10100/v1，功能完整，更新对话框显示已是最新。
- 恢复内置浏览器：完全退出并重启 Codex 应用。此前该内置浏览器已报 saved browser permissions could not be verified，权限存储损坏，重启应恢复。
- 本次诊断未改代码/配置、未重启网关，InfiniSynapse 桥接（7001）未受影响。

## 复现命令（存档）
- 打开干净浏览器会话：npx --yes --package @playwright/cli@latest playwright-cli -s=diag open http://127.0.0.1:10100/v1
- 列出页面请求状态：playwright-cli -s=diag requests
- 对比连接数：Get-NetTCPConnection -RemotePort 10100，按 OwningProcess 分组观察（正常浏览器有 ESTABLISHED 轮询连接，内置浏览器为零）。

## UI 错误路径备忘
opencodex 前端：fetch() 成功但响应非 2xx 时显示 HTTP <code>；显示 Failed to fetch 等价于 fetch() 本身抛 TypeError（浏览器网络层失败）。据此可快速区分服务端问题与浏览器侧问题。