# codex-checkin Runbook — 订阅平台自动签到

日期：2026-09-25 | 状态：**已上线**（小浣熊任务实测 OK，LaunchAgent 每日 09:00 自动执行）

## 架构
```
launchd(com.local.codex-checkin) 每日 09:00 + RunAtLoad
   → ~/codex-checkin/checkin.py --daemon
       → 按任务注册表执行签到，写 ~/.codex-checkin/state.json 与 checkin.log
```
- 脚本：`~/codex-checkin/checkin.py`（复用 workbuddy2codex venv Python）
- 状态：`~/.codex-checkin/state.json`（每任务 last_success_date / 余额 / 详情）；日志 `~/.codex-checkin/checkin.log`
- launchd 日志：`/tmp/codex-checkin.log`
- 幂等：同一自然日（北京时间）已成功则跳过；桌面端当天启动过也会先领掉，脚本会记录“已发放过”

## 任务清单

### xhx — 商汤小浣熊 每日登录积分
- 契约（逆向自官方桌面端 app.asar）：`POST https://xiaohuanxiong.com/api/web/desktop/v1/login/points/grant`
  - 头：`Authorization: Bearer <access_token>`、`X-Client-Platform: desktop-macos`、`X-Client-Version: v1.0.28`
  - 无 body；响应 `data.granted=true` = 本次新发放；`false` = 今日已发放（桌面端启动时也会调，天然幂等）
- token 来自 `~/.box-agent/config/auth.json`；过期走单次轮换刷新（立即落盘），刷新失败重读盘（桌面端可能已重同步）
- 签到后顺带拉 `GET /api/web/points/v1/balance` 记账（available_points 等）

### 待扩展（有验证接口后按 TASKS 注册表加）
- 灵犀 / qoder / workbuddy / codely / trae：目前均未发现每日签到端点（codely 为预算制、trae 为积分制无签到接口）

## 使用方法
```bash
VENV=~/.local/node-v22.20.0-darwin-arm64/lib/node_modules/workbuddy2codex/.venv/bin/python
$VENV ~/codex-checkin/checkin.py --status          # 看今日是否已签、余额
$VENV ~/codex-checkin/checkin.py --run-now         # 手动签到（全部任务）
$VENV ~/codex-checkin/checkin.py --run-now xhx --force   # 强制某任务
tail -5 /tmp/codex-checkin.log                               # launchd 执行日志
launchctl kickstart -k gui/$(id -u)/com.local.codex-checkin  # 手动触发一次守护任务
```

## 排坑
1. 小浣熊 refresh_token 单次轮换——脚本刷新成功后立即原子写回 auth.json（0600）；失败时重读盘拿桌面端新同步的 token
2. 桌面 app 启动时会自行调用同一 grant 接口，所以“自动签到”的实际价值是：用户当天没开 app 时也能领到
3. 时间按 Asia/Shanghai 记“今日”，与平台结算一致
4. 新平台扩展：在 `TASKS` 注册表加 `{"desc":..., "fn": async (client) -> dict}`，返回 ok/detail/available_points 即可
