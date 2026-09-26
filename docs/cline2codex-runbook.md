# cline2codex Runbook — Cline 免费模型反代理

日期：2026-09-26 | 状态：**逆向完成，推理路由未打通**（阻塞：Cline 无 OpenAI 兼容 chat 端点）

## 架构（目标）
```
Codex → ocx(:10100) → cline2codex 桥(:8799) → api.cline.bot → 上游模型
```

## 逆向结论（全部有据可查）

### 凭据（已验证可用）

- 存储：`~/.cline/data/settings/providers.json` → `providers.cline.settings.auth`
  - `accessToken`：WorkOS JWT，**有效期仅约 1 小时**
  - `refreshToken`：长期有效，**可反复换新票**
  - `accountId`：`usr-01M33YFPFXS4J4ZDRTBC3DYGAB`，账号 `cpufreestyle@gmail.com`
- 刷新端点（实测 200）：`POST https://api.cline.bot/api/v1/auth/refresh`
  - body：`{"refreshToken": "...", "grantType": "refresh_token"}`
  - 返回：`{data:{accessToken, tokenType, expiresAt, refreshToken, userInfo}, success:true}`
- 换新票逻辑来自开源仓库 `sdk/packages/core/src/auth/cline.ts` 的 `refreshClineToken()`，与实测一致。

### 端点（实测）

同一个刚刷新出来的 token，同时打两类端点：

| 端点 | 结果 |
|---|---|
| `POST /api/v1/auth/refresh` | 200 |
| `GET  /api/v1/ai/cline/recommended-models` | 200 |
| `GET  /api/v1/models` | 200（**458 个 OpenRouter 全量市场**） |
| `POST /api/v1/chat/completions` | **401** |
| `POST /api/v1/language-model` | **401** |

**注意：上表的 200 全都不证明凭据有效。** 同一批 URL 换成 Bearer garbage-xxx 再打：
`/api/v1/models` 与 `recommended-models` 依然返回 200 —— 它们是公开端点，压根不校验 token。
唯一的真判据是 `/api/v1/users/me`：拿有效 token 打它，同样 401。

而凭据链本身是健康的，三条独立证据：

1. `POST /api/v1/auth/refresh` 200，可反复换新票。
2. accessToken 是合法 WorkOS OIDC JWT（RS256，kid=sso_oidc_key_pair_...，
   iss=https://api.workos.com/user_management/client_01K3A541FN8TA3EPPHTD2325AR）。
3. `POST https://api.workos.com/user_management/authenticate` 用同一个 refreshToken 也 200。

修正后的结论：401 与凭据无效、token 过期都无关。
api.cline.bot 有一层全局 auth 中间件，非白名单路径一律用同一条文案挡掉
（连 `/api/v1/definitely-not-a-real-path-xyz` 也是同样的 401，
所以 401 甚至说明不了路径是否存在）。
而那枚 WorkOS JWT 只是给 Cline 自家后端识别「你是谁」用的；
Cline 的模型网关（OpenRouter 代购层）要的是下游 provider key，由运行时注册表经 hub 注入。


### 关键证据

- `code-sidecar` 二进制里 Cline 自家 `/api/v1/*` 路径共 20 个（放宽到 120 字符重扫，
  仍是同一批，只是补全了 `/api/v1/session/${...}` 这类模板后缀），
  其中**没有任何无状态的 chat/messages/completions 推理端点**；
  最接近的推理入口是 `/v1/sessions/${id}/events/stream` 与
  `/v1/sessions/${id}/threads/${id}/stream`（SSE 流），但它挂在 session 生命周期上，不是 HTTP 单次调用。
- `createCline()` 用标准 `createOpenAICompatible`（baseURL `https://api.cline.bot/api/v1`，会自动拼 `/chat/completions`），
  header 只有 `Authorization: Bearer`，无定制头。但它的 key 来自 `apiKeyResolver`——
  该 resolver 的赋值源在二进制里搜不到，由运行时注册表（`GatewayRegistry.configureProvider`）动态注入，
  而注册表通过 hub WebSocket（`ws://127.0.0.1:<port>/hub`）下发。
  `~/.cline/data/logs/hub-daemon.log` 可见 `client.register` / `session.list` / `schedule.list` 一整套 hub 命令。
- Cline 的 agent 推理走 `/api/v1/session` 云端任务 + WebSocket（hub），不是无状态 chat 接口。
- `/api/v1/models` 的 458 个是 OpenRouter 市场（`openrouter/free`、`:batch` 后缀为 OR 特征），
  与 Cline 自有模型（`cline-free/*`、`cline-pass/*`）是两套命名空间，且 Cline 自有模型**不在** `/models` 里。

## 可用成果：免费模型目录（已提取）

`bridges/cline/free_models.json` — 从 app 内置 catalog 提取，含 contextWindow / maxTokens / capabilities / 计费档：

| 模型 ID | 上下文 | 最大输出 |
|---|---|---|
| `cline-free/deepseek-v4.1-flash` | 1,048,576 | 384,000 |
| `cline-free/muse-spark-1.3-contributor` | 1,048,576 | 943,718 |
| `z-ai/glm-5.3-flash` | 1,310,720 | 943,718 |
| `cline-free/solar-pro4` | 524,288 | 131,072 |
| `poolside/laguna-s-2.1:free` | 262,144 | 32,768 |

再生方式：

    python3 - <<'PY'  # 从 /Applications/Cline.app/Contents/MacOS/code-sidecar 提取
    ...
    PY

## 已排查且为空的线索（别再重复）

为找那个运行期注入的 provider key，以下位置全部查过，**都没有**：

| 位置 | 结果 |
|---|---|
| `~/.cline/data/settings/global-settings.json` | 只有 autoUpdate/telemetry/web_search |
| `~/.cline/data/db/*.db`（9 个库全表枚举） | 无 provider key |
| `~/.cline/data/cache/feature-flags*.json` | 只有开关 |
| macOS Keychain（`security dump-keychain`） | 无 cline 条目 |
| `code-sidecar` 静态字符串 | 无 key 赋值源 |

各库里唯一有价值的发现：`sessions.db`.`sessions` 有 `provider` / `model` 两列，
实际记录是 `provider=cline` + `model=anthropic/claude-sonnet-5`（不是 `cline-free/*`）；
`tasks.db`.`agenda_tasks` 有 `model_selection_json` 列，但表为 0 行。

另有一条容易误解的：`~/.cline/data/sessions/<id>/*.messages.json` 是从 Codex
**导入**的历史会话（`metadata.importedFrom.sourceProvider=openai-native`），不是推理记录。

## 本轮突破：绕开 HTTP 网关，直连本地 hub（可行路径已找到）

不再需要用户在 App 里点任何东西——**hub daemon 是本地 WebSocket 服务，可直连并驱动推理**。

### 连接方式（已实测全部跑通）

启动 App 后 `open -a Cline`，hub 会监听 `127.0.0.1:25463`：

    pgrep -fl 'cline-hub-daemon'   # ... --cline-hub-daemon --cwd / --host 127.0.0.1 --port 25463 --pathname /hub

凭据不用猜，就在 discovery 文件里：

    ~/.cline/data/locks/hub/production.json
      -> { url, port, pathname, authToken, capabilities[27 个], coreVersion }

鉴权：`authToken` 走 WebSocket 子协议，**必须带前缀** `cline-hub-auth.`；
或者 HTTP 用 `Authorization: Bearer <authToken>`（`/status`、`/drain` 走这个）。

    HUB_AUTH_PROTOCOL_PREFIX = "cline-hub-auth."

另有一条捷径：loopback + `Origin: http://127.0.0.1:25463` 也能通过
（`isLocalHubHostName(host) && isLocalHubOrigin(origin)`），但带 token 才是正道。

### 协议（v1，抓包+源码双向确认）

**帧不是裸 envelope，必须包一层**，这是最容易踩的坑：

    -> {"kind":"command","envelope":{"version":"v1","command":"...","clientId":"...","requestId":"...","payload":{...}}}
    <- {"kind":"reply","envelope":{...,"ok":true,"payload":{...}}}
    <- {"kind":"event","envelope":{...,"event":"...",...}}

`client.register` 必填字段（缺一个就静默无响应）：

    clientId, clientType, displayName, transport, actorKind, capabilities[],
    workspaceContext:{workspaceRoot, cwd}

### 已实测可用的命令

| 命令 | 结果 |
|---|---|
| `client.register` | ok |
| `hub.status` | ok（含 activeRpcTurns / eventLog.lastSequence） |
| `client.list` | ok（能看到 sidecar 与 observer 两个真实 client） |
| `session.create` | ok（返回 sessionId） |
| `run.enqueue` | ok（返回 runId + queuePosition） |
| `run.list` | ok |
| `session.attach` | ok |
| `settings.get` | `not_implemented`（服务端没实现，非我方问题） |

`run.enqueue` 的 `sessionId` 放在 **envelope 顶层**（不是 payload 里），
payload 用 `prompt` 或 `input` 均可。

### 还差的两点（当前阻塞）

1. **run 立即 failed**：`run.list` 显示 `state:failed`、`error:"Run finished with an error."`，
   从 acceptedAt 到 endedAt 只隔 20~40ms，不是网络超时而是立即拒绝。
   根因：session 创建时 `model` 固定为 `{providerId:"hub", modelId:"hub"}` 占位，
   而我传的 `modelSelection:{providerId:"cline",...}` 没有被采纳；
   真正的 model 选择要走 `session.update` / `setModel`，或经 `settings.set`。
2. **收不到事件流**：`publish()` 只投递给在HubClient 里 `subscribe()` 声明过的 listener，
   `session.attach` 只做 participant 管理、不建立事件订阅。
   需要在命令外加订阅声明（客户端库 `subscribe(listener,{sessionId})` 对应的 wire 动作），
   或退而用 `run.list` 轮询 + `session.get` 读快照。

两者任一解决即可拿到真实模型输出；建议先攻第 1 点（model 选择），
   因为 run 失败时根本不会有输出可看。

### 现成资产

- 探测脚本：`/tmp/clh/hub12.py`（register → create → attach → enqueue 全流程）
- 帧落盘：`/tmp/clh/hub12_dump.jsonl`
- 关键源码位置（`code-sidecar` 内）：
  - WS 鉴权：`startHubWebSocketServer` / `readWebSocketAuthToken` / `isValidHubAuthToken`
  - 帧分发：`switch (frame.kind) { case "command": ... }`
  - 命令表：`HUB_CAPABILITIES`（27 个）
  - publish：`publish(event)` 只遍历 `this.listeners`
  - run 入口：`handleRunEnqueue` → `queue.admit` → `executor.pump`

## 参考

- 开源仓库：`github.com/cline/cline`（浅克隆在 `/tmp/cline-src`）
- refresh 实现：`sdk/packages/core/src/auth/cline.ts` → `refreshClineToken()`
- provider 定义：`code-sidecar` 内 `createCline()` / `createClineProviderModule()`

