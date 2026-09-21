const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function load(pathname = '/team/repo/pull/42/files', stored = {}) {
  const requests = [];
  const timers = [];
  const removed = [];
  const localRemoved = [];
  const storage = {
    get(keys, callback) {
      callback(Object.fromEntries(keys.filter((key) => Object.hasOwn(stored, key)).map((key) => [key, stored[key]])));
    },
    set(items, callback) {
      Object.assign(stored, items);
      callback?.();
    },
    remove(keys) {
      removed.push(...keys);
      for (const key of keys) delete stored[key];
    },
  };
  const context = vm.createContext({
    URLSearchParams,
    setInterval() {},
    setTimeout(fn, delay) { const timer = { fn, delay }; timers.push(timer); return timer; },
    clearTimeout(timer) { timer.cancelled = true; },
    chrome: { storage: { local: storage }, runtime: {
      sendMessage(message, callback) { requests.push({ message, callback }); },
    } },
    document: {
      addEventListener() {}, body: {},
      querySelector() { return null; },
      querySelectorAll() { return []; },
    },
    window: {
      location: { pathname, host: 'github.example.com' },
      addEventListener() {},
      localStorage: { getItem() { return null; }, removeItem(key) { localRemoved.push(key); } },
    },
    MutationObserver: class { observe() {} },
  });
  // This suite exercises a configured T3 install, including its existing preferences.
  vm.runInContext('const DEFAULT_REVIEW_CLI = "t3code";', context);
  vm.runInContext(fs.readFileSync(`${__dirname}/../extension/content.js`, 'utf8'), context);
  const run = (code) => vm.runInContext(code, context);
  run.removed = removed;
  run.localRemoved = localRemoved;
  run.stored = stored;
  run.context = context;
  run.requests = requests;
  run.timers = timers;
  return run;
}

test('new destination defaults to T3 and normalizes PR subpages', () => {
  const run = load();
  assert.equal(run('getLaunchUrl(DEFAULT_REVIEW_CLI, getPrUrl())'),
    'agent-pr-review://github.example.com/team/repo/pull/42?cli=t3code');
});
test('terminal choices remain explicit and unknown choices use T3', () => {
  const run = load();
  for (const cli of ['agent', 'claude', 't3code']) {
    assert.equal(run(`getLaunchUrl('${cli}', getPrUrl())`).split('?')[1], `cli=${cli}`);
  }
  assert.equal(run('normalizeCli("unrecognized")'), 't3code');
});
test('non-PR pages have no launch target', () => {
  assert.equal(load('/team/repo/issues/42')('getPrUrl()'), null);
});
test('upgrade clears the retired extension and page storage keys', async () => {
  const run = load('/team/repo/pull/42', { 'review-cli': 'agent' });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(run.stored['review-target'], 'agent');
  assert.deepEqual(run.removed, ['review-cli']);
  assert.deepEqual(run.localRemoved, ['review-target', 'review-cli']);
});

test('live existing review changes the visible button label and color', async () => {
  const run = load();
  await new Promise(setImmediate);
  assert.equal(run.requests[0].message.prUrl, 'https://github.example.com/team/repo/pull/42');
  run.requests[0].callback({ state: 'exists' });
  const button = {};
  const classes = new Map();
  run.context.launcher = {
    querySelector(sel) { return sel.endsWith('primary') ? button : { querySelectorAll() { return []; } }; },
    classList: { toggle(name, value) { classes.set(name, value); } },
  };
  run('renderLauncher(launcher)');
  assert.match(button.innerHTML, /Review exists in T3/);
  assert.equal(classes.get('has-t3-review'), true);
  assert.equal(run('getLaunchUrl(selectedCli, getPrUrl())'), 'agent-pr-review://github.example.com/team/repo/pull/42?cli=t3code');
});

test('SPA navigation ignores old responses even when the launcher stays mounted', async () => {
  const run = load();
  await new Promise(setImmediate);
  run.context.window.location.pathname = '/team/repo/pull/43';
  run('refreshReviewStatus()');
  assert.equal(run.requests.length, 2);
  run.requests[0].callback({ state: 'exists' });
  assert.equal(run('reviewStatus'), 'checking');
  run.requests[1].callback({ state: 'missing' });
  assert.equal(run('reviewStatus'), 'missing');
});

test('failed, invalid and timed out checks never masquerade as missing reviews', async () => {
  const run = load();
  await new Promise(setImmediate);
  run.context.chrome.runtime.lastError = { message: 'host missing' };
  run.requests[0].callback({ state: 'exists' });
  delete run.context.chrome.runtime.lastError;
  assert.equal(run('reviewStatus'), 'unavailable');
  run('refreshReviewStatus(true)');
  run.requests[1].callback({ state: 'unexpected' });
  assert.equal(run('reviewStatus'), 'unavailable');
  run('refreshReviewStatus(true)');
  run.timers.at(-1).fn();
  run.requests[2].callback({ state: 'exists' });
  assert.equal(run('reviewStatus'), 'unavailable');
});

test('polls coalesce while pending and do not run for hidden tabs or other tools', async () => {
  const run = load();
  await new Promise(setImmediate);
  run('refreshReviewStatus(true); refreshReviewStatus(true)');
  assert.equal(run.requests.length, 1);
  run.requests[0].callback({ state: 'exists' });
  run.context.document.hidden = true;
  run('refreshReviewStatus(true)');
  run.context.document.hidden = false;
  run('selectedCli = "agent"; refreshReviewStatus(true)');
  assert.equal(run.requests.length, 1);
});

test('launcher mounts in the React pull request header and unhides its actions slot', () => {
  const run = load('/team/repo/pull/42');
  const classes = new Set(['d-none']);
  const prepended = [];
  const actions = { classList: { remove(name) { classes.delete(name); } }, prepend(node) { prepended.push(node); } };
  const seen = [];
  run.context.document.querySelector = (sel) => {
    seen.push(sel);
    return sel === '[data-component="PageHeader"] [data-component="PH_Actions"]' ? actions : null;
  };
  const element = () => {
    const children = [];
    const matches = (sel) => children.flatMap((child) => (child.className.split(' ').includes(sel.slice(1)) ? [child] : child.querySelectorAll(sel)));
    return { className: '', innerHTML: '', dataset: {}, classList: { add() {}, toggle() {}, remove() {} }, setAttribute() {}, addEventListener() {},
      append(...nodes) { children.push(...nodes); }, appendChild(node) { children.push(node); },
      querySelector(sel) { return matches(sel)[0] ?? null; }, querySelectorAll: matches };
  };
  run.context.document.createElement = element;
  run('injectLauncher()');
  assert.equal(seen[0], '.review-launcher');
  assert.equal(seen[1], '[data-component="PageHeader"] [data-component="PH_Actions"]');
  assert.equal(prepended.length, 1);
  assert.equal(classes.has('d-none'), false);
});

test('1.5.0 declares the background native messaging bridge', () => {
  const manifest = JSON.parse(fs.readFileSync(`${__dirname}/../extension/manifest.json`));
  assert.equal(manifest.version, '1.5.0');
  assert.ok(manifest.permissions.includes('nativeMessaging'));
  assert.equal(manifest.background.service_worker, 'background.js');
});

test('public bundle starts with Cursor and injects configuration before the launcher', () => {
  const manifest = JSON.parse(fs.readFileSync(`${__dirname}/../extension/manifest.json`));
  assert.deepEqual(manifest.host_permissions, ['https://github.com/*']);
  assert.deepEqual(manifest.content_scripts[0].js, ['defaults.js', 'content.js']);
  assert.match(fs.readFileSync(`${__dirname}/../extension/defaults.js`, 'utf8'), /DEFAULT_REVIEW_CLI = "agent"/);
});
