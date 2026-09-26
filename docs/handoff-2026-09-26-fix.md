# 交接文档 — FleetKit 修复任务（2026-09-26 22:45）

## 目标
修复状态面板显示 down 的两个桥：catpaw(p8795)、qwen(p8798)。
其余 10 桥全部正常（12 桥 / 11 up / 104 模型 / 0 警告）。

## 当前状态快照

| 桥 | 端口 | launchd | 端口监听 | /v1/models | 面板判定 | 根因 |
|---|---|---|---|---|---|---|
| catpaw | 8795 | running PID 65606 | YES | 200 但 5.2s | probe FAIL | 上游网络不通(503)+探活超时 |
| qwen | 8798 | NOT LOADED | NO | - | AGENT-DOWN | plist 未加载+fleet.env 缺 key |

## 根因详解

### 1. catpaw (p8795) — 桥活着但上游不通 + 探活超时

**A. /v1/models 慢(5.2s)**：
- list_models() -> get_token() -> 读 state.vscdb 拿 token -> 对 3 个外网 base
  (catpaw.sankuai.com / mcopilot-emb.sankuai.com / catpaw.meituan.com) 各发
  /api/login/userInfo 验证(每个 timeout=12s)。token 缓存 1500s,但进程重启后
  首次必走外网,最坏 36s。面板 PROBE_TIMEOUT=2.0s -> 必超时 -> probe ok=false。
- catpaw_bridge.py:32 显式 install_opener(ProxyHandler({})) 禁用代理,
  但上游是美团内网域名,需公司 VPN。当前实测:Tunnel connection failed: 503。

**B. /health 卡死(10s 无响应)**：
- do_GET /health 对 3 个 base 各发 /api/ping(timeout=5s)= 最坏 15s,
  再调 get_token()。面板不探 /health 而探 /v1/models,但 ThreadingHTTPServer
  下 /health 的外网阻塞不阻塞 /v1/models(实测 /v1/models 5.2s 能返回)。
  面板 probe ok=false 纯粹因为 PROBE_TIMEOUT=2.0 < 实际 5.2s。

**C. 实际调用失败**：
- curl /v1/chat/completions -> URLError: Tunnel connection failed: 503。
- catpaw 上游需美团 VPN/内网,当前网络环境连不上。
- state.vscdb 路径正确:~/Library/Application Support/CatPawAI/User/globalStorage/state.vscdb(274KB,有数据)。

### 2. qwen (p8798) — plist 未加载 + fleet.env 缺 key

**A. launchd 未加载**：
- launchctl list | grep qwen -> 无输出(NOT LOADED)。
- launchctl print gui/501/com.local.qwen2codex -> Could not find service。
- plist 文件存在于 ~/Library/LaunchAgents/com.local.qwen2codex.plist(1289B,9/26 20:04)。
- 日志 /tmp/fleet-logs/qwen.log 显示:桥曾启动,收到大量 401(models 端点),最后 Shutting down。
- 需要执行 launchctl load ~/Library/LaunchAgents/com.local.qwen2codex.plist。

**B. fleet.env 缺 QWEN2CODEX_KEY**：
- grep QWEN fleet.env -> 0 行。install.sh 的 pick_key() 对 QWEN2CODEX_KEY 无默认值
  (不像 GEMINI/CATPAW 有 sk-local-* 兜底),会生成 gen_key() 随机 key。
- plist 硬编码 QWEN2CODEX_KEY=sk-local-qwen,但 fleet.env 没有此行。
- 面板 probe_bridge 从 fleet.env 读 key -> 读不到 -> http_get 不带 Authorization
  -> 桥 check_bridge_auth 比较 Bearer sk-local-qwen 不等 无头 -> 401。
- 日志证实:12 次 200 OK(带正确 key 的请求)+ 53 次 401(面板探活无 key)。

**C. 用户尚未注册 Qwen Cloud**：
- runbook(docs/qwen2codex-runbook.md)明确:需注册 www.qwencloud.com 拿 sk- key。
- 桥的 QWEN2CODEX_KEY 应为真实 Qwen Cloud API key,不是 sk-local-qwen。
- 在用户填入真实 key 前,桥即使加载也会因上游 401 无模型。

## 修复方案(下一步执行)

### catpaw
1. 提高面板 PROBE_TIMEOUT:status_ui.py:72 从 2.0 -> 8.0(catpaw 首次探活 5s+)。
   或:catpaw 桥 list_models() 在 token 缓存有效时跳过外网验证,直接返回缓存模型。
2. /health 加超时兜底:do_GET /health 的 ping 循环改异步/缩短 timeout,
   或直接返回静态 ok=true 不探外网(与 cline 桥一致)。
3. 上游连通:需用户确认连了美团 VPN;否则 catpaw 桥只能返回静态模型列表,
   实际调用必 503。若长期无 VPN,考虑从 ocx provider 移除 catpaw。

### qwen
1. 加载 plist:launchctl load ~/Library/LaunchAgents/com.local.qwen2codex.plist。
2. fleet.env 补 key:等用户注册 Qwen Cloud 后,把真实 sk- key 写进
   runtime/fleet.env 的 QWEN2CODEX_KEY= 行。在用户给 key 前,可先写
   QWEN2CODEX_KEY=sk-local-qwen 让面板探活带正确头(桥会起,但上游无 key -> 0 模型)。
3. install.sh 对齐:install.sh pick_key 对 QWEN 无默认值,应加 QWEN2CODEX_KEY 兜底
   (与 GEMINI/CATPAW 一致),或保留 gen_key 但同时写进 plist。

## 其他已知正常桥(无需动)
- workbuddy(8787) 13 模型 ok
- workbuddy-gpt(8788) 14 模型 ok
- qoder(8789) 15 模型 ok
- codely(8790) 8 模型 ok
- trae(8791) 22 模型 ok
- lingxi(8792) 3 模型 ok
- xhx(8793) 9 模型 ok
- gemini(8794) 4 模型 ok
- antigravity(8797) 12 模型 ok
- cline(8799) 4 模型 ok(free-only,刚上线)

## 环境
- 仓库:/Users/a1-6/AI Shared/repo/FleetKit/kit,remote github.com/cpufreestyle/fleet-kit,branch main
- runtime(不入库):/Users/a1-6/AI Shared/repo/FleetKit/runtime
- venv:runtime/.venv/bin/python(python 3.14)
- 面板:http://127.0.0.1:8796/(status_ui.sh 起的 ThreadingHTTPServer)
- ocx 网关:http://127.0.0.1:10100(provider list 含 qwen-cloud/catpaw/cline 等)
- fleet.env 路径:runtime/fleet.env(不入 git,holds all bridge keys)
- 日志:/tmp/fleet-logs/(catpaw.log 缺失因进程 stdout 未生成;qwen.log 有完整 401->shutdown 记录)

## 用户偏好(贯穿全程)
- 中文沟通
- 模型名短(选择框看全),不该缩写的别缩写
- 不可用模型隐藏
- 标注 free 模型及时间段
- 默认模型 step 5(trae/trae-step-5-preview)
- 可复用方案 + git 同步 fleet-kit
- UI 面板 http://127.0.0.1:8796/
- 自动签到/部署自动化

## 执行陷阱(务必遵守)
- exec 输入必须原始 JS(顶层代码、禁 import、text() 输出)
- apply_patch 内容含 dollar-brace 或 @PORT@ 会导致 hunk 解析失败 -> 改用 Python 按行号改
- rm -f 禁用(用 > file 覆盖);zsh echo === 报错(用引号包)
- & 后台进程会被 exec 会话回收 -> 常驻服务一律 launchd
- 含空格路径 heredoc 内必须加引号
- exec 约 10s 输出截断 -> 长任务用 (timeout N cmd > log 2>&1) 后再读 log
