# 2026-10-02 面板卡死修复与三桥现状

## 面板「卡在 loading」根因与修复(已部署验证)

现象:面板进程存活、端口在听,但 GET /api/status 长时间不返回(实测 curl -m 10 超时,http=000)。

根因:tools/status_ui.py 的 collect_cached() 曾持有 _COLLECT_LOCK 跑完整个 collect();
其中 free_models()(subprocess timeout 60s)与 node_credits()(120s)在国际出口不可达时
逐个桥串行探测,轻易超过一分钟。COLLECT_TTL_SECONDS 只有 1.5s,所以每个过期后的轮询
都排在这一轮后面,页面永远 loading。

修复(commit e74fb20):

- stale-while-revalidate:缓存过期立即返回旧值(stale=True),由单个 daemon worker 后台重采;
  只有冷启动(无任何缓存)阻塞等第一轮
- free_models()/node_credits() subprocess 超时收紧到 25s,超时时保留上一次可用值(不把错误毒进缓存)
- 采集失败降级为空壳响应并释放 worker 槽位,面板不死
- 测试:tools/test_status_ui_stale_while_revalidate.py(4 例)

部署后实测:冷启动首轮 9.6s(此前 >90s),TTL 内 1.5ms,过期后 2-3ms。

## 三桥现状(2026-10-02 实测)

网络层(共同根因,属用户侧):系统代理 127.0.0.1:1082 隧道全 503,国际出口全不可达;
国内可达(gitee、tokendance.space、api.stepfun.com、dashscope.aliyuncs.com)。

- trae(8791):CN 账号「用户6781982309」额度耗尽(日志 quota dead,冷却 600s);国际账号
  「Q Micheal」网络不可达。桥已升级为账号池(credential_pool()/region_first(),commit 1fd66e2),
  额度只能等重置或加账号。
- gemini(8794):非 403 账号问题,日志为 urlopen Tunnel 503;token 19:29 仍可成功刷新,
  多账号池(~/.gemini2codex/auths/)在用。网络恢复即可恢复。
- antigravity(8797):进程活着(12 models),loadCodeAssist 阶段被代理隧道挡下;需在网络恢复后
  由操作员在已登录 Google 的浏览器点 VALI 链接(commit 7f4a8d0 透传)。
- qwen(8798):QWEN_API_KEY 为空(fleet.env:20-28 有说明),全盘无真实 key。
  新用户 70M 免费 token,需去 qwencloud 注册拿 key 填入 runtime/fleet.env 的 QWEN_API_KEY。

## 用户待办(代码侧无法解决)

1. 修复 MacPacket 国际节点或提供可用代理端口
2. trae CN 额度重置 / 增加账号
3. 注册 qwencloud 拿 API key 填入 runtime/fleet.env
4. antigravity 在网络恢复后点 VALI 链接完成授权
5. 网络恢复后重跑 POST /api/action/verify-real-calls,把 gemini/antigravity/trae
   从 free-windows.json 的 blocked 翻回可用

## 仓库状态

- 本地领先 gitee:0(gitee main = 1fd66e2,已同步)
- github origin 因国际出口不可达暂无法推送,网络恢复后执行:
  git push origin main

