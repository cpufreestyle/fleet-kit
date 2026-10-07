# trae2codex Runbook — Trae CN（SOLO）模型反代理到 Codex

日期：2026-09-25 | 状态：**全链路已通**（桥 / ocx / 官方 CLI 三处实测 200）

## 架构（实测）
```
Codex 官方 CLI/桌面 → ocx(:10100) → trae2codex 桥(:8791) → https://trae-api-cn.mchost.guru（Trae CN SOLO 通道）
                          ↑
              [model_providers.trae] config.toml (wire_api=responses) 用于官方 CLI 直连桥
```
- 桥：`~/trae2codex/trae_bridge.py`（FastAPI，复用 workbuddy2codex venv Python）
- LaunchAgent：`~/Library/LaunchAgents/com.local.trae2codex.plist`（KeepAlive，日志 `/tmp/trae-bridge.log`）
- 密钥：`TRAE2CODEX_KEY`（`~/.zshrc`；读取：`grep TRAE2CODEX_KEY ~/.zshrc | head -1 | sed -E 's/.*="([^"]+)".*/\1/'`）
- ocx provider：`trae`（openai-chat，base `http://127.0.0.1:8791/v1`，`allowPrivateNetwork:true`）
- 官方 CLI：`~/.codex/config.toml` `[model_providers.trae]` → `http://127.0.0.1:10100/v1` wire_api=responses
- Codex 目录：22 个 `trae/<model>` slug 已注入 `~/.codex/cc-switch-model-catalog.json`（每模型带 Trae 目录返回的真实 context window）

## 学习来源（开源项目）
- npm `dsh-connect-trae@2.3.0`（MIT，github.com/dingminhua/dsh-connect-trae，作者 dmh2002）
  ——「Connect locally signed-in Trae models to DeepSeek Harness」
- tarball：`/tmp/dsh-trae/package/lib/index.js`（4276 行，全部逆向逻辑）；本桥复用了它的：
  storage.json 解密算法、ExchangeToken 刷新契约、get_detail_param 目录解析（含 model_detail_list 上下文窗口、两 function 求并集）、
  llm_utils_chat 请求构造、命名 SSE → OpenAI chunk 转换

## 逆向结论（实测验证）
- **凭据解密**：`~/Library/Application Support/{Trae CN,TRAE SOLO CN}/User/globalStorage/storage.json` 的
  `iCubeAuthInfo://icube.cloudide` = base64 → 6 字节魔数（`746305100000`=aes）→ `random=buf[6:38]` →
  `first=sha512(random)` → `derived=sha512(first+salt)`，salt=`SALT_A^SALT_B`（或 `SALT_C^SALT_D`）→ AES-128-CBC
  （key=derived[0:16] iv=derived[16:32]）→ 明文前 64 字节 sha512 校验 → JSON（python 需手动剥 PKCS#7）
- **刷新契约**：`POST https://api.trae.cn/cloudide/api/v3/trae/oauth/ExchangeToken`
  `{ClientID:"ono9krqynydwx5", ClientSecret:"-", RefreshToken, UserID}` → `Result.{Token, UserJwt, RefreshToken, TokenExpireAt, RefreshExpireAt}`
- **目录**：`POST {chat网关}/api/ide/v1/get_detail_param` body `{function:"solo_work_remote"|"solo_work_lite", ...}` →
  `config_info_list[]`（remote+lite 求并集；glm-5.3 等 21-22 个可聊模型；已剔除 custom_model_* / 内部 agent 槽位）
- **聊天**：`POST {chat网关}/api/agent/v3/llm_utils_chat`（`Authorization: Cloud-IDE-JWT <token>` 等一整套 x-* 头）
- **坑（重要）**：ExchangeToken 返回的 `TokenExpireAt` 可能是**过去时间**（TokenExpireDuration 为负），但该 token 实际可用！
  前序模型据此误判"账号会话已死"。判活要用 `/health` 的 `session_alive` 探针（真打一次 get_detail_param），不要看 expiredAt。
  另：IDE 自带 DeviceInfo+DeviceProof 的 exchange 与裸 exchange 等价；IDE 若在后台跑刷新循环会和桥竞争 refresh token 轮换，必要时可关掉 IDE。

## 当前账号
- Trae CN 3.3.93，账号 **用户6781982309**（userId 4012546872851660，region CN）
- storage.json 内 token 显示过期，但刷新换发的 token 实测可正常聊天（glm-5.2 / kimi-k3 均 200，积分制）
- 凭据缓存：`~/.trae2codex/creds.json`（不覆盖 IDE 文件）

## 22 个模型（get_detail_param 实测）
`Doubao-Seed-Evolving`(224k) `Doubao-Seed-2.1-Pro`(224k) `Doubao-Seed-2.1-Turbo`(224k) `seed-code-pro-0430` `Doubao-Seed-2.0-Code`
`step-5-preview`(168k) `glm-5.3` `glm-5.2` `glm-5-turbo` `glm-5` `deepseek-v4.1-flash`
`DeepSeek-V4-Flash-Official` `DeepSeek-V4-Flash` `DeepSeek-V4-Pro-Official` `DeepSeek-V4-Pro`
`kimi-k3` `kimi-k2.7-code` `kimi-k2.6` `minimax-m3` `qwen3.8-max` `qwen-3.7-plus` + solo_work_lite 独有 1 个

## 运维命令
```bash
# 一键收尾（含验证）
~/trae2codex/finish_setup.sh
# 重启桥
launchctl kickstart -k gui/$(id -u)/com.local.trae2codex
# 手动验证
curl -s http://127.0.0.1:8791/health
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8791/v1/models
# 重新注入目录
TRAE2CODEX_KEY=$KEY <venv>/python ~/trae2codex/inject_catalog.py
# 官方 CLI 使用
codex exec -c model_provider=trae -m "trae/glm-5.2" "..."
```

## 排坑记录
1. `ocx provider add trae` 后配置回滚报 `baseUrl points to a loopback address` → 需在 `~/.opencodex/config.json` 的 trae 条目手动加 `"allowPrivateNetwork": true`，备份在 `~/.opencodex/config.json.invalid-*`
2. 新 provider 必须 `ocx restart` 才进路由表，再 `ocx models provider trae on` + `ocx sync`
3. `@app.get` 装饰器紧贴函数定义，插入新函数时容易把装饰器粘错函数（本次踩过，/health 曾返回探针元组）
4. Trae 是积分制，验证用 max_tokens 压到 32-64
5. Codex 桌面 app-server 缓存模型列表：`ocx sync --restart-codex`（会中断活跃回合，本次未执行）

## 未动/警告
- workbuddy 桥（8787 CN + 8788 GPT）、qoder 桥（8789）、codely 桥（8790）保持运行，未受影响
- Trae CN IDE 仍在后台运行（前序模型为排查登录态启动）；若其刷新循环与桥竞争 refresh token 轮换导致偶发 401，可 `osascript -e 'quit app "Trae CN"'`
## 2026-10-02 更新：账号池 + 配额冷却 + 双区目录（本轮修复）

架构位置已变：launchd 现在跑 runtime/bridges/trae/trae_bridge.py（kit 是源），改完必须
cp kit/bridges/trae/trae_bridge.py runtime/bridges/trae/ 再 cd runtime/bridges && ./finish.sh trae --skip-chat
（finish.sh 的 FLEET_HOME 取 runtime，在 kit/ 下跑会报 fleet.env not found）。

- 账号池：credential_pool() 汇总桥缓存 + 每套桌面 edition 的登录态，按 (region, account) 去重，
  活的排前、配额冷却中的垫底。本机实测两个账号：CN 用户6781982309（Trae CN / TRAE SOLO CN）、
  国际 Q Micheal（region=ai，edition=Trae）。/health 的 accounts[] 现在给出
  {account, region, edition, quota_dead}。
- 配额冷却：配额错误（HTTP 200 流里的 event:error Your requests have exceeded the quota.）命中即
  mark_quota_dead() 冷却 TRAE_QUOTA_COOLDOWN（默认 600s），顺延池内下一个凭证；流式请求在
  _peek_quota() 流头窥探阶段就完成换号，不会把半截响写给客户端（窥探读走的字节由 _ReplayResponse 接回）。
- 双区目录：get_catalog() 逐凭证 fetch_directory() 合并，模型带 region 标签；
  chat_completions() 用 region_first(pool, want_region) 让同区凭证优先——否则拿 CN 模型 id 打国际网关
  只会得到 param invalid。目录接口失败只跳过该凭证，不清空已有目录。
- 失败信息：循环内维护 notes，逐个凭证记录「配额耗尽(冷却 Ns)」「网络不可达(ExcType)」「上游 <code>」；
  400/422（请求体本身的问题）原样返回上游状态码，其他非 200 记账后 continue；全部不可用时 502 消息拼
  「：<notes>」。实测形态：
  trae upstream unreachable: ProxyError: 503 Service Unavailable：cn:用户6781982309 配额耗尽(冷却 600s)；ai:Q Micheal 网络不可达(ProxyError)
- 头净化：x-uid 只保留可打印 ASCII；account.username 是中文昵称时，混进 HTTP 头会让 httpx 抛
  UnicodeEncodeError，以前被误记成「网络不可达」，现在单独归类为「凭证非法」。
- 测试：tools/test_trae_quota_failover.py（13 个，含新加的 region_first / 双区目录合并 / 502 逐号点名）。

## 2026-10-02 实测结论（供运维判断）
- CN 账号 22 个模型配额耗尽（账号级，非代码问题）：502 里 cn 那条就是「配额耗尽」。
- 国际账号 Q Micheal 的网关 coresg-normal.trae.ai 本机不可达：本机系统代理（MacPacket :1082）没有国际出口，
  Tunnel connection failed: 503 Service Unavailable。这不是账号问题，修出口或给桥配代理后才轮得到它。
- 2026-10-03 续期一次：access token 从 2026-10-09 续到 2026-10-17，refresh token 轮换到 2027-04-01（备份 ~/.trae2codex/creds.json.bak-20261003-172845）。续期命令 `runtime/.venv/bin/python3 kit/tools/trae_renew.py`（默认 access 剩 7 天内才续，--refresh 强制，--json 机器可读）；桥自身仍只在会话末尾 5 分钟内自动刷新
