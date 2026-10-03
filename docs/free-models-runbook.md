# 免费模型标注 + 选择器缺口（2026-09-26 实测记录）

## 目标

把所有反代理模型的免费状态和时间段标注出来（依据各服务官网），并解释「为什么有的
模型不在 Codex 选择器里」。

## 数据来源与抓取方法

各官网定价页多为 SPA（HTML 只有壳），价格/活动文案在 JS chunk 里。方法：官网首页
HTML 提取 script/link 的 .js 资产并下载，按关键词（免费/限免/限时/公测/额度/定价/
订阅/峰谷/闲时/intlFree/Community…）提取上下文。tokendance 的 changelog/docs 内容
直接内嵌在其 index JS 中。

| 来源 | 抓到的事实 |
|------|-----------|
| www.trae.cn/pricing | 免费计划 ¥0：「所有功能均可免费使用」「支持 2 个云端任务同时执行」 |
| www.trae.ai/pricing | Free $0：仅 Auto 模式、Limited usage、5000 次/月补全、2 并发云任务；Pro $20/月、Pro+ $60、Ultra $200 |
| www.codebuddy.cn/pricing | meta：「限时免费个人版、限时免费企业旗舰版、企业专享版」；JS：Hy4 preview「邀您免费体验，限免两周」badge 8.28-9.10；起止 2026-09-10 00:00:00 到 2026-09-23 23:59:59（国内） |
| www.workbuddy.ai/pricing | 同上海外版：起止 2026-08-28 00:00:00 到 2026-09-23 23:59:59；DeepSeek-v4.1-flash 独家首发限时折扣；intlFree 免费计划 + Pro 试用 |
| codely.tuanjie.cn（index JS） | 账户余额 = 可用点数 + 充值点数 + 月度免费点数（扣减时优先扣月度免费点数）；套餐 Lite/Pro/Max + 团队标准/高级/旗舰/尊享；FAQ：Lite/Pro/Max 每 5 小时 + 每周额度限制，Ultra 无 5 小时窗口仅受周/月额度约束；10月12日前限时优惠 |
| lingxi.regaing.com（index JS） | 「登录后即可免费使用」；7 天滚动额度 + 5 小时窗口 + 30 天额度；加量包 每 100 灵力、30 天有效、限时优惠；订阅 元/月起 |
| qoder.com + /pricing | FAQ：新用户 2 周免费 Pro 试用（全 Pro 功能）；到期升级或不操作自动降级 Free；credits 计量；i18n 有 Pricing.CommunityEdition = Free |
| tokendance.space（index JS） | changelog 2026-08-24：DeepSeek V4 峰谷定价，空闲时段价格为高峰 50%。DeepSeek 官方端点高峰 = 北京时间周一至周五 09:00-12:00、14:00-18:00；百度千帆/阿里云端点（仅 deepseek-v4-flash-0731）高峰 = 每天 08:00-22:00；其余时间空闲自动切换。实名公告提到平台「含免费模型」（清单需登录控制台） |
| www.xiaohuanxiong.com | SPA 壳，未抓到价格/免费信息 -> unknown |
| codeassist.google.com | 未抓到限额数值 -> 按「免费档月度限额 + 用户 AI Pro 订阅」定性标注 |
| catpaw.sankuai.com | 503 Tunnel（需 VPN）-> blocked |

## 工具与数据

- free-windows.json（仓库根）：providers（vendor/site/free/window/credits/credits_note/facts/sources/verified）
  + models（逐模型覆盖，同样可带 credits/credits_note）+ gaps（选择器缺口原因）
  + legend（free 口径 + credits 口径）。改标注只改这个文件。
- tools/free_models.py：合并 free-windows.json + ocx models live --json +
  ~/.codex/cc-switch-model-catalog.json（slug）输出表格/JSON；--missing 输出缺口报告；
  --credits <kind> 只看某一种积分口径。
- tools/status_ui.py：「模型标注」区块（30s 缓存，subprocess 调
  free_models.py --json，失败不影响面板其他部分），多一列「客户端积分」，
  单元格 title 显示 credits_note，并有「只看走客户端积分」开关。

## 客户端积分口径（2026-09-29 新增）

一列 `credits`，回答「这次调用会不会消耗被反代理客户端账号里的额度」：

| 值 | 含义 | 证据 |
|----|------|------|
| `client` | 走客户端积分，可扣完 | bridges/workbuddy/core.py 读 `credits`/`discountedCredits`；tools/checkin.py 读 xhx `available_points`/`daily_points`；codely/lingxi/zcode 桥与官网额度说明 |
| `limit` | 不走积分，只占账号免费限额 | Trae 官网「限量使用」；Gemini CLI 官方文档 60 次/分、1000 次/天；Cline free 家族 input/output 计费 0 |
| `own` | 不走客户端积分 | tokendance / stepfun 独立 API Key 按量；openai 为 ChatGPT 原生订阅 |
| `unknown` | 无法核实 | catpaw 需美团 VPN |

排查额度耗尽：`python3 tools/free_models.py --credits client`；
要区分「免费但限流」和「按量扣费」：对比 `--credits limit` 与 `--credits own`。

当前规模：live 173 + catalog 14（qoder，代理未重载）+ 1 原生 = 188 行；94 个在选择器。
分类：FREE 22（trae）/ LIMITED 26（workbuddy 系）/ QUOTA 10（codely 8 + lingxi 2）/
TRIAL 14（qoder）/ SUB 12（gemini 4 + 原生 8）/ PAID 95（tokendance）/ N/A 9（xhx）。

## 选择器缺口归因

1. tokendance 95 live 只有 1 个在选：ocx provider 的 selectedModels 只有
   step-5-preview。批量加入：ocx models selected tokendance --set <id,id> 然后 ocx sync。
2. qoder 15 live 0 个在选（本日修复）：桥 8789 一直健康，但 qoder 从未注册进 ocx
   （~/.opencodex/config.json 的 providers 列表里没有它）。opencodex/setup-providers.sh
   其实包含 qoder（本机当初漏注册）。已手动补：
   ocx provider add qoder --adapter openai-chat --base-url http://127.0.0.1:8789/v1 --api-key <plist 里的 QODER2CODEX_KEY> --allow-private-network
   catalog 80 变 94。qoder 桥强制校验 QODER2CODEX_KEY（plist 里有、fleet.env 里没有），
   占位 key 会 401；setup-providers.sh 已加 plist 回退取 key。
   已 ocx service restart 让代理加载 qoder：live=14，经代理聊天实测返回 OK。
3. openai 原生 7 个：native 模型，Codex/ChatGPT 账号直管，按设计不进 catalog。
4. catpaw：需美团 VPN，8795 不可达，live=0。
5. 选择器显示名不带 provider 前缀（hy4-preview），catalog slug 才带（workbuddy/hy4-preview）。

## 同日发现的其它问题（均为用户侧条件）

- tokendance API key 失效（影响默认模型 step-5-preview）：直连
  tokendance.space/gateway/v1/chat/completions 返回 401「API 密钥不存在」；
  /v1/models 列表端点是公开的所以照样列得出。需控制台重建 key 后
  ocx provider add tokendance ... --force + ocx sync。usage.jsonl 显示 00:02 起全部 401。
- gemini 桥 502：Google token refresh failed，需重登。
- codely chat 400：onboarding 门禁（models/key 正常）。
- fleet_chat_test：5/10 PASS（workbuddy、workbuddy-gpt、qoder、trae、xhx）；
  codely 400 / lingxi 401 / gemini 502 / catpaw 502 / antigravity 90s 超时五项失败，
  后三项均为网络侧（cloudcode-pa、VPN），详见 README「已知问题」。

## 复现命令

    python3 tools/free_models.py                 # 全量表（免费 + 客户端积分两维）
    python3 tools/free_models.py --credits client # 只看走客户端积分的模型
    python3 tools/free_models.py --missing       # 缺口报告
    python3 tools/free_models.py --json          # 面板同源数据
    python3 tools/free_models.py --check-sources # 官网可达性
    python3 tools/fleet_chat_test.py             # 全舰队聊天实测
    ocx service restart                          # 让代理加载新 provider（如 qoder）

## 选择器短名（2026-09-26）

问题：catalog 的 display_name 默认等于 slug，最长 36 字符（xhx/xhx-sn-sensenova-6-8-
flash-lite），Codex 选择器里显示不全。

方案：ocx alias（ocx alias set / 管理 API PUT /api/providers/<p>/model-aliases）。
显示名规则：有模型别名时显示 <provider 别名>/<模型别名>，否则保持完整 slug——
所以每个模型都要配别名，provider 别名才会透显。别名与模型 id 相同（大小写不敏感）会
409 collision，脚本用「去连字符」兜底（glm-5.2 → glm5.2）。

坑（已写进脚本）：
1. trae/xhx/lingxi 桥 advertised 的模型 id 自带斜杠（trae/DeepSeek-V4-Flash），
   catalog slug 是连字符形式（trae/trae-DeepSeek-V4-Flash）；别名键必须用原生 id
   （斜杠形式），脚本从 ocx models live 取原生 id。
2. 之前用连字符键设的别名会被 API 标 stale:true 且占用别名值导致 409，脚本的
   prune_stale_aliases 会清掉这类键。
3. 别名 value 在 provider 内必须唯一。

结果：94 个 catalog 条目全部 <=20 字符（最长 trae/sd-code-pro-0430 = 21）；
路由不变（slug 未动），经代理聊天实测 OK。

## Gemini 档位定性（2026-09-26 官网确认）

问：gemini 模型是免费的还是套餐里的？

答：两者都不算——我们桥用的是 Gemini Code Assist 个人消费者档，它本身就是免费档：
个人 Google 账号登录即有额度（官方 Gemini CLI 文档：60 次/分、1000 次/天），不按
token 计费；Google AI Pro/Ultra 订阅只是把这个免费档的限额抬高，不占订阅内独立额度。

但官方弃用页（2026-09-02 更新）明确：2026-06-18 起 individuals / Google AI Pro /
Google AI Ultra 档全面停止服务（含 Gemini CLI，Login with Google 入口关闭），消费者
账号需迁移 Antigravity；仅 Code Assist Standard/Enterprise（GCP 付费订阅）不受影响。
本机桥 502 refresh failed 的根本原因即此——不是 token 过期，是登录链路被关。

出路：迁 Antigravity（本机已装 /Applications/Antigravity.app，可仿舰队模式建桥），
或改用 Google AI Studio API key（ocx registry 有 google 条目，AI Studio 有免费额度）。

## OpenAI 原生模型的账号档位差异（2026-09-27 实测）

`openai` provider 走 `chatgpt.com/backend-api/codex`（ChatGPT 账号 OAuth 转发）。
同一个账号下，模型可用性分三档：

| 模型 | /v1/chat/completions | 结论 |
|------|---------------------|------|
| `gpt-6-luna` | 200 pong | 可用 |
| `gpt-5.6-terra` | 200 pong | 可用 |
| `gpt-5.6-luna` | 200 pong | 可用 |
| `gpt-6-sol` | 400 `The 'gpt-6-sol' model is not supported when using Codex with a ChatGPT account.` | 账号档位不够 |
| `gpt-6-astra` | 400 同上 | 账号档位不够 |
| `gpt-5.6-sol` | 400 同上 | 账号档位不够 |
| `gpt-5.5` | 404 `does not exist or you do not have access to it.` | 已下线/无权限 |

注意这跟 `Codex` 内部报的是同一条文案：不是桥的问题，是 ChatGPT 账号
（Plus / Pro / Team 档位）没开这些模型。

处理：`ocx models disable <id> --native` 把这几个从选择器摘掉，
否则选了下单就是 400。已禁用的 4 个：gpt-6-sol / gpt-6-astra /
gpt-5.6-sol / gpt-5.5。剩下的 gpt-6-luna / gpt-5.6-terra / gpt-5.6-luna
实测通过。

不要用 `catalog-filter.sh --hide` 去盖这件事：它的判据是
`real_calls.json` 的桥 verdict，只认反向代理桥，不认原生模型，
跑一次会把 cline/gemini/antigravity/catpaw 一起扫掉（216 -> 136）。

## 免费额度时段 = 算出来的状态（2026-10-03）

以前时段只活在 `window` 自由文本里，手写的「（已结束）」会在表里躺到不再为真，
而一个死掉的限时折扣看起来和长期免费档一模一样。现在 `window_start` /
`window_end` / `window_standing` 三个结构化字段由人工显式填写，
`free_models.py` 据此把时段算成 LIVE / SOON / EXPIRED / STANDING / `?`
一列 badge（时间一律按北京时间，时刻取当前系统时间）。

### 字段怎么写（放在 window 键后面）

| 字段 | 含义 | 例 |
|------|------|----|
| `window_start` | 窗口起点，ISO 8601；裸日期按上海时区解析（`2026-09-10` = 09-10T00:00+08:00） | `"2026-09-10T00:00:00+08:00"` |
| `window_end` | 窗口终点；当前时间过了它状态即 EXPIRED | `"2026-09-23T23:59:59+08:00"` |
| `window_standing` | `true` = 官网写明长期免费/无固定截止；这是断言，不是推断 | `true` |
| `window_why` | 这个口径的出处（官方弃用页、首发折扣同窗口…） | 见 free-windows.json |

优先级同 credits 口径：模型级覆盖 provider 级；给了日期 `standing` 就不生效
（workbuddy provider 是 standing，但 hy4-preview 模型带了日期，结果算 EXPIRED）。

### 为什么不能从 window 文本解析日期

`window` 一格里混着两种东西：真额度时段（「限免两周：2026-09-10 → 2026-09-23」）
和实时状态记录（「2026-10-02 实测账号级额度用尽」——这是测量日期）。解析文本会把
测量日期当成额度窗口，造出根本不存在的假窗口（antigravity 会变成
2026-10-02 → 2026-10-02）。所以字段只由人工/脚本显式写，代码只读结构字段；
`tools/test_free_windows.py::test_status_record_dates_stay_measurement_dates`
守住这条决策。

### --refresh

    python3 tools/free_models.py --refresh               # 全量重探测
    python3 tools/free_models.py --refresh --provider trae

会真的改写 free-windows.json，写前先落一份 `free-windows.json.bak-<时间戳>`
（`*.bak*` 已 gitignore）。每个 provider 打 `last_checked` 时间戳；`verified`
的语义是「至少一个官方来源今天还答 200」，全部不可达才翻 false——一个死链挨着
一个活链不推翻今天还读得动的记录。2026-10-03 实测：16 个 provider，13 个可达
（HTTP 200），catpaw（美团 VPN）/ openai / qwen 不可达 → verified=false。

### 哪些条目故意留 `?`

trae 的 22 个模型和 antigravity 的 12 个（2026-06-18 是 Code Assist 关停，不是
antigravity 的窗口）、qoder（「2 周试用」无绝对日期）、codely/lingxi（滚动额度无
截止）、xhx/stepfun/openai/qwen/catpaw/tokendance。`?` 的意思是「没人查过」，
不是「没有窗口」。

### 实测输出（2026-10-03）

    window states: EXPIRED=7  STANDING=68  ?=195

EXPIRED 7 = gemini 4（Google 2026-06-18 起停服 individuals 档）+
workbuddy/hy4-preview + workbuddy-gpt/hy4-preview + zcode/GLM-5.3-Flash。
`--free-only` 下 cline 12 行全 STANDING、codely/lingxi/qoder 全 `?`。
回归：`tools/test_free_windows.py`（27 例）+ `tools/test_free_models_credits.py`
（2 例）。注意 `tools/test_no_undefined_names.py` 会全仓扫描，他人未提交的
账号池改动（node_credits.py 引用未定义的 `_pool_accounts_row`）会让它红，
与本节无关。
