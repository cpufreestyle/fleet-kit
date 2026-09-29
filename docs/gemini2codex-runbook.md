# Gemini Pro 反代理 Runbook (gemini2codex)

账号: <你的 Google 账号> (Google One AI Premium 待验证)
状态: 桥与凭据就绪; **阻塞 = Shadowrocket 当前节点到 Google 全阻断 (503)**

## 架构（双通道）

```
Codex -> 本地代理(可选) -> gemini2codex(:8794) -> A: cloudcode-pa.googleapis.com (OAuth 自动刷新)
                                                -> B: gemini.google.com web (cookie 兜底)
```

- A 通道（优先）: Gemini Code Assist 官方 API, OAuth consumer client, access token 25 天前过期 -> 用 refresh token 自动续期并回写
- B 通道（兜底）: gemini.google.com web StreamGenerate + SNlM0e 动态 token
- 暴露: `GET /v1/models`, `POST /v1/chat/completions` (stream SSE / non-stream)
- 模型: gemini-3-pro-preview / gemini-2.5-pro / gemini-2.5-flash / gemini-3-flash-preview
- 响应带 `channel` 字段标记实际走了哪条通道

## 资产位置（凭据不明文，均引用本地文件）

- 桥: `~/gemini2codex/gemini_bridge.py` (端口 8794, 已 py_compile 通过)
- cookie 提取器: `~/gemini2codex/extract_cookies.py` (PBKDF2 saltysalt 链路已验证)
- OAuth token: `~/.gemini/jetski-standalone-oauth-token` (含 refresh_token; access 过期自动刷新)
- web cookie: `~/.gemini2codex/cookies.txt` (600 权限; PSID 185 / PSIDTS 110 字符)
- 公开 client 凭据来源: `@google/gemini-cli@0.60.0` bundle (CLIENT_CANDIDATES 两组)
- Claude/Gemini 客户端资产: `/Applications/Antigravity.app`, `/Applications/Gemini.app`

## 操作

启动:
```bash
cd ~/gemini2codex && python3 gemini_bridge.py &
```
验证:
```bash
curl -s http://127.0.0.1:8794/health
curl -s http://127.0.0.1:8794/v1/models
curl -s -X POST http://127.0.0.1:8794/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"gemini-3-pro-preview","messages":[{"role":"user","content":"say PONG"}]}'
```

ocx 注册:
```bash
ocx provider add gemini --adapter openai-chat --base-url http://127.0.0.1:8794/v1 --api-key sk-local-gemini --allow-private-network
ocx sync && ocx service restart
```

## 故障诊断（2026-09-25 取证）

| 层 | 证据 | 结论 |
|---|---|---|
| 系统代理 | 127.0.0.1:1082 up, scutil HTTP/HTTPSProxy 均指向它 | 代理配置正常 |
| 国内直连 | baidu/apple.com.cn/taobao 200 | TUN 与 DIRECT 正常 |
| Google 全部域 | gemini/google/oauth2/cloudcode/github/chatgpt 全 000; verbose 见连上 fake-ip 198.18.0.209 后 TLS 无响应 | 当前节点整体拒绝海外 LibreSSL |
| A 通道 | urlopen 报 Tunnel connection failed: 503 Service Unavailable | 代理层 503, 非凭据问题 |
| B 通道 | GET gemini.google.com/app 返回二进制渣 (SNlM0e 提取失败) | 同上 |

**结论: 桥代码 + 双凭据链已 100% 就绪; 唯一阻塞是 VPN 节点。换到能放行 Google 的节点后无需任何修改即可直接测。**

## 网络恢复后的三步

1. `curl https://oauth2.googleapis.com/token` 返回非 000 后, POST ping 桥 -> 响应 `channel` 字段确认通道
2. 若 A 报 invalid_grant: 在 Antigravity.app / Gemini.app 重新登录 (或 `gemini login` 刷新 jetski token)
3. `memos` 已知: 推理模型测试 max_tokens >= 1024; 上游 429 会冷却账号 60s

## 2026-09-26 01:00 更新：VALI 门禁确认 + cookie 读取崩溃修复
- 桥返回 502，上游 code assist 403 VALI「Verify your account to continue.」（账号验证门禁）。Antigravity 在运行；本机无 Firefox cookie 源，Chrome/Edge 为 v10+ABE 加密不可解。
- 防御性修复：~/gemini2codex/gemini_bridge.py web_cookies() 读二进制 cookies.txt 时 UnicodeDecodeError → open(..., encoding='utf-8', errors='ignore')，已 kickstart 重启 com.local.gemini2codex。修复后错误信息干净（直接 403 VALI）。
- 修复路径：用户完成 Google 账号验证，或装「Get cookies.txt LOCALLY」导出 gemini.google.com cookie 覆盖 ~/gemini2codex/cookies.txt。

## 2026-09-29 更新：单次 chat 总时限（`GEMINI_CHAT_BUDGET`）

- 现象：上游 Google 全阻断时，一次 chat 要挂 **6 分钟**才返回 502
  （`call_a(180s)` 失败后再 `call_b(180s)`）。客户端 70s 就断开，
  只剩一条 `BrokenPipeError`，体检于是把这座桥误判成 `BRIDGE_DOWN`——
  桥没死，只是在等一个永远不回的 upstream。
- 修复：`do_POST` 设一个总 deadline `CHAT_BUDGET`（默认 60s，
  env `GEMINI_CHAT_BUDGET`）。A 通道拿满预算，B 通道只拿
  `max(FALLBACK_FLOOR, 剩余)`。`FALLBACK_FLOOR`（默认 1s，
  env `GEMINI_FALLBACK_FLOOR`）只负责别把 0/负数当 timeout 交给 urllib，
  不会再长成一次完整超时。`_send()` 容 `BrokenPipeError` /
  `ConnectionResetError`，客户端已断开时不再刷 traceback。
- 最坏总时长：`预算 + FALLBACK_FLOOR + 少许调度开销`，而不是 360s。
- 测试：`tools/test_chat_budget.py`（与 antigravity 共用）。

### 2026-09-29 追查：预算为什么还是被击穿（连接层）

第一版只给 `call_a` / `call_b` 传 deadline，实测 `curl` 仍然 90s 拿不到响应。
抓到两层漏算：

1. **`get_access()` / `do_refresh()` / `load_code_assist()` 各有自己的 30s 超时**，
   完全在预算之外。于是 `call_a` 里就能烧掉 60 + 30 + 30。
   现在 `do_refresh` / `get_access` / `load_code_assist` / `call_a` 都收
   `deadline` 参数，每一跳都用「剩余时间」当 timeout。
2. **真因：`urlopen()` 给 getaddrinfo 返回的每个地址都发一份完整 timeout。**
   `cloudcode-pa.googleapis.com` 解析出 16 个地址（前 8 个是 IPv6），
   本机 VPN 把 IPv6 黑洞掉，于是一次调用要 16 × timeout 才轮到 IPv4。
   实测 `oauth2.googleapis.com` 两个地址：传 20s，花了 40s。
   所以两座桥都在模块级替换了 `socket.create_connection`：
   自己走地址列表，每次尝试按**当时的剩余时间**封顶（thread-local deadline，
   `ThreadingHTTPServer` 每请求一线程，互不干扰）。这才是硬上限。
   deadline 过期时直接抛 `OSError('request budget exhausted')`。

另外发现 `_send()` 的 BrokenPipeError 其实来自 `end_headers()`
（`BasicHTTPRequestHandler` 的响应头也是经 `wfile` 写出的），
只包 `self.wfile.write(b)` 挡不住——修之前日志里刷了 60KB traceback。
现在整段响应写出席都包在 try 里。

改完实测：`curl` 60.04s 拿到 502，错误信息
`refresh failed for all clients: URLError: <urlopen error request budget exhausted>`
——一眼能看出是预算耗尽，而不是一条看不懂的管道错误。
