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
finish.sh kimi-code 之后直接就能用，不用手贴 key。

- KIMI_UPSTREAM_KEY_FILE=<path> 指定别的位置
- KIMI_NO_APP_KEY=1 关掉回落（测试、或不想读 app 存储时）

环境变量永远优先于回落文件；/health 的 key_source 会说明当前用的是哪一路。

## 安装与使用

    bash install.sh --skip-deps --home '<FleetKit>/runtime'   # 同步代码 + 装服务
    bash bridges/finish.sh kimi-code --home '<FleetKit>/runtime'   # 重启 + 注入 catalog
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

## 注册表：一共十张，不是八张

上轮说「8 张桥表」，漏了两张真正决定能不能用的：

- install.sh 的 BRIDGES 数组——决定服务是否存在。漏了它，桥有代码但没进程。
- opencodex/setup-providers.sh 的 PROVIDERS 列表——决定 Codex 能不能调到。
  漏了它，桥在听、目录里有、但模型选择器里根本没有。

两座桥推出时正是只改了八张、漏了这两张，所以第一版测试全绿但跑不起来。
现在这两张也进了 test_bridge_tables_agree.py，一共十张互锁。

## 现状：全链路已通

    Codex 选择器 --> ocx 代理 10100 --> 本地桥 8802/8803 --> 上游
                                    --> FleetKit Anthropic 网关 8801 --> 同上

2026-10-03 实测：

- 8802 kimi /v1/models    -> 200，拉到上游实时 4 个模型
- 8803 minimax /v1/models -> 200，出静态 9 个模型
- 10100 ocx /v1/models    -> 231 个（原 218，新增 kimi 4 + minimax 9）
- 10100 -> minimax chat   -> 401，原文是 MiniMax 自己的
  login fail: Please carry the API secret key——链路通到厂商了
- 8801 -> kimi chat       -> 403 kimi_plan_inactive，带续费地址

注意 minimax 在 ocx 里是双前缀 minimax/minimax/MiniMax-M3。这是机队存量
行为（231 个里 58 个都双前缀：trae/cline/lingxi/qwen/xhx 全一样），不是新 bug。

换机器部署时若 10100 不认新模型，是代理缓存：

    launchctl kickstart -k gui/$(id -u)/com.opencodex.proxy

磁盘上的 opencodex-catalog.json 是对的，只有跑着的代理是旧的。


## 账号池：积分品牌的统一契约

kimi 与 minimax 都接上 bridges/plan_key_pool.py 的 KeyPool 契约（xhx/workbuddy
同款），先把 key 池化，调用时才决定用哪把：

    pool = KeyPool(brand, auth_dir, seed=lambda: read_keys(), read_points=reader)
    verdict = request_with_pool(pool, send, classify)

契约行为：

- 200 -> mark_success，这把 key 升为活跃
- 非 200 先过 classify：有 verdict -> mark_failure 并换下一把 key；
  无 verdict 原样返回上游响应
- 异常 -> PoolUnavailable（没 key）/ PoolExhausted（failures 里带每把 key 的
  (ref, status, body, reason, tag)）

classify 的 tag 分六类：key_dead / plan_inactive / forbidden / rate_limit /
upstream_5xx / transport。两个桥的差异只有归类口径：

- kimi：403 access_terminated_error = plan_inactive（套餐过期，key 本身是好的，
  单独措辞提示续费）
- minimax：401 = key_dead（提示里同时给换 key 和 MINIMAX_UPSTREAM 指到
  https://api.minimax.io/v1 两条路），403 = forbidden

播种 key（默认读 codex auth.json 里已有的账号 key，也可以用环境变量追加）：

    KIMI_API_KEYS=sk-a,sk-b     # 逗号分隔，追加为主 key 源
    MINIMAX_API_KEYS=sk-a,sk-b  # 同上

配额读取器返回 {"points","unit","plan","detail","error"}，tools/node_credits.py
按节点汇总，两座桥的 /health 里都有 account_pool 段（key 数、当前活跃 ref、
熔断剩余秒数）。

## 移植清单：机队每个积分品牌的池状态

契约只对「有多份可互换凭据」的品牌有意义——一把 key 的桥没有可切换的对象。
下表按 node_credits.py 的全量输出逐节点给出结论（2026-10-03）：

| 节点            | 积分口径     | 池状态                                        |
|-----------------|--------------|-----------------------------------------------|
| workbuddy       | client       | 已移植（account_pool.py，样板）               |
| workbuddy-gpt   | client       | 已移植（与 workbuddy 共用 core.py，auths 独立）|
| xhx             | client       | 已移植（account_pool.py，v0.2.1 显式加载）    |
| kimi-code       | subscription | 已移植（plan_key_pool.py，v1.2.0）            |
| minimax         | subscription | 已移植（plan_key_pool.py，v1.2.0，缺 key）    |
| gemini          | limit        | 自有 GeminiAccountPool（多谷歌账号轮换）      |
| zcode           | client       | 单 JWT + 自带 captcha 票据池，见下            |
| codely          | client       | 单会话，上游无余额端点                        |
| trae            | client       | 单会话，上游无余额端点                        |
| lingxi          | client       | 单会话，上游无余额端点                        |
| qoder           | limit        | 单 IDE 账号，上游未提供余额接口               |
| cline           | limit        | 单会话，上游无余额端点                        |
| catpaw          | unknown      | 上游未提供余额接口                            |
| antigravity     | limit        | 上游未提供余额接口                            |
| stepfun         | own          | ocx 网关 provider，无本地 client 积分         |
| tokendance      | own          | ocx 网关 provider，无本地 client 积分         |

zcode 单独说明：它确实燃烧套餐（ZCode Trust Build 100M tokens），但凭据只有
~/.zcode/v2/credentials.json 里的一把 OAuth JWT（safeStorage 加密，脱离本机
桌面端拿不到第二把），没有可轮换的凭据面。它真正的失败轴是每次调用都要消耗的
一次性 captchaVerifyParam——这条轴已经由 captcha 票据池（captcha-mint /
captcha-relay :8910 / captcha-pool-keeper.sh）解决，与 KeyPool 是不同的旋转轴，
不重复实现。

## 测试

    cd kit/tools
    ../../runtime/.venv/bin/python -m pytest test_kimi_bridge.py \
      test_minimax_bridge.py test_bridge_tables_agree.py -q   # 22 passed

