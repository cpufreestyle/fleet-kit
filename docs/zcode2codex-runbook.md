# zcode2codex（智谱 Z.AI Coding / ZCode）反代理 runbook

# 2026-09-28 CLI 路线打通记录（关键结论）

## 签名矩阵（逆向自 zcode.cjs byte 3711950）

    cRs({access, baseURL}):
      start-plan / off-peak           -> 不签名（直接可调）
      individual/team-coding-plan     -> V4 签名（X-Client-Sig/Pow/Nonce）
      zhipu-coding-plan-api-key       -> V4 签名

## 实测结论（本机, 2026-09-28 20:00-20:30）

1. **CLI 路线无 3012**：同一个请求，桥直连被 ESA 边缘 3012 拦，走
   zcode.cjs app-server 一样的 URL 直接到达应用层（3007 → 1005）。
   指纹差异在客户端 transport，不在 URL/头。
2. **captcha 票池有效**：CLI 请求带 x-aliyun-captcha-verify-param 后
   3007 消失，到达配额层（1005 exceed quota limit）。
   票据指纹绑定问题只影响「跨上下文搬票给直连 HTTP」这条路线。
3. **当前配额状态（硬事实，非代码问题）**：
   - start-plan: 1005 exceed quota limit（Weekend Build 免费额度耗尽）
   - team-coding-plan: 1113 Insufficient balance（V4 签名已通过，无余额）
   - billing/current: {"plans":[]} 证实套餐不在有效期
4. **V4 签名协议**（team-coding-plan 用）：
   - credential: <apiKeyId>.<secret>（credentials.json 里
     account-provider:coding-plan:...:api-key，恰好一个点）
   - handshake: POST /api/paas/c1f3a7e2/v2/client {apiKey, nonce, sig, ts}
   - 业务请求头: X-Client-Ts/Version/Sig/Nonce/App-Id/Pow + X-Session-Id
   - 握手失败 failOpen（sendUnsigned）；verify 两次被拒进 bypassSigning
5. **错误码语义**（zcode.cjs byte 4016776）：
   - 1005 = 配额耗尽（I4s 集合：不重试，换 provider）
   - 1113 = 余额不足
   - 3007 = captcha 被拒（start-plan 会自动 captcha-retry 重拿 headers）

## 桥内实现（Route A / Route B）

    /v1/chat/completions
      -> Route A: cli_backend.ask() 经官方 CLI（start-plan -> team 依次试）
      -> 配额全耗尽: 503 PLAN_EXHAUSTED（明确报套餐问题，不烧票不降级）
      -> Route B 降级: 原有 HTTP 直连（3012 风险仍在，仅兜底）

文件：kit/bridges/zcode/{cli_client,cli_backend,zcode_bridge}.py
      （kit 与 runtime 双份，md5 必须一致）

## captcha minter 环境

- 系统无 playwright 的解释器调 minter 会静默挂起：cli_client._mint_ticket
  的探测顺序是 `sys.executable`（桥自己就在带 playwright 的 venv 里）优先，
  然后 /usr/bin/python3（Xcode CLT）、`python3`；ZCAP_PY 可覆盖。
- **调用路径上的 mint 一律 `--headless`**：2026-10-02 修，之前每请求现场
  mint 是有头 Chrome，滑块窗直接弹到运营者脸上，40~60s 后失败再把人推去
  relay 页——验证就是这么反复跳的。无头 mint 解不了滑块，但它静默失败，
  由调用方回落成「开一次页换一张」。
- minter 的 Chrome profile 落在 `bridges/zcode/captcha_profile`（不再 /tmp，
  重启即清）：profile 每次重置等于设备指纹永远冷，无感验证必然不过。
- minter 改为**盯票池判断成功**：人工在页面存票即写池文件，minter 读池领取；
  原先读 `window.__capParam` 实测不可靠（人工存票成功它仍超时），自动拖滑块
  （`drag_slider`）从来没成功过、已删除。副作用是好事：任何浏览器在窗口期内
  存的票都会被这次 mint 捡走。
- mint 成功率间歇（traceless 卡 F001/F015），--serve 模式维持池子比
  每请求现场 mint 更稳；失败重试可成。

链路：Codex -> 本桥(:8800) -> https://zcode.z.ai/api/v1/zcode-plan/anthropic
      -> GLM-5.3-Flash / GLM-5.3（Start Plan 免费额度）

launchd：`com.local.zcode2codex`（plist 由 install.sh 生成，offset 13）
captcha 换取页：http://127.0.0.1:8910/ （launchd `com.local.zcode-captcha-relay`）


## 别反复跳验证（2026-10-02 修的那一圈）

症状：zcode 每次调用都跳一次验证——要么一个 Chrome 窗弹到脸上，要么被推去
127.0.0.1:8910 拖一次滑块，刚存好的票只够一发，下一发再来一圈。

根因是三个设计缺口叠出来的：

1. 池子空了以后，调用路径上的 mint 是**有头** Chrome（滑块窗当场弹）；
2. minter 的 profile 在 /tmp，重启/周期清理即重置，无感验证永远冷启失败；
3. relay 页每次打开 2.2 秒后自动唤起验证，且把票覆写进 captcha.txt——
   同一张 param 在池里一份、旧文件里一份，就是两次领取，第二次必 3007。

现在的形状：页面手动唤起、可连续备票进池；调用只从池里静默取票；池干时
无头 mint 静默失败一次，回落成「开一次页换一张」。备票的有效窗口按消费方
不同：CLI 路线（cli_client）600s 新鲜度 + 900s 寿龄，桥的直连路线 75s。
`tools/test_zcode_captcha_flow.py` 钉住这几条契约。

## 凭证

全部从 `~/.zcode/v2/credentials.json` 自动解密（safeStorage AES-256-GCM，
密钥 = sha256(`zcode-credential-fallback:<platform>:<home>:<user>`)，
见 `zcode_bridge.py` 的 `_decrypt`/`_safe_storage_key`）：

- `zcodejwttoken` -> `Authorization: Bearer`
- `oauth:zai:user_info` -> 账号（<your-account@example.com> / Q Micheal）

无需手填任何 key；`ZCODE2CODEX_KEY` 只是本桥自己的本地口令。

## 免费额度（2026-09-27 实测）

`GET /api/v1/zcode-plan/billing/current`：

| plan | status | entitlement | grant | 到期 |
|------|--------|-------------|-------|------|
| ZCode Weekend Build (`zcode-v3-start-plan-0924-wk-2`) | active | `model:glm-5.3-flash` | 300,000,000 tokens（一次性） | starts 1790390946 / ends 1790557200 |

`client/configs` 的 `startPlanPreview`（日常 Start Plan 档）：

| 模型 | 额度 |
|------|------|
| GLM-5.3 | 3,000,000 tokens/天 |
| GLM-5.3-Flash | 5,000,000 tokens/天 |

所以桥固定暴露两个模型：`GLM-5.3-Flash`（周末活动 + 日常都有）和
`GLM-5.3`（只有日常档；周末活动 capabilities 不含它，活动期内调用可能 4xx）。

## captcha（关键卡点）

上游对 `zcode-plan/anthropic/v1/messages` 的**每一次**调用都校验阿里云滑块：
sceneId `11xygtvd` / region `cn` / prefix `no8xfe`。

- `captchaVerifyParam` 一次性，用过即废；过期/失效时上游返回
  `{"code":3007,"msg":"captcha verify failed"}`。
- 换取：打开 http://127.0.0.1:8910/ 点「开始验证」完成滑块，param 写入票池
  `runtime/bridges/zcode/captcha_pool/<epoch>-<rand>.txt`（一个文件一张票，
  桥凭 claim-by-delete 领取）。页面不再自动唤起验证，也**不再覆写
  captcha.txt**——同一张票放两处就是两次领取，第二次必 3007。
- 桥在没有 param 时返回 `503` + `captcha_relay` 字段；param 失效时同样 503
  并附上游原文，方便判断是「该换了」还是「别的错」。

relay 页与 ZCode.app 内部 SDK 调用保持同构（`mode:'popup'`、真实 `button`、
`startTracelessVerification()`），但 2026-10-02 起**唤起是手动的**：点一下
「开始验证」才弹滑块，存完一张按钮立即可再点，页脚显示本次已存与池内总数，
一次备几张覆盖一段时间的调用。
（对齐 `onn()` 的 auto 分支）。

## 上游风控（当前未解决）

即使用 fresh captcha + 完整 X-ZCode-* 请求头 + Electron cookie，仍会撞：

    {"code":3012,"msg":"request has been blocked due to unusual activity."}   HTTP=405

判定为边缘 WAF（Aliyun ESA）按客户端指纹（TLS JA3 / Electron 网络栈）拦
非 ZCode 进程的请求。带上 cookie 后 3012 会消失、退回 3007，说明 cookie 是
必要条件之一，但不足以完全放行。**能稳定打通的唯一路径是让 ZCode.app 自己
发这个请求**（在 App 里开 remote-debugging-port 走 CDP，或驱动 UI），
桥目前只做到「凭证/额度/协议/错误分类全对，等放行」。

复现：

    bash /tmp/fk/ck.sh        # cookies + fresh captcha -> 3012
    bash /tmp/fk/exact.sh     # 同上去掉 cookie  -> 3012
    curl /api/v1/zcode-plan/billing/current   # 这个 GET 一直 200，不带 captcha

## 操作

    # 健康 / 模型 / 额度
    curl -s http://127.0.0.1:8800/health
    curl -s http://127.0.0.1:8800/v1/models
    curl -s http://127.0.0.1:8800/entitlements

    # 换 captcha
    open http://127.0.0.1:8910/

    # 重启桥 / 看日志
    launchctl kickstart -k gui/$(id -u)/com.local.zcode2codex
    tail -f /tmp/fleet-logs/zcode2codex.log

    # FleetKit 侧收尾（重启 + 列模型 + ocx sync + 冒烟聊天）
    bash kit/bridges/finish.sh zcode --home "$PWD/runtime"

    # Codex 选择器（ ocx 侧已注册 provider + 两个模型 + 别名）
    ocx models live --provider zcode --json
    ocx sync

## 已知差异 / 备注

- `api.z.ai` 的 coding-plan key（`fca8ee6d...`，credentials 里 team 和
  individual 两个账号同一个 key）在 anthropic/paas 两个端点上都是
  `429 [1113] Insufficient balance or no resource package`，不能用作备用通道。
- `bigmodel.cn` 国内端点同样 1113。
- off-peak 链路（`/api/v1/off-peak/ticket`，头 `X-Coding-Plan-Api-Key` +
  `X-Off-Peak-Ticket-ID`）不需要 captcha，但对本账号返回
  `403 [3101] coding plan is required`，未打通。
- `/v1/models` 端点在 zcode-plan 上不存在（404），模型清单由 entitlement +
  client/configs 合并而来，所以桥内置 `FREE_MODELS` 常量。

## 2026-09-27 captcha 排查：滑块不是瓶颈，上游服务端二次验证才是

结论先写：**别再拖滑块了**。用户侧滑块是对的、阿里云浏览器侧也确实放行
（`success` 回调触发、票据落盘），但 ZCode 服务端二次验证拒绝，最终从
`3007 captcha verify failed` 升级到 `3012 unusual activity` 风控拦截。

证据链：

1. **票据本身没问题**。用可控 Chrome 加载换取页，无感验证（`startTracelessVerification`）
   可零交互反复通过，票据 280 字节、解出 `certifyId/sceneId/isSign/securityToken`。
2. **sceneId/prefix/region 与线上一致**。App 包里根本搜不到 `11xygtvd` / `no8xfe`，
   它们来自 `client/configs`：`{"captcha":{"enabled":true,"prefix":"no8xfe",
   "region":"cn","sceneId":"11xygtvd"}}` —— 换取页用的就是这一组。
3. **不是出口 IP 不一致**。系统代理 `127.0.0.1:1082`，但 `curl` 直连与走代理
   的公网出口 IP 完全相同（`188.253.120.173`），浏览器与桥同机同 IP。
4. **不是过期**。10:45 存票、10:51 用（6 分钟）→ 3007；另测 0.05s 内立刻发 → 同样不是
   验证码问题（当时已升级成 3012）。
5. **真 App 从不报 3007**。`~/.zcode/v2/logs/2026-09-26.log` 有 66 条
   `"lastSdkEvent":"success"`，且全文搜不到 `captcha verify failed`。App 的做法是
   `send_preflight`：拿到票据**立刻**发请求（见 `provider-runtime-headers` requestId）。
6. **账号没被封**。同期 `billing/current` 与 `client/configs` 都 200，只有带验证码的
   `/anthropic/v1/messages` 路径被拦（3012），属该路径的独立风控。
7. 手动拖拽偶发 `{"success":true,"verifyResult":false,"verifyCode":"F001"}`：
   阿里云侧风控判定失败，与位置无关（位置对时也出现）。

### 顺带修掉的三个实现 bug

- **重发废票**（主因之一）：handler 用 `read_captcha()` 只看不销毁，每发一次请求
  就把同一张**已用过**的票再发一遍，天然必 3007。现改为 `take_captcha()`
  取用即销毁 + 最多 `ZCODE_CAPTCHA_RETRIES`(默认 3) 次换票重试。
- **过期票也照发**：新增 `ZCODE_CAPTCHA_MAX_FRESH`（默认 75s）与
  `usable_ticket()`。过期票不再送上游（白烧一张 + 给风控多留失败样本），
  池空时调 `captcha-mint.py` 现场 mint，mint 不出来就返回可执行提示。
- **`sys` 未导入**：`_mint_now()` 用了 `sys.executable` 却没有 `import sys`，
  一旦走到 mint 就是 NameError。
- 错误文案区分 `3007`（票据被拒）与 `3012`（风控，先停手、别再重试），
  之前一律说"captcha 过期，再拖一次"，会把人往错误方向带。
- `captcha-relay.py` 之前 `log_message` 静默，`/tmp/fleet-logs/zcode-captcha.log`
  永远是 0 字节，排查时看不到 `/save` 记录；现已输出到 stderr。

### 复测条件

`3012` 冷却后再验，且**一次一验**：换票立刻发（<75s），失败就停手。
若仍 3007，则可确认"非 App 场景产出的票据不被 ZCode 二次验证接受"，
届时应转向抓 App 自身成功请求做逐字段比对（`X-Device-Mid` 是可疑项：
App 必带且被 zod 校验为 UUID，桥目前完全不带）。


## 2026-09-27：3012 风控的真相与方案 C

排查结论：3012 `unusual activity` 是**阿里云 ESA 边缘网关按 TLS/JA3 指纹拦截**，
与出口 IP、captcha 票据、cookie、请求头均无关。判定依据：App 自身网络栈同时刻
30/30 成功，而 renderer fetch（含 App 进程内铸出的有效 captcha）100% 被拦。

因此继续在 Python `urllib` 桥里换 IP、换票据、补 header **都不可能修好**。
唯一可行方向是让 Node/Electron 原生栈代发，即随 App 分发的 ZCode CLI。

完整的 CLI stdio 协议逆向、账号授权调用、以及本地尚未走通的 provider registry
一环，见 [zcode-cli-protocol.md](zcode-cli-protocol.md)。
封装好的无头客户端：`bridges/zcode/cli_client.py`。

在 registry 一环解决前，本桥的 `zcode/GLM-5.3` 与 `zcode/GLM-5.3-Flash`
会以 503 失败；按用户约定**不在选择器里隐藏它们**。


## 2026-09-28：唯一卡点是一次 OAuth 登录（不是风控，也不是票据）

结论先写：ZCode.app 的登出流程会删掉 zcodejwttoken（asar 里
xue(e){return e==="zai"||e===Ne} = shouldClearZcodeJwtOnLogout，登出即删）。
18:00 那次 oauth.logout + clearCodingPlanWebviewStorage + relaunchApp 之后，
~/.zcode/v2/credentials.json 里只剩一个 49 字符 opaque key
（account-provider:coding-plan:account:zai-team-coding-plan:account:8f26e3e6-...:api-key），
而 zcode-plan 上游只认 JWT：同一个 key 下 /api/v1/client/configs 200，但
/api/v1/zcode-plan/billing/current 401 空 body。所以过去桥只报裸 upstream 401；
现在 /health 多一个 auth 字段，未登录直接 503 + 可执行修复命令。

### 恢复步骤（一次点击，之后自动）

    node "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs" login --no-browser
    # 在浏览器打开它打印的 URL：
    # https://chat.z.ai/api/oauth/authorize?client_id=...&state=...&response_type=code

- JWT 落盘后不需要重启桥：read_token() 每请求重读凭证。
- 本环境里 nohup ... & 会立刻退出，所以用 launchd 挂着等回调：
  ~/Library/LaunchAgents/com.local.zcode-login.plist
  （RunAtLoad，URL 打印到 /tmp/fleet-logs/zcode-login.log）。
- CLI 是 zcode 0.16.9，与 App 读同一份 ~/.zcode/v2/credentials.json（n$s() 硬编码该路径）。

### 顺带修掉的两处

- 探针一直把 zcode 打到 8798（qwen 的口）：tools/fleet_probe.py 把 zcode 放在
  GATEWAY 里，端口走 plist_port()，落回 qwen 的 offset。现已移出 GATEWAY、
  加进 PORTS: 8800 与 KEY_ENV: ZCODE2CODEX_KEY；实测
  DOWN zcode 8800 GLM-5.3-Flash: HTTP 503（503 = 未登录），端口终于对了。
- tools/catalog_sort.py 里 provider_of() 重复定义 3 次，删到 1 处，并同步到
  runtime/（两边不一致会让排序行为对不上）。

## 2026-09-28 无头 CLI 路线（方案 C）进展

- zcode --prompt ... --json 能起来，但报 Error: Model creation failed。
- 真因在 ~/.zcode/cli/log/zcode-2026-09-28.jsonl 的
  zcode_protocol.provider_registry.ready：accountRevision 里 8 个 provider
  全是 entitled:false，~/.zcode/v2/coding-plan-cache.json 也全是
  coding_plan_not_entitled —— 与 JWT 缺失完全一致，登录后这个错应当消失。
- ZCode Built-in skipped (lease-held) 是正常提示，不是错误：
  ~/.zcode/v2/runtime/provider/darwin-aarch64/<ver>/endpoint-<sha256>/zcode-builtin-refresh.json
  的 leaseUntil 在 2026-11 之后，本实例直接用本地缓存的 zcode-builtin.json
  （186KB，provider/model 规则齐全）。
- 无头跑 provider 注册要用 bridges/zcode/_provider_env.py 导出
  ZCODE_BUILTIN_PROVIDER_CONFIG_FILE / ZCODE_BUILTIN_PROVIDER_BUNDLED_CONFIG_FILE /
  ZCODE_PERSONAL_PROVIDER_CONFIG_FILE / ZCODE_DATA_BASE_DIR，
  否则 registry 里一个 provider 都没有。

