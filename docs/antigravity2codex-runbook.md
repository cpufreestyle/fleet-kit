# Antigravity (Google) 反代理 Runbook (antigravity2codex)

结论: 可以反代理。反代自 Google Antigravity IDE 的 language_server 所调用的
cloudcode-pa (Code Assist) v1internal 接口，凭据复用 Antigravity/Gemini CLI 的 OAuth
refresh_token，可自动续期。**唯一阻塞 = 本机当前 VPN 节点对 cloudcode-pa.googleapis.com
TLS 握手超时**（oauth2.googleapis.com 同节点可达，说明不是整体断 Google）。

## 架构

```
Codex -> ocx(:10100) -> antigravity2codex(:8797) -> cloudcode-pa.googleapis.com/v1internal:
                                             (OAuth: oauth2.googleapis.com/token)
```

- 暴露: `GET /health`, `GET /v1/models`, `POST /v1/chat/completions` (stream SSE / non-stream)
- IDE 标识: `ideType=ANTIGRAVITY`（回退 `GEMINI_CLI`），`pluginType=GEMINI`
- 模型目录来源: `/Applications/Antigravity.app/Contents/Resources/bin/language_server` 二进制提取
- 与 gemini2codex(:8794) 共用同一个 token 文件，但用 Antigravity 自己的 OAuth client pair
- OAuth client pair: **不硬编码在源码**。`install.sh` 用 `extract_client.py --verify` 从同一
  二进制提取并实测（能换到 access_token 的排首位），经 `fleet.env` → launchd plist 注入
  `ANTIGRAVITY_OAUTH_CLIENT_ID` / `ANTIGRAVITY_OAUTH_CLIENT_SECRET`，桥启动时读 env 装载；
  `/health` 的 `oauth` 字段即候选 pair 数（当前 1）。

## 资产位置（凭据不明文）

- 桥: `bridges/antigravity/antigravity_bridge.py`（开发态）/ `runtime/bridges/antigravity/antigravity_bridge.py`（运行态）
- catalog 注入: `bridges/antigravity/inject_catalog.py`
- catalog 注入: `bridges/antigravity/inject_catalog.py`
- OAuth pair 提取: `bridges/antigravity/extract_client.py`（默认读上述 binary；`--verify` 逐个
  实测 jetski refresh_token 换 token，可用者排首位、输出 `id<TAB>secret`；换机/换版本后重跑即可）
- OAuth token: `~/.gemini/jetski-standalone-oauth-token`（含 refresh_token，自动刷新并回写）
- launchd: `~/Library/LaunchAgents/com.local.antigravity2codex.plist`
- 日志: `runtime/logs/antigravity.log`
- 端口/密钥: `8797` / `ANTIGRAVITY2CODEX_KEY`（值在 `runtime/fleet.env` 与 plist，勿外泄）

## 12 个模型与选择器短名（前缀 agy）

| 上游 model id | 选择器显示名 | 备注 |
|---|---|---|
| `claude-opus-4-8@default` | `agy/claude-opus-4-8` | |
| `claude-opus-4-6@default` | `agy/claude-opus-4-6` | |
| `claude-opus-4-5@20251101` | `agy/claude-opus-4-5` | |
| `claude-sonnet-4-5@20250929` | `agy/claude-sonnet-4-5` | |
| `claude-haiku-4-5@20251001` | `agy/claude-haiku-4-5` | |
| `gemini-3.1-pro-preview` | `agy/gem-3.1-pro-pv` | |
| `gemini-3-pro-preview` | `agy/gem-3-pro-pv` | |
| `gemini-3-flash-preview` | `agy/gem-3-fl-pv` | |
| `gemini-2.5-pro` | `agy/gem-2.5-pro` | |
| `gemini-2.5-flash` | `agy/gem-2.5-fl` | |
| `gpt-oss-120b-maas` | `agy/oss-120b-maas` | |
| `gpt-oss-20b-maas` | `agy/oss-20b-maas` | |

## 上线步骤（已完成，重放用）

```bash
export FLEET_HOME="<FleetKit>/runtime"
# 1. 桥（install.sh 已内置 antigravity 段；手动版见 bridges/antigravity 启动 plist）
# 2. catalog 注入（catalog 从 203 -> 215）
python3 kit/bridges/antigravity/inject_catalog.py
# 3. provider + 短名注册（antigravity 在 PROVIDERS 表 offset=10 -> 8787+10=8797）
bash kit/opencodex/setup-providers.sh --home "$FLEET_HOME"
# 4. 免费标注 + 状态
python3 kit/tools/free_models.py --provider antigravity
bash kit/tools/status.sh --home "$FLEET_HOME"
```

注意: 改完选择器列表必须**重启 Codex / ChatGPT** 才会刷新模型下拉。

## 凭据链实测（2026-09-26 15:xx，本机）

| 检查项 | 结果 |
|---|---|
| `~/.gemini/jetski-standalone-oauth-token` 存在 refresh_token | 是（103 字符） |
| 缓存 access_token 有效期至 | `2026-09-26T16:02:17+0800` |
| `POST https://oauth2.googleapis.com/token`（Antigravity 自有 client pair） | **OK，expires_in=3599，scope 含 experimentsandconfigs** |
| `GET https://cloudcode-pa.googleapis.com/` | **http=000, rc=28（6s 超时）** |
| `GET https://gemini.google.com/` | **http=000, rc=28（6s 超时）** |
| 桥 `/health` | `status=ok, models=12, calls=N, client_ok=false, project=false` |
| `ocx models live` | antigravity 12/12 |
| catalog | antigravity 12/12（`agy/*` 短名） |

`client_ok=false` / `project=false` **不代表凭据坏**：二者只在 `do_refresh()` 成功 /
`loadCodeAssist` 成功后才置位，而这两步都要先经过被阻断的 cloudcode-pa。
token 未过期时 `get_access()` 直接返回缓存 token，压根不走 refresh。

## 真实调用检测结论

`python3 kit/tools/verify_real_calls.py --only antigravity`

```
BRIDGE         PORT  MODEL                        HTTP  LAT(s) VERDICT       NOTE
antigravity    8797  claude-sonnet-4-5@20250929   None  70.0   BRIDGE_DOWN   TimeoutError('timed out')
```

桥日志根因:

```
UpstreamError: loadCodeAssist failed: URLError: <urlopen error _ssl.c:1064: The handshake operation timed out>
```

同轮 `kit/tools/fleet_chat_test.py`（存活/冒烟）结论——客户端 90s 超时，服务端写响应时
Broken pipe：

```
antigravity    8797  claude-opus-4-8@default   None   FAIL | 90.0s | models=12 | key=9f2718f8 | TimeoutError('timed out')
```

注意桥本身是活的：`/health` 与 `/v1/models` 均正常返回（models=12、key md5 有值），
掉的只是「上游推理」这一段，与启动/端口/ocx 注册无关。

**所以现状与 gemini2codex 完全一致：选得到、发不出，卡在网络而非代码或账号。**

## 网络恢复后的三步

1. `curl -s -o /dev/null -m 6 -w '%{http_code}' https://cloudcode-pa.googleapis.com/` 返回非 000 后，
   `launchctl kickstart -k gui/$(id -u)/com.local.antigravity2codex` 重启桥（清掉缓存的失败态）
2. 再跑 `python3 kit/tools/verify_real_calls.py --only antigravity`，期望 `REAL`（随机运算题答对）
3. 若报 `invalid_grant`：打开 Antigravity.app 重新登录（或 `gemini login`）刷新 jetski token，
   然后重跑 1

## 已知坑

- **8797 端口曾被游离进程占用**（PPID 1 的旧桥实例），导致 launchd 版 `Address already in use`
  crash-loop。排查: `lsof -nP -iTCP:8797 -sTCP:LISTEN`；杀掉非 launchd 的 PID 即可。
- `ocx models selected <provider> --set <id,...>` 会把该 provider 收窄成只列这些 id，随后 `ocx sync`
  会把其余 live 模型从 Codex catalog 里删掉。tokendance 就因此从 95 条掉到 1 条，
  已在 `opencodex/setup-providers.sh` 改成 `--clear`（= all models）。stepfun 是 plan API、
  本来就只放 5 个，属预期。
