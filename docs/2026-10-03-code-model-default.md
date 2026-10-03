# 维护模型定为 workbuddy/deepseek-v4-flash（2026-10-03）

## 诉求

"测试 FleetKit 机队里哪个模型适合维护 FleetKit 自己"——bridges、launchd glue、
"工具链都要能写能改。结论要能跑，不要感觉；胜出者要真正开放成可选、可默认。

## 现状盘点（当天实测）

`tools/code_model_bench.py live` 对 `/v1/models` 里全部 21 个模型各跑一遍
（smoke + 两道 FleetKit 真实代码题：`parse_window` 8 检查、
`convert_anthropic_to_openai` 7 检查，45s timeout）。评分器先跑
`offline` 自检：good 8/8 + 7/7、garbage NO_RUNNABLE，`OFFLINE_SELFTEST PASS`。

| 结论 | 模型 | 桥 |
| --- | --- | --- |
| 双题满分、prose 0 | `deepseek-v4-flash` / `deepseek-v4.1-flash` / `deepseek-v4-pro` | workbuddy |
| 双题满分但 36.8s（约 5 倍慢） | `DeepSeek-V4-Pro` | qoder |
| task1 8/8、task2 超时 | `trae/seed-code-pro-0430` | trae |
| 能聊、永远不给代码块 | xhx 三个模型、`codely-core` | xhx / codely |
| smoke 就挂 | `hy4-preview` 429、`gpt-5.3-codex` 503、`hy3` 503、`qwen3.8-max` 503、`GLM-5.3` 503、`kimi-for-coding` 503、`gemini-3-flash-preview` 502、cline 两个 502、`lingxi/deepseek-flash` 429、antigravity 502、catpaw 502 | 其余各桥 |

三个当晚复跑看不出来的发现：

1. **只有 workbuddy 的 deepseek 路线能可靠地写这个仓库。** 四个模型做完了两道题
   （workbuddy 三个 + qoder 的 DeepSeek-V4-Pro），但 qoder 36.8s 对 flash 7.5s，
   且用户本来就排除 qoder。其余桥要么 smoke 就挂，要么在需要围栏代码块的时候回散文。
2. **xhx 看着活着、其实不可用。** 三个模型 15-39s 都有响应，但一个可运行代码块都产
   不出来。"端口在听、有回复"和"能改代码"是两件事。
3. **当时的 live 默认就漂在这种死路里。** `~/.codex/config.toml` 的 `model` 是
   `xhx/xhx-sn-deepseek-v4-1-flash`，正是 NO_RUNNABLE 路线。`default_model_guard.py`
   没报警是对的：那条路确实能答一次聊天，而守卫只管路由健康，不管路由后面的模型能不
   能产出代码。这个缺口就是 `docs/code-model-selection.md` 存在的理由。

## 实现

1. `runtime/fleet.env`：`FLEET_DEFAULT_MODEL=workbuddy/deepseek-v4-flash`
   （原 `stepfun/step-5-preview`，港湾身份保留为 failover 目标）。
2. `~/.codex/config.toml` 的 `model` 键 pin 成同一 slug，用的就是
   `setup-providers.sh` 那段行重写（下次 setup 才不会被打回）。
3. `tools/default_model_guard.py --env-file fleet.env` 复跑：
   `default workbuddy/deepseek-v4-flash answers via workbuddy bridge on 8787 (E2E_OK)`。
4. 端到端再验一遍 Codex 真正走的路径（10100 网关 + responses API +
   `PROXY_MANAGED`）：三个 workbuddy deepseek 模型全部 200 且回 `E2E_OK`，1.4-1.7s。
5. 单测回归：`test_default_model_guard.py` 18 例 OK、
   `test_code_model_bench.py` pytest 8 例 OK、`code_model_bench.py offline` PASS、
   `test_default_model_pin_gating.py` 3 例 OK。
6. 文档：`docs/code-model-selection.md` 加 2026-10-03 全机队复测表并改 TL;DR；
   README「默认模型」一节同步；本文档记录决策。

## 遗留

- `hy4-preview`（workbuddy-gpt）当天 429 限流，仍是用户关心的模型：恢复后重跑 `live`。
  它强但慢（>9s/轮），适合 opus 档和交互式对话，不适合本仓库的紧凑改跑循环。
- cline、gemini、qwen、zcode、antigravity、catpaw、kimi-code、lingxi、codely、trae
  的 smoke 失败都记在 `docs/code-model-selection.md` 表里，账号/配额恢复后按同一命令复测。
- `xhx` 三模型与 `codely-core` 属于"能连上但不能产出代码"，恢复后要看的是指令遵循，
  不只是连通性。

## 相关

- `kit/docs/code-model-selection.md`（完整实测表与选型结论）
- `kit/tools/code_model_bench.py`、`kit/tools/default_model_guard.py`
