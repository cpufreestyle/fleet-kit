# qwen2codex Runbook — Qwen Cloud（千问海外托管）反代理到 Codex

日期：2026-09-26 | 状态：**链路已通；未注册账号，等待用户填入 QWEN2CODEX_KEY**

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
- 密钥：`QWEN2CODEX_KEY`（即 Qwen Cloud API key，`sk-` 前缀；桥本地访问控制与上游调用同一个 key）
- ocx provider：`qwen`（base `http://127.0.0.1:8798/v1`，`allowPrivateNetwork:true`）
- Codex 目录：`qwen/<model>` slug 由 `inject_catalog.py` 注入

## 激活步骤（一次性）

1. 注册 Qwen Cloud 并拿 key：
   - 打开 https://www.qwencloud.com/ → 注册/登录 → `Get API keys` → 创建 key（`sk-` 前缀）
   - 新账号自带免费额度（70M+ tokens），无需绑卡
2. 把 key 写进 `runtime/fleet.env`（fleet.env 不入库）：
   ```bash
   cd ~/AI\ Shared/repo/FleetKit/runtime
   echo 'QWEN2CODEX_KEY=sk-你的key' >> fleet.env    # 已有该行先删掉旧行
   ```
3. 让桥进程拿到 key。**桥进程只读 launchd plist 的 EnvironmentVariables**，
   只改 fleet.env 不生效（`finish.sh` 的 kickstart 不重读 fleet.env）。二选一：
   - 在役舰队（推荐，只动 qwen 一座桥）：同步 plist 后 finish
     ```bash
     KEY="$(grep -m1 '^QWEN2CODEX_KEY=' fleet.env | cut -d= -f2- | tr -d '\"')"
     python3 - "$KEY" <<'PY'
     import os, plistlib, sys
     p = os.path.expanduser("~/Library/LaunchAgents/com.local.qwen2codex.plist")
     with open(p, "rb") as f: d = plistlib.load(f)
     d["EnvironmentVariables"]["QWEN2CODEX_KEY"] = sys.argv[1]
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

`/v1/models` 实时拉上游目录并过滤非对话模型（image/video/tts/asr/embedding 等不暴露）；
无 key 时返回静态兜底：`qwen3.8-flash`（默认，Qwen4 架构生产版）、`qwen3.8-max`。
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
