const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function load(host = 'github.example.com') {
  const calls = [];
  let listener;
  const runtime = { getManifest() { return { host_permissions: ['github.com', 'github.example.com', 'octocorp.ghe.com'].map(h => `https://${h}/*`) }; }, id: 'extension-id', onMessage: { addListener(fn) { listener = fn; } },
    sendNativeMessage(host, message, callback) { calls.push({ host, message, callback }); } };
  const context = vm.createContext({ URL, chrome: { runtime } });
  vm.runInContext(fs.readFileSync(`${__dirname}/../extension/background.js`, 'utf8'), context);
  const prUrl = `https://${host}/team/repo/pull/42`;
  const sender = { id: runtime.id, tab: { id: 1 }, url: prUrl + '/files' };
  return { calls, runtime, sender, prUrl,
    request(message = { type: 'review-status', prUrl }, origin = sender) {
      return new Promise(resolve => listener(message, origin, resolve));
    } };
}

test('status requests use native messaging, coalesce, and cache briefly', async () => {
  const bridge = load();
  const first = bridge.request();
  const second = bridge.request();
  assert.equal(bridge.calls.length, 1);
  assert.equal(bridge.calls[0].host, 'com.agent_pr_review.status');
  bridge.calls[0].callback({ state: 'exists', privateField: 'must not leak' });
  assert.equal((await first).state, 'exists');
  assert.deepEqual(Object.keys(await second), ['state']);
  assert.equal((await bridge.request()).state, 'exists');
  assert.equal(bridge.calls.length, 1);
});

test('rejects other origins, other PRs and unsupported commands without contacting the host', async () => {
  const bridge = load();
  const invalidSenders = [
    { ...bridge.sender, id: 'other-extension' },
    { ...bridge.sender, tab: null },
    { ...bridge.sender, url: 'https://evil.example/team/repo/pull/42' },
    { ...bridge.sender, url: 'https://github.example.com/team/repo/issues/42' },
  ];
  for (const sender of invalidSenders) assert.equal((await bridge.request(undefined, sender)).state, 'unavailable');
  assert.equal((await bridge.request({ type: 'review-status', prUrl: bridge.prUrl + '0' })).state, 'unavailable');
  assert.equal((await bridge.request({ type: 'launch', prUrl: bridge.prUrl })).state, 'unavailable');
  assert.equal(bridge.calls.length, 0);
});

test('host errors and malformed responses become unavailable, not absent', async () => {
  for (const error of [true, false]) {
    const bridge = load();
    const result = bridge.request();
    if (error) bridge.runtime.lastError = { message: 'native host not installed' };
    bridge.calls[0].callback(error ? { state: 'exists' } : { state: 'unexpected' });
    assert.equal((await result).state, 'unavailable');
  }
});

for (const host of ['github.com', 'github.example.com', 'octocorp.ghe.com']) {
  test(`routes status for configured host ${host}`, async () => {
    const bridge = load(host);
    const result = bridge.request();
    assert.equal(bridge.calls[0].message.prUrl, bridge.prUrl);
    bridge.calls[0].callback({ state: 'running' });
    assert.equal((await result).state, 'running');
  });
}
