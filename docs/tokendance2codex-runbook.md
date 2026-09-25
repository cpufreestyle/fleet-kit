# TokenDance（tokendance.space）接入 Codex runbook

## 结论

- provider 已注册、95 个模型已进 Codex 目录（含 `tokendance/step-5-preview`），路由链路全通。
- **当前 key 无效**：TokenDance 上游返回 `401 API 密钥不存在`（与随机假 key 报错一致，判定为 key 被删/抄漏）。需用户在 https://tokendance.space/keys 重新创建 key 后更新。

## 已完成的操作

1. `ocx provider add tokendance --adapter openai-chat --base-url https://tokendance.space/gateway/v1 --api-key <KEY>`
   - 注意 base_url 是 `/gateway/v1`，不是 `/v1`（`/v1` 是 SPA 首页）。
2. `~/.opencodex/config.json` 的 tokendance 条目补 `allowPrivateNetwork: true`
   - 原因：域名经 Clash fake-ip 解析到 `198.18.0.40`，ocx 目的地址策略默认拦截非全局地址的模型发现。
3. `ocx service restart`（自定义 provider 不会热加载，必须重启代理）
4. `ocx models provider tokendance on`（模型默认 disabled，需显式开）
5. `ocx sync` → 174 models 注入 `~/.codex/cc-switch-model-catalog.json`
6. `~/.codex/config.toml` 追加 `[model_providers.tokendance]` → `http://127.0.0.1:10100/v1`（wire_api=responses）

## key 失效后的更新方法

```bash
ocx provider add tokendance --adapter openai-chat \
  --base-url https://tokendance.space/gateway/v1 --api-key <新KEY>
ocx service restart
```

（`ocx provider add` 对已存在同名 provider 是覆盖写；`ocx provider edit` 对 custom provider 报 unknown provider，勿用。）

## 验证命令

```bash
curl -s http://127.0.0.1:10100/v1/models | grep step-5
```

## 模型速查（95 个，节选）

step-5-preview（1M ctx，支持 openai:chat-completions / anthropic:messages）、step-3.7-flash、glm-5.3、minimax-m3、deepseek-v4-pro、kimi-k3、qwen3.8-max、hy4-preview、longcat-2.0 等。完整清单：`ocx models live --provider tokendance`。

## 2026-09-26 01:20 更新：模型选项已加入，key 仍失效
- 用户给了 key 01M053GJE05H2S3K8Y9JH8329S（md5 7302f2aa…）。上游 /v1/chat/completions 返回 401「API 密钥不存在」（00:55、01:20 两次复测一致；/v1/models 公开无鉴权，不能用来判断 key）。
- 已把 step-5-preview 选入模型选项：
  - ocx models selected tokendance --set step-5-preview → selected=['step-5-preview']，catalogRefresh committed
  - ocx sync → 72 models 注入 ~/.codex/cc-switch-model-catalog.json（含 tokendance/step-5-preview）
  - ~/.codex/config.toml [model_providers.tokendance] → http://127.0.0.1:10100/v1（wire_api=responses）
  - 代理实测：curl 127.0.0.1:10100/v1/chat/completions model=tokendance/step-5-preview → Provider error 401（路由通，key 死）
- key 换新后：ocx provider add tokendance --adapter openai-chat --base-url https://tokendance.space/gateway/v1 --api-key <新KEY> && ocx service restart（无需重新选模型）
