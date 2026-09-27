// Runs inside the real ZCode.app renderer. Wraps initAliyunCaptcha so that
// every ticket the App itself obtains is mirrored to the local relay.
//
// Why this exists: Aliyun binds a captchaVerifyParam to the device/browser
// fingerprint that produced it. A ticket minted in a separate Chrome and then
// replayed by ZCode is rejected upstream with 3012 "unusual activity". The
// only ticket that survives is one the App mints in its own popup, so we
// harvest that one instead of trying to forge our own.
(() => {
  if (window.__fkSniffInstalled) return 'already-installed';
  window.__fkSniffInstalled = true;
  window.__fkTickets = [];

  const RELAY = 'http://127.0.0.1:8910/save';
  const post = (param) => {
    window.__fkTickets.push({at: Date.now(), len: (param || '').length});
    const body = 'p=' + encodeURIComponent(param || '');
    try {
      return fetch(RELAY, {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: body,
        mode: 'cors',
        keepalive: true
      }).then((r) => 'relay:' + r.status)
        .catch((e) => 'relay-failed:' + e.message);
    } catch (e) {
      return 'post-threw:' + e.message;
    }
  };

  const wrapSuccess = (cfg) => {
    const orig = cfg.success;
    cfg.success = function (param, ...rest) {
      const r = post(param);
      console.log('[fk-sniff] ticket captured len=' + (param || '').length);
      if (typeof orig === 'function') {
        try { return orig.call(this, param, ...rest); } catch (e) { /* ignore */ }
      }
      return undefined;
    };
    return cfg;
  };

  const origInit = window.initAliyunCaptcha;
  if (typeof origInit !== 'function') return 'no-initAliyunCaptcha';

  window.initAliyunCaptcha = function (cfg) {
    try { cfg = wrapSuccess(cfg || {}); } catch (e) { /* ignore */ }
    return origInit.call(this, cfg);
  };
  return 'installed';
})();
