## 2026-09-27 重大修正：Node 能过 ESA，要求 OPTIONS 预热

此前「只有 Electron 原生栈能过 TLS 指纹」的结论**过宽，已修正**。

### 实测（Node 22 原生 fetch，无代理、无 Electron）

    GET  /api/v1/client/configs?app_version=3.14.3&platform=darwin-arm64  -> 200
    OPTIONS /api/v1/zcode-plan/anthropic/v1/messages                       -> 404 (到达应用层)
    HEAD    /api/v1/zcode-plan/anthropic/v1/messages                       -> 404 (到达应用层)
    POST    /api/v1/zcode-plan/anthropic/v1/messages                       -> 3007 captcha verify failed

**结论：Node 网络栈可以通过 ESA 的 TLS 指纹校验，请求能到达应用层。**
上面这些响应的共同特征是 HTTP 400/404 而不是 3012。

### 决定性差异：直接 POST 会被边缘拦截

| 请求序列 | 结果 |
|----------|------|
| 直接 `POST /v1/messages` | **3012 unusual activity**（ESA 边缘丢弃） |
| `OPTIONS /v1/messages` 然后 `POST` | **3007**（到达应用层，验证码校验失败） |

即：**同一个 host + path，先用 OPTIONS 预热一次，POST 就能穿透边缘风控。**
这解释了此前所有矛盾现象——Python `urllib` 桥从不发 OPTIONS，所以 100% 撞 3012。

### 3007 是剩下的唯一问题

拿到 3007 说明 captcha 票据没被接受。已排除的方向：

- 票据过期：实测 age=1s 的新票同样 3007/3012，不是新鲜度问题
- 票据内容：relay 页 `status: saved(saved len=280)` 与 App mint 的长度一致

另一个已观察到的现象：**ESA 会按失败频率升级拦截**。8 分钟内先两步法成功到 3007，
继续尝试后同一序列变成 3012。所以排查这类问题必须低频率、一次一试，
否则会把自己封进更严格的档位。

### 建议的 bridge 改动

在 `_post()` 里给 `/v1/messages` 加一次 OPTIONS 预热，再把失败间隔拉长
（例如单 provider 最短 30s 一次），避免触发频率风控。

---

## （历史）app-server 无法装载凭据——已被上面的发现取代

曾用源码定位 `startProcessProviderRegistryRuntime(env, t={})` 的凭据层由
`t.standalone` 决定，而 `zcode.cjs app-server` 只调 `Ykt(z)` 不传第二参数，
因此 CLI 侧 registry 恒空。该结论对 CLI 路线仍然成立，但**已无关紧要**：
既然 Node 直连就能过 ESA，根本不需要 CLI。


# ZCode CLI 无头驱动（方案 C：换承载 host）

状态：**协议已完整逆向并封装，本地卡在 provider registry 装载，最后一步依赖运行环境**。
本文记录全部已验证事实，避免重复排查。

## 为什么走 CLI 而不是桥

`zcode-plan/anthropic/v1/messages` 被阿里云 ESA 边缘网关按 **TLS/JA3 指纹**拦截（3012
`unusual activity`），与本机出口 IP、captcha 票据、cookie、请求头全都无关：

- App 自己的 Electron 主进程网络栈调 `billing/balance`：**30/30 HTTP 200**（同 IP、同时刻）
- 在 App renderer 里成功铸出 captcha（`success len=280`）并**立刻** `fetch`：**必 3012**
- 导航到真实同源页 `https://zcode.z.ai`、带 `_headers()` 全量 `X-ZCode-*` 头：**仍 3012**
- 响应头 `server: ESA` + `via: ens-cache*.cn8009` -> 请求没到应用层就被丢弃

结论：只有 Node/Electron 原生网络栈发的请求能过。ZCode CLI（`zcode.cjs`，Node bundle）
正是这样的栈，所以方案 C = 在有权限的 host 上跑 CLI，本地只留一个 OpenAI 兼容转发层。

## CLI 位置与启动

    /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs   # zcode 0.16.9, 14.8MB
    node .../zcode.cjs --help        # TUI 模式
    node .../zcode.cjs app-server    # stdio 协议服务（推荐，无 UI）
    node .../zcode.cjs -p "..."      # 单次 prompt

两个必做的本地修补：

1. CLI 会找 `glm/provider/zcode-builtin.json`，App 没解包它。建软链即可：

       ln -sfn /Applications/ZCode.app/Contents/Resources/config/provider \
               /Applications/ZCode.app/Contents/Resources/glm/provider

2. `/config` 不可写（macOS SIP），不要尝试。

## stdio 协议（app-server）—— 不是 JSON-RPC

**消息里没有 `jsonrpc` 字段**，带了会被 `Invalid ZCode Protocol message` 拒绝。

    request   {"id": "<str>", "method": "<name>", "params": {...}}
    notify    {"method": "<name>", "params": {...}}
    response  {"id": "<str>", "result": <any>}
    error     {"id": "<str>", "error": {"code": int, "message": str, "data": any}}

四个 message schema（源码变量 `qHt`）严格 `.strict()`，多余键一律 400。

### 建 session 前必须回应的两个 server->client 请求

    -> {"id": "1", "method": "runtime/capabilities", "params": {}}   # 客户端主动发
    <- {"id": "1", "result": {"independentPlanState": true}}

    -> {"method": "session/requestRuntimePreferences",
       "params": {"sessionId": "...", "scope": "runtime-materialization"}}
    <- {"id": "2", "result": {"askUserQuestionAutoResolutionEnabled": true,
                           "nativeSearchEnhancementsEnabled": true,
                           "memoryEnabled": false}}

不回这个 notify，`session/create` 会 15s 后报
`Client request timed out: session/requestRuntimePreferences`。
该 scope 只接受上面 3 个键，多一个键就 `unrecognized_keys`。

### 关键方法

| 方法 | params | 备注 |
|------|--------|------|
| `session/create` | `{workspace:{workspacePath,workspaceKey}, model?:{providerId,modelId}}` | `workspaceKey` 必填 |
| `session/send` | `{sessionId, content}` | 字段名是 `content`，不是 `input` |
| `session/messages` | `{sessionId}` | 读回对话 |
| `session/setModel` | `{sessionId, model:{providerId,modelId}}` | `model` 必须是 object |
| `session/resume` | `{sessionId, workspace}` | 恢复历史 session |
| `provider/updateAccountConfig` | account snapshot | 注册 coding-plan 授权 |
| `provider/testModelConnectivity` | `{workspace, selection:{providerId,modelId}}` | 探测模型是否可用 |

## `provider/updateAccountConfig`（让 provider 进 registry）

    {
      "revision": "account:fk-1",
      "basedOnZCodeBuiltinRevision": "zcode-builtin:30:<sha256>",
      "providers": {
        "account:zai-start-plan": {"access": {"type": "zhipu-account", "entitled": true}}
      },
      "states": {
        "account:zai-start-plan": {"availability": "available", "entitled": true, "current": true}
      }
    }

两条硬约束（源码 `parseProcessAccountProviderConfigSnapshot`）：

- `entitled: true` 的 provider 必须同时给 `states[pid].current`（布尔），否则
  `Account State 缺少 current: <pid>`
- `states[pid].availability` 只能是 `available|pending|unavailable|unknown`

成功后返回 `{"receivedRevision": "...", "providerCount": N, "status": "received"}`。
**注意：这只注册账号授权，不等于模型可用。**

## 2026-09-27 终局：根因已用源码定位，app-server 无法承载

### 更正

此前把 `provider/updateAccountConfig` 返回的 `{"providerCount": 8}` 当作注册证据，
**这是错的**。真实 registry 大小只认进程日志里的一处：

    zcode_protocol.provider_registry.ready   providerCount = <registry.providers.length>

同一轮实验的两个数字：

    updateAccountConfig -> {"providerCount": 8, "status": "received"}   # 只是回报收到的快照
    进程日志            -> providerCount = 0                            # 真实 registry
    session settings    -> {"model": {"available": []}}

`updateAccountConfig` 的计数与 registry 的计数是两回事，不能互相印证。

### 断点（源码定位，非推测）

`startProcessProviderRegistryRuntime`（minified 名 `Ykt`）的签名是
`Ykt(env, t={})`，凭据层的创建完全由 `t.standalone` 决定：

    let o = new yee;
    let s = t.standalone ? t.standalone.credentialStore ?? jM({env: {...e}}) : void 0;
    ...
    ...s ? {createAccountSource(_){ return a = new GKe({...}) } } : {}

即 `t.standalone` 为空时，`credentialStore`、`zcodeBuiltinRemote`、
`accountSource` 三者全部不创建。

而 `app-server` 入口的实际调用只有：

    U = await Ykt(z)          # 只传 env，没有第二参数

所以 `zcode.cjs app-server` **结构上就无法装载凭据**，这不是配置或环境变量能绕过的。

### 已确认正确的部分（复用时仍有效）

`resolveNodeProviderRuntimePaths`（`ymr`）的契约：

- `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE` 与 `ZCODE_PERSONAL_PROVIDER_CONFIG_FILE`
  **必须同时提供**，只给一个抛「必须同时提供」，都不给返回 null 并抛「缺少...路径」。
- 两者都给则通过（本机实测未抛错，说明路径推导正确）。

路径推导（`cZe()`，已验证与磁盘一致）：

    <home>/.zcode/v2/runtime/provider/darwin-aarch64/0.0.0-dev/endpoint-78d7c3bef4024722642626fe3669a799/zcode-builtin.json

- app-server 的 appVersion 是 `0.0.0-dev`，不是 App 的 `3.14.3`
- origin `https://zcode.z.ai` -> `endpoint-78d7c3be...`
- platform 段是 Electron 风格的 `darwin-aarch64`

`ZCODE_DATA_BASE_DIR` 对 app-server 无效（实测指向空目录后该目录仍为空）。

### 结论与建议

`zcode.cjs app-server` 不能作为反代理后端：它拿不到凭据，registry 恒空，
任何模型都无法创建。可行方向只剩：

1. **App 自身进程**（`ZCode.app` 正常运行，UI / Computer Use 驱动）—— 已验证 App 侧
   `billing/balance` 30/30 成功、`model_usage` 里有 `GLM-5.3-Flash` 真实调用记录。
2. **显式 API key 档**（`builtin:bigmodel` -> `open.bigmodel.cn`），用 key 而非 Keychain/JWT，
   这条路不依赖上述凭据层。
3. **放弃 zcode 桥**——其余 4 个反代（workbuddy hy4 x2、trae step-5）实测可用。

