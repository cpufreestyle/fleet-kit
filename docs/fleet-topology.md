# FleetKit 舰队拓扑

> 由 `kit/docs/fleet-topology.json` 生成（`python3 tools/topology.py all`）。
> 改拓扑改 JSON，不要手改本文件。实测：2026-10-07 00:16:37

```mermaid
flowchart LR
  subgraph client["① 客户端"]
    codex["Codex CLI / 桌面"]
    claude["Claude Code"]
    agent["其他 agent 客户端"]
    zcode["ZCode（goal 模式）"]
  end
  subgraph entry["② 本地入口"]
    ocx["opencodex 反代理 :10100"]
    ccswitch["CC Switch :15721"]
    gw8801["FleetKit Anthropic 网关 :8801"]
    freellm["FreeLLMAPI :3001"]
    shim["StepFun 图片 shim :15722"]
    ui["状态面板 :8796"]
  end
  subgraph tier["③ 档位 / 产能"]
    tier0["档 0 · StepFun 月配额"]
    tier1["档 1 · 订阅积分桥"]
    tier2["档 2 · OpenRouter 免费档"]
  end
  subgraph upstream["④ 上游"]
    stepfun["api.stepfun.com/step_plan/v1"]
    subs["各家客户端订阅积分"]
    or["openrouter.ai :free"]
    tokend["tokendance（目录里占比最大）"]
    hub["Cline hub daemon（:25463 WS）"]
  end
  br8787["8787 workbuddy"]
  br8788["8788 workbuddy-gpt"]
  br8789["8789 qoder"]
  br8790["8790 codely"]
  br8791["8791 trae"]
  br8792["8792 lingxi"]
  br8793["8793 xhx"]
  br8794["8794 gemini"]
  br8795["8795 catpaw"]
  br8797["8797 antigravity"]
  br8798["8798 qwen"]
  br8799["8799 cline"]
  br8800["8800 zcode"]
  br8802["8802 kimi"]
  br8803["8803 minimax"]
  br8805["8805 doubao"]
  codex -->|直连 :10100（CC Switch 只写配置，不承载流式请求，实测 2026-10-07）| ocx
  zcode -->|短别名 qdr/qwen3.8-fl → qoder/Qwen3.8-Flash（同样直连，不走 CC Switch）| ocx
  claude -->|ANTHROPIC_BASE_URL=:15721，PROXY_MANAGED token| ccswitch
  ccswitch -->|CC Switch claude 当前项 = FleetKit(8801)| gw8801
  agent -->|统一密钥 + 端点 :3001/v1| freellm
  shim -->|带 / 的舰队 slug → :10100（剥掉 Authorization）| ocx
  shim -->|step* 原生 id 照旧| stepfun
  ocx -->|defaultProvider=stepfun| tier0
  ocx -->|按 slug 前缀路由到各桥| tier1
  freellm -.->|priority 1-4 月配额优先| tier0
  freellm -.->|priority 5-185 订阅积分| tier1
  freellm -.->|priority 188-199 兜底| tier2
  gw8801 -->|step* 直连官方月配额| tier0
  gw8801 -->|有桥的 provider 走桥| tier1
  gw8801 -.->|其余回落 opencodex| ocx
  tier0 --> stepfun
  tier1 --> subs
  tier1 -->|tokendance 98 模型（401，不可用）| tokend
  tier1 -->|cline 免费池经 hub daemon| hub
  tier2 -.-> or
  ui -->|观测：/api/status| tier1
  tier1 --- br8787
```

## 入口端口（实测在听）

| 组件 | 端口 | 状态 | 模型 |
|---|---|---|---|
| opencodex 反代理 :10100 | 10100 | 在听 | 242 |
| CC Switch :15721 | 15721 | 在听 | — |
| FleetKit Anthropic 网关 :8801 | 8801 | 在听 | 283 |
| FreeLLMAPI :3001 | 3001 | 在听 | — |
| StepFun 图片 shim :15722 | 15722 | 在听 | — |
| 状态面板 :8796 | 8796 | 在听 | — |

## 订阅积分桥

| 端口 | 桥 | 来源 | 状态 | 免费/限额 | 在听 |
|---|---|---|---|---|---|
| 8787 | workbuddy | WorkBuddy 国内版 | 通 | LIMITED 免费档 | 是 |
| 8788 | workbuddy-gpt | 海外版 | 通 | — | 是 |
| 8789 | qoder | Qoder CN | 通 | TRIAL 2 周 / 已补 tools 支持 | 是 |
| 8790 | codely | 团结 AI | 不通 | QUOTA 月度点数 | 是 |
| 8791 | trae | Trae CN | 通 | limit 限速 | 是 |
| 8792 | lingxi | 灵犀 | 通 | QUOTA 灵力 | 是 |
| 8793 | xhx | 商汤小浣熊 | 通 | — | 是 |
| 8794 | gemini | Google Gemini | 不通 | — | 是 |
| 8795 | catpaw | CatPawAI | 不通 | — | 是 |
| 8797 | antigravity | Google Antigravity | 不通 | — | 是 |
| 8798 | qwen | 阿里 Qwen | 不通 | — | 是 |
| 8799 | cline | Cline 免费池 | 通 | 12× FREE | 是 |
| 8800 | zcode | ZCode / 阿里云 | 不通 | — | 是 |
| 8802 | kimi | Kimi Code | 未测 | — | 是 |
| 8803 | minimax | MiniMax | 未测 | — | 是 |
| 8805 | doubao | 豆包 seed-main | 未测 | — | 是 |

## fleet_probe 快照（2026-10-06T23:40:00+08:00）

- 可达：codely, lingxi, qoder, stepfun, trae
- 不可达：antigravity, catpaw, cline, doubao, gemini, kimi-code, minimax, qwen, tokendance, workbuddy, workbuddy-gpt, xhx
