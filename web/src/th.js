/**
 * twoHelixes analytics tracker.
 *
 * Rules this file lives by:
 *  - it can never throw into the host page: every entry point is wrapped
 *  - it can never block: no synchronous XHR, no layout reads on the hot path,
 *    delivery is sendBeacon first and fetch(keepalive) second
 *  - it can never lose the tail of a session: the queue flushes on
 *    visibilitychange and pagehide, not on unload alone (which iOS ignores)
 *  - a failed request is dropped, not retried forever: analytics must not
 *    become the reason a page is slow or a device is hot
 *
 * API (GA-shaped, with a Segment-shaped alias):
 *   th('track', 'signup_started', {plan: 'pro'})
 *   th('page')                       // auto-fired on load and on SPA nav
 *   th('identify', 'user_123', {email: ...})
 *   analytics.track(...) / analytics.page() / analytics.identify(...)
 */
(function (window, document) {
  'use strict';

  var CONFIG_KEY = '__thConfig';
  var STORAGE_CLIENT = 'th_cid';
  var STORAGE_SESSION = 'th_sid';
  var STORAGE_SESSION_TS = 'th_sts';
  var SESSION_GAP_MS = 30 * 60 * 1000;
  var FLUSH_DELAY_MS = 2000;
  var MAX_QUEUE = 50;

  var config = window[CONFIG_KEY] || {};
  var privacySignal =
    navigator.globalPrivacyControl === true ||
    navigator.doNotTrack === '1' ||
    window.doNotTrack === '1';
  if (privacySignal || !config.siteId) {
    window.th = function () {};
    window.analytics = window.analytics || {
      track: function () {},
      page: function () {},
      identify: function () {}
    };
    return;
  }
  var endpoint = config.endpoint || 'https://twohelixes.com/v1/collect';
  var siteId = config.siteId;
  var queue = [];
  var timer = null;
  var started = Date.now();
  var engagement = 0;
  var lastActive = Date.now();
  var userId = null;

  function safe(fn) {
    return function () {
      try {
        return fn.apply(null, arguments);
      } catch (e) {
        /* analytics must never break the page */
      }
    };
  }

  function uuid() {
    try {
      if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
      if (window.crypto && window.crypto.getRandomValues) {
        var buf = new Uint8Array(16);
        window.crypto.getRandomValues(buf);
        var out = '';
        for (var i = 0; i < buf.length; i++) out += (buf[i] + 0x100).toString(16).slice(1);
        return out;
      }
    } catch (e) {}
    return 'x' + Math.random().toString(36).slice(2) + Date.now().toString(36);
  }

  function store(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (e) {}
  }

  function load(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }

  function clientId() {
    var id = load(STORAGE_CLIENT);
    if (!id) {
      id = uuid();
      store(STORAGE_CLIENT, id);
    }
    return id;
  }

  function sessionId() {
    var now = Date.now();
    var id = load(STORAGE_SESSION);
    var ts = parseInt(load(STORAGE_SESSION_TS) || '0', 10);
    if (!id || !ts || now - ts > SESSION_GAP_MS) {
      id = uuid();
      store(STORAGE_SESSION, id);
    }
    store(STORAGE_SESSION_TS, String(now));
    return id;
  }

  function param(name) {
    try {
      return new URLSearchParams(location.search).get(name) || '';
    } catch (e) {
      return '';
    }
  }

  function baseEvent(name, props) {
    var event = {
      event: name,
      ts: Date.now(),
      client_id: clientId(),
      session_id: sessionId(),
      user_id: userId,
      page_location: location.href,
      page_path: location.pathname,
      referrer: document.referrer || '',
      utm_source: param('utm_source'),
      utm_medium: param('utm_medium'),
      utm_campaign: param('utm_campaign'),
      utm_term: param('utm_term'),
      utm_content: param('utm_content'),
      screen: (window.screen ? window.screen.width + 'x' + window.screen.height : ''),
      viewport: (window.innerWidth || 0) + 'x' + (window.innerHeight || 0),
      language: navigator.language || '',
      engagement_ms: engagement,
      props: props || {}
    };
    engagement = 0;
    return event;
  }

  function sensitiveSheetOpen() {
    try {
      return Boolean(
        document.querySelector(
          '#signin-overlay[open],#checkout-overlay[open],.signin:not([hidden]),' +
          '[data-sensitive-analytics]:not([hidden])'
        )
      );
    } catch (e) {
      return false;
    }
  }

  function send(body, useBeacon) {
    var payload = JSON.stringify(body);
    try {
      if (useBeacon && navigator.sendBeacon) {
        // text/plain keeps it a simple request: no CORS preflight, no delay.
        var blob = new Blob([payload], { type: 'text/plain;charset=UTF-8' });
        if (navigator.sendBeacon(endpoint, blob)) return;
      }
    } catch (e) {}
    try {
      fetch(endpoint, {
        method: 'POST',
        body: payload,
        keepalive: true,
        mode: 'cors',
        credentials: 'omit',
        headers: { 'Content-Type': 'text/plain;charset=UTF-8' }
      }).catch(function () {});
    } catch (e) {}
  }

  function flush(useBeacon) {
    if (!queue.length || !siteId) return;
    var events = queue.splice(0, queue.length);
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    send({ site_id: siteId, events: events }, useBeacon !== false);
  }

  function enqueue(event) {
    queue.push(event);
    if (queue.length >= MAX_QUEUE) {
      flush(false);
      return;
    }
    if (!timer) {
      timer = setTimeout(function () {
        timer = null;
        flush(false);
      }, FLUSH_DELAY_MS);
    }
  }

  var track = safe(function (name, props) {
    if (!name || sensitiveSheetOpen()) return;
    enqueue(baseEvent(String(name).slice(0, 64), props));
  });

  var page = safe(function (props) {
    if (sensitiveSheetOpen()) return;
    started = Date.now();
    enqueue(baseEvent('page_view', props));
  });

  var identify = safe(function (id, traits) {
    if (sensitiveSheetOpen()) return;
    userId = id ? String(id).slice(0, 64) : null;
    var event = baseEvent('identify', traits);
    event.type = 'identify';
    event.traits = traits || {};
    enqueue(event);
  });

  // -- engagement: count only time the tab is actually visible --------------
  function bumpEngagement() {
    var now = Date.now();
    if (document.visibilityState === 'visible') engagement += Math.min(now - lastActive, 60000);
    lastActive = now;
  }

  // -- automatic instrumentation -------------------------------------------
  function hookHistory() {
    var wrap = function (method) {
      var original = history[method];
      if (typeof original !== 'function') return;
      history[method] = function () {
        var result = original.apply(this, arguments);
        try {
          setTimeout(function () {
            page();
          }, 0);
        } catch (e) {}
        return result;
      };
    };
    wrap('pushState');
    wrap('replaceState');
    window.addEventListener('popstate', safe(function () {
      page();
    }));
  }

  function hookOutbound() {
    document.addEventListener(
      'click',
      safe(function (e) {
        var node = e.target;
        while (node && node.nodeName !== 'A') node = node.parentNode;
        if (!node || !node.href) return;
        if (node.closest && node.closest(
          '#signin-overlay,#checkout-overlay,.signin,[data-sensitive-analytics]'
        )) return;
        var href = node.href;
        var isOutbound = node.hostname && node.hostname !== location.hostname;
        var isFile = /\.(pdf|zip|mp4|mp3|wav|csv|xlsx?|docx?|png|jpe?g|webp|svg)$/i.test(node.pathname || '');
        if (isOutbound) track('outbound_click', { url: href, host: node.hostname });
        else if (isFile) track('file_download', { url: href });
      }),
      true
    );
  }

  function hookErrors() {
    window.addEventListener(
      'error',
      safe(function (e) {
        if (!e || !e.message) return;
        track('js_error', {
          error_type: e.error && e.error.name ? String(e.error.name).slice(0, 80) : 'Error',
          source_host: (function () {
            try { return new URL(e.filename || '', location.href).host; } catch (err) { return ''; }
          })()
        });
      })
    );
  }

  function hookLifecycle() {
    document.addEventListener(
      'visibilitychange',
      safe(function () {
        bumpEngagement();
        if (document.visibilityState === 'hidden') flush(true);
      })
    );
    window.addEventListener(
      'pagehide',
      safe(function () {
        bumpEngagement();
        track('page_exit', { duration_ms: Date.now() - started });
        flush(true);
      })
    );
    setInterval(safe(bumpEngagement), 5000);
  }

  // -- public surface -------------------------------------------------------
  var api = safe(function (command) {
    var args = Array.prototype.slice.call(arguments, 1);
    switch (command) {
      case 'track':
        return track(args[0], args[1]);
      case 'page':
        return page(args[0]);
      case 'identify':
        return identify(args[0], args[1]);
      case 'flush':
        return flush(false);
      case 'config':
        if (args[0] && args[0].siteId) siteId = args[0].siteId;
        if (args[0] && args[0].endpoint) endpoint = args[0].endpoint;
        return;
    }
  });

  // Replay anything queued before this script finished loading.
  var pending = window.th && window.th.q ? window.th.q : [];
  window.th = api;
  window.analytics = window.analytics || {
    track: function (name, props) {
      track(name, props);
    },
    page: function (props) {
      page(props);
    },
    identify: function (id, traits) {
      identify(id, traits);
    }
  };

  safe(function () {
    hookHistory();
    hookOutbound();
    hookErrors();
    hookLifecycle();
    for (var i = 0; i < pending.length; i++) api.apply(null, pending[i]);
    if (config.autoPage !== false) page();
  })();
})(window, document);
