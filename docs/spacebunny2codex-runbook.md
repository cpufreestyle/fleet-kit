# Space Bunny Alpha 接入 Codex runbook

## 结论

- provider `spacebunny` 已注册，`spacebunny/space-bunny-alpha` 已进 Codex 模型选择器，路由链路全通。
- 上游为官方托管网关 `https://spacebunnymodel.com/api/v1`（OpenAI 兼容），模型本体是 OpenRouter 隐身模型 `stealth/space-bunny-alpha`（1M 上下文、多模态 text+image+video→text、reasoning mandatory）。
- **限时窗口：OpenRouter `expiration_date` 为 2026-10-05**，隐身预览到期后可能下架；本机 key 为注册赠 $10 额度，之后 $5 起按量。

## 已完成的操作

1. key 存入 `runtime/fleet.env`：`SPACEBUNNY_API_KEY=sb_live_...`（fleet.env 不进 git，CI 有 sk-/PII 扫描）。
2. `ocx provider add spacebunny --adapter openai-chat --base-url https://spacebunnymodel.com/api/v1 --api-key <KEY>`
   - 官方 base_url 即 `/api/v1`（OpenAI 兼容；`/api/v1/models` 不存在，返回 SPA HTML，见下面「自动发现 404」）。
3. `~/.opencodex/config.json` 的 spacebunny 条目补 `allowPrivateNetwork: true`
   - 原因：域名经 Clash fake-ip 解析到 `198.18.1.65`，ocx 目的地址策略默认拦截非全局地址。（备份：config.json.bak-before-spacebunny-discovery-*）
4. `ocx models add spacebunny space-bunny-alpha --display-name 'Space Bunny Alpha' --context-window 1000000 --modalities text,image --reasoning-efforts max,xhigh,high,medium,low --default-reasoning-effort high`
5. **手改 `~/.opencodex/config.json`**：`spacebunny.initialModelSelection.status: pending → ready`，`modelCount = 1`
   - 原因：上游无 `/models` 端点（/api/v1/models 返回 SPA HTML，发现永远 404），ocx 的初始模型发现永远完不成，只能手推状态。
6. `ocx service restart`（自定义 provider 不热加载，必须重启）
7. `ocx models provider spacebunny on`  （restart 之前执行会报 no models available，顺序不能反）
8. `ocx models selected spacebunny --set space-bunny-alpha`
9. `ocx sync`

`~/.codex/config.toml` **不需要改**：根 `openai_base_url=http://127.0.0.1:10100/v1` 全量路由，catalog（~/.codex/opencodex-catalog.json）即模型列表。

## 裸 curl 测上游必须 --http1.1

`curl` 默认走 HTTP/2 打 `https://spacebunnymodel.com/api/v1/chat/completions` 会被 Clerk 中间件吞掉 Authorization 头，返回 `401 Missing API key`（假 key / 空 Bearer 报错相同，证明头没到应用层）。

```bash
curl -s --http1.1 --noproxy '*' https://spacebunnymodel.com/api/v1/chat/completions \
  -H 'Authorization: Bearer sb_live_...' -H 'Content-Type: application/json' \
  -d '{"model":"space-bunny-alpha","messages":[{"role":"user","content":"hi"}]}'
```

代理链路（ocx / Node fetch 默认 h1）不受影响，真调用实测 200。

## 验证命令

```bash
# 经本地代理真调用（无需 auth）
curl -s http://127.0.0.1:10100/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"spacebunny/space-bunny-alpha","messages":[{"role":"user","content":"2+3=?"}]}'
# 模型列表/选择状态
ocx models live | grep spacebunny
grep spacebunny ~/.codex/opencodex-catalog.json
```

## kit 侧联动文件

- `kit/tools/catalog_sort.py`：DEFAULT_ORDER 尾部追加 `spacebunny`；PER_PROVIDER_IMPORTANT `"spacebunny": ("space-bunny-alpha",)`
- `kit/tools/fleet-reach.json`：reachable + evidence `space-bunny-alpha -> E2E_OK`
- `kit/free-windows.json`：providers / models 条目（free=trial、credits=own）
- runtime 与 kit 的对应副本由部署脚本同步（tools/ 与 free-windows.json、docs 带 .bak）

## key 失效/到期后的处理

- 本机 key 死在 `runtime/fleet.env` 的 `SPACEBUNNY_API_KEY`，更新后重跑上面 2→9。
- 若 OpenRouter 侧 2026-10-05 后下架隐身模型，本 provider 整体作废：`ocx models provider spacebunny off`，并在 kit 的 catalog_sort/fleet-reach/free-windows 中移除对应条目。
