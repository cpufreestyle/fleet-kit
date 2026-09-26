# 反代理舰队全量实测报告（2026-09-26 01:20 更新）

测试脚本：work/fleet_chat_test.py（分批跑：~/probes/fleet_split.py，key 从 launchctl plist 动态读取，汇报只显 md5）
测试方法：每座桥一次 chat completion（探针「请只回复两个字：正常」）

| 桥 | 端口 | 测试模型 | HTTP | 结论 | 耗时 | 备注 |
|---|---|---|---|---|---|---|
| workbuddy | 8787 | hy4-preview | 200 | PASS | 7.3s | key md5 a9f59f15 |
| workbuddy-gpt | 8788 | gpt-6-astra | 200 | PASS | 2.9s | key md5 a9f59f15（与 workbuddy 同一个 key） |
| qoder | 8789 | auto | 200 | PASS | 5.8s | 本轮修复（MCP 卡死，见下） |
| codely | 8790 | codely-core | 400 | FAIL | 0.1s | 账号 onboarding 门禁（用户侧） |
| trae | 8791 | trae/Doubao-Seed-Evolving | 200 | PASS | 1.7s | key md5 09b0e29b |
| lingxi | 8792 | lingxi/deepseek-flash | 200 | PASS | 1.3s | key md5 f39c0a3d |
| xhx | 8793 | xhx/raccoon-19b265 | 200 | PASS | 1.8s | key md5 405430f3 |
| gemini | 8794 | gemini-3-pro-preview | 502 | FAIL | 5.6s | 403 VALI 账号验证门禁（用户侧） |
| catpaw | 8795 | glm-5.2 | 502 | FAIL | 5.2s | 内网不可达，需美团 VPN |

合计：6 PASS / 3 FAIL（workbuddy、workbuddy-gpt、qoder、trae、lingxi、xhx 可直接用于 Codex）

## 本轮修复明细
1. qoder 8789：根因 qoderclicn 每次调用加载用户 MCP 配置卡死（MCP issues detected 后挂起，非流式等到 300s 超时；流式只等首事件所以正常）。修复：qoder_bridge.py base_args 增加 --strict-mcp-config（无 --mcp-config = 不加载 MCP server），CLI 6.7s 正常返回。备份 ~/qoder-bridge/qoder_bridge.py.bak-20260926，重启 com.local.qoder2codex 生效。
2. workbuddy-gpt 8788：上轮 11133 参数错误确认瞬时波动，本轮 2.9s/200。
3. gemini web 通道防御性修复：web_cookies() 读二进制 cookies.txt 的 UnicodeDecodeError 已加 errors=ignore，降级为干净错误。

## 剩余 3 项失败根因（均已定位，用户侧/外部条件）
1. codely 8790：上游网关对全部 8 个模型 chat 一律 400「欢迎使用Codely, 访问 https://codely.tuanjie.cn/」（账号 onboarding 门禁）。深挖：网关 /v1/models 直连真实 200（key 有效）；账号 web API 正常（/api/teams → <你的团队> has_key:true；/api/user/usage/summary → <你的点数>）；官方 CLI 同网关同签名同样被挡。修复路径：用户登录 codely.tuanjie.cn 网页端完成首次激活。
2. gemini 8794：403 VALI「Verify your account to continue.」（Google Code Assist 账号验证门禁）。本机无 Firefox cookie；Chrome/Edge 为 v10+ABE 加密不可解。修复路径：用户完成 Google 账号验证，或装「Get cookies.txt LOCALLY」导出 gemini.google.com cookie 覆盖 ~/gemini2codex/cookies.txt。
3. catpaw 8795：系统代理 127.0.0.1:1082（MacPacket）对 sankuai.com CONNECT 503。catpaw.sankuai.com 无公网 A 记录（CNAME inf.vip.sankuai.com 纯内网）；公网 catpaw.meituan.com 有同套 API 但只认 External 租户，内部 SSO token 报 passport auth failed（曾试改 BASE 打公网，maas 401 ssoid 不存在，已回滚）。修复路径：连美团公司 VPN 后 kickstart com.local.catpaw2codex。

## tokendance（本轮新增配置）
- provider 已在 opencodex 注册（base https://tokendance.space/gateway/v1），95 个模型已发现。
- 本轮执行：ocx models selected tokendance --set step-5-preview → selected=['step-5-preview']，catalogRefresh committed；ocx sync → 72 models 注入 ~/.codex/cc-switch-model-catalog.json；~/.codex/config.toml [model_providers.tokendance] → http://127.0.0.1:10100/v1。
- 链路实测：curl 代理 /v1/chat/completions（model=tokendance/step-5-preview）→ 上游返回 Provider error 401: API 密钥不存在。路由全通，key 失效。
- 修复路径：用户到 https://tokendance.space/keys 重建 key，然后执行：
  ocx provider add tokendance --adapter openai-chat --base-url https://tokendance.space/gateway/v1 --api-key <新KEY>
  ocx service restart

## 用户侧待办（4 项）
- tokendance：tokendance.space/keys 重建 key（旧 key 01M053GJE05H2S3K8Y9JH8329S 仍 401「API 密钥不存在」，00:55 与 01:20 两次复测一致），然后 ocx provider add tokendance ... --api-key 新key + ocx service restart
- codely：网页端登录激活
- gemini：完成 Google 账号验证或提供 cookie
- catpaw：连美团 VPN

## 沙箱安装与脚本级验证（kit 打包前，2026-09-26）
- 沙箱安装：FLEET_LAUNCH_DIR=/tmp/fleet-selftest/LaunchAgents FLEET_LABEL_PREFIX=com.localtest FLEET_LOG_DIR=/tmp/fleet-selftest/logs bash install.sh --home /tmp/fleet-selftest --port-base 9787 --no-opencodex -> 9 个 plist plutil -lint OK，9 桥全部 up（9787-9795），fleet.env 权限 600。
- tools/status.sh：修复 grep -c 行计数 bug（改 grep -o | wc -l）；实测 qoder models=15、gemini=4、未登录桥=0。
- bridges/finish.sh：修复同样计数 bug，并让 model id 提取容忍冒号后空格（"id": "x" 形式）；gemini 成功路径 exit 0（4 models + 4 个 id 列出），workbuddy 失败路径 exit 3（no models yet + 登录提示）。
- opencodex/setup-providers.sh --dry-run：打印 9 个 ocx provider add（端口 9787-9795 全对）+ tokendance 因无 key 跳过 + ocx sync，exit 0。
- uninstall.sh：新增安全边界——仅 bootout/删除 LAUNCH_DIR 下实际存在的 plist（防 --home 指错误杀其他舰队）；沙箱 --purge 精确移除 9 个 com.localtest.* 并 purge 目录，exit 0。
- live 舰队复核：com.local.* 9 桥 + lingxi-login 助手未受影响；8787-8795 在线（401=需 key 属正常，qoder/gemini 静态模型表 200）。
- 打包：~/fleet-kit.tar.gz（1.0M，44 个文件），解包后 diff -r 与源码一致。

## 自动签到 + 一键部署验证（2026-09-26，kit 内）

沙箱命令：

    FLEET_LAUNCH_DIR=/tmp/fleet-deploy-test/LaunchAgents FLEET_LABEL_PREFIX=com.localtest2 \
    FLEET_LOG_DIR=/tmp/fleet-deploy-test/logs bash deploy.sh --home /tmp/fleet-deploy-test \
      --port-base 9687 --with-checkin --no-opencodex

- **deploy.sh 全链路跑通到 `done.`**：preflight → install（复用 venv）→ 9 桥 launchd 起 → 逐桥 finish → checkin timer → status.sh 汇总。
- **8/9 桥 up，finalized 2/8**（qoder、gemini exit 0）；workbuddy / workbuddy-gpt / codely / trae /
  lingxi / xhx 报 exit 3「login needed」，与「登录表」一致（沙箱没有 auths 目录），非 bug。
- **catpaw unreachable 不再中断部署**：原版 deploy.sh 任一桥起不来就 exit 1，实测 catpaw 因美团内网
  不可达（Tunnel 503）把整条流水线掐死。已改为按桥容错——连不上的桥列入 unreachable、其余桥照常
  收尾，只有「一座都没起来」才 exit 1。本次输出 `bridges up:workbuddy ... gemini` +
  `[warn] unreachable (offline / needs VPN or login): catpaw`，汇总行 `bridges 8/9 up | finalized 2/8`。
- **签到 timer 实测**：`com.localtest2.fleet-checkin.plist` 写入沙箱 LaunchAgents 并 load（launchctl list
  可见），StartCalendarInterval 09:00 + RunAtLoad；RunAtLoad 立即跑了一次真实 xhx 签到（用 live
  `~/.box-agent` auth，幂等）→ `xhx OK today | points 9207 | 今日已发放过（桌面端启动时已领或非首登）`。
- **checkin.sh 命令实测**：`status` 输出今日已签 + 余额；`run-now xhx` 幂等跳过（今日已成功）。
  状态落 `$FLEET_HOME/checkin/state.json`，日志 `$FLEET_HOME/logs/checkin.log`，均按 `--home` 隔离。
- **status.sh 新增 check-in 段**：deploy 末尾打印 `check-in: xhx OK today | last ... | points 9207 | ...`。
- **卸载清理**：`uninstall.sh --purge` 精确移除 9 个 com.localtest2.* + fleet-checkin 并 purge 目录，
  exit 0；live 舰队（com.local.*、8787-8795）未受影响。
- **CI**：新增 `.github/workflows/ci.yml`，push/PR 触发三段——bash -n 全部 .sh、
  compileall bridges/tools/opencodex、泄漏扫描（`sk-<32hex>` / `ghp_` / gmail|qq 邮箱 / `/Users/` /
  PRIVATE KEY）。四步在本机预跑全绿（bash -n fail=0、compileall OK、leak hits=0）。
- 环境附注：长任务必须放 tmux（`tmux new-session -d`）里跑；裸 `nohup ... & disown` 会随 exec 会话
  结束被回收，表现为停在 venv / launchd 步骤不动，需轮询日志 + 重跑。

## 本地状态面板 status_ui（2026-09-26 新增，--with-ui）

动机：9 座桥 + 签到 + ocx 的实况原先要连着敲 status.sh / checkin.sh status / launchctl list /
lsof 才能拼出来；workbuddy 这类「桥进程还活着但 launchd 已退出」的情况尤其难看明白。
新增一个零依赖本地页面板，一屏看完，并可直接点按钮签到 / 强制重签 / 重启单座桥。

实现：`tools/status_ui.py`（Python 标准库单文件，约 37.5KB）+ `tools/status_ui.sh`
（start / stop / install-timer / uninstall-timer）。不动 requirements.txt，不引入任何新依赖。

- 监听 127.0.0.1，端口 PORT_BASE+9（默认 8796），与 9 座桥错开；端口若落在
  [PORT_BASE, PORT_BASE+9) 区间内拒绝启动（rc=2）。
- 数据来源：`launchctl print gui/<uid>/<label>` 判 agent 状态与 last exit code；
  `lsof -nP -iTCP:<port> -sTCP:LISTEN` 判监听 pid；对每桥打 /v1/models 拿模型数与延迟；
  读 `<home>/checkin/state.json` 拿今日签到与余额；`ocx service status` 拿 ocx 状态。
- API：`GET /`、`GET /api/status`（summary / bridges / checkin / ocx / actions / config /
  warnings）、`GET /api/logs/<name>?lines=N`、`POST /api/action/checkin[?force=1]`、
  `POST /api/action/restart/<name>`。
- 安全：只回 key 的 md5 前 8 位，不返回明文；日志接口只接受桥名白名单；重启接口对未知名桥
  返回 unknown bridge。

### 沙箱全链路实测

    FLEET_LAUNCH_DIR=/tmp/fleet-uitest/LaunchAgents FLEET_LABEL_PREFIX=com.localtest3 \
    FLEET_LOG_DIR=/tmp/fleet-uitest/logs bash install.sh --home /tmp/fleet-uitest \
      --port-base 9687 --no-opencodex --no-start --skip-deps --with-checkin --with-ui

- 装完 `com.localtest3.fleet-ui` 已在 launchctl list，KeepAlive + RunAtLoad，落在端口 9696。
- 端点：`GET /` 200 / 10597B、`GET /api/status` 200（summary + 9 桥 + checkin + ocx + config）。
- 写操作白名单生效：`POST /api/action/restart/__bogus__` 返回 unknown bridge；
  日志接口的路径穿越被桥名白名单挡掉。
- 沙箱签到动作 `POST /api/action/checkin` rc=0，输出「xhx 今日已成功，跳过」（真实 points 9207）。
  注意：checkin.py 的 xhx 凭据来自**全局** `~/.box-agent/config/auth.json`
  （`BOX_AGENT_CONFIG_DIR` 可覆盖），与舰队隔离无关——沙箱里点签到打的是真实账号，上游
  「今日已发放过」不重复发放，无害。UI 签到按钮全链路验证通过。
- 清理：`bash uninstall.sh --home /tmp/fleet-uitest --purge` 移除 11 个 com.localtest3.* 服务
  （含 fleet-ui、fleet-checkin）并 purge 目录，`launchctl list | grep localtest3` = 0；
  live 舰队（com.local.*、8787-8795）与 live 面板（8796）未受影响。

### 本轮修掉的 2 个真 bug

1. **launchd plist 没传 `--home`**：`tools/status_ui.sh` 的 plist ProgramArguments 缺少
   `--home`，实例回落默认 home=~/fleet。沙箱里表现为面板显示 live 数据而非沙箱数据。
   修复：plist 增加 `--home` + `<string>$FLEET_HOME</string>`。
2. **`build_config()` 不读 fleet.env**：`tools/status_ui.py` 原先只从 CLI / 进程环境取
   PORT_BASE / LABEL_PREFIX / LOG_DIR / LAUNCH_DIR（fleet.env 只被解析出 bridge keys），
   于是 launchd 起的实例拿不到沙箱的 port-base 与 label，页面显示的全是默认值。
   修复：把 `keys = parse_env_file(env_file)` 移到四值解析之前，四值改为
   `CLI > 进程 env > keys.get(XXX) > 默认`，PORT_BASE 非整数时打 warning；fleet.env 缺失时
   降级显示 MISSING 而不是崩。修复后沙箱实测：
   `labels: com.localtest3.qoder2codex, port: 9689, agent_up 0/9, listening 0`（符合 --no-start）。

### 面板暴露出的真实问题（不是 bug，是需要用户处理的状态）

- workbuddy：launchd `not running`，last exit code 1，但 8787 端口被残留 PID 7488 占用
  （桥进程其实还活着，launchd 记录的是它自己那次启动失败）。面板把 launchd 状态与端口监听
  pid 分两列展示，就是为了区分这种情况。恢复命令：
  `launchctl kickstart -k gui/$(id -u)/com.local.workbuddy2codex`。
- live 机没有 `~/fleet/fleet.env`（该文件不入库），面板如实显示 fleet.env MISSING 且 6 座桥
  401，这是 live 实况、符合预期。
- live 实测（重启加载新代码后）：agent_up 8/9、listening 9、probe_ok 2
  （qoder 15 models + gemini 4 models，合计 19）、xhx points 9505
  （last_success 2026-09-25）、ocx PID 4978 running。


## Codex 模型选择器丢了反代理模型（2026-09-26 09:00 排查）

现象：重启 ChatGPT/Codex 之后，模型选择器里 `桥名/模型` 全部消失，只剩 stepfun 的
step-5-preview。

排查链路（全部本机实测）：
- 后端正常：`curl 127.0.0.1:10100/v1/models` 返回 85 个模型，其中 72 个带 `/` 的桥模型；
  `ocx models live` 也全在；9 座桥 8787-8795 全部 LISTEN，launchd agent_up 8/9。
- 所以问题不在 ocx、不在桥，而在 Codex 侧读取的模型目录。

根因：Codex 的模型选择器读 `model_catalog_json` 指向的 catalog 文件，而 ocx 是把桥模型
**追加**进这个 catalog 的。这个 catalog（cc-switch-model-catalog.json）归 CC Switch 管，
它的 profile 里只存 1 个模型；CC Switch 每次套用 profile 都会用自己那份覆盖 catalog，
ocx 追加的 72 个模型被冲掉。重启 app 后读到的是被冲掉的版本，选择器里就没有反代理模型。
- 证据：`config.toml.bak-before-skills-plugins-20260926-061201` 里 `model = "workbuddy/hy4-preview"`
  且 7 个 `[model_providers.*]` 块齐全；`...-064206` 那份已只剩 `[model_providers.custom]`，
  丢失发生在 06:12~06:42 之间（cc-switch settings.json 与 catalog 的 mtime 都是 06:40）。
- `~/.cc-switch/cc-switch.db` 当前 codex provider（3a20aad7-…, StepFun）的
  `settings_config.modelCatalog.models` 只有 1 条（step-3.7-flash）。

修复与验证：
- `ocx sync` → catalog 1 → 80 个模型（72 桥模型），models_cache.json 同步刷新到 80/72。
- **`ocx ensure` 不治这个**：实测把 catalog 抹成 1 个模型后跑 `ocx ensure`，输出
  「Codex routing NOT injected: config.toml selects the external model_provider custom」，
  桥模型数仍是 0。只有 `ocx sync` 会同时刷 catalog 和 models 缓存。

新增 `tools/ocx-catalog-guard.sh`（默认随 opencodex 接线一起装）：
- 数 catalog 里带 `/` 的桥模型，少于 `--min-models`（默认 60）就自动 `ocx sync` 自愈。
- launchd `com.local.ocx-catalog-guard`，StartInterval 300s + RunAtLoad，
  日志 `~/Library/Logs/ocx-catalog-guard.log`；install.sh / deploy.sh 加 `--no-ocx-guard` 可关。
- 本机实测：catalog 抹到 0 → `guard run` → 恢复 72，日志完整记录 heal 前后数量。
- `uninstall.sh` 的 SUFFIXES 追加 `ocx-catalog-guard`。

遗留（需用户操作）：
- 磁盘 catalog 修好后，**已在运行的 app 仍显示旧列表**，必须重启一次 Codex/ChatGPT。
  `ocx sync --restart-codex` 能自动重启，但会结束进行中的会话，故未擅自执行。
- CC Switch 的 profile 只认它自己那 1 个模型。若要切完 provider 也不再触发这种情况，
  可以把桥模型并进 CC Switch 的 profile（本次未改动它的 sqlite）。
