# 2026-09-26 修复记录：面板卡 loading + 反代理模型全部不可选

症状（用户报告）：
- `http://127.0.0.1:8796/` 显示「FleetKit 状态面板 / loading...」后永远不动。
- ChatGPT 里所有反代理模型都没法选；实际请求报 401 Unauthorized:
  `OpenAI account pool has no usable account credential, url: http://127.0.0.1:10100/v1/responses`。

## 根因 1：面板内联 JS 三元表达式写错，整个 script 块解析失败

`tools/status_ui.py` 的 `renderFree()` 末尾写成：

    rows.innerHTML=list.map(function(m){ ... }).join('')
      : '<tr>...空态...</tr>';

`?` 缺失，浏览器对整段 script 抛 `SyntaxError: Unexpected token ':'`。
因为脚本是一整块，**一个语法错就让 `load()` / `render()` 全都没定义**，页面停在初始的 `loading...`，
而且 `catch` 分支也进不去（不是 fetch 失败，是根本没跑）。

这类故障的特征：后端 `/api/status` 200 正常（curl 5.4s 返回完整 JSON），
但 DOM 里 `#meta` 仍是 `loading...`、`#cards` 为空。定位方式：

    curl -s http://127.0.0.1:8796/ -o dash.html
    node --check extracted.js

把 script 体抽出来用 `new vm.Script(js)`（或 `node --check`）会直接给出行号和截断内容，比逐行猜快得多。

修复：`list.map(...)` 改为基于 `list.length` 判断的三元。
顺带把该表达式里的字面量双引号换成中文引号，避免把 JS 字符串提前闭合。

## 根因 2：默认模型走的是 Codex 账号池，而池是空的

选择器里没有 `vendor/` 前缀的行（gpt-5.5 / gpt-5.6-* / gpt-6-* / step-3.7-flash）
不是反代理模型，而是 ocx 内置 `openai` provider 的账号池模型（`codexAccountMode: pool`）。

实测：

    ocx account list openai
    # openai  codex  main  Codex App login  0  needs-reauth
    # ~/.codex/auth.json 里只有一个 OPENAI_API_KEY，调 chatgpt.com 后端返回
    # 401 Could not parse your authentication token

账号池既没有有效 OAuth、也没有可用 key，所以这 8 行「看得见、选得动、一提交就 401」。
连 `subagentModels`（原本是 gpt-6-astra / gpt-6-sol / gpt-6-luna）和 `fastRows` 都踩同一个坑。

### 处置

1. `~/.opencodex/config.json`：`defaultProvider` 从 `openai` 改为 `trae`，
   `defaultModel` 设为 `trae/trae-step-5-preview`（用户偏好 step 5）。
2. `subagentModels` 改为舰队内可用模型：`trae/trae-step-5-preview` / `codely/codely-core` / `workbuddy-gpt/gpt-6-astra`。
3. `tools/catalog_filter.py` 新增 `--hide-native-when-pool-down`：
   用一次最小请求探测 `http://127.0.0.1:10100/v1/responses`，只有明确读到
   「pool has no usable account credential」的 401 才判定不可用并隐藏无前缀行；
   其他任何结果都按「可用」处理，避免误清空。
   `catalog-filter.sh install-timer` 生成的 plist 默认带这个开关，池恢复后下个周期自动加回。
4. 实测效果：8 个原生行隐藏，选择器 149 → 141，141 个反代理模型不受影响。

### 需要用户做的事（脚本不能代办）

在 Codex App / ChatGPT 里重新登录一次，让账号池恢复：

    ocx account list openai     # 应看到 main 不再是 needs-reauth

登录后 catalog 会在 300s 内自动把这 8 行加回。

## 顺带修掉的历史遗留：qwen 桥没有 key 却常驻

`QWEN2CODEX_KEY` 不在 `runtime/fleet.env` 里，qwen 桥（:8798）永远是 401，
面板告警、verify 判不可用、catalog 隐藏，但 launchd 和 ocx provider 都还挂着。

- 停掉 `com.local.qwen2codex`，`ocx provider remove qwen`。
- `install.sh`：key 为空时跳过该桥的 launchd agent，并提示去 `finish.sh <name>` 登录后重装。
- `opencodex/setup-providers.sh`：plist 里也读不到 key 时不再用 `local` 占位注册，直接跳过。
- `tools/catalog_filter.py` 的 `unavailable` 已含 qwen，行数保持 0。

## 验收（2026-09-26 21:30）

- 面板 `/api/status` 渲染：11 桥 / 10 up / 100 模型，warnings 为空。
- `POST /v1/responses` model=trae/trae-step-5-preview 返回 `status=completed`，中文回复正常。
- `ocx` healthz ok；catalog 141 行，native 0 行，qwen 0 行。

