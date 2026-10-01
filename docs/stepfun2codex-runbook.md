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

`tools/stepfun_image_shim.py` 是架在 CC Switch（127.0.0.1:15721）**下面**的透明透传，
监听 15722，转发前对请求体做两件无损重写（实现在 `tools/image_cap.py`）：

- 去重：同一个 data URL 重复出现（重新贴的截图、被回声两次的工具结果）算一张，
  只留最新的 citation，也就是会话当前那份拷贝所在的位置。
- 截断：去重后仍超过上限时保留最近的图片，最旧的替换成一句短文本说明。
  被丢掉的槽位变成文本而不是被删掉 —— 空的 content 列表是畸形请求，
  而一个静默的空洞对模型读起来像「用户这里什么都没发」。

### 链路

    Codex -> 127.0.0.1:15721 (CC Switch) -> 127.0.0.1:15722 (本 shim)
          -> https://api.stepfun.com/step_plan/v1

shim 的上游是真正的上游而不是 CC Switch，所以两边不可能绕成环。

### 为什么在下面，不在前面（2026-09-30 Go）

第一版把 shim 架在 CC Switch 前面，靠 `tools/pin_shim_base_url.py` 把
`~/.codex/config.toml` 里 custom provider 的 `base_url` 钉到 15722，并实测确认文件里
写的确实是 15722。**但它一条请求都没拦住过**：

- `~/.cc-switch/cc-switch.db` 的 `proxy_request_logs` 里 03:28:52、03:29:15 两条 400
  的 `provider_id` 是 StepFun（`3a20aad7-bc99-4b10-8a72-d7b7dacd2c16`），请求到的是
  CC Switch 自己的端口；
- shim 自己的 health 是 `requests=1`，只有一次自测；
- CC Switch 记录的转发目标是 `https://api.stepfun.com/step_plan/v1/chat/completions`，
  来自它自己库里的 providers 行。

两个原因叠在一起就无解：CC Switch 拥有 `~/.codex/config.toml`，运营者每切一次 provider
它就把 `base_url` 写回 15721；而**已经在跑的 Codex 不重读配置文件**。钉文件这一手因此
永远赢不了。真正有效的杠杆是 CC Switch 自己的路由表：`tools/pin_cc_switch_endpoint.py`
改写 `~/.cc-switch/cc-switch.db` 里 StepFun/codex 这一行的转发目标
（`provider_endpoints.url` 和内嵌在 `providers.settings_config` 里的 `base_url` 两处都改），
链路反过来之后，不管配置文件是谁写的、Codex 有没有重启，每条请求都必须经过 shim 才能
到 StepFun。

CC Switch 把路由表缓存在内存里，改完数据库要重启 app 才生效，运营者是手动重启的。shim
自己也每 300 秒重指一次，防止谁手工重加了一次 StepFun provider 又指回直连；
`tools/pin_shim_base_url.py` 降级成默认关闭的兜底（`IMAGE_CAP_REPIN_INTERVAL=0`），
需要时可以单独打开。

### 实测

| 场景 | 上游实际收到 |
|------|--------------|
| 70 图 → 200；71 图 → 400 `images_too_many` | 直接测量 |
| 沙盒 POST 100 图，cap 生效 | 32（`cap_dropped=68`） |
| 经 launchd 全链路 100 图，`stepfun/step-5-preview` | 32 |
| 经 launchd 全链路 100 图，`workbuddy/hy4-preview` | 100（原样透传） |
| 上线后 71 张互不相同的图，直连 15721 | 400 `images_too_many` |
| 上线后同一批 71 图，经 launchd shim 15722 | 200（`images=71 unique=71 kept=32 cap_dropped=39`） |
| 2026-10-01 sweep 后同一批 71 张互异图，经 launchd shim 15722 | 200（`kept=32 cap_dropped=39`） |

stats：`requests:2, rewritten:1, images_seen:100, images_kept:32`。
`hy4-preview` 拿到完整的 100 张，证明 `IMAGE_CAP_MODELS=step` 只对 step 前缀生效，
没有顺手砍别的船。

2026-10-01 sweep 上线后对着正在跑的服务复打（launchd `com.local.stepfun-image-cap`）：
health `requests:23, rewritten:1, images_seen:71, images_kept:32, passthrough:3`，
`cc_pin.changed=true`。`kept=32` 等于 cap 上限，`passthrough` 是非 step 前缀的请求，
再一次证明 cap 只对 step 前缀生效。

上线后那一行是对着正在跑的服务打的（launchd `com.local.stepfun-image-cap`，
shim health：`requests:19, rewritten:5, images_seen:355, images_kept:36, passthrough:5`）：
同一批 71 张**互不相同**的图，POST 到 15721 复现用户看到的 400，POST 到 15722
拿到 200 和 6053 input_tokens。这是「修好了」而不是「单测里修好了」的证据。

排错时踩过的坑：拿一批 71 张**完全相同**的图去打，shim 去重到 1 张，上游回的是
`400 input_invalid`，跟 cap 无关 —— 单图、原文不改、直连 15721 也是同样的
`input_invalid`，那张 70 字节的测试 PNG 本身不合法。判据：**shim 日志里的 `kept=N`
是它对上游的承诺；回来一个非图片数的 400，就是请求本身有问题**。

### 部署

```bash
tools/stepfun_image_shim.sh <run|start|stop|status|install-timer|uninstall-timer> [--home DIR]
```

`install.sh` 和 `opencodex/setup-providers.sh` 都已自动接线；两个入口都有 `--dry-run`
分支，只打印计划、一个字节都不写。

| 变量 | 作用 | 默认 |
|------|------|------|
| `IMAGE_CAP_PORT` | shim 监听端口 | 15722 |
| `IMAGE_CAP_UPSTREAM` | 转发目标 | `https://api.stepfun.com/step_plan/v1` |
| `IMAGE_CAP_CC_DB` | CC Switch 数据库 | `~/.cc-switch/cc-switch.db` |
| `IMAGE_CAP_CC_PROVIDER`、`IMAGE_CAP_CC_APP_TYPE` | 要重指的 provider 行 | `StepFun` / `codex` |
| `IMAGE_CAP_CC_PIN_INTERVAL` | CC Switch 定时重指间隔（秒） | 300（`<= 0` 关闭） |
| `IMAGE_CAP_CC_PIN_ALL` | sweep：重指所有指向 StepFun 的 codex 行 | 1（`0` 收窄回只动命名 provider） |
| `IMAGE_CAP_MAX` | 每请求保留图片数 | 32（`<= 0` 不限） |
| `IMAGE_CAP_MODELS` | 生效模型子串，逗号分隔 | `step` |
| `IMAGE_CAP_REPIN_INTERVAL` | 兜底：config.toml 定时重 pin 间隔（秒），默认关 | 0 |
| `IMAGE_CAP_PIN_CONFIG` | 兜底重 pin 的 Codex 配置文件 | `~/.codex/config.toml` |
| `FLEET_PYTHON` | shim 用的 python | 取 `runtime/.venv/bin/python` |

日志：`/tmp/fleet-logs/com.local.stepfun-image-cap.log`。
`uninstall.sh` 的 `SUFFIXES` 已含 `stepfun-image-cap`，卸载不会在 launchd 里留孤儿作业。

### 必须重指 CC Switch 的路由表

CC Switch 是 Codex 与 StepFun 之间的那一跳，它自己库里记着 StepFun provider 该转发到哪。
只装 shim 不改这个目标的结果是：请求从 CC Switch 直连 StepFun，shim 在 15722 上空转，
图片照样撞 400。

`tools/pin_cc_switch_endpoint.py` 就是这次重指，幂等，`setup-providers.sh` 每次都会重跑：

- **默认 sweep 模式**：不只 `StepFun` 这一行，而是按**转发目标**把本机所有指向 StepFun 的
  codex 行都指到 shim（CLI `--all-stepfun` 是同一个开关，`IMAGE_CAP_CC_PIN_ALL=0` 收窄回
  只动命名的那一行）；
- 按目标而非 provider 名判断 —— 名字不可信，同一个上游可以叫 `StepFun`、`nv spark`
  或者别的；
- 别的 app 一个字节都不碰：`StepFun/claude` 指向
  `https://api.stepfun.com/step_plan`（无 `/v1`），是正常通道，不在 sweep 范围内；
- 没有 StepFun 的机器上就是 no-op；
- 改两处，因为 CC Switch 可能读任意一处：`provider_endpoints.url`，以及内嵌在
  `providers.settings_config`（JSON，其 `"config"` 值里的 TOML）中的 `base_url`；
- 目标既不是 stepfun 也不是 shim 时拒绝改写 —— 有人手工指到别处是别人的决定，
  静默改掉比不 cap 更糟；
- 首次改写前取一个带时间戳的 sqlite 备份（`cc-switch.db.bak-before-fleetkit-endpoint-*`，
  只取一次），之后不再取；
- 已经指向 shim 时报 `no change`，可以每次 setup 都跑，也可以挂定时任务。
- 按 host:port 子串替换，scheme 和 `/v1` 路径原样保留；
- 已经指向 shim、或指向别的 host 时文件一个字节都不动，报 `no change` —— 可以每次
  setup 都跑，也可以挂定时任务。

人工重加或编辑一次 StepFun provider 就会拿到一条指回直连的新行，所以 shim 自己每
`IMAGE_CAP_CC_PIN_INTERVAL` 秒（默认 300）重指一次。**改完数据库要重启 CC Switch.app 才
生效**：它把路由表缓存在内存里。

### provider-switch 旁路（2026-10-01）

`--dry-run --all-stepfun` 实测发现本机还有第二条旁路：

```
[dry-run] nv spark/codex forwards to https://api.stepfun.com/step_plan/v1 (providers.settings_config); would write it to http://127.0.0.1:15722/v1
[dry-run] StepFun/codex already points at http://127.0.0.1:15722/v1 (provider_endpoints)
```

`nv spark` 这个 codex provider 也转发到 StepFun，但不叫 StepFun，原来那只动命名行的
窄模式够不着它。运营者在 CC Switch UI 里选中它 → 直接绕过 shim → 71 图照样 400。
判据不是名字而是目标：**凡是指向 StepFun 的 codex 行，都是要过 cap 的船**。

于是 sweep 成为默认。上线后 shim 日志实测：

```
[cc-pin] repointed nv spark/codex providers.settings_config: https://api.stepfun.com/step_plan/v1 -> http://127.0.0.1:15722/v1 (1 endpoint row, 1 embedded config)
```

现在 DB 里两个 codex 行都指向 `http://127.0.0.1:15722/v1`。仍然提醒一次：
**改完数据库要重启 CC Switch.app 才生效**，它把路由表缓存在内存里。

`tools/pin_shim_base_url.py`（改 `~/.codex/config.toml`）降级为默认关闭的兜底，
`IMAGE_CAP_REPIN_INTERVAL=0`。实测它赢不了，见上文「为什么在下面，不在前面」。

### 回滚

```bash
tools/stepfun_image_shim.sh uninstall-timer
python3 tools/pin_cc_switch_endpoint.py --dry-run   # 先看会动哪里
```

然后把 CC Switch 里 StepFun provider 的转发目标改回
`https://api.stepfun.com/step_plan/v1` —— 首次改写前的整库备份就在同目录的
`cc-switch.db.bak-before-fleetkit-endpoint-<时间戳>`，拷回去即可。
`~/.codex/config.toml` 里 custom provider 的 `base_url` 也改回 15721。

### 两个坑

- **CC Switch 改完数据库不重启不生效**：它把路由表缓存在内存里，
  `pin_cc_switch_endpoint.py` 写完 `cc-switch.db` 之后，已经在跑的 CC Switch 仍按旧目标
  转发。判据：shim health 里 `cc_pin.changed` 是 true 但 `requests` 不涨。运营者手动
  重启 CC Switch.app 之后链路才通，这也是 setup 结尾要打印那行提示的原因。
- **代理环境变量吃 loopback**：本机开着 `http_proxy=127.0.0.1:1082` 时，httpx 和 curl
  都会把 loopback 流量送进代理，探活 curl 一律加 `--noproxy '*'`。shim 内部用
  `LOOPBACK_MOUNTS`（`trust_env=False`）自己绕开了这一层。
- **日志名字必须和 launchd 一致**：platform.sh 把 plist 的
  `StandardOutPath`/`StandardErrorPath` 指向 `$LOG_DIR/<label>.log`。控制脚本原来写
  `stepfun-image-cap.log`（另一个名字），运营商去看日志时只看到空文件，而服务其实
  一直在正常服务流量。已改成 `SHIM_LOG="$LOG_DIR/${SHIM_LABEL}.log"`。
- **安装后 `-15` 不是崩溃**：`fleet_service_install` 末尾 `kickstart -k` 的 SIGTERM
  残留，正常服务 `com.local.fleet-ui` 同样显示 `-15`；等几秒再看 PID 就在跑了。
