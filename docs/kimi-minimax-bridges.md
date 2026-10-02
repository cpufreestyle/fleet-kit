# 新增两桥：Kimi Code 与 MiniMax（2026-10-03）

把 Kimi Code（Moonshot 的 coding 套餐）和 MiniMax 的语言模型接进机队，
成为第 14、15 座本地桥。两座都是 OpenAI 兼容上游，所以桥本身很薄，
真正的工作全在「认错失败原因」上。

| 桥      | 端口 | label          | 本地 key 环境变量 | 上游 |
|---------|------|----------------|-------------------|------|
| kimi    | 8802 | kimi2codex     | KIMI2CODEX_KEY    | https://api.kimi.com/coding/v1 |
| minimax | 8803 | minimax2codex  | MINIMAX2CODEX_KEY | https://api.minimaxi.com/v1 |

端口从 offset 15/16 起算，避开 8796（status_ui）和 8801（anthropic 网关）。

## 上游实测（2026-10-03，真实调用）

Kimi Code，key 取自本机 Kimi 桌面版自己存储的那把：

    GET  https://api.kimi.com/coding/v1/models            -> 200
      kimi-for-coding            K2.8 Preview          1M 上下文
      kimi-for-coding-highspeed  K2.7 Code Highspeed   256K
      k3                         K3                    1M
      k3-256k                    K3-256k               256K
    POST https://api.kimi.com/coding/v1/chat/completions  -> 403
      {"error":{"type":"access_terminated_error",
        "message":"Your current subscription does not have access to Kimi Code
                   right now. Upgrade your plan to keep coding with Kimi Code:
                   https://www.kimi.com/code/#pricing"}}

MiniMax，本机没有 key，只确认路由与鉴权形态：

    GET  https://api.minimaxi.com/v1/models           -> 401 authorized_error(1004)
    POST https://api.minimaxi.com/v1/chat/completions -> 401 authorized_error(1004)
    POST https://api.minimax.cn/anthropic/v1/messages -> 401 authentication_error（要 X-Api-Key）

模型静态目录取自官方文档 platform.minimaxi.com/docs/guides/models-intro.md：
MiniMax-M3.1-Flash-Preview、MiniMax-M3、MiniMax-M2.7 / -highspeed、M2.5 /
-highspeed、M2.1 / -highspeed、M2，按质排序。

## 两个必须分开说的失败

**Kimi：套餐停了不等于 key 死了。** 同一个 key，/v1/models 回 200（key 是好的），
只有 chat 回 403 access_terminated_error。把这种情况说成「key 失效」会让人去换一把
根本没坏的 key。所以 kimi 桥把这类单独判出来：

    {"error":{"message":"kimi code has no active plan for this key (the key
      itself is accepted): ... -- renew at
      https://www.kimi.com/code/#pricing","type":"kimi_plan_inactive"}}

**MiniMax：key 被拒不等于 key 死了。** MiniMax 的 key 按区域签发，海外账号的 key 在
CN 主机上也是同样的 401。所以 minimax 桥的 401 提示里同时给出两种修法：换 key，
或者把 MINIMAX_UPSTREAM 指到 https://api.minimax.io/v1。

## kimi 的 key 从哪来

Kimi 桌面版把签发出去的 coding key 存在
~/Library/Application Support/kimi-desktop/daimon-share/daimon/kimi-code-key.json。
本桥在 KIMI_CODING_API_KEY 没给时回落到那份文件（只读），所以这台机器上
finish.sh kimi 之后直接就能用，不用手贴 key。

- KIMI_UPSTREAM_KEY_FILE=<path> 指定别的位置
- KIMI_NO_APP_KEY=1 关掉回落（测试、或不想读 app 存储时）

环境变量永远优先于回落文件；/health 的 key_source 会说明当前用的是哪一路。

## 安装与使用

    bash bridges/finish.sh kimi        # 装 launchd 服务 + 注入 catalog
    bash bridges/finish.sh minimax
    bash tools/status.sh               # 看两座桥的健康与 key 状态

Codex 里模型以 kimi/kimi-for-coding、minimax/MiniMax-M2.7 出现。

## 现状与下一步

- kimi：桥已通，/v1/models 拉的是上游实时目录；chat 要等套餐续费
  （https://www.kimi.com/code/#pricing），续完立刻可用，代码不用改。
- minimax：桥已通，静态目录可用；要一把按量计费 API Key 才能真调。
  注意 Token Plan 的订阅 Key 是另一种凭据，官方明示不能混用——订阅 Key 走
  https://www.minimax.cn/v1/token_plan/remains 查额度（见 plan_credits.py），
  不走本桥的 /v1。

两座桥都已注册进全部 8 张桥表（status.sh、status_ui.py、verify_real_calls.py、
fleet_chat_test.py、finish.sh、fleet_probe.py、default_model_guard.py、
fleet_platform.py），test_bridge_tables_agree.py 会一直盯着它们不再漏。

## 测试

    cd kit/tools
    ../../runtime/.venv/bin/python -m pytest test_kimi_bridge.py \
      test_minimax_bridge.py test_bridge_tables_agree.py -q   # 22 passed

