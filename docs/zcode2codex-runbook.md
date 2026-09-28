# zcode2codex（智谱 Z.AI Coding / ZCode）反代理 runbook

链路：Codex -> 本桥(:8798) -> https://zcode.z.ai/api/v1/zcode-plan/anthropic
      -> GLM-5.3-Flash / GLM-5.3（Start Plan 免费额度）

launchd：`com.local.zcode2codex`（plist 由 install.sh 生成，offset 13）
captcha 换取页：http://127.0.0.1:8910/ （launchd `com.local.zcode-captcha-relay`）

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
- 换取：打开 http://127.0.0.1:8910/ 完成滑块，param 自动写入
  `runtime/bridges/zcode/captcha.txt`（桥每次调用现读现取）。
- 桥在没有 param 时返回 `503` + `captcha_relay` 字段；param 失效时同样 503
  并附上游原文，方便判断是「该换了」还是「别的错」。

relay 页刻意与 ZCode.app 内部 SDK 调用对齐：`mode:'popup'`、真实
`button` 元素、先 `startTracelessVerification()` 再 8s 回退按钮点击
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
    curl -s http://127.0.0.1:8798/health
    curl -s http://127.0.0.1:8798/v1/models
    curl -s http://127.0.0.1:8798/entitlements

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
