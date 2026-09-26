# StepFun Plan API 接入 Codex runbook

## 结论

- provider 已注册：`stepfun`（adapter `openai-chat`，base `https://api.stepfun.com/step_plan/v1`），
  9 个 live 模型，5 个文本模型已进 Codex 选择器（catalog slug `stepfun/<model>`）。
- **默认模型已切到 `stepfun/step-5-preview`**（`~/.codex/config.toml` 的 `model` 键），
  端到端实测 200：Codex → custom provider（127.0.0.1:15721，CC Switch 网关，当前
  codex 渠道 = StepFun）→ StepFun 官方 API，返回真实 reasoning + 输出。
- 不占本地桥端口；trae 桥 8791 上游挂掉后经 CC Switch 出现的
  `503 所有供应商已熔断，无可用渠道` 随本次切换解决。

## 凭据来源

key 来自 `~/.cc-switch/config.json` → `codex.providers` 里 name `StepFun` 的条目
（官网 https://platform.stepfun.com/step-plan）。kit 侧环境变量名
`STEPFUN_PLAN_API_KEY`，写在 `runtime/fleet.env`（600，不入库）；
`opencodex/setup-providers.sh` 已含条件注册块，没有 key 时自动跳过。

## 两个坑（注册时必须）

1. **`--allow-private-network` 不能省**：本机代理工具把 `api.stepfun.com` 解析成
   fake-ip 非全局地址，ocx 目的地址策略默认拦截非全局地址的模型发现，
   `ocx provider add` 不加这个 flag 会发现不了模型。
2. **注册后要跑一次裸 `ocx models`**：list 命令会刷新 discovery 缓存；不刷的话
   `ocx models provider stepfun on` 报 `no models are available`
   （`ocx sync` 不写 knownModels，救不回来）。

## 已完成的操作（按序）

```bash
ocx provider add stepfun --adapter openai-chat \
  --base-url https://api.stepfun.com/step_plan/v1 \
  --api-key <KEY> --allow-private-network --force
ocx models                          # 刷 discovery 缓存（坑 2）
ocx models provider stepfun on
ocx models selected stepfun --set step-5-preview,step-3.7-flash,step-3.5-flash-2603,step-3.5-flash,step-router-v1
ocx sync
```

- `~/.codex/config.toml`：`model = "stepfun/step-5-preview"`（setup-providers.sh 每次 sync 后重新 pin）
- `~/.opencodex/config.json` 的 `providers.stepfun`：`liveModels: true`、
  `modelContextWindows: step-5-preview=1000000`、`reasoningEfforts: low/medium/high`
- CC Switch `~/.cc-switch/config.json` 的 `codex.current` 指向 StepFun 条目

## 验证命令

```bash
# 经 CC Switch 全链路（Codex 实际走的路）
curl -s http://127.0.0.1:15721/v1/responses \
  -H 'Authorization: Bearer PROXY_MANAGED' -H 'Content-Type: application/json' \
  -d '{"model":"stepfun/step-5-preview","input":"回复OK两个字","stream":false}'
# 模型清单（live）
ocx models live --provider stepfun
# catalog 里的条目（选择器数据源）
grep -o "stepfun/[a-z0-9.-]*" ~/.codex/cc-switch-model-catalog.json | sort -u
```

2026-09-26 实测：全链路 HTTP 200，返回真实 reasoning summary 与输出。
（`max_output_tokens` 给小了会返回 `status: incomplete`——推理先把额度吃光了，属正常。）

## 模型清单（9 个 live，5 个文本已选入选择器）

| 模型 | 上下文 | 输入模态 | 备注 |
|------|--------|----------|------|
| step-5-preview | 1M（记 1000000） | 文本+图像 | 默认模型；推理档 low/medium/high |
| step-3.7-flash | 256K | 文本+图像 | |
| step-3.5-flash | 256K | 文本 | 无视觉（ocx noVisionModels） |
| step-3.5-flash-2603 | — | 文本 | |
| step-router-v1 | — | 文本 | 路由器 |
| stepaudio-2.5-chat / -tts / -asr / -realtime | — | 音频 | live 可见，未选入选择器 |

注：Plan API 的 openai-chat / responses 双协议都可用，船队统一用 openai-chat。
上下文只填 ocx 明确记录的值，`—` 表示配置里没给。
