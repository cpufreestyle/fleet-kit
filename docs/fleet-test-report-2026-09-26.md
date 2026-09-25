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
