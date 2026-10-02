# 默认模型港湾与跳回（2026-10-01）

## 诉求

以后默认模型固定 `step-5-preview`；其他模型出问题时，默认跳回这个模型。

## 现状盘点（当天实测）

- `~/.codex/config.toml`：`model = "step-5-preview"`、`model_provider = "custom"`
  （base_url 127.0.0.1:15721 = CC Switch）。
- CC Switch（`cc-switch` pid 773，15721）：codex 有 6 个 provider，**StepFun 是
  current**（`is_current=1`），其内嵌 TOML 钉 `model = "step-5-preview"`、
  base_url 127.0.0.1:15722（image-cap shim）。其余：nv spark（15722）、
  NVIDIA NIM（11436）、netapi、OpenAI Official、3day。
- `proxy_config`（codex）：`auto_failover_enabled=1`；`provider_health` 里
  StepFun/codex `is_healthy=1`。
- ocx（`@bitkyc08/opencodex`，10100）：`defaultModel=stepfun/step-5-preview`，
  默认 provider 就是 stepfun；PATH 上的 `ocx` 是 fleet 的包装脚本
  （`tools/fleet-sort-after-sync.sh`），只转发真 CLI 并在 sync 后重排目录。
- `fleet.env`：**`FLEET_DEFAULT_MODEL=trae/trae-step-5-preview`** —— 与
  README「当前默认 stepfun/step-5-preview」矛盾，且 trae 该上游 401→502 已死
  （README「已知问题」有记录）。下次 `setup-providers.sh` 会把它 pin 回去，
  旧 bug 复发。**当天已改为 `stepfun/step-5-preview`。**

## 两个必须知道的链路事实

1. **Codex 的流量走 CC Switch，不走 ocx 代理**。config.toml 的 custom provider
   指向 15721；15721 是 CC Switch app 自己的端口。ocx 代理在 10100，只有探活
   （`fleet_probe.py` 的 `probe_gateway`）和管理用。
2. **CC Switch 把外来模型改写成当前 provider 的模型**。实测：经 15721 请求
   `workbuddy/hy4-preview`、`xhx/...`、`qoder/...`，响应 `model` 全是
   `step-5-preview`，自报身份全是 "Step"；`proxy_request_logs` 里今天 740 条
   `workbuddy/hy4-preview` 的 `model` 列也都是 `step-5-preview`；而 CC Switch 的
   codex provider 里**根本没有 workbuddy**——请求只可能去了 StepFun。只有属于
   StepFun 的模型（`step-3.5-flash`、`step-5-preview`）原样穿过。
   **推论：从网关侧（15721）探活永远探不到真实默认模型**，死默认也会得到
   200 step-5-preview。trae 路线挂了一周没被发现，部分原因在此。

## 实现：两级跳回

### 1. pin 时守卫（新工具）

`tools/default_model_guard.py`，每次 `setup-providers.sh` pin 完默认模型后自动
跑（也可手跑，`--dry-run` / `--json` / `--config` / `--env-file` / `--cc-db`）：

- 拿活配置 `model` 键，解析它自己的路由：stepfun 系（含裸 slug，默认 provider
  就是 stepfun）→ 15722 shim；其余 → 各自桥端口，探测模型名**去掉 provider 前缀**
  （Plan API 和桥都只认裸模型名，带上前缀会 404）；
- 打一次真实聊天（nonce `E2E_OK`，budget 60→1024 升档，和 `fleet_probe.py`
  同一套宽松判定）；
- 死 + 当前不是港湾 → 把 `model` 改钉回 `stepfun/step-5-preview`，打印证据；
- 死 + 当前就是港湾（含裸名拼写）→ 只报告，退出码 1，**不改写**：没处可跳；
- `fleet.env` 的 `FLEET_DEFAULT_MODEL` 指着死路线 → 告警（下次 setup 会 pin 它）；
- 只读检查 CC Switch 的运行时 failover 状态，漂移时打印修复 SQL、退出码 1；
  **不写** CC Switch 的库（现版本现读数据库，见 stepfun2codex-runbook
  「改完数据库要不要重启」；老版本要重启，重启是人的决定）。

测试：`tools/test_default_model_guard.py`（18 例，网络全 stub）。

### 2. 运行时 failover（CC Switch 自带）

会话中临时切到别的 provider，它挂掉时 CC Switch 按熔断把请求转给 failover
队列里的 provider。港湾要在 codex 的 failover 队列里（`in_failover_queue=1`，
现状满足），这层才不是空转。守卫每次运行检查这两项。

## 用户选择其他模型的路径（说明，非本次改动）

切模型有两条路：CC Switch UI 切 provider（当前 6 个，各自钉模型），或 Codex
选择器选 catalog slug——后者经 15721 会被改写成当前 provider 的模型（事实 2）。
也就是说只要 StepFun 是 current，选择器选什么都是 step-5-preview。要让某个桥
模型真正生效，先在 CC Switch 里把对应 provider 设为 current。这与本次诉求
（默认 step-5-preview、出事跳回）不冲突，但值得知道。
