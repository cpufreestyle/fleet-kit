"""Self-contained local status dashboard for codebuddy2openai."""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <link rel="icon" type="image/png" href="/assets/bridge-logo.png">
  <title>WorkBuddy Bridge v__BRIDGE_VERSION__</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #07090d;
      --surface: rgba(23, 26, 33, .78);
      --surface-solid: #15181f;
      --surface-raised: rgba(255, 255, 255, .045);
      --surface-hover: rgba(255, 255, 255, .072);
      --border: rgba(255, 255, 255, .105);
      --border-strong: rgba(255, 255, 255, .18);
      --text: #f5f5f7;
      --muted: #9b9ba3;
      --muted-strong: #b8b8bf;
      --accent: #30d158;
      --accent-soft: rgba(48, 209, 88, .12);
      --warning: #ffd60a;
      --danger: #ff453a;
      --promotion: #ffcc66;
      --focus: #64d2ff;
      --radius-lg: 24px;
      --radius-md: 16px;
      --radius-sm: 12px;
      --ease-out: cubic-bezier(.23, 1, .32, 1);
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Segoe UI", sans-serif;
      font-synthesis: none;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-width: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at 12% -8%, rgba(48, 209, 88, .13), transparent 31rem),
        radial-gradient(circle at 92% 8%, rgba(100, 210, 255, .09), transparent 30rem),
        var(--bg);
      color: var(--text);
      line-height: 1.47;
      letter-spacing: -.006em;
      -webkit-font-smoothing: antialiased;
    }

    button, input { font: inherit; }
    button:focus-visible, input:focus-visible {
      outline: 3px solid color-mix(in srgb, var(--focus) 58%, transparent);
      outline-offset: 3px;
    }

    .shell {
      width: min(1120px, calc(100% - 2.5rem));
      margin: 0 auto;
      padding: 2rem 0 2.75rem;
    }

    header {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 1.5rem;
      padding: 1.15rem;
      background: linear-gradient(135deg, rgba(32, 36, 45, .82), rgba(17, 20, 26, .66));
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .07), 0 24px 64px rgba(0, 0, 0, .28);
      backdrop-filter: blur(24px) saturate(140%);
      -webkit-backdrop-filter: blur(24px) saturate(140%);
    }

    .brand { display: flex; align-items: flex-start; gap: 1rem; min-width: 0; }
    .brand-logo {
      width: 4rem;
      height: 4rem;
      flex: 0 0 auto;
      border: 1px solid rgba(255, 255, 255, .12);
      border-radius: 17px;
      object-fit: cover;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .1), 0 10px 28px rgba(0, 0, 0, .3);
    }
    .brand-copy {
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-width: 0;
      height: 4rem;
    }

    h1, h2, p { margin: 0; }
    h1 { font-size: clamp(1.55rem, 4vw, 2.1rem); line-height: 1.05; letter-spacing: -.042em; font-weight: 720; }
    h2 { font-size: 1.05rem; line-height: 1.25; letter-spacing: -.02em; }
    .subtitle { color: var(--muted); line-height: 1; font-size: .92rem; }

    .local-badge, .version-badge {
      padding: .38rem .65rem;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: rgba(0, 0, 0, .16);
      font-size: .75rem;
    }
    .local-badge { color: var(--muted-strong); font-variant-numeric: tabular-nums; }
    .header-meta { display: flex; align-items: center; gap: .45rem; flex: 0 0 auto; }
    .version-badge {
      border-color: color-mix(in srgb, var(--accent) 36%, var(--border));
      color: #8df0a6;
      background: color-mix(in srgb, var(--accent) 8%, rgba(0, 0, 0, .12));
      font-family: "Cascadia Code", "SFMono-Regular", Consolas, monospace;
      font-weight: 700;
    }

    .overview {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      margin: 1rem 0;
      overflow: hidden;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .035);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
    }

    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .04), 0 18px 46px rgba(0, 0, 0, .16);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
    }

    .metric { min-width: 0; padding: .9rem 1rem; border-right: 1px solid var(--border); }
    .metric:last-child { border-right: 0; }
    .metric-label { color: var(--muted); font-size: .72rem; letter-spacing: .01em; }
    .metric-value { display: flex; align-items: center; gap: .5rem; margin-top: .28rem; font-size: .92rem; font-weight: 650; }

    .dot {
      width: .5rem;
      height: .5rem;
      border-radius: 50%;
      background: var(--muted);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--muted) 12%, transparent);
    }
    .dot.ok { background: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 13%, transparent), 0 0 12px color-mix(in srgb, var(--accent) 48%, transparent); }
    .dot.warn { background: var(--warning); box-shadow: 0 0 0 3px color-mix(in srgb, var(--warning) 13%, transparent); }
    .dot.error { background: var(--danger); box-shadow: 0 0 0 3px color-mix(in srgb, var(--danger) 13%, transparent); }

    main { display: grid; grid-template-columns: minmax(0, 1.28fr) minmax(280px, .72fr); gap: 1rem; }
    .panel { padding: 1.25rem; }
    .panel-heading { margin-bottom: .95rem; }
    .panel-heading p { max-width: 66ch; margin-top: .35rem; color: var(--muted); font-size: .84rem; }

    .status-line {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      padding: .95rem 1rem;
      background: rgba(0, 0, 0, .2);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
    }
    .status-copy strong { display: block; font-size: 1.05rem; }
    .status-copy span { display: block; margin-top: .25rem; color: var(--muted); font-size: .85rem; }

    button {
      min-height: 2.6rem;
      padding: .62rem .9rem;
      border: 1px solid color-mix(in srgb, var(--accent) 62%, var(--border));
      border-radius: 11px;
      color: #fff;
      background: linear-gradient(180deg, #35cb58, #25a947);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .22), 0 7px 18px rgba(21, 145, 61, .18);
      cursor: pointer;
      font-weight: 650;
      transition: transform 140ms var(--ease-out), opacity 160ms ease;
    }
    button:active:not(:disabled) { transform: scale(.975); }
    button:disabled { cursor: default; opacity: .48; box-shadow: none; }

    .details {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: .6rem;
      margin-top: 1rem;
    }
    .detail { padding: .8rem; background: var(--surface-raised); border: 1px solid rgba(255, 255, 255, .035); border-radius: var(--radius-sm); }
    .detail span { display: block; color: var(--muted); font-size: .75rem; }
    .detail strong { display: block; margin-top: .22rem; font-size: .9rem; font-variant-numeric: tabular-nums; }

    .endpoint-list { display: grid; gap: .65rem; }
    .endpoint {
      display: flex;
      justify-content: space-between;
      gap: 1rem;
      padding-bottom: .65rem;
      border-bottom: 1px solid var(--border);
    }
    .endpoint:last-child { padding-bottom: 0; border-bottom: 0; }
    .endpoint span { color: var(--muted); font-size: .84rem; }
    code { color: #d1d1d6; font-family: "SFMono-Regular", "Cascadia Code", Consolas, monospace; font-size: .78rem; }

    .route-panel { position: relative; margin-top: 1rem; overflow: hidden; }
    .route-panel::before {
      position: absolute;
      inset: 0 0 auto;
      height: 1px;
      background: linear-gradient(90deg, transparent 5%, rgba(100, 210, 255, .28), rgba(48, 209, 88, .35), transparent 95%);
      content: "";
      pointer-events: none;
    }
    .route-summary {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      margin-bottom: 1rem;
      padding: .78rem .9rem;
      background: rgba(0, 0, 0, .2);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
    }
    .route-summary span { color: var(--muted); font-size: .85rem; }
    .route-summary code { color: #8df0a6; font-size: .88rem; font-weight: 700; overflow-wrap: anywhere; text-align: right; }
    fieldset { min-width: 0; margin: 0; padding: 0; border: 0; }
    legend { margin-bottom: .55rem; font-size: .88rem; font-weight: 650; }
    .model-toolbar { display: flex; align-items: center; justify-content: flex-end; gap: .4rem; margin-bottom: .75rem; }
    .sort-label { margin-right: .2rem; color: var(--muted); font-size: .75rem; }
    .sort-button {
      min-height: 2.05rem;
      padding: .3rem .6rem;
      color: var(--muted);
      background: rgba(255, 255, 255, .025);
      border-color: var(--border);
      border-radius: 9px;
      box-shadow: none;
      font-size: .74rem;
      font-weight: 600;
    }
    .sort-button[aria-pressed="true"] { color: #a7f3b8; border-color: rgba(48, 209, 88, .44); background: var(--accent-soft); }
    .model-catalog-collapse {
      margin-top: .2rem;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      background: var(--surface-raised);
    }
    .model-catalog-collapse summary {
      list-style: none;
      cursor: pointer;
      user-select: none;
      display: flex;
      align-items: center;
      gap: .5rem;
      padding: .65rem .85rem;
      color: var(--muted-strong);
      font-size: .86rem;
      font-weight: 600;
      transition: color 160ms ease;
    }
    .model-catalog-collapse summary::-webkit-details-marker { display: none; }
    .model-catalog-collapse summary::before {
      content: "▸";
      display: inline-block;
      transition: transform 180ms var(--ease-out);
      color: var(--muted);
    }
    .model-catalog-collapse[open] summary::before { transform: rotate(90deg); }
    .model-catalog-collapse summary:hover { color: var(--text); }
    .model-catalog-collapse[open] .model-list { padding: .1rem .8rem .8rem; }
    .model-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .65rem; }
    .model-option {
      display: flex;
      align-items: flex-start;
      gap: .65rem;
      min-width: 0;
      min-height: 5.75rem;
      padding: .78rem;
      background: var(--surface-raised);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      cursor: pointer;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .025);
      transition: transform 160ms var(--ease-out), opacity 160ms ease;
    }
    .model-option:active { transform: scale(.988); }
    .model-option.selected {
      border-color: rgba(48, 209, 88, .58);
      background: linear-gradient(145deg, rgba(48, 209, 88, .14), rgba(48, 209, 88, .055));
      box-shadow: inset 0 1px 0 rgba(170, 255, 190, .08), 0 8px 24px rgba(0, 0, 0, .12);
    }
    .model-option input {
      width: 1rem;
      height: 1rem;
      flex: 0 0 auto;
      margin: .18rem 0 0;
      appearance: none;
      border: 1px solid rgba(255, 255, 255, .3);
      border-radius: 50%;
      background: rgba(0, 0, 0, .18);
    }
    .model-option input:checked { border-color: var(--accent); box-shadow: inset 0 0 0 3px var(--surface-solid), inset 0 0 0 8px var(--accent); }
    .model-copy { min-width: 0; width: 100%; }
    .model-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: .55rem; }
    .model-name-row { display: flex; align-items: center; flex-wrap: wrap; gap: .3rem; min-width: 0; }
    .model-name-row .model-name { min-width: 0; overflow-wrap: anywhere; font-weight: 700; font-size: .88rem; }
    .model-name-row .model-badge { margin: 0; }
    .model-id { margin-top: .12rem; color: var(--muted); font-family: "SFMono-Regular", "Cascadia Code", Consolas, monospace; font-size: .66rem; }
    .model-credit {
      flex: 0 0 auto;
      padding: .13rem .42rem;
      border: 1px solid var(--border-strong);
      border-radius: 999px;
      color: #d1d1d6;
      background: rgba(0, 0, 0, .16);
      font-family: "SFMono-Regular", "Cascadia Code", Consolas, monospace;
      font-size: .68rem;
      font-weight: 700;
    }
    .model-credit .original { margin-right: .28rem; color: var(--muted); text-decoration: line-through; }
    .model-tags { display: flex; flex-wrap: wrap; gap: .3rem; margin-top: .55rem; }
    .model-badge {
      padding: .12rem .38rem;
      border: 1px solid color-mix(in srgb, var(--focus) 38%, var(--border));
      border-radius: 999px;
      color: #b8e7ff;
      background: color-mix(in srgb, var(--focus) 8%, transparent);
      font-size: .65rem;
    }
    .model-badge.promotion { border-color: color-mix(in srgb, var(--promotion) 38%, var(--border)); color: #ffe2a6; background: color-mix(in srgb, var(--promotion) 8%, transparent); }
    .model-badge.vision { border-color: rgba(100,210,255,.45); color: #b8e7ff; background: rgba(100,210,255,.1); }
    .model-badge.vision-weak { border-color: rgba(255,214,10,.4); color: #ffe47a; background: rgba(255,214,10,.09); }
    .model-badge.vision-no { border-color: var(--border); color: var(--muted); background: rgba(255,255,255,.04); }
    .model-badge.image-gen { border-color: rgba(255,204,102,.45); color: #ffe2a6; background: rgba(255,204,102,.1); }
    .metadata-note { margin-top: .7rem; color: var(--muted); font-size: .75rem; }
    .route-actions { display: flex; align-items: center; gap: .85rem; margin-top: 1rem; }
    .route-message { color: var(--muted); font-size: .85rem; }
    .route-message.ok { color: #8df0a6; }
    .route-message.error { color: #ff9f98; }

    .account-panel { position: relative; margin-top: 1rem; padding: 1rem; overflow: hidden; }
    .account-panel::before {
      position: absolute;
      inset: 0 0 auto;
      height: 1px;
      background: linear-gradient(90deg, transparent 4%, rgba(48, 209, 88, .58), rgba(100, 210, 255, .32), transparent 96%);
      content: "";
      pointer-events: none;
    }
    .account-panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: .75rem; }
    .account-toolbar { display: grid; grid-template-columns: repeat(3, max-content); justify-content: end; gap: .4rem; min-width: 0; }
    .account-toolbar button { min-height: 2.05rem; padding: .3rem .52rem; font-size: .72rem; white-space: nowrap; }
    .button-label-compact { display: none; }
    .secondary-button, .danger-button {
      min-height: 2.25rem;
      padding: .42rem .68rem;
      box-shadow: none;
      font-size: .76rem;
    }
    .secondary-button { color: #a7f3b8; border-color: rgba(48, 209, 88, .3); background: var(--accent-soft); }
    .danger-button { color: #ffaaa4; border-color: rgba(255, 69, 58, .28); background: rgba(255, 69, 58, .09); }
    .account-intro {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: .75rem;
      margin-bottom: .7rem;
      padding: .6rem .75rem;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      background: rgba(0, 0, 0, .18);
      color: var(--muted);
      font-size: .78rem;
    }
    .pool-stats { display: flex; flex-wrap: wrap; align-items: center; gap: .45rem .8rem; }
    .pool-stat { display: inline-flex; align-items: center; gap: .38rem; color: var(--muted-strong); }
    .pool-stat strong { color: var(--text); font-variant-numeric: tabular-nums; }
    .folder-link {
      min-height: auto;
      max-width: 50%;
      padding: .15rem 0;
      border: 0;
      color: #8df0a6;
      background: transparent;
      box-shadow: none;
      text-align: right;
      font-size: .75rem;
      font-weight: 600;
      overflow-wrap: anywhere;
    }
    .account-list { display: grid; grid-template-columns: minmax(0, 1fr); gap: .6rem; }
    .account-card {
      min-width: 0;
      padding: .8rem;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      background: linear-gradient(145deg, rgba(255,255,255,.052), rgba(255,255,255,.026));
      box-shadow: inset 0 1px 0 rgba(255,255,255,.035);
    }
    .account-card.primary { border-color: rgba(48, 209, 88, .28); background: linear-gradient(145deg, rgba(48,209,88,.075), rgba(255,255,255,.025)); }
    .account-head { display: grid; grid-template-columns: 2.25rem minmax(0, 1fr) auto; align-items: center; gap: .55rem; }
    .account-avatar { width: 2.25rem; height: 2.25rem; display: grid; place-items: center; border-radius: 11px; color: #07100a; background: linear-gradient(145deg, #9af4af, var(--accent)); font-weight: 800; box-shadow: inset 0 1px 0 rgba(255,255,255,.3); }
    .account-title-row { display: flex; align-items: center; gap: .35rem; min-width: 0; }
    .account-name { min-width: 0; overflow: hidden; flex: 0 1 auto; text-overflow: ellipsis; white-space: nowrap; font-size: .9rem; font-weight: 720; }
    .account-primary-badge { flex: 0 0 auto; padding: .13rem .42rem; border-radius: 999px; color: #07100a; background: #9af4af; font-size: .62rem; font-weight: 780; text-align: center; }
    .account-meta { margin-top: .12rem; overflow-wrap: anywhere; color: var(--muted); font: .68rem/1.35 "SFMono-Regular", Consolas, monospace; }
    .account-state { padding: .2rem .45rem; border-radius: 999px; font-size: .68rem; font-weight: 700; }
    .account-state.ready { color: #8df0a6; background: rgba(48, 209, 88, .11); }
    .account-state.cooling, .account-state.expired { color: #ffe47a; background: rgba(255, 214, 10, .1); }
    .account-state.error { color: #ffaaa4; background: rgba(255, 69, 58, .1); }
    .account-flags { display: flex; flex-wrap: wrap; gap: .3rem; margin-top: .5rem; }
    .account-flag { padding: .12rem .4rem; border: 1px solid var(--border); border-radius: 999px; color: var(--muted-strong); font-size: .65rem; }
    .account-flag.primary { color: #a7f3b8; border-color: rgba(48, 209, 88, .32); }
    .quota-grid { display: grid; grid-template-columns: minmax(7rem, .72fr) minmax(0, 1.28fr); gap: .75rem; align-items: end; margin-top: .65rem; padding: .65rem .7rem; border: 1px solid rgba(255,255,255,.055); border-radius: var(--radius-sm); background: rgba(0,0,0,.16); }
    .quota-label { color: var(--muted); font-size: .68rem; }
    .quota-balance { margin-top: .18rem; font-size: 1.22rem; line-height: 1.05; letter-spacing: -.025em; font-weight: 750; font-variant-numeric: tabular-nums; }
    .quota-detail { display: grid; grid-template-columns: max-content minmax(0, 1fr); justify-content: space-between; gap: .7rem; color: var(--muted-strong); font-size: .7rem; font-variant-numeric: tabular-nums; white-space: nowrap; }
    .quota-detail span { min-width: 0; white-space: nowrap; }
    .quota-detail span:last-child { overflow: hidden; text-overflow: ellipsis; text-align: right; }
    .quota-progress { height: .38rem; margin-top: .42rem; overflow: hidden; border-radius: 999px; background: rgba(255,255,255,.08); }
    .quota-progress span { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--accent), var(--focus)); transition: width 320ms var(--ease-out); }
    .quota-unavailable { grid-column: 1 / -1; color: var(--muted); font-size: .75rem; }
    .account-actions { display: flex; flex-wrap: wrap; gap: .35rem; margin-top: .6rem; }
    .account-empty { grid-column: 1 / -1; padding: 2rem 1rem; border: 1px dashed var(--border-strong); border-radius: var(--radius-md); text-align: center; color: var(--muted); }
    .account-empty strong { display: block; margin-bottom: .3rem; color: var(--text); }
    .account-empty p { max-width: 34rem; margin: 0 auto .9rem; }
    .account-empty button { width: auto; }
    .account-message { min-height: 1rem; margin-top: .5rem; color: var(--muted); font-size: .74rem; }
    .account-message.ok { color: #8df0a6; }
    .account-message.error { color: #ffaaa4; }

    dialog {
      width: min(680px, calc(100% - 1.25rem));
      max-height: calc(100vh - 1.25rem);
      padding: 0;
      overflow: hidden;
      border: 1px solid var(--border-strong);
      border-radius: var(--radius-lg);
      color: var(--text);
      background: #12161c;
      box-shadow: 0 36px 96px rgba(0,0,0,.62), inset 0 1px 0 rgba(255,255,255,.06);
    }
    dialog::backdrop { background: rgba(0,0,0,.68); backdrop-filter: blur(8px); }
    .dialog-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; padding: 1.1rem 1.15rem .9rem; }
    .dialog-head p { margin-top: .3rem; color: var(--muted); font-size: .8rem; }
    .dialog-close { width: 2.25rem; min-height: 2.25rem; padding: 0; border-color: var(--border); border-radius: 50%; color: var(--muted-strong); background: var(--surface-raised); box-shadow: none; font-size: 1.2rem; }
    .login-frame-shell { position: relative; min-height: min(300px, 42vh); margin: 0 1.15rem; overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius-md); background: #f7f8fa; }
    .login-placeholder { min-height: min(300px, 42vh); display: grid; place-items: center; padding: 2rem; color: #48515b; background: #f7f8fa; text-align: center; }
    .login-placeholder[hidden] { display: none; }
    .login-placeholder strong { display: block; color: #152019; font-size: 1rem; }
    .login-placeholder span { display: block; margin-top: .4rem; font-size: .82rem; }
    .login-launch-card { display: grid; justify-items: center; max-width: 25rem; }
    .login-launch-icon { display: grid; place-items: center; width: 3rem; height: 3rem; margin-bottom: .8rem; border-radius: 1rem; color: #16833a; background: #dcfce5; font-size: 1.4rem; font-weight: 760; }
    .login-launch-card .secondary-button { margin-top: 1.1rem; }
    .login-status { display: flex; align-items: center; justify-content: space-between; gap: 1rem; min-height: 3.2rem; padding: .75rem 1.15rem; color: var(--muted); font-size: .78rem; }
    .login-status strong { color: #a7f3b8; font-weight: 680; }
    .dialog-actions { display: flex; justify-content: flex-end; gap: .5rem; padding: 0 1.15rem 1.1rem; }

    .notice { margin-top: 1rem; color: var(--muted); font-size: .8rem; }
    footer { margin-top: 1.35rem; color: #6f7078; font-size: .75rem; text-align: center; }

    @media (hover: hover) and (pointer: fine) {
      button:hover:not(:disabled) { opacity: .92; }
      .sort-button:hover { color: var(--text); background: var(--surface-hover); }
      .model-option:hover { border-color: var(--border-strong); background-color: var(--surface-hover); }
      .model-option.selected:hover { border-color: rgba(48, 209, 88, .7); }
    }

    @media (max-width: 820px) {
      .shell { width: min(100% - 1.25rem, 1120px); padding-top: .65rem; }
      header { align-items: stretch; }
      .header-meta { flex-wrap: wrap; }
      .overview { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .metric:nth-child(3) { border-right: 0; }
      .metric:nth-child(n+4) { border-top: 1px solid var(--border); }
      main { grid-template-columns: 1fr; }
      .model-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .account-list { grid-template-columns: 1fr; }
    }

    @media (max-width: 560px) {
      .shell { width: min(100% - .75rem, 1120px); }
      header { flex-direction: column; padding: 1rem; border-radius: 20px; }
      .brand-logo { width: 3.6rem; height: 3.6rem; border-radius: 15px; }
      .brand-copy { height: 3.6rem; }
      .panel { padding: 1rem; }
      .account-panel { padding: .85rem; }
      .overview { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .metric, .metric:nth-child(3) { border-right: 1px solid var(--border); border-top: 1px solid var(--border); }
      .metric:nth-child(-n+2) { border-top: 0; }
      .metric:nth-child(even) { border-right: 0; }
      .metric:last-child { grid-column: 1 / -1; border-right: 0; }
      .status-line { align-items: flex-start; flex-direction: column; }
      .route-summary, .route-actions { align-items: flex-start; flex-direction: column; }
      .account-panel-head, .account-intro { align-items: stretch; flex-direction: column; }
      .account-toolbar { width: 100%; grid-template-columns: repeat(3, minmax(0, 1fr)); justify-content: stretch; gap: .35rem; }
      .folder-link { max-width: 100%; text-align: left; }
      .route-summary code { text-align: left; }
      .model-toolbar { align-items: flex-end; flex-direction: column; }
      .model-list { grid-template-columns: 1fr; }
      .account-list { grid-template-columns: 1fr; }
      .quota-grid { grid-template-columns: 1fr; gap: .65rem; }
      button { width: 100%; }
      .sort-button { width: auto; }
      .account-toolbar button { width: 100%; min-width: 0; padding-right: .3rem; padding-left: .3rem; }
      .account-toolbar .button-label-wide { display: none; }
      .account-toolbar .button-label-compact { display: inline; }
      .account-actions button { width: auto; }
      .account-empty button { width: auto; }
      .dialog-actions { flex-wrap: wrap; }
      .dialog-actions button { width: auto; }
      .login-frame-shell { min-height: min(280px, 40vh); margin: 0 .75rem; }
      .dialog-head, .login-status, .dialog-actions { padding-left: .75rem; padding-right: .75rem; }
    }

    @media (prefers-reduced-motion: reduce) {
      button, .model-option, .model-option input, .quota-progress span { transition-duration: .01ms; }
      button:active:not(:disabled), .model-option:active { transform: none; }
    }

    @media (prefers-reduced-transparency: reduce) {
      header, .panel, .overview { background: var(--surface-solid); backdrop-filter: none; -webkit-backdrop-filter: none; }
    }

    /* Buddy 加油站 — apple-design: restrained material, instant press feedback */
    .checkin-panel { margin-top: 1.1rem; }
    .checkin-summary {
      padding: .9rem 1rem;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      background: linear-gradient(145deg, rgba(48,209,88,.07), rgba(255,255,255,.022));
      box-shadow: inset 0 1px 0 rgba(255,255,255,.04);
    }
    .checkin-hero {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      flex-wrap: wrap;
    }
    .checkin-hero-main { display: flex; flex-direction: column; gap: .1rem; min-width: 0; }
    .checkin-activity {
      font-size: .76rem;
      color: var(--muted);
      letter-spacing: .01em;
    }
    .checkin-daily {
      font-size: 2rem;
      line-height: 1.05;
      letter-spacing: -.02em;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
      color: var(--text);
    }
    .checkin-daily-label { font-size: .76rem; color: var(--muted); }
    .checkin-claim {
      appearance: none;
      border: 1px solid rgba(48,209,88,.32);
      background: linear-gradient(180deg, rgba(48,209,88,.92), rgba(40,180,76,.92));
      color: #04130a;
      font-weight: 650;
      font-size: .92rem;
      padding: .6rem 1.25rem;
      border-radius: 999px;
      cursor: pointer;
      transition: transform 120ms ease-out, box-shadow 160ms ease-out, opacity 160ms ease-out;
      box-shadow: 0 6px 18px rgba(48,209,88,.22);
      white-space: nowrap;
    }
    .checkin-claim:hover { box-shadow: 0 8px 22px rgba(48,209,88,.3); }
    .checkin-claim:active { transform: scale(.96); }
    .checkin-claim:focus-visible { outline: 3px solid color-mix(in srgb, var(--focus) 58%, transparent); outline-offset: 3px; }
    .checkin-claim[disabled] {
      opacity: .5;
      cursor: default;
      box-shadow: none;
      transform: none;
    }
    .checkin-claim.done {
      background: linear-gradient(180deg, rgba(255,255,255,.1), rgba(255,255,255,.05));
      color: var(--muted-strong);
      border-color: var(--border);
      box-shadow: none;
    }
    .checkin-stats {
      display: flex;
      flex-wrap: wrap;
      gap: .9rem;
      margin-top: .8rem;
      color: var(--muted-strong);
      font-size: .84rem;
    }
    .checkin-stat { display: inline-flex; align-items: center; gap: .38rem; }
    .checkin-stat strong { color: var(--text); font-variant-numeric: tabular-nums; }
    .checkin-list { display: grid; grid-template-columns: minmax(0, 1fr); gap: .5rem; margin-top: .8rem; }
    .checkin-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: .75rem;
      padding: .6rem .8rem;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      background: var(--surface-raised);
      transition: border-color 160ms ease-out, background 160ms ease-out;
    }
    .checkin-item.claimed { border-color: rgba(48,209,88,.26); }
    .checkin-item .ci-name { font-weight: 600; font-size: .9rem; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .checkin-item .ci-meta { font-size: .78rem; color: var(--muted); }
    .checkin-badge {
      flex: none;
      font-size: .74rem;
      font-weight: 600;
      padding: .18rem .6rem;
      border-radius: 999px;
      letter-spacing: .01em;
    }
    .checkin-badge.done { color: #8df0a6; background: rgba(48,209,88,.12); }
    .checkin-badge.todo { color: #ffd60a; background: rgba(255,214,10,.12); }
    .checkin-message { min-height: 1rem; margin-top: .5rem; color: var(--muted); font-size: .74rem; }
    .checkin-message.ok { color: #8df0a6; }
    .checkin-message.error { color: #ffaaa4; }

    /* Apiget 外部网关 — 与 Buddy 加油站同构的克制风格 */
    .apiget-panel { margin-top: 1.1rem; }
    .apiget-summary {
      display: inline-flex;
      align-items: center;
      gap: .5rem;
      padding: .5rem .9rem;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--surface-raised);
      font-size: .84rem;
      color: var(--muted-strong);
    }
    .apiget-summary.ok { color: #8df0a6; }
    .apiget-summary.error { color: #ffaaa4; }
    .apiget-list { display: grid; grid-template-columns: minmax(0, 1fr); gap: .4rem; margin-top: .7rem; }
    .apiget-gateways { display: grid; grid-template-columns: minmax(0, 1fr); gap: .75rem; margin-top: .75rem; }
    .apiget-gw-card {
      padding: .85rem .95rem;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      background: linear-gradient(145deg, rgba(255,255,255,.05), rgba(255,255,255,.024));
      box-shadow: inset 0 1px 0 rgba(255,255,255,.035);
    }
    .apiget-gw-head { display: flex; align-items: center; justify-content: space-between; gap: .6rem; flex-wrap: wrap; }
    .apiget-gw-name { font-weight: 700; font-size: .94rem; }
    .apiget-gw-badges { display: flex; align-items: center; gap: .4rem; flex-wrap: wrap; }
    .apiget-gw-status { font-size: .72rem; font-weight: 600; padding: .14rem .5rem; border-radius: 999px; }
    .apiget-gw-status.ok { color: #8df0a6; background: rgba(48,209,88,.12); }
    .apiget-gw-status.err { color: #ffaaa4; background: rgba(255,69,58,.12); }
    .apiget-gw-status.proto { color: #b8e7ff; background: rgba(100,210,255,.1); }
    .apiget-gw-meta { margin-top: .35rem; color: var(--muted); font-size: .74rem; overflow-wrap: anywhere; }
    .gw-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: .8rem; flex-wrap: wrap; }
    .gw-heading > div { min-width: 0; }
    .gw-form {
      display: grid;
      gap: .7rem;
      padding: 1rem 1.15rem 1.2rem;
      max-height: calc(100vh - 9rem);
      overflow-y: auto;
      overscroll-behavior: contain;
    }
    .gw-form label { display: grid; gap: .3rem; color: var(--muted-strong); font-size: .8rem; font-weight: 600; }
    .gw-form select {
      width: 100%;
      padding: .55rem .7rem;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      background: rgba(0,0,0,.25);
      color: var(--text);
      font: inherit;
      font-size: .9rem;
    }
    .gw-form input {
      width: 100%;
      padding: .55rem .7rem;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      background: rgba(0,0,0,.25);
      color: var(--text);
      font: inherit;
      font-size: .9rem;
    }
    .gw-form input:focus { outline: 3px solid color-mix(in srgb, var(--focus) 40%, transparent); outline-offset: 2px; border-color: var(--focus); }
    .gw-key-row { display: flex; align-items: center; gap: .4rem; }
    .gw-key-row input { flex: 1 1 auto; min-width: 0; }
    .gw-key-toggle {
      appearance: none;
      flex: 0 0 auto;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 2.4rem;
      height: 2.4rem;
      padding: 0;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      background: rgba(0,0,0,.25);
      color: var(--muted-strong);
      cursor: pointer;
      transition: color 160ms ease, border-color 160ms ease, transform 120ms ease-out;
    }
    .gw-key-toggle:hover { color: var(--text); border-color: var(--border-strong); }
    .gw-key-toggle:active { transform: scale(.95); }
    .gw-form-row { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; }
    .gw-form-actions {
      position: sticky;
      bottom: 0;
      justify-content: flex-end;
      margin-top: 0;
      padding-top: .6rem;
      background: #12161c;
      border-top: 1px solid var(--border);
    }
    .gw-note { color: var(--muted); font-size: .74rem; }
    .gw-model-list { display: block; max-height: 280px; overflow: auto; padding: .4rem; border: 1px solid var(--border); border-radius: var(--radius-sm); background: rgba(0,0,0,.16); }
    .gw-model-group { margin-bottom: .35rem; }
    .gw-model-group:last-child { margin-bottom: 0; }
    .gw-model-group-head { display: flex; align-items: center; justify-content: space-between; gap: .5rem; padding: .2rem .3rem .35rem; color: var(--muted-strong); font-size: .76rem; font-weight: 650; }
    .gw-select-all { display: inline-flex; align-items: center; gap: .35rem; color: var(--muted); font-size: .74rem; font-weight: 550; cursor: pointer; }
    .gw-model-items { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: .4rem; }
    .gw-model-item { display: flex; align-items: center; gap: .45rem; padding: .3rem .5rem; border-radius: 8px; background: var(--surface-raised); font-size: .78rem; }
    .gw-model-item input { width: auto; }
    .gw-message { min-height: 1rem; color: var(--muted); font-size: .78rem; }
    .gw-message.ok { color: #8df0a6; }
    .gw-message.error { color: #ffaaa4; }
    .primary-button {
      appearance: none;
      border: 1px solid color-mix(in srgb, var(--accent) 62%, var(--border));
      background: linear-gradient(180deg, #35cb58, #25a947);
      color: #fff;
      font-weight: 650;
      font-size: .86rem;
      padding: .5rem 1rem;
      border-radius: 11px;
      cursor: pointer;
      min-height: 2.4rem;
      box-shadow: inset 0 1px 0 rgba(255,255,255,.22), 0 7px 18px rgba(21,145,61,.18);
      transition: transform 140ms var(--ease-out), opacity 160ms ease;
    }
    .primary-button:active:not(:disabled) { transform: scale(.975); }
    .primary-button:disabled { opacity: .48; cursor: default; box-shadow: none; }
    .gw-card-actions { display: inline-flex; gap: .35rem; }
    .gw-card-action {
      appearance: none;
      border: 1px solid var(--border);
      background: var(--surface-raised);
      color: var(--muted-strong);
      font-size: .72rem;
      font-weight: 600;
      padding: .25rem .55rem;
      border-radius: 999px;
      cursor: pointer;
      transition: color 160ms ease, border-color 160ms ease;
    }
    .gw-card-action:hover { color: var(--text); border-color: var(--border-strong); }
    .gw-card-action.danger:hover { color: #ffaaa4; border-color: rgba(255,69,58,.4); }
    .gw-card-action.on { color: #8df0a6; border-color: rgba(48,209,88,.35); }
    .gw-card-action.on:hover { color: #8df0a6; border-color: rgba(48,209,88,.6); }
    .gw-card-action.off { color: #ffaaa4; border-color: rgba(255,69,58,.35); }
    .gw-card-action.off:hover { color: #ffaaa4; border-color: rgba(255,69,58,.6); }
    .apiget-links {
      display: flex;
      flex-wrap: wrap;
      gap: .4rem;
      margin-top: .6rem;
    }
    .apiget-link {
      display: inline-flex;
      align-items: center;
      gap: .35rem;
      padding: .34rem .7rem;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--surface-raised);
      color: #8df0a6;
      font-size: .78rem;
      font-weight: 600;
      text-decoration: none;
      transition: border-color 160ms ease, background 160ms ease, transform 120ms ease-out;
    }
    .apiget-link:hover { border-color: rgba(48,209,88,.42); background: var(--accent-soft); }
    .apiget-link:active { transform: scale(.97); }
    .apiget-collapse {
      margin-top: .7rem;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      background: var(--surface-raised);
    }
    .apiget-collapse summary {
      list-style: none;
      cursor: pointer;
      user-select: none;
      display: flex;
      align-items: center;
      gap: .5rem;
      padding: .55rem .8rem;
      color: var(--muted-strong);
      font-size: .82rem;
      font-weight: 600;
      transition: color 160ms ease;
    }
    .apiget-collapse summary::-webkit-details-marker { display: none; }
    .apiget-collapse summary::before {
      content: "›";
      display: inline-block;
      transition: transform 180ms var(--ease-out);
      color: var(--muted);
    }
    .apiget-collapse[open] summary::before { transform: rotate(90deg); }
    .apiget-collapse summary:hover { color: var(--text); }
    .apiget-collapse[open] .apiget-list { padding: 0 .8rem .7rem; margin-top: 0; }
    .apiget-collapse .apiget-list { margin-top: 0; }
    .apiget-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: .6rem;
      padding: .5rem .8rem;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      background: var(--surface-raised);
      font-size: .84rem;
    }
    .apiget-item code { font-size: .8rem; color: var(--muted-strong); }
    .apiget-badge {
      flex: none;
      font-size: .72rem;
      font-weight: 600;
      padding: .15rem .55rem;
      border-radius: 999px;
    }
    .apiget-badge.on { color: #8df0a6; background: rgba(48,209,88,.12); }
    .apiget-badge.off { color: var(--muted); background: rgba(255,255,255,.06); }
    .apiget-message { min-height: 1rem; margin-top: .5rem; color: var(--muted); font-size: .74rem; }
    .apiget-message.ok { color: #8df0a6; }
    .apiget-message.error { color: #ffaaa4; }

    @media (prefers-reduced-motion: reduce) {
      .checkin-claim { transition: opacity 200ms ease; }
      .checkin-claim:active { transform: none; }
    }

    @media (prefers-contrast: more) {
      :root { --border: rgba(255, 255, 255, .28); --muted: #c4c4ca; }
      .model-option.selected { border-width: 2px; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div class="brand">
        <img class="brand-logo" src="/assets/bridge-logo.png" alt="WorkBuddy Bridge 标志" width="64" height="64">
        <div class="brand-copy">
          <h1>WorkBuddy Bridge</h1>
          <p class="subtitle">本地 AI 路由与连接中心</p>
        </div>
      </div>
      <div class="header-meta" aria-label="Bridge 版本与本地地址">
        <span class="version-badge">v__BRIDGE_VERSION__</span>
        <span class="local-badge">127.0.0.1:8787</span>
      </div>
    </header>

    <section class="panel account-panel" aria-labelledby="accountTitle">
      <div class="account-panel-head">
        <div class="panel-heading">
          <h2 id="accountTitle">WorkBuddy 账号池</h2>
          <p>独立账号池，异常自动切换。</p>
        </div>
        <div class="account-toolbar" role="group" aria-label="账号池操作">
          <button id="addAccountButton" type="button" title="扫码添加 WorkBuddy 账号" aria-label="添加 WorkBuddy 账号"><span class="button-label-wide">＋ 添加账号</span><span class="button-label-compact">添加</span></button>
          <button id="refreshAccountsButton" class="secondary-button" type="button" title="刷新所有账号积分" aria-label="刷新所有账号积分"><span class="button-label-wide">刷新积分</span><span class="button-label-compact">刷新</span></button>
          <button id="importAccountButton" class="secondary-button" type="button" title="导入官方 WorkBuddy 当前登录账号" aria-label="导入当前账号"><span class="button-label-wide">导入当前账号</span><span class="button-label-compact">导入</span></button>
        </div>
      </div>
      <div class="account-intro">
        <div class="pool-stats" aria-live="polite">
          <span class="pool-stat"><span class="dot ok" aria-hidden="true"></span><strong id="poolReady">—</strong> 可用</span>
          <span class="pool-stat"><span class="dot warn" aria-hidden="true"></span><strong id="poolCooling">—</strong> 冷却</span>
          <span class="pool-stat">共 <strong id="poolCount">—</strong> 个账号</span>
          <span class="pool-stat">积分余额 <strong id="poolBalance">—</strong></span>
        </div>
        <button id="openAuthFolderButton" class="folder-link" type="button" title="在资源管理器中打开本地 auths 目录">凭据仅保存在 <code id="authDirectory">D:\WorkBuddy\tools\codebuddy2openai\auths</code> ↗</button>
      </div>
      <div id="accountList" class="account-list" aria-live="polite"></div>
      <p id="accountMessage" class="account-message" role="status" aria-live="polite">正在读取本地账号池…</p>
    </section>

    <section class="panel checkin-panel" aria-labelledby="checkinTitle">
      <div class="panel-heading">
        <h2 id="checkinTitle">Buddy 加油站</h2>
        <p>每日签到领积分，本机账号池自动领取。可领状态实时显示，点击一键领取全部未领账号。</p>
      </div>
      <div class="checkin-summary">
        <div class="checkin-hero">
          <div class="checkin-hero-main">
            <span class="checkin-activity" id="checkinActivity">本期活动</span>
            <strong class="checkin-daily" id="checkinDaily">+0</strong>
            <span class="checkin-daily-label">每日可领积分</span>
          </div>
          <button id="checkinClaimButton" class="checkin-claim" type="button">
            <span class="checkin-claim-label">一键领取</span>
          </button>
        </div>
        <div class="checkin-stats" aria-live="polite">
          <span class="checkin-stat"><span class="dot ok" aria-hidden="true"></span><strong id="checkinClaimed">0</strong> 已领</span>
          <span class="checkin-stat"><span class="dot warn" aria-hidden="true"></span><strong id="checkinUnclaimed">0</strong> 待领</span>
          <span class="checkin-stat">连续 <strong id="checkinStreak">0</strong> 天</span>
        </div>
      </div>
      <div id="checkinList" class="checkin-list" aria-live="polite"></div>
      <p id="checkinMessage" class="checkin-message" role="status" aria-live="polite">正在读取签到状态…</p>
    </section>

    <section class="panel apiget-panel" aria-labelledby="apigetTitle">
      <div class="panel-heading gw-heading">
        <div>
          <h2 id="apigetTitle">其他 API</h2>
          <p>接入任意 OpenAI 兼容厂商，经 Bridge 统一暴露为 <code>厂商前缀/模型</code>。</p>
        </div>
        <button id="addGatewayButton" class="secondary-button" type="button">＋ 添加第三方 API</button>
      </div>
      <div class="apiget-summary" id="apigetSummary">
        <span class="dot warn" aria-hidden="true"></span> 正在读取网关状态…
      </div>
      <div id="apigetGateways" class="apiget-gateways" aria-live="polite"></div>
      <p id="apigetMessage" class="apiget-message" role="status" aria-live="polite">读取中</p>
    </section>

    <dialog id="gatewayDialog" aria-labelledby="gatewayDialogTitle">
      <div class="dialog-head">
        <div>
          <h2 id="gatewayDialogTitle">添加第三方 API</h2>
          <p>填写接口地址与 API Key，自动发现可用模型；密钥仅用于探测，不会写入配置文件。</p>
        </div>
        <button id="closeGatewayDialogButton" class="dialog-close" type="button" aria-label="关闭">×</button>
      </div>
      <div class="gw-form">
        <label>名称
          <input id="gwName" type="text" placeholder="如 Apiget" required>
        </label>
        <label>接口地址
          <input id="gwBaseUrl" type="text" placeholder="https://api.example.com/v1" required>
        </label>
        <label>API Key
          <span class="gw-key-row">
            <input id="gwApiKey" type="password" placeholder="sk-..." autocomplete="off">
            <button id="gwKeyToggle" class="gw-key-toggle" type="button" aria-label="显示/隐藏 API Key" title="显示/隐藏">
              <svg id="gwKeyEyeOpen" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>
              <svg id="gwKeyEyeClosed" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="display:none"><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c6.5 0 10 8 10 8a13.16 13.16 0 0 1-1.67 2.68"/><path d="M6.61 6.61A13.53 13.53 0 0 0 2 12s3.5 8 10 8a9.74 9.74 0 0 0 5.39-1.61"/><line x1="2" y1="2" x2="22" y2="22"/></svg>
            </button>
          </span>
        </label>
        <label>模型前缀
          <input id="gwPrefix" type="text" placeholder="如 AG" required>
        </label>
        <label>官网地址（可选，用于充值）
          <input id="gwHomeUrl" type="text" placeholder="https://...">
        </label>
        <label>调用协议
          <select id="gwProtocol">
            <option value="auto">自动（推荐：原生 Responses 优先，否则转 Chat）</option>
            <option value="chat">Chat（强制走 chat/completions 转换）</option>
            <option value="responses">Responses（强制原生 /responses）</option>
          </select>
        </label>
        <div id="gwProtocolProbe" class="gw-note"></div>
        <div class="gw-form-row">
          <button id="gwDiscoverButton" class="secondary-button" type="button">发现模型</button>
          <span id="gwDiscoverNote" class="gw-note">发现后请勾选要启用的模型</span>
        </div>
        <div id="gwModelList" class="gw-model-list" aria-live="polite"></div>
        <p id="gwMessage" class="gw-message" role="status" aria-live="polite"></p>
        <div class="gw-form-row gw-form-actions">
          <button id="gwSaveButton" class="primary-button" type="button" disabled>保存</button>
          <button id="gwCancelButton" class="secondary-button" type="button">取消</button>
        </div>
      </div>
    </dialog>

    <section class="panel route-panel" aria-labelledby="routeTitle">
      <div class="panel-heading">
        <h2 id="routeTitle">WorkBuddy 模型路由</h2>
        <p>选择默认上游模型；子代理显式请求有效模型时自动直达，否则回退到此默认模型。</p>
      </div>
      <div class="route-summary">
        <span>自适应路由</span>
        <code id="currentRoute">检查中</code>
      </div>
      <fieldset>
        <div class="model-toolbar" role="group" aria-label="模型目录排序方式">
          <span class="sort-label">排序</span>
          <button id="sortDefaultButton" class="sort-button" type="button" aria-pressed="true">默认顺序</button>
          <button id="sortCreditsButton" class="sort-button" type="button" aria-pressed="false">倍率从低到高 ↑</button>
        </div>
        <details class="model-catalog-collapse" id="modelCatalog">
          <summary id="modelCatalogToggle">可用模型目录（11 个）</summary>
          <div id="modelList" class="model-list" role="radiogroup" aria-label="选择 WorkBuddy 上游模型"></div>
        </details>
      </fieldset>
      <p id="modelMetadataNote" class="metadata-note">正在读取 WorkBuddy 模型倍率与活动信息…</p>
      <div class="route-actions">
        <button id="saveRouteButton" type="button" disabled>保存路由</button>
        <span id="routeMessage" class="route-message" role="status" aria-live="polite">正在加载模型目录…</span>
      </div>
    </section>

    <section class="panel image-route-panel" aria-labelledby="imageRouteTitle">
      <div class="panel-heading">
        <h2 id="imageRouteTitle">生图模型</h2>
        <p>汇总 WorkBuddy 官方与各第三方 API 的生图模型；选择默认生图路由后，统一通过 <code>/v1/images/generations</code>（model 传 <code>auto</code> 或留空）调用。</p>
      </div>
      <div class="route-summary">
        <span>默认生图路由</span>
        <code id="currentImageRoute">检查中</code>
      </div>
      <fieldset>
        <div id="imageModelList" class="model-list" role="radiogroup" aria-label="选择默认生图模型"></div>
      </fieldset>
      <div class="route-actions">
        <button id="saveImageRouteButton" type="button" disabled>保存生图路由</button>
        <span id="imageRouteMessage" class="route-message" role="status" aria-live="polite">正在加载生图模型…</span>
      </div>
    </section>

    <section class="overview" aria-label="连接概览">
      <article class="metric">
        <p class="metric-label">Bridge 服务</p>
        <p class="metric-value"><span class="dot ok" aria-hidden="true"></span><span>在线</span></p>
      </article>
      <article class="metric">
        <p class="metric-label">账号池</p>
        <p class="metric-value"><span id="credentialDot" class="dot warn" aria-hidden="true"></span><span id="credentialState">检查中</span></p>
      </article>
      <article class="metric">
        <p class="metric-label">模型目录</p>
        <p class="metric-value"><span id="modelsDot" class="dot warn" aria-hidden="true"></span><span id="modelsState">检查中</span></p>
      </article>
      <article class="metric">
        <p class="metric-label">当前路由</p>
        <p class="metric-value"><span id="routeDot" class="dot warn" aria-hidden="true"></span><span id="routeState">检查中</span></p>
      </article>
      <article class="metric">
        <p class="metric-label">OpenCodex</p>
        <p class="metric-value"><span id="opencodexDot" class="dot warn" aria-hidden="true"></span><span id="opencodexState">检查中</span></p>
      </article>
    </section>

    <main>
      <section class="panel" aria-labelledby="statusTitle">
        <div class="panel-heading">
          <h2 id="statusTitle">总体状态</h2>
          <p>页面打开后自动检查，无需输入 API Key。</p>
        </div>
        <div class="status-line" role="status" aria-live="polite">
          <div class="status-copy">
            <strong id="overallState">正在检查…</strong>
            <span id="overallDetail">正在读取 Bridge 状态。</span>
          </div>
          <button id="refreshButton" type="button">刷新状态</button>
        </div>
        <div class="details">
          <div class="detail"><span>运行模式</span><strong id="modeValue">—</strong></div>
          <div class="detail"><span>Python</span><strong id="pythonValue">—</strong></div>
          <div class="detail"><span>模型数量</span><strong id="modelCountValue">—</strong></div>
          <div class="detail"><span>检查时间</span><strong id="checkedAtValue">—</strong></div>
        </div>
        <p class="notice">此页面只显示脱敏账号标识与本地状态，不展示 Token、API Key 或官方登录文件路径。</p>
      </section>

      <aside class="panel" aria-labelledby="endpointTitle">
        <div class="panel-heading">
          <h2 id="endpointTitle">当前端点</h2>
          <p>OpenCodex 使用 openai-chat 适配器连接。</p>
        </div>
        <div class="endpoint-list">
          <div class="endpoint"><span>Dashboard</span><code>/</code></div>
          <div class="endpoint"><span>Base URL</span><code>/v1</code></div>
          <div class="endpoint"><span>健康检查</span><code>/health</code></div>
          <div class="endpoint"><span>模型目录</span><code>/v1/models</code></div>
          <div class="endpoint"><span>当前路由</span><code id="endpointRoute">hy3 → hy3</code></div>
        </div>
      </aside>
    </main>

    <dialog id="loginDialog" aria-labelledby="loginTitle">
      <div class="dialog-head">
        <div>
          <h2 id="loginTitle">扫码添加 WorkBuddy 账号</h2>
          <p>使用官方登录页完成微信扫码，凭据只会写入本地账号池。</p>
        </div>
        <button id="closeLoginButton" class="dialog-close" type="button" aria-label="关闭扫码窗口">×</button>
      </div>
      <div class="login-frame-shell">
        <div id="loginPlaceholder" class="login-placeholder">
          <div class="login-launch-card">
            <div class="login-launch-icon" aria-hidden="true">↗</div>
            <strong>在官方登录页扫码</strong>
            <span>官方页面禁止嵌入本地窗口，请打开新标签页完成微信扫码；Bridge 会自动等待登录结果。</span>
            <button id="openLoginPageButton" class="secondary-button" type="button" hidden>打开官方登录页</button>
          </div>
        </div>
      </div>
      <div class="login-status" role="status" aria-live="polite">
        <strong id="loginStatusText">准备登录…</strong>
        <span id="loginCountdown"></span>
      </div>
      <div class="dialog-actions">
        <button id="retryLoginButton" class="secondary-button" type="button" hidden>重新生成</button>
        <button id="cancelLoginButton" class="secondary-button" type="button">取消</button>
      </div>
    </dialog>

    <footer>WorkBuddy2Codex v__BRIDGE_VERSION__ · 本地回环连接</footer>
  </div>

  <script>
    const refreshButton = document.getElementById('refreshButton');
    const saveRouteButton = document.getElementById('saveRouteButton');
    const saveImageRouteButton = document.getElementById('saveImageRouteButton');
    const imageModelList = document.getElementById('imageModelList');
    const imageRouteMessage = document.getElementById('imageRouteMessage');
    const sortDefaultButton = document.getElementById('sortDefaultButton');
    const sortCreditsButton = document.getElementById('sortCreditsButton');
    const routeMessage = document.getElementById('routeMessage');
    const accountList = document.getElementById('accountList');
    const accountMessage = document.getElementById('accountMessage');
    const addAccountButton = document.getElementById('addAccountButton');
    const importAccountButton = document.getElementById('importAccountButton');
    const refreshAccountsButton = document.getElementById('refreshAccountsButton');
    const openAuthFolderButton = document.getElementById('openAuthFolderButton');
    const loginDialog = document.getElementById('loginDialog');
    const loginPlaceholder = document.getElementById('loginPlaceholder');
    const loginStatusText = document.getElementById('loginStatusText');
    const loginCountdown = document.getElementById('loginCountdown');
    const openLoginPageButton = document.getElementById('openLoginPageButton');
    const retryLoginButton = document.getElementById('retryLoginButton');
    const cancelLoginButton = document.getElementById('cancelLoginButton');
    const closeLoginButton = document.getElementById('closeLoginButton');
    let routeDirty = false;
    let currentRouteModel = 'hy3';
    let currentImageRouteModel = 'hunyuan-image-v3.0-art';
    let imageRouteDirty = false;
    let modelSortMode = 'default';
    let latestModels = [];
    let latestModelMetadata = {};
    let loginId = '';
    let loginUrl = '';
    let loginPollTimer = null;
    let loginDeadline = 0;
    let loginCompleted = false;
    let loginPolling = false;

    const setMetric = (name, text, state) => {
      document.getElementById(`${name}State`).textContent = text;
      document.getElementById(`${name}Dot`).className = `dot ${state}`;
    };

    const setRouteMessage = (text, state = '') => {
      routeMessage.textContent = text;
      routeMessage.className = `route-message ${state}`.trim();
    };

    const setAccountMessage = (text, state = '') => {
      accountMessage.textContent = text;
      accountMessage.className = `account-message ${state}`.trim();
    };

    async function postManagement(path, body = {}) {
      const response = await fetch(path, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
      return payload;
    }

    function formatCredits(value) {
      const number = Number(value);
      if (!Number.isFinite(number)) return '—';
      return number.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
    }

    function appendFlag(container, text, className = '') {
      const flag = document.createElement('span');
      flag.className = `account-flag ${className}`.trim();
      flag.textContent = text;
      container.append(flag);
    }

    function renderQuota(quota = {}) {
      const grid = document.createElement('div');
      grid.className = 'quota-grid';
      if (!quota.ok) {
        const unavailable = document.createElement('div');
        unavailable.className = 'quota-unavailable';
        unavailable.textContent = quota.message || '积分暂不可读；账号连接不受影响。';
        grid.append(unavailable);
        return grid;
      }
      const balance = document.createElement('div');
      const balanceLabel = document.createElement('div');
      balanceLabel.className = 'quota-label';
      balanceLabel.textContent = '剩余积分';
      const balanceValue = document.createElement('div');
      balanceValue.className = 'quota-balance';
      balanceValue.textContent = quota.unlimited ? '不限量' : formatCredits(quota.balance);
      balance.append(balanceLabel, balanceValue);
      const usage = document.createElement('div');
      const detail = document.createElement('div');
      detail.className = 'quota-detail';
      const detailLabel = document.createElement('span');
      detailLabel.textContent = quota.label || '积分';
      const detailValue = document.createElement('span');
      detailValue.textContent = quota.unlimited ? '—' : `${formatCredits(quota.used)} / ${formatCredits(quota.total)}`;
      detail.append(detailLabel, detailValue);
      usage.append(detail);
      if (!quota.unlimited && Number(quota.total) > 0) {
        const percent = Math.min(100, Math.max(0, Number(quota.used) / Number(quota.total) * 100));
        const progress = document.createElement('div');
        progress.className = 'quota-progress';
        progress.setAttribute('role', 'progressbar');
        progress.setAttribute('aria-label', '积分使用进度');
        progress.setAttribute('aria-valuemin', '0');
        progress.setAttribute('aria-valuemax', String(quota.total));
        progress.setAttribute('aria-valuenow', String(quota.used));
        const bar = document.createElement('span');
        bar.style.width = `${percent}%`;
        progress.append(bar);
        usage.append(progress);
      }
      grid.append(balance, usage);
      return grid;
    }

    function renderAccountPool(pool = {}) {
      const accounts = Array.isArray(pool.accounts) ? pool.accounts : [];
      document.getElementById('authDirectory').textContent = pool.auth_dir || '本地 auths 目录';
      document.getElementById('poolReady').textContent = String(pool.ready || 0);
      document.getElementById('poolCooling').textContent = String(pool.cooling || 0);
      document.getElementById('poolCount').textContent = String(pool.count || accounts.length);
      const poolQuota = pool.quota || {};
      document.getElementById('poolBalance').textContent = poolQuota.unlimited ? '不限量' : poolQuota.ok ? formatCredits(poolQuota.balance) : '—';
      accountList.replaceChildren();
      if (!accounts.length) {
        const empty = document.createElement('div');
        empty.className = 'account-empty';
        const title = document.createElement('strong');
        title.textContent = '还没有 WorkBuddy 账号';
        const copy = document.createElement('p');
        copy.textContent = '扫码登录 WorkBuddy 账号，凭据只保存在本机 auths 目录；也可以导入官方客户端当前登录态。';
        const add = document.createElement('button');
        add.type = 'button';
        add.dataset.action = 'add';
        add.textContent = '扫码添加第一个账号';
        empty.append(title, copy, add);
        accountList.append(empty);
        setAccountMessage('账号池为空；Bridge 不会修改官方 WorkBuddy 登录文件。');
        return;
      }
      const stateLabels = { ready: '可用', cooling: '冷却中', expired: '待重新扫码', error: '异常' };
      accounts.forEach(account => {
        const card = document.createElement('article');
        card.className = `account-card${account.primary ? ' primary' : ''}`;
        const head = document.createElement('div');
        head.className = 'account-head';
        const avatar = document.createElement('div');
        avatar.className = 'account-avatar';
        avatar.textContent = String(account.name || 'W').slice(0, 1).toUpperCase();
        const identity = document.createElement('div');
        const titleRow = document.createElement('div');
        titleRow.className = 'account-title-row';
        const name = document.createElement('div');
        name.className = 'account-name';
        name.textContent = account.name || 'WorkBuddy account';
        titleRow.append(name);
        if (account.primary) {
          const primaryBadge = document.createElement('span');
          primaryBadge.className = 'account-primary-badge';
          primaryBadge.textContent = '主账号';
          primaryBadge.setAttribute('aria-label', '主账号');
          titleRow.append(primaryBadge);
        }
        const meta = document.createElement('div');
        meta.className = 'account-meta';
        meta.textContent = [account.uid, account.enterprise_name].filter(Boolean).join(' · ') || '本地凭据已加载';
        identity.append(titleRow, meta);
        const state = document.createElement('span');
        state.className = `account-state ${account.state || 'error'}`;
        state.textContent = stateLabels[account.state] || account.state || '异常';
        head.append(avatar, identity, state);

        const flags = document.createElement('div');
        flags.className = 'account-flags';
        if (account.active) appendFlag(flags, '当前使用');
        if (account.reason) appendFlag(flags, account.cooldown_until ? `${account.reason} · 至 ${account.cooldown_until}` : account.reason);

        const actions = document.createElement('div');
        actions.className = 'account-actions';
        if (!account.primary) {
          const primary = document.createElement('button');
          primary.type = 'button'; primary.className = 'secondary-button';
          primary.dataset.action = 'primary'; primary.dataset.ref = account.ref;
          primary.textContent = '设为主账号'; primary.title = '设为主账号'; actions.append(primary);
        }
        const reauth = document.createElement('button');
        reauth.type = 'button'; reauth.className = 'secondary-button';
        reauth.dataset.action = 'reauth'; reauth.textContent = '重新扫码'; reauth.title = '重新扫码登录'; actions.append(reauth);
        const remove = document.createElement('button');
        remove.type = 'button'; remove.className = 'danger-button';
        remove.dataset.action = 'remove'; remove.dataset.ref = account.ref;
        remove.dataset.name = account.name || '该账号'; remove.textContent = '移除'; remove.title = '移除该账号'; actions.append(remove);
        card.append(head, flags, renderQuota(account.quota || {}), actions);
        accountList.append(card);
      });
      const balanceText = poolQuota.unlimited ? '存在不限量账号' : poolQuota.ok ? `总积分余额 ${formatCredits(poolQuota.balance)}` : '积分读取失败';
      setAccountMessage(`${pool.ready || 0}/${pool.count || accounts.length} 个账号可用 · ${balanceText}；主账号异常时自动切换。`, pool.ready ? 'ok' : 'error');
    }

    function clearLoginPoll() {
      if (loginPollTimer !== null) window.clearInterval(loginPollTimer);
      loginPollTimer = null;
      loginPolling = false;
    }

    function updateLoginCountdown() {
      const seconds = Math.max(0, Math.ceil((loginDeadline - Date.now()) / 1000));
      loginCountdown.textContent = seconds ? `剩余 ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}` : '已过期';
    }

    function resetLoginFrame() {
      loginPlaceholder.hidden = false;
      openLoginPageButton.hidden = true;
      retryLoginButton.hidden = true;
      loginStatusText.textContent = '正在创建安全登录会话…';
      loginCountdown.textContent = '';
    }

    async function closeLoginDialog() {
      clearLoginPoll();
      if (loginId && !loginCompleted) {
        try { await postManagement('/ui/accounts/login/cancel', { login_id: loginId }); } catch (error) { /* local dialog is already closing */ }
      }
      loginId = '';
      loginUrl = '';
      loginCompleted = false;
      loginDialog.close();
      resetLoginFrame();
      addAccountButton.focus();
    }

    async function pollLoginStatus() {
      if (!loginId || loginPolling) return;
      loginPolling = true;
      try {
        const result = await postManagement('/ui/accounts/login/status', { login_id: loginId });
        if (result.status === 'waiting') {
          loginStatusText.textContent = '请先在官方页点击“同意”，再使用微信扫码';
          updateLoginCountdown();
        } else if (result.status === 'completed') {
          clearLoginPoll();
          loginCompleted = true;
          loginStatusText.textContent = '登录成功，账号已加入本地池';
          loginCountdown.textContent = '正在刷新账号与积分…';
          window.setTimeout(async () => {
            loginDialog.close();
            resetLoginFrame();
            loginId = ''; loginUrl = ''; loginCompleted = false;
            await refreshStatus();
            addAccountButton.focus();
          }, 900);
        } else {
          clearLoginPoll();
          loginStatusText.textContent = result.message || '登录未完成';
          loginCountdown.textContent = '请重新生成登录二维码';
          retryLoginButton.hidden = false;
        }
      } catch (error) {
        clearLoginPoll();
        loginStatusText.textContent = `登录状态读取失败：${error.message}`;
        retryLoginButton.hidden = false;
      } finally {
        loginPolling = false;
      }
    }

    async function startLoginFlow() {
      clearLoginPoll();
      if (loginId && !loginCompleted) {
        try { await postManagement('/ui/accounts/login/cancel', { login_id: loginId }); } catch (error) { /* stale attempt */ }
      }
      loginId = '';
      loginCompleted = false;
      resetLoginFrame();
      try {
        const result = await postManagement('/ui/accounts/login/start');
        loginId = result.login_id;
        loginUrl = result.auth_url;
        loginDeadline = Date.now() + Number(result.expires_in || 300) * 1000;
        loginPlaceholder.hidden = false;
        openLoginPageButton.hidden = false;
        loginStatusText.textContent = '请先在官方页点击“同意”，再使用微信扫码';
        updateLoginCountdown();
        loginPollTimer = window.setInterval(() => {
          updateLoginCountdown();
          if (Date.now() >= loginDeadline) {
            clearLoginPoll();
            loginStatusText.textContent = '登录会话已过期';
            loginCountdown.textContent = '请重新生成登录二维码';
            retryLoginButton.hidden = false;
          } else pollLoginStatus();
        }, 1500);
        pollLoginStatus();
      } catch (error) {
        loginStatusText.textContent = `无法创建登录会话：${error.message}`;
        retryLoginButton.hidden = false;
      }
    }

    function openLoginDialog() {
      if (!loginDialog.open) loginDialog.showModal();
      startLoginFlow();
      window.setTimeout(() => cancelLoginButton.focus(), 0);
    }

    async function importCurrentAccount() {
      importAccountButton.disabled = true;
      setAccountMessage('正在从官方 WorkBuddy 当前登录态导入…');
      try {
        await postManagement('/ui/accounts/import-current');
        setAccountMessage('当前 WorkBuddy 账号已复制到独立 auths 目录。', 'ok');
        await refreshStatus();
      } catch (error) {
        setAccountMessage(`导入失败：${error.message}`, 'error');
      } finally {
        importAccountButton.disabled = false;
      }
    }

    async function refreshAccounts() {
      refreshAccountsButton.disabled = true;
      try {
        await postManagement('/ui/accounts/refresh');
        await refreshStatus();
      } catch (error) {
        setAccountMessage(`刷新失败：${error.message}`, 'error');
      } finally {
        refreshAccountsButton.disabled = false;
      }
    }

    function updateSelectedStyle() {
      document.querySelectorAll('.model-option').forEach(label => {
        const radio = label.querySelector('input');
        label.classList.toggle('selected', radio.checked);
      });
    }

    function effectiveCredit(model, metadata) {
      const info = metadata[model] || {};
      if (info.credits_dynamic) return Number.POSITIVE_INFINITY;
      const promotion = info.promotion || {};
      const display = promotion.active && promotion.discounted_credits
        ? promotion.discounted_credits
        : info.credits;
      const value = Number.parseFloat(display);
      return Number.isFinite(value) ? value : Number.POSITIVE_INFINITY;
    }

    function sortedModels(models, metadata) {
      if (modelSortMode !== 'credits-asc') return [...models];
      return models
        .map((model, index) => ({ model, index, credit: effectiveCredit(model, metadata) }))
        .sort((a, b) => a.credit - b.credit || a.index - b.index)
        .map(item => item.model);
    }

    function updateSortButtons() {
      sortDefaultButton.setAttribute('aria-pressed', String(modelSortMode === 'default'));
      sortCreditsButton.setAttribute('aria-pressed', String(modelSortMode === 'credits-asc'));
    }

    function applySortMode(mode) {
      const selected = document.querySelector('input[name="routeModel"]:checked')?.value || currentRouteModel;
      modelSortMode = mode;
      updateSortButtons();
      renderModels(sortedModels(latestModels, latestModelMetadata), selected, latestModelMetadata);
    }

    function renderModels(models, selectedModel, metadata = {}) {
      const container = document.getElementById('modelList');
      container.replaceChildren();
      models.forEach(model => {
        const info = metadata[model] || {};
        const promotion = info.promotion || {};
        const label = document.createElement('label');
        label.className = 'model-option';
        if (info.description) label.title = info.description;
        const radio = document.createElement('input');
        radio.type = 'radio';
        radio.name = 'routeModel';
        radio.value = model;
        radio.checked = model === selectedModel;
        radio.addEventListener('change', () => {
          routeDirty = radio.value !== currentRouteModel;
          saveRouteButton.disabled = !routeDirty;
          setRouteMessage(routeDirty ? `待保存：${radio.value} → ${radio.value}` : '当前路由已生效。');
          updateSelectedStyle();
        });
        const copy = document.createElement('div');
        copy.className = 'model-copy';
        const heading = document.createElement('div');
        heading.className = 'model-heading';
        const identity = document.createElement('div');
        const nameRow = document.createElement('div');
        nameRow.className = 'model-name-row';
        const name = document.createElement('span');
        name.className = 'model-name';
        name.textContent = info.name || model;
        nameRow.append(name);
        const badges = Array.isArray(info.badges) ? info.badges : [];
        badges.forEach(item => {
          const badge = document.createElement('span');
          badge.className = `model-badge ${item.source === 'promotion' ? 'promotion' : ''}`.trim();
          badge.textContent = item.label;
          nameRow.append(badge);
        });
        identity.append(nameRow);
        if (info.name && info.name !== model) {
          const id = document.createElement('div');
          id.className = 'model-id';
          id.textContent = model;
          identity.append(id);
        }
        const vision = info.vision;
        const isImageGen = info.image_generation === true;
        if (isImageGen) {
          const vtag = document.createElement('div');
          vtag.className = 'model-tags';
          const vbadge = document.createElement('span');
          vbadge.className = 'model-badge image-gen';
          vbadge.textContent = '生图';
          vtag.append(vbadge);
          identity.append(vtag);
        } else if (vision) {
          const vtag = document.createElement('div');
          vtag.className = 'model-tags';
          const vbadge = document.createElement('span');
          if (vision === 'yes') {
            vbadge.className = 'model-badge vision';
            vbadge.textContent = '识图';
          } else if (vision === 'weak') {
            vbadge.className = 'model-badge vision-weak';
            vbadge.textContent = '识图弱';
          } else {
            vbadge.className = 'model-badge vision-no';
            vbadge.textContent = '仅对话';
          }
          vtag.append(vbadge);
          identity.append(vtag);
        }
        const credit = document.createElement('div');
        credit.className = 'model-credit';
        const discounted = promotion.active ? promotion.discounted_credits : null;
        if (discounted && info.credits && discounted !== info.credits) {
          const original = document.createElement('span');
          original.className = 'original';
          original.textContent = info.credits;
          const current = document.createElement('span');
          current.textContent = discounted;
          credit.append(original, current);
        } else {
          credit.textContent = info.credits_dynamic ? '浮动' : (info.credits || '—');
        }
        heading.append(identity, credit);
        copy.append(heading);
        label.append(radio, copy);
        container.append(label);
      });
      updateSelectedStyle();
    }

    async function refreshStatus() {
      refreshButton.disabled = true;
      document.getElementById('overallState').textContent = '正在检查…';
      document.getElementById('overallDetail').textContent = '正在读取 Bridge 状态。';

      try {
        const response = await fetch('/ui/status', { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const status = await response.json();

        const credentialReady = status.credential_ready === true;
        const accountPool = status.account_pool || {};
        const hasHy3 = status.hy3_available === true;
        const openCodexInSync = status.opencodex_in_sync === true;
        const ready = status.status === 'ok' && credentialReady && hasHy3 && openCodexInSync;

        setMetric('credential', accountPool.count ? `${accountPool.ready || 0}/${accountPool.count} 可用` : '未导入', credentialReady ? 'ok' : 'error');
        setMetric('models', `${status.model_count || 0} 个`, status.model_count ? 'ok' : 'error');
        setMetric('route', status.route_model || '未设置', status.route_model ? 'ok' : 'error');
        setMetric('opencodex', openCodexInSync ? '已同步' : status.opencodex_connected ? '未同步' : '未连接', openCodexInSync ? 'ok' : 'error');

        document.getElementById('overallState').textContent = ready ? '连接就绪' : '需要处理';
        document.getElementById('overallDetail').textContent = ready
          ? `Bridge 默认使用 ${status.route_model}，子代理显式模型自动直达。`
          : 'Bridge 在线，但登录凭据、模型目录或 OpenCodex 默认模型需要同步。';
        document.getElementById('modeValue').textContent = status.mode || '—';
        document.getElementById('pythonValue').textContent = status.python || '—';
        document.getElementById('modelCountValue').textContent = String(status.model_count ?? '—');
        document.getElementById('checkedAtValue').textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
        renderAccountPool(accountPool);
        if (status.checkin) renderCheckin(status.checkin);
        if (status.gateways) renderGateways(status.gateways);
        currentRouteModel = status.route_model || 'hy3';
        latestModels = status.model_display_order?.length
          ? status.model_display_order
          : (status.models || []);
        latestModelMetadata = status.model_metadata || {};
        document.getElementById('currentRoute').textContent = `自适应 · 默认 ${currentRouteModel}`;
        document.getElementById('endpointRoute').textContent = `自适应 · 默认 ${currentRouteModel}`;
        if (!routeDirty) {
          renderModels(sortedModels(latestModels, latestModelMetadata), currentRouteModel, latestModelMetadata);
          const toggle = document.getElementById('modelCatalogToggle');
          if (toggle) toggle.textContent = '可用模型目录（' + latestModels.length + ' 个）';
          const metadataStatus = status.model_metadata_status || {};
          document.getElementById('modelMetadataNote').textContent = metadataStatus.available
            ? `倍率与活动来自 ${metadataStatus.source}；活动按当前时间自动显示，真实计费以 WorkBuddy 服务端为准。`
            : '未读取到 WorkBuddy 模型元数据，仅显示模型 ID；路由功能不受影响。';
          saveRouteButton.disabled = true;
          setRouteMessage('当前路由已生效。', 'ok');
        }
        const imageModels = status.image_models || [{ id: 'hunyuan-image-v3.0-art', owner: 'WorkBuddy' }];
        currentImageRouteModel = status.image_route_model || 'hunyuan-image-v3.0-art';
        document.getElementById('currentImageRoute').textContent = currentImageRouteModel;
        if (!imageRouteDirty) {
          renderImageModels(imageModels, currentImageRouteModel);
          saveImageRouteButton.disabled = true;
          setImageRouteMessage('当前生图路由已生效。', 'ok');
        }
      } catch (error) {
        setMetric('credential', '检查失败', 'error');
        setMetric('models', '检查失败', 'error');
        setMetric('route', '检查失败', 'error');
        setMetric('opencodex', '检查失败', 'error');
        document.getElementById('overallState').textContent = '状态读取失败';
        document.getElementById('overallDetail').textContent = `无法读取本地状态：${error.message}`;
        setRouteMessage(`模型目录读取失败：${error.message}`, 'error');
      } finally {
        refreshButton.disabled = false;
      }
    }

    async function saveRoute() {
      const selected = document.querySelector('input[name="routeModel"]:checked');
      if (!selected) {
        setRouteMessage('请先选择一个模型。', 'error');
        return;
      }
      saveRouteButton.disabled = true;
      setRouteMessage(`正在保存：${selected.value} → ${selected.value}…`);
      try {
        const response = await fetch('/ui/route', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: selected.value })
        });
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
        currentRouteModel = body.route_model;
        routeDirty = false;
        updateSelectedStyle();
        setMetric('route', body.route_model, 'ok');
        setMetric('opencodex', '已同步', 'ok');
        document.getElementById('currentRoute').textContent = `自适应 · 默认 ${body.route_model}`;
        document.getElementById('endpointRoute').textContent = `自适应 · 默认 ${body.route_model}`;
        setRouteMessage(`已同步 Bridge + OpenCodex：${body.route_model}`, 'ok');
      } catch (error) {
        saveRouteButton.disabled = false;
        setRouteMessage(`保存失败：${error.message}`, 'error');
      }
    }

    function setImageRouteMessage(text, kind) {
      imageRouteMessage.textContent = text;
      imageRouteMessage.className = 'route-message' + (kind ? ' ' + kind : '');
    }

    function renderImageModels(models, selected) {
      imageModelList.replaceChildren();
      models.forEach(item => {
        const label = document.createElement('label');
        label.className = 'model-option';
        const radio = document.createElement('input');
        radio.type = 'radio';
        radio.name = 'imageRouteModel';
        radio.value = item.id;
        radio.checked = item.id === selected;
        radio.addEventListener('change', () => {
          imageRouteDirty = radio.value !== currentImageRouteModel;
          saveImageRouteButton.disabled = !imageRouteDirty;
          setImageRouteMessage(imageRouteDirty ? `待保存：${radio.value}` : '当前生图路由已生效。');
        });
        const copy = document.createElement('div');
        copy.className = 'model-copy';
        const heading = document.createElement('div');
        heading.className = 'model-name';
        heading.textContent = item.id;
        const sub = document.createElement('div');
        sub.className = 'model-meta';
        sub.textContent = `来源：${item.owner}`;
        copy.append(heading, sub);
        label.append(radio, copy);
        imageModelList.appendChild(label);
      });
    }

    async function saveImageRoute() {
      const selected = document.querySelector('input[name="imageRouteModel"]:checked');
      if (!selected) {
        setImageRouteMessage('请先选择一个生图模型。', 'error');
        return;
      }
      saveImageRouteButton.disabled = true;
      setImageRouteMessage(`正在保存：${selected.value}…`);
      try {
        const response = await fetch('/ui/image-route', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: selected.value })
        });
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
        currentImageRouteModel = body.image_route_model;
        imageRouteDirty = false;
        document.getElementById('currentImageRoute').textContent = body.image_route_model;
        setImageRouteMessage(`已保存默认生图路由：${body.image_route_model}`, 'ok');
      } catch (error) {
        saveImageRouteButton.disabled = false;
        setImageRouteMessage(`保存失败：${error.message}`, 'error');
      }
    }

    refreshButton.addEventListener('click', refreshStatus);
    addAccountButton.addEventListener('click', openLoginDialog);
    importAccountButton.addEventListener('click', importCurrentAccount);
    refreshAccountsButton.addEventListener('click', refreshAccounts);
    openAuthFolderButton.addEventListener('click', async () => {
      try {
        await postManagement('/ui/accounts/open-folder');
        setAccountMessage('已在资源管理器中打开本地 auths 目录。', 'ok');
      } catch (error) {
        setAccountMessage(`无法打开目录：${error.message}`, 'error');
      }
    });
    accountList.addEventListener('click', async event => {
      const button = event.target.closest('button[data-action]');
      if (!button) return;
      const ref = button.dataset.ref;
      button.disabled = true;
      try {
        if (button.dataset.action === 'primary') {
          await postManagement('/ui/accounts/primary', { ref });
          setAccountMessage('主账号已更新。', 'ok');
        } else if (button.dataset.action === 'reauth' || button.dataset.action === 'add') {
          openLoginDialog();
          return;
        } else if (button.dataset.action === 'remove') {
          if (!window.confirm(`仅从 Bridge 本地账号池移除“${button.dataset.name}”？官方 WorkBuddy 登录不会受影响。`)) return;
          await postManagement('/ui/accounts/remove', { ref });
          setAccountMessage('本地账号凭据已移除。', 'ok');
        }
        await refreshStatus();
      } catch (error) {
        setAccountMessage(`账号操作失败：${error.message}`, 'error');
      } finally {
        button.disabled = false;
      }
    });
    closeLoginButton.addEventListener('click', closeLoginDialog);
    cancelLoginButton.addEventListener('click', closeLoginDialog);
    retryLoginButton.addEventListener('click', startLoginFlow);
    openLoginPageButton.addEventListener('click', () => {
      if (loginUrl) window.open(loginUrl, '_blank', 'noopener,noreferrer');
    });
    loginDialog.addEventListener('cancel', event => {
      event.preventDefault();
      closeLoginDialog();
    });
    saveRouteButton.addEventListener('click', saveRoute);
    saveImageRouteButton.addEventListener('click', saveImageRoute);
    sortDefaultButton.addEventListener('click', () => applySortMode('default'));
    sortCreditsButton.addEventListener('click', () => applySortMode('credits-asc'));
    refreshStatus();
    // ---- Buddy 加油站 ----
    function renderCheckin(checkin = {}) {
      const activity = checkin.activity || {};
      const accounts = Array.isArray(checkin.accounts) ? checkin.accounts : [];
      document.getElementById('checkinActivity').textContent =
        [activity.theme_name || 'Buddy加油站', activity.activity_name || ''].filter(Boolean).join(' · ') || '本期活动';
      document.getElementById('checkinDaily').textContent = '+' + (activity.daily_credit || 0);
      const claimed = accounts.filter(a => a.ok && a.today_checked_in).length;
      const unclaimed = checkin.unclaimed_count || accounts.filter(a => a.ok && !a.today_checked_in).length;
      document.getElementById('checkinClaimed').textContent = String(claimed);
      document.getElementById('checkinUnclaimed').textContent = String(unclaimed);
      const maxStreak = accounts.reduce((m, a) => Math.max(m, a.streak_days || 0), 0);
      document.getElementById('checkinStreak').textContent = String(maxStreak);
      const btn = document.getElementById('checkinClaimButton');
      const label = btn.querySelector('.checkin-claim-label');
      if (unclaimed === 0) {
        btn.disabled = true;
        btn.classList.add('done');
        label.textContent = '今日已全部领取';
      } else {
        btn.disabled = false;
        btn.classList.remove('done');
        label.textContent = '一键领取 (' + unclaimed + ')';
      }
      const list = document.getElementById('checkinList');
      list.replaceChildren();
      if (!accounts.length) {
        const empty = document.createElement('div');
        empty.className = 'account-empty';
        empty.textContent = '还没有可签到的 WorkBuddy 账号';
        list.appendChild(empty);
        return;
      }
      accounts.forEach(a => {
        const item = document.createElement('div');
        item.className = 'checkin-item' + (a.today_checked_in ? ' claimed' : '');
        const left = document.createElement('div');
        left.style.minWidth = '0';
        const name = document.createElement('div');
        name.className = 'ci-name';
        name.textContent = a.name || 'WorkBuddy account';
        const meta = document.createElement('div');
        meta.className = 'ci-meta';
        meta.textContent = (a.today_checked_in ? '今日已领' : '待领取') + (a.streak_days ? ' · 连签 ' + a.streak_days + ' 天' : '');
        left.appendChild(name); left.appendChild(meta);
        const badge = document.createElement('span');
        badge.className = 'checkin-badge ' + (a.today_checked_in ? 'done' : 'todo');
        badge.textContent = a.today_checked_in ? '已领' : '待领';
        item.appendChild(left); item.appendChild(badge);
        list.appendChild(item);
      });
      const msg = document.getElementById('checkinMessage');
      if (unclaimed === 0) {
        msg.textContent = '今日已全部领取 · 连续最高 ' + maxStreak + ' 天';
        msg.className = 'checkin-message ok';
      } else {
        msg.textContent = unclaimed + ' 个账号待领取，点击上方按钮一键领取';
        msg.className = 'checkin-message';
      }
    }

    async function claimCheckin() {
      const btn = document.getElementById('checkinClaimButton');
      const msg = document.getElementById('checkinMessage');
      btn.disabled = true;
      const prev = btn.querySelector('.checkin-claim-label').textContent;
      btn.querySelector('.checkin-claim-label').textContent = '领取中…';
      msg.textContent = '正在领取每日签到积分…';
      msg.className = 'checkin-message';
      try {
        const res = await fetch('/ui/checkin/claim', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        const claimed = data.claimed_accounts || 0;
        const total = (data.claimed_total || 0);
        msg.textContent = claimed > 0 ? ('领取成功：' + claimed + ' 个账号，共 +' + total + ' 积分') : '今日已全部领取';
        msg.className = 'checkin-message ok';
        refreshStatus();
      } catch (e) {
        msg.textContent = '领取失败：' + e.message;
        msg.className = 'checkin-message error';
      } finally {
        btn.querySelector('.checkin-claim-label').textContent = prev;
      }
    }

    document.getElementById('checkinClaimButton').addEventListener('click', claimCheckin);

    // ---- Apiget 外部网关 ----
    function renderGateways(gateways = []) {
      const summary = document.getElementById('apigetSummary');
      const msg = document.getElementById('apigetMessage');
      const container = document.getElementById('apigetGateways');
      const list = Array.isArray(gateways) ? gateways : [];
      window.__latestGateways = list;
      if (!list.length) {
        summary.className = 'apiget-summary';
        summary.innerHTML = '<span class="dot warn" aria-hidden="true"></span> 未配置外部 API 网关';
        container.replaceChildren();
        msg.textContent = '在 auths/gateways.json 配置厂商后启用';
        msg.className = 'apiget-message';
        return;
      }
      const totalModels = list.reduce((n, g) => n + (g.models?.length || 0) + (g.image_models?.length || 0), 0);
      const okCount = list.filter(g => g.configured).length;
      summary.className = 'apiget-summary' + (okCount === list.length ? ' ok' : ' error');
      summary.innerHTML = '<span class="dot ' + (okCount === list.length ? 'ok' : 'error') + '" aria-hidden="true"></span> '
        + okCount + '/' + list.length + ' 个厂商已配置 · 共 ' + totalModels + ' 个模型';
      container.replaceChildren();
      list.forEach(g => {
        const card = document.createElement('div');
        card.className = 'apiget-gw-card';
        const head = document.createElement('div');
        head.className = 'apiget-gw-head';
        const name = document.createElement('span');
        name.className = 'apiget-gw-name';
        name.textContent = g.name || g.id || 'Gateway';
        const badges = document.createElement('span');
        badges.className = 'apiget-gw-badges';
        const status = document.createElement('span');
        status.className = 'apiget-gw-status ' + (g.configured ? 'ok' : 'err');
        status.textContent = g.configured ? '已配置' : '缺 Key';
        badges.appendChild(status);
        const proto = document.createElement('span');
        proto.className = 'apiget-gw-status proto';
        const pLabel = (g.protocol || 'auto') === 'responses' ? 'Responses' : (g.protocol || 'auto') === 'chat' ? 'Chat' : '自动';
        proto.textContent = pLabel;
        badges.appendChild(proto);
        if (g.home_url) {
          const link = document.createElement('a');
          link.className = 'apiget-link';
          link.href = g.home_url;
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
          link.textContent = '官网 ↗';
          badges.appendChild(link);
        }
        const actions = document.createElement('span');
        actions.className = 'gw-card-actions';
        const editBtn = document.createElement('button');
        editBtn.type = 'button';
        editBtn.className = 'gw-card-action';
        editBtn.textContent = '编辑';
        editBtn.dataset.gwId = g.id;
        editBtn.dataset.gwEdit = '1';
        const delBtn = document.createElement('button');
        delBtn.type = 'button';
        delBtn.className = 'gw-card-action danger';
        delBtn.textContent = '删除';
        delBtn.dataset.gwId = g.id;
        delBtn.dataset.gwDelete = '1';
        const toggleBtn = document.createElement('button');
        toggleBtn.type = 'button';
        toggleBtn.className = 'gw-card-action ' + (g.enabled === false ? 'off' : 'on');
        toggleBtn.textContent = g.enabled === false ? '启用' : '停用';
        toggleBtn.dataset.gwId = g.id;
        toggleBtn.dataset.gwToggle = '1';
        toggleBtn.title = g.enabled === false ? '启用该厂商（模型将重新出现在目录中）' : '停用该厂商（模型将从目录中移除）';
        actions.append(editBtn, delBtn, toggleBtn);
        badges.appendChild(actions);
        head.append(name, badges);
        const meta = document.createElement('div');
        meta.className = 'apiget-gw-meta';
        meta.textContent = (g.models?.length || 0) + ' 对话模型 · ' + (g.image_models?.length || 0) + ' 图像模型 · ' + (g.base_url || '');
        card.appendChild(head);
        card.appendChild(meta);
        const models = Array.isArray(g.models) ? g.models : [];
        const imageModels = Array.isArray(g.image_models) ? g.image_models : [];
        if (models.length || imageModels.length) {
          const details = document.createElement('details');
          details.className = 'apiget-collapse';
          const sum = document.createElement('summary');
          sum.textContent = '展开模型列表（' + (models.length + imageModels.length) + ' 个）';
          details.appendChild(sum);
          const listEl = document.createElement('div');
          listEl.className = 'apiget-list';
          models.forEach(m => {
            const item = document.createElement('div');
            item.className = 'apiget-item';
            const left = document.createElement('code');
            left.textContent = (g.prefix || g.id) + '/' + m;
            const badge = document.createElement('span');
            badge.className = 'apiget-badge on';
            badge.textContent = '可用';
            item.append(left, badge);
            listEl.appendChild(item);
          });
          imageModels.forEach(m => {
            const item = document.createElement('div');
            item.className = 'apiget-item';
            const left = document.createElement('code');
            left.textContent = (g.prefix || g.id) + '/' + m;
            const badge = document.createElement('span');
            badge.className = 'apiget-badge on';
            badge.textContent = '图像';
            item.append(left, badge);
            listEl.appendChild(item);
          });
          details.appendChild(listEl);
          card.appendChild(details);
        }
        container.appendChild(card);
      });
      msg.textContent = '经 OpenCodex 选 <厂商前缀>/<模型> 即可调用；图像模型走 /v1/images/generations';
      msg.className = 'apiget-message ok';
    }

    // ---- 第三方 API 网关: 添加 / 编辑 / 删除 ----
    const gatewayDialog = document.getElementById('gatewayDialog');
    const gwNameInput = document.getElementById('gwName');
    const gwBaseUrlInput = document.getElementById('gwBaseUrl');
    const gwApiKeyInput = document.getElementById('gwApiKey');
    const gwPrefixInput = document.getElementById('gwPrefix');
    const gwHomeUrlInput = document.getElementById('gwHomeUrl');
    const gwModelList = document.getElementById('gwModelList');
    const gwMessage = document.getElementById('gwMessage');
    const gwDiscoverButton = document.getElementById('gwDiscoverButton');
    const gwSaveButton = document.getElementById('gwSaveButton');
    let gwEditingId = null;
    let gwDiscoveredModels = { chat: [], image: [] };

    function setGwMessage(text, kind) {
      gwMessage.textContent = text;
      gwMessage.className = 'gw-message' + (kind ? ' ' + kind : '');
    }

    function openGatewayDialog(gateway) {
      gwEditingId = gateway?.id || null;
      gwNameInput.value = gateway?.name || '';
      gwBaseUrlInput.value = gateway?.base_url || '';
      gwApiKeyInput.value = '';
      gwPrefixInput.value = gateway?.prefix || '';
      gwHomeUrlInput.value = gateway?.home_url || '';
      document.getElementById('gwProtocol').value = gateway?.protocol || 'auto';
      document.getElementById('gwProtocolProbe').textContent = '';
      gwDiscoveredModels = { chat: [], image: [] };
      gwModelList.replaceChildren();
      gwSaveButton.disabled = true;
      setGwMessage(gateway ? '编辑 ' + gateway.name + '；正在读取已保存的 API Key…' : '填写接口地址与 API Key 后点击「发现模型」；Key 将保存在本地 auths 配置', '');
      document.getElementById('gatewayDialogTitle').textContent = gateway ? '编辑第三方 API' : '添加第三方 API';
      gatewayDialog.showModal();
      if (gateway) {
        fetch('/ui/gateways/key', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: gateway.id }),
        }).then(r => r.json()).then(data => {
          if (data && data.api_key) {
            gwApiKeyInput.value = data.api_key;
            gwApiKeyInput.type = 'password';
            gwKeyEyeOpen.style.display = '';
            gwKeyEyeClosed.style.display = 'none';
            const src = data.key_source === 'env' ? '环境变量' : '本地配置';
            setGwMessage('已读取 API Key（来源：' + src + '；密文显示，点眼睛可查看）', 'ok');
          } else {
            setGwMessage('编辑 ' + gateway.name + '；未读取到 API Key，可重新填写', '');
          }
        }).catch(() => {
          setGwMessage('编辑 ' + gateway.name + '；读取 Key 失败，可重新填写', 'error');
        });
      }
    }

    function closeGatewayDialog() {
      if (gatewayDialog.open) gatewayDialog.close();
    }

    function renderDiscoveredModels() {
      gwModelList.replaceChildren();
      const chat = gwDiscoveredModels.chat.map(m => ({ name: m, image: false }));
      const image = gwDiscoveredModels.image.map(m => ({ name: m, image: true }));
      if (!chat.length && !image.length) return;
      if (chat.length) appendGwModelGroup(chat, '对话模型');
      if (image.length) appendGwModelGroup(image, '生图模型');
      gwSaveButton.disabled = false;
      setGwMessage('已发现 ' + (chat.length + image.length) + ' 个模型，默认全选，可取消不需要的', 'ok');
    }

    function appendGwModelGroup(items, title) {
      const group = document.createElement('div');
      group.className = 'gw-model-group';
      const head = document.createElement('div');
      head.className = 'gw-model-group-head';
      const titleEl = document.createElement('span');
      titleEl.textContent = `${title}（${items.length}）`;
      const selAll = document.createElement('label');
      selAll.className = 'gw-select-all';
      const allCb = document.createElement('input');
      allCb.type = 'checkbox';
      allCb.checked = true;
      allCb.dataset.group = 'all';
      allCb.addEventListener('change', () => {
        group.querySelectorAll('input[type=checkbox]').forEach(cb => {
          if (cb !== allCb) cb.checked = allCb.checked;
        });
        gwSaveButton.disabled = checkedGwModelCount() === 0;
      });
      selAll.append(allCb, document.createTextNode('全选'));
      head.append(titleEl, selAll);
      group.appendChild(head);
      const list = document.createElement('div');
      list.className = 'gw-model-items';
      items.forEach(m => {
        const item = document.createElement('label');
        item.className = 'gw-model-item';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = true;
        cb.value = m.name;
        cb.dataset.image = m.image ? '1' : '0';
        cb.addEventListener('change', () => {
          gwSaveButton.disabled = checkedGwModelCount() === 0;
        });
        const span = document.createElement('span');
        span.textContent = m.name + (m.image ? '（图像）' : '');
        item.append(cb, span);
        list.appendChild(item);
      });
      group.appendChild(list);
      gwModelList.appendChild(group);
    }

    function checkedGwModelCount() {
      return [...gwModelList.querySelectorAll('input[type=checkbox]:checked')]
        .filter(cb => !cb.dataset.group).length;
    }

    async function discoverGatewayModels() {
      const baseUrl = gwBaseUrlInput.value.trim();
      const apiKey = gwApiKeyInput.value.trim();
      if (!baseUrl || !apiKey) {
        setGwMessage('请先填写接口地址和 API Key', 'error');
        return;
      }
      gwDiscoverButton.disabled = true;
      gwDiscoverButton.textContent = '发现中…';
      setGwMessage('正在探测 ' + baseUrl + ' /models …', '');
      try {
        const res = await fetch('/ui/gateways/discover', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ base_url: baseUrl, api_key: apiKey }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
        gwDiscoveredModels = { chat: data.models || [], image: data.image_models || [] };
        renderDiscoveredModels();
        const protocols = data.protocols || {};
        const probeEl = document.getElementById('gwProtocolProbe');
        const chatOk = protocols.chat ? 'Chat ✅' : 'Chat ❌';
        const respOk = protocols.responses ? 'Responses ✅' : 'Responses ❌';
        probeEl.textContent = '协议探测：' + chatOk + ' · ' + respOk;
        if (protocols.responses) {
          document.getElementById('gwProtocol').value = 'responses';
        } else {
          document.getElementById('gwProtocol').value = 'chat';
        }
      } catch (e) {
        setGwMessage('发现失败：' + e.message, 'error');
      } finally {
        gwDiscoverButton.disabled = false;
        gwDiscoverButton.textContent = '发现模型';
      }
    }

    async function saveGateway() {
      const selected = [...gwModelList.querySelectorAll('input[type=checkbox]:checked')]
        .filter(cb => !cb.dataset.group)
        .map(cb => ({ name: cb.value, image: cb.dataset.image === '1' }));
      if (!selected.length) {
        setGwMessage('请至少勾选一个模型', 'error');
        return;
      }
      const apiKeyVal = gwApiKeyInput.value.trim();
      const payload = {
        id: (gwEditingId || gwNameInput.value.trim()).toLowerCase(),
        name: gwNameInput.value.trim(),
        base_url: gwBaseUrlInput.value.trim(),
        prefix: gwPrefixInput.value.trim(),
        home_url: gwHomeUrlInput.value.trim(),
        models: selected.filter(x => !x.image).map(x => x.name),
        image_models: selected.filter(x => x.image).map(x => x.name),
      };
      if (apiKeyVal) payload.api_key = apiKeyVal;
      payload.protocol = document.getElementById('gwProtocol').value;
      if (!payload.name || !payload.base_url || !payload.prefix) {
        setGwMessage('名称、接口地址、前缀必填', 'error');
        return;
      }
      gwSaveButton.disabled = true;
      try {
        const res = await fetch('/ui/gateways/save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
        closeGatewayDialog();
        refreshStatus();
      } catch (e) {
        setGwMessage('保存失败：' + e.message, 'error');
        gwSaveButton.disabled = false;
      }
    }

    async function deleteGateway(id) {
      if (!window.confirm('确定移除该第三方 API 网关？仅从 Bridge 本地配置移除，不影响线上账号。')) return;
      try {
        const res = await fetch('/ui/gateways/remove', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
        refreshStatus();
      } catch (e) {
        setGwMessage('删除失败：' + e.message, 'error');
      }
    }

    async function toggleGateway(id) {
      const gw = (window.__latestGateways || []).find(g => g.id === id);
      if (!gw) return;
      const next = gw.enabled === false;
      try {
        const res = await fetch('/ui/gateways/toggle', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id, enabled: next }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
        refreshStatus();
      } catch (e) {
        setGwMessage('切换失败：' + e.message, 'error');
      }
    }

    document.getElementById('addGatewayButton').addEventListener('click', () => openGatewayDialog(null));
    document.getElementById('closeGatewayDialogButton').addEventListener('click', closeGatewayDialog);
    document.getElementById('gwCancelButton').addEventListener('click', closeGatewayDialog);
    document.getElementById('gwDiscoverButton').addEventListener('click', discoverGatewayModels);
    document.getElementById('gwSaveButton').addEventListener('click', saveGateway);
    document.getElementById('apigetGateways').addEventListener('click', event => {
      const tog = event.target.closest('button[data-gw-toggle]');
      if (tog) {
        toggleGateway(tog.dataset.gwId);
        return;
      }
      const edit = event.target.closest('button[data-gw-edit]');
      if (edit) {
        const gw = (window.__latestGateways || []).find(g => g.id === edit.dataset.gwId);
        if (gw) openGatewayDialog(gw);
        return;
      }
      const del = event.target.closest('button[data-gw-delete]');
      if (del) deleteGateway(del.dataset.gwId);
    });
    gatewayDialog.addEventListener('cancel', event => { event.preventDefault(); closeGatewayDialog(); });

    const gwKeyToggle = document.getElementById('gwKeyToggle');
    const gwKeyEyeOpen = document.getElementById('gwKeyEyeOpen');
    const gwKeyEyeClosed = document.getElementById('gwKeyEyeClosed');
    gwKeyToggle.addEventListener('click', () => {
      const show = gwApiKeyInput.type === 'password';
      gwApiKeyInput.type = show ? 'text' : 'password';
      gwKeyEyeOpen.style.display = show ? 'none' : '';
      gwKeyEyeClosed.style.display = show ? '' : 'none';
    });

    window.setInterval(() => {
      if (!document.hidden && !loginDialog.open) refreshStatus();
    }, 15000);
  </script>
</body>
</html>
"""
