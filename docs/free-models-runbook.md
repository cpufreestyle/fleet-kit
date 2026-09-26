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

- free-windows.json（仓库根）：providers（vendor/site/free/window/facts/sources/verified）
  + models（逐模型覆盖）+ gaps（选择器缺口原因）+ legend。改标注只改这个文件。
- tools/free_models.py：合并 free-windows.json + ocx models live --json +
  ~/.codex/cc-switch-model-catalog.json（slug）输出表格/JSON；--missing 输出缺口报告。
- tools/status_ui.py：新增「免费模型标注」区块（30s 缓存，subprocess 调
  free_models.py --json，失败不影响面板其他部分）。

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
- fleet_chat_test：6/9 PASS（workbuddy、workbuddy-gpt、qoder、trae、lingxi、xhx）。

## 复现命令

    python3 tools/free_models.py                 # 全量表
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
