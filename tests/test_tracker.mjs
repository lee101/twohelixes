import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';

const source = readFileSync(new URL('../web/src/th.js', import.meta.url), 'utf8');

function tracker({ blockedStorage = false, privacy = false, sensitive = false, sendBeacon, responses = [] } = {}) {
  const requests = [];
  const listeners = {};
  const storage = new Map();
  let serial = 0;
  let now = 100000;
  let timerId = 0;
  const timers = new Map();
  const window = {
    __thConfig: { siteId: 'thw_test', autoPage: false, sampleAfterPerSecond: 1 },
    crypto: { randomUUID: () => `id-${++serial}` },
    localStorage: {
      getItem(key) { if (blockedStorage) throw Error('blocked'); return storage.get(key); },
      setItem(key, value) { if (blockedStorage) throw Error('blocked'); storage.set(key, value); },
    },
    addEventListener: (name, callback) => { listeners[name] = callback; },
  };
  const document = {
    referrer: '', visibilityState: 'visible',
    querySelector: () => sensitive ? {} : null,
    addEventListener: (name, callback) => { listeners[name] = callback; },
  };
  runInNewContext(source, {
    window, document, Blob, URL, URLSearchParams, AbortController,
    navigator: { globalPrivacyControl: privacy, userAgent: 'Mozilla/5.0 Chrome/120', language: 'en', sendBeacon },
    location: { href: 'https://example.com/', pathname: '/', search: '', hostname: 'example.com' },
    history: {}, Date: { now: () => now, parse: Date.parse },
    setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout: id => timers.delete(id), setInterval: () => 1,
    fetch: (_url, options) => {
      requests.push(options);
      const response = responses.length ? responses.shift() : { status: 204 };
      if (response === 'stall') {
        return new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(Error('timeout'))));
      }
      return response instanceof Error ? Promise.reject(response) : Promise.resolve(response);
    },
  });
  return {
    settle: async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); },
    runRetry: () => {
      const entry = [...timers].find(([, timer]) => timer.delay >= 1000);
      assert.ok(entry, 'a retry timer exists');
      timers.delete(entry[0]); entry[1].callback(); return entry[1].delay;
    },
    window, requests, listeners, advance: ms => { now += ms; },
    events: () => requests.flatMap(request => JSON.parse(request.body).events),
  };
}

test('business funnel milestones survive burst sampling', async () => {
  const t = tracker();
  for (let i = 0; i < 500; i++) t.window.th('track', 'pointer_moved');
  const milestones = ['sign_up_completed', 'sign_in_started', 'sign_in_completed',
    'checkout_started', 'purchase_completed', 'begin_checkout', 'purchase',
    'paywall', 'paywall_open', 'paywall_signup_clicked', 'paywall_login_clicked'];
  for (const name of milestones) t.window.th('track', name);
  t.window.th('flush');
  await t.settle();
  for (const name of milestones) {
    const event = t.events().find(event => event.event === name);
    assert.ok(event, name);
    assert.equal(event.sample_rate, 1);
  }
});

test('blocked storage retains identity and rolls sessions after inactivity', async () => {
  const t = tracker({ blockedStorage: true });
  t.window.th('track', 'purchase');
  t.window.th('track', 'purchase');
  t.advance(31 * 60 * 1000);
  t.window.th('track', 'purchase');
  t.window.th('flush');
  await t.settle();
  const events = t.events();
  assert.equal(new Set(events.map(event => event.client_id)).size, 1);
  assert.equal(events[0].session_id, events[1].session_id);
  assert.notEqual(events[0].session_id, events[2].session_id);
});

test('UTF-8 batches fit the exit budget; foreground sends avoid keepalive quota', async () => {
  const t = tracker();
  for (let i = 0; i < 50; i++) {
    t.window.th('track', 'purchase', { detail: '\u{1f680}'.repeat(2000) });
    await t.settle();
  }
  t.listeners.pagehide();
  assert.equal(t.events().filter(event => event.event === 'purchase').length, 50);
  assert.ok(t.requests.length > 1);
  for (const request of t.requests) assert.ok(Buffer.byteLength(request.body) <= 48 * 1024);
  assert.ok(t.requests.slice(0, -1).every(request => !request.keepalive));
  assert.equal(t.requests.at(-1).keepalive, true);
});

test('oversized props preserve milestones without poisoning later batches', async () => {
  const t = tracker();
  t.window.th('track', 'purchase', { detail: 'x'.repeat(100000) });
  const circular = {}; circular.self = circular;
  t.window.th('track', 'broken_props', circular);
  t.window.th('track', 'purchase_completed');
  t.window.th('flush');
  await t.settle();
  assert.equal(t.events().length, 2);
  assert.equal(t.events()[0].props._twohelixes_truncated, true);
});

test('exit uses beacon when accepted and fetch keepalive when rejected', async () => {
  const blobs = [];
  const accepted = tracker({ sendBeacon: (_url, blob) => { blobs.push(blob); return true; } });
  accepted.window.th('track', 'purchase');
  accepted.listeners.pagehide();
  assert.equal(accepted.requests.length, 0);
  assert.equal(JSON.parse(await blobs[0].text()).events[0].event, 'purchase');
  assert.ok(blobs[0].size <= 48 * 1024);
  const rejected = tracker({ sendBeacon: () => false });
  rejected.window.th('track', 'purchase');
  rejected.listeners.pagehide();
  assert.equal(rejected.requests[0].keepalive, true);
});

test('privacy signals and sensitive overlays still suppress collection', async () => {
  for (const options of [{ privacy: true }, { sensitive: true }]) {
    const t = tracker(options);
    t.window.th('track', 'purchase');
    t.window.th('identify', 'customer');
    t.window.th('page');
    t.window.th('flush');
  await t.settle();
    assert.equal(t.requests.length, 0);
  }
});

test('503 retries the identical batch and bounded Retry-After before recovery', async () => {
  const t = tracker({ responses: [
    { status: 503, headers: { get: () => '9999999' } }, { status: 204 },
  ] });
  t.window.th('track', 'purchase');
  t.window.th('flush');
  await t.settle();
  assert.equal(t.requests.length, 1);
  const delay = t.runRetry();
  assert.ok(delay >= 30000 && delay < 30250);
  await t.settle();
  assert.equal(t.requests.length, 2);
  assert.equal(t.requests[0].body, t.requests[1].body);
  const event = JSON.parse(t.requests[0].body).events[0];
  assert.ok(event.messageId);
  t.window.th('track', 'purchase_completed');
  t.window.th('flush');
  await t.settle();
  assert.equal(t.requests.length, 3);
  assert.notEqual(JSON.parse(t.requests[2].body).events[0].messageId, event.messageId);
});

test('network and 429 failures retry twice then release the single-flight queue', async () => {
  const t = tracker({ responses: [new Error('offline'), { status: 429 }, { status: 503 }, { status: 204 }] });
  t.window.th('track', 'purchase'); t.window.th('flush');
  t.window.th('track', 'purchase_completed'); t.window.th('flush');
  await t.settle();
  assert.equal(t.requests.length, 1);
  assert.ok(t.runRetry() >= 1000); await t.settle();
  assert.equal(t.requests.length, 2);
  assert.ok(t.runRetry() >= 2000); await t.settle();
  assert.equal(t.requests.length, 4);
  assert.equal(t.requests[0].body, t.requests[1].body);
  assert.equal(t.requests[1].body, t.requests[2].body);
  assert.equal(JSON.parse(t.requests[3].body).events[0].event, 'purchase_completed');
});

test('permanent 4xx responses are not retried', async () => {
  for (const status of [400, 401, 403, 413]) {
    const t = tracker({ responses: [{ status }] });
    t.window.th('track', 'purchase'); t.window.th('flush');
    await t.settle();
    assert.equal(t.requests.length, 1);
  }
});

test('outage buffers at most four batches and resumes without duplicate IDs', async () => {
  const t = tracker({ responses: [{ status: 503 }] });
  for (let i = 0; i < 100; i++) {
    t.window.th('track', 'purchase', { sequence: i }); t.window.th('flush');
  }
  await t.settle();
  assert.equal(t.requests.length, 1);
  t.runRetry(); await t.settle();
  assert.equal(t.requests.length, 5);
  const delivered = t.requests.slice(1).flatMap(request => JSON.parse(request.body).events);
  assert.equal(delivered.length, 4);
  assert.equal(new Set(delivered.map(event => event.messageId)).size, 4);
  assert.deepEqual(delivered.map(event => event.props.sequence), [0, 1, 2, 3]);
});

test('stalled foreground requests abort after ten seconds and retry boundedly', async () => {
  const t = tracker({ responses: ['stall', { status: 204 }] });
  t.window.th('track', 'purchase'); t.window.th('flush');
  await t.settle();
  assert.equal(t.runRetry(), 10000);
  await t.settle();
  assert.equal(t.requests.length, 1);
  assert.ok(t.runRetry() >= 1000);
  await t.settle();
  assert.equal(t.requests.length, 2);
  assert.equal(t.requests[0].body, t.requests[1].body);
});

test('HTTP-date Retry-After honors the bounded remaining delay', async () => {
  const t = tracker({ responses: [{ status: 429, headers: { get: () => new Date(105000).toUTCString() } }] });
  t.window.th('track', 'purchase'); t.window.th('flush');
  await t.settle();
  const delay = t.runRetry();
  assert.ok(delay >= 5000 && delay < 5250);
  await t.settle();
  assert.equal(t.requests.length, 2);
});
