# qwen2codex Runbook — Qwen Cloud（千问海外托管）反代理到 Codex

日期：2026-09-26 | 状态：**链路已通；未注册账号，等待用户填入 QWEN_API_KEY**

## 背景核实结论（2026-09-26，官网 + HF 实测）

- **不存在「Qwen4 Preview 邀请码」**：千问官网 chat.qwen.ai `/api/models` 公开只有 3 个模型
  （`qwen3.7-plus` / `qwen3.8-max` / `qwen3.8-omni-flash`）；qwenlm.github.io 博客最新文章仍是
  Qwen3 时代；Qwen Cloud（www.qwencloud.com）Model Marketplace 在售 9 个模型，无任何 qwen4 /
  preview / waitlist / 邀请码条目。
- **官方所称「Qwen4 架构预览」= Qwen3.8-Flash-Next**（huggingface.co/Qwen/Qwen3.8-Flash-Next，
  内部架构名 `Qwen4ExpForConditionalGeneration`）：125B-A6B MoE + GDN/QSA 混合注意力 + 51B
  n-gram embedding。**开放权重、HF 未设门槛（gated: False）、无需邀请码**；NVFP4 量化版
  （nvidia/Qwen3.8-Flash-Next-NVFP4）约 124GB，适合单机自托管（DGX Spark 级显存）。
- **无自托管条件时的官方托管版 = qwen3.8-flash**（Qwen Cloud）：与 Flash-Next 同架构的生产版，
  原生 1M 上下文（最大输入 991K / 输出 131K），兼容 OpenAI + Anthropic 双协议，官方明示
  "integrates seamlessly with developer tools like Claude Code and **Codex**"。
  新用户免费额度 70M+ tokens，注册即用，**不需要邀请码**。
- 结论：用户所说的「qwen4 preview 预览内测」在本时间线不存在邀请制入口；本桥接入的是其
  官方托管生产版 `qwen3.8-flash`。

## 架构（实测）
```
Codex 桌面/CLI → ocx(:10100) → qwen2codex 桥(:8798) → https://maas.qwencloudapi.com/compatible-mode/v1
                                          （Qwen Cloud，OpenAI 兼容，Bearer sk-...）
```
- 桥：`bridges/qwen/qwen_bridge.py`（FastAPI + httpx，无状态，Bearer 透传，SSE 流式透传）
- LaunchAgent：`~/Library/LaunchAgents/com.local.qwen2codex.plist`（offset 11，端口 8798）
- 密钥：两个不同的 key，**别混用**
  - `QWEN2CODEX_KEY`：**本地**桥访问控制（任意字符串；Codex/ocx 带着它访问 127.0.0.1:8798）
  - `QWEN_API_KEY`：**上游** Qwen Cloud API key（`sk-` 前缀）。可选；不设则
    `/v1/models` 只给静态兜底目录（2 个模型），chat 一律 503 `qwen_key_missing`
- ocx provider：`qwen`（base `http://127.0.0.1:8798/v1`，`allowPrivateNetwork:true`）
- Codex 目录：`qwen/<model>` slug 由 `inject_catalog.py` 注入

## 激活步骤（一次性）

1. 注册 Qwen Cloud 并拿 key：
   - 打开 https://www.qwencloud.com/ → 注册/登录 → `Get API keys` → 创建 key（`sk-` 前缀）
   - 新账号自带免费额度（70M+ tokens），无需绑卡
2. 把 key 写进 `runtime/fleet.env`（fleet.env 不入库）。
   **写 `QWEN_API_KEY`，不要写 `QWEN2CODEX_KEY`**：后者只是本地桥 key；桥不再
   把它当上游 key 用，但没有 `QWEN_API_KEY` 时 chat 依然不可用（503
   `qwen_key_missing`）：
   ```bash
   cd ~/AI\ Shared/repo/FleetKit/runtime
   grep -v '^QWEN_API_KEY=' fleet.env > fleet.env.tmp && mv fleet.env.tmp fleet.env
   echo 'QWEN_API_KEY=sk-你的key' >> fleet.env
   ```
3. 让桥进程拿到 key。**桥进程只读 launchd plist 的 EnvironmentVariables**，
   只改 fleet.env 不生效（`finish.sh` 的 kickstart 不重读 fleet.env）。二选一：
   - 在役舰队（推荐，只动 qwen 一座桥）：同步 plist 后 finish
     ```bash
     KEY="$(grep -m1 '^QWEN2CODEX_KEY=' fleet.env | cut -d= -f2- | tr -d '\"')"
     UPKEY="$(grep -m1 '^QWEN_API_KEY=' fleet.env | cut -d= -f2- | tr -d '\"')"
     python3 - "$KEY" "$UPKEY" <<'PY'
     import os, plistlib, sys
     p = os.path.expanduser("~/Library/LaunchAgents/com.local.qwen2codex.plist")
     with open(p, "rb") as f: d = plistlib.load(f)
     d["EnvironmentVariables"]["QWEN2CODEX_KEY"] = sys.argv[1]
     if sys.argv[2]:
         d["EnvironmentVariables"]["QWEN_API_KEY"] = sys.argv[2]
     with open(p, "wb") as f: plistlib.dump(d, f)
     print("plist updated:", p)
     PY
     bash bridges/finish.sh qwen    # 重启 + 探活 + 注入 catalog + ocx sync + 冒烟
     ```
   - 新机器 / 整套重装：`bash ~/AI\ Shared/repo/FleetKit/kit/install.sh`
     （install.sh 的 pick_key 从现有 fleet.env 读 QWEN2CODEX_KEY 并烘进新 plist）
4. 验证：
   ```bash
   curl -s http://127.0.0.1:8798/health | python3 -m json.tool
   curl -s http://127.0.0.1:8798/v1/models | python3 -m json.tool
   bash bridges/finish.sh qwen        # 重启+探活+注入目录+冒烟聊天
   ```

## 模型

`/v1/models` 实时拉上游目录并过滤非对话模型（image/video/tts/asr/embedding 等不暴露）。
三种上游响应的处理方式不同，别再指望「不报错 = 可用」：

| 上游 | `/v1/models` | chat |
|---|---|---|
| 200 | 上游真实目录（过滤后） | 正常 |
| 401 / 403（key 被拒） | **401 + `upstream_auth_error`**，不给目录 | 401 |
| 网络错误 / 5xx | 200 + 静态兜底 `qwen3.8-flash` / `qwen3.8-max` + `_fallback` 说明 | 502 |

（2026-09-29 前是第 401 种也返回 200 兜底目录，于是 picker 和 `/health` 一片绿、
每个 chat 却 401。）
Codex 选择器 slug 形如 `qwen/qwen3.8-flash`。

## 网络说明

- 上游为国际站点，**默认直连**（实测 0.6s 内完成 401 握手）；
  网络受限机器设 `QWEN_UPSTREAM_PROXY=http://127.0.0.1:7890`（写入 plist 的
  EnvironmentVariables 或 bridge 进程环境变量）。

## 运维命令
```bash
bash bridges/finish.sh qwen                       # 重启 + /v1/models + 注入 catalog + 冒烟
launchctl kickstart -k gui/$(id -u)/com.local.qwen2codex
tail -f /tmp/fleet-logs/qwen2codex*.log           # 桥日志
python3 tools/verify_real_calls.py --only qwen    # 真实调用检测（REAL/BRIDGE_DOWN 判定）
```

## 2026-09-29 更新：上游 key 被拒时诚实报错

- 现象：`fleet.env` 只有本地桥 key `QWEN2CODEX_KEY`，没有 `QWEN_API_KEY`；桥的
  `API_KEY = QWEN_API_KEY or BRIDGE_KEY` 把本地 key 当上游 key 发给 Qwen Cloud →
  上游 401。而 `/v1/models` 仍返回 200 + 两行静态模型，`/health` 也 ok，
  `verify_real_calls.py` 之外看不出任何异常。
- 修复：`bridges/qwen/qwen_bridge.py` 的上游 401/403 直接以 401 +
  `type: upstream_auth_error` 返回，message 指明要设 `QWEN_API_KEY`；
  网络错误与 5xx 仍给静态兜底目录（那两个模型名本身是真的）。
- 部署侧：`install.sh` 新增 `pick_optional`（不生成假 key），`fleet.env` 增加
  一行 `QWEN_API_KEY=`，qwen 服务的 launchd env 只在非空时才注入
  `QWEN_API_KEY`。设好后 `bash bridges/finish.sh qwen`.
- 测试：`tools/test_qwen_models_auth.py`（含上游健康时的 junk 过滤回归）。

## 2026-09-29 追加：本地桥 key 不再冒充上游 key

- 现象：上一轮只让上游 401/403 诚实报错，`API_KEY = QWEN_API_KEY or BRIDGE_KEY`
  仍在：没有 `QWEN_API_KEY` 时桥把本地 `QWEN2CODEX_KEY` 发给
  maas.qwencloudapi.com——既把本地密钥泄给第三方，`/health` 还答
  `has_api_key: true`，运营商顺着"健康"线索去查一个不存在的过期会话。
- 修复：`API_KEY` 只认 `QWEN_API_KEY`，不再回退；chat 在无 key 时本地直接 503
  `qwen_key_missing`（message 里带确切修法），不再白发一次上游请求。
- 测试：`tools/test_qwen_models_auth.py` 增加两个回归（本地 key 不外发；
  chat 无 key 本地失败且不触上游），共 8 个。
- 连带修正：`tools/status.sh` 的 `models=0` 提示不再一律说"会话过期"，并从
  桥 `/health` 读真实 `has_api_key` 给出 qwen 专项说明。
