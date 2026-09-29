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

## 图片数 400：image-cap shim（2026-09-30）

### 根因

CC Switch 这一跳而不是模型设了上限。同一批图片，71 张以内 OK，第 71 张开始
Plan API 回 `400 images_too_many`（`tools/image_cap.py` 是这次测量）：

```
70 images in one request -> HTTP 200
71 images in one request -> HTTP 400 images_too_many
```

同一个 71 图请求直连 `api.stepfun.com` 返回 200 —— StepFun 文档写 1M 上下文，
读图毫无问题，卡住的是 CC Switch 那一跳。

Codex 对此毫无办法：`disable_response_storage = true` 让它每轮重发整段会话，
所以运营者贴的 N 张截图每轮都被重发一次，长会话必然撞上一个用户无法处理的 400
（`The amount of images you provided exceeds the model's limitation` ——
这句话是报给 API 调用方的，不是报给屏幕上那个人的）。

### 方案

`tools/stepfun_image_shim.py` 是架在 CC Switch（127.0.0.1:15721）前面的透明透传，
监听 15722，转发前对请求体做两件无损重写（实现在 `tools/image_cap.py`）：

- 去重：同一个 data URL 重复出现（重新贴的截图、被回声两次的工具结果）算一张，
  只留最新的 citation，也就是会话当前那份拷贝所在的位置。
- 截断：去重后仍超过上限时保留最近的图片，最旧的替换成一句短文本说明。
  被丢掉的槽位变成文本而不是被删掉 —— 空的 content 列表是畸形请求，
  而一个静默的空洞对模型读起来像「用户这里什么都没发」。

### 实测

| 场景 | 上游实际收到 |
|------|--------------|
| 70 图 → 200；71 图 → 400 `images_too_many` | 直接测量 |
| 沙盒 POST 100 图，cap 生效 | 32（`cap_dropped=68`） |
| 经 launchd 全链路 100 图，`stepfun/step-5-preview` | 32 |
| 经 launchd 全链路 100 图，`workbuddy/hy4-preview` | 100（原样透传） |

stats：`requests:2, rewritten:1, images_seen:100, images_kept:32`。
`hy4-preview` 拿到完整的 100 张，证明 `IMAGE_CAP_MODELS=step` 只对 step 前缀生效，
没有顺手砍别的船。

### 部署

```bash
tools/stepfun_image_shim.sh <run|start|stop|status|install-timer|uninstall-timer> [--home DIR]
```

`install.sh` 和 `opencodex/setup-providers.sh` 都已自动接线；两个入口都有 `--dry-run`
分支，只打印计划、一个字节都不写。

| 变量 | 作用 | 默认 |
|------|------|------|
| `IMAGE_CAP_PORT` | shim 监听端口 | 15722 |
| `IMAGE_CAP_UPSTREAM` | 转发目标 | `http://127.0.0.1:15721` |
| `IMAGE_CAP_MAX` | 每请求保留图片数 | 32（`<= 0` 不限） |
| `IMAGE_CAP_MODELS` | 生效模型子串，逗号分隔 | `step` |
| `FLEET_PYTHON` | shim 用的 python | 取 `runtime/.venv/bin/python` |

日志：`/tmp/fleet-logs/com.local.stepfun-image-cap.log`。
`uninstall.sh` 的 `SUFFIXES` 已含 `stepfun-image-cap`，卸载不会在 launchd 里留孤儿作业。

### 必须配 base_url pin

CC Switch 拥有 `~/.codex/config.toml`，运营者每切换一次 provider 它就把 custom
provider 的 `base_url` 写回 15721。只装 shim 不 pin 的结果是：Codex 直连 CC Switch，
shim 在 15722 上空转，图片照样撞 400。

`tools/pin_shim_base_url.py` 就是这次 pin，幂等，`setup-providers.sh` 每次都会重跑：

- 读顶层 `model_provider` 名字（默认 `custom`），只改那一个 provider 的 `base_url`，
  不碰别的 provider，也不碰文件里其它 15721 引用；
- 按 host:port 子串替换，scheme 和 `/v1` 路径原样保留；
- 已经指向 shim、或指向别的 host 时文件一个字节都不动，报 `no change` —— 可以每次
  setup 都跑，也可以挂定时任务。

### 回滚

```bash
tools/stepfun_image_shim.sh uninstall-timer
python3 tools/pin_shim_base_url.py --dry-run   # 先看会动哪里
```

然后把 `~/.codex/config.toml` 里 custom provider 的 `base_url` 改回 15721。

### 两个坑

- **代理环境变量吃 loopback**：本机开着 `http_proxy=127.0.0.1:1082` 时，httpx 和 curl
  都会把 loopback 流量送进代理，探活 curl 一律加 `--noproxy '*'`。shim 内部用
  `LOOPBACK_MOUNTS`（`trust_env=False`）自己绕开了这一层。
- **日志名字必须和 launchd 一致**：platform.sh 把 plist 的
  `StandardOutPath`/`StandardErrorPath` 指向 `$LOG_DIR/<label>.log`。控制脚本原来写
  `stepfun-image-cap.log`（另一个名字），运营商去看日志时只看到空文件，而服务其实
  一直在正常服务流量。已改成 `SHIM_LOG="$LOG_DIR/${SHIM_LABEL}.log"`。
- **安装后 `-15` 不是崩溃**：`fleet_service_install` 末尾 `kickstart -k` 的 SIGTERM
  残留，正常服务 `com.local.fleet-ui` 同样显示 `-15`；等几秒再看 PID 就在跑了。
