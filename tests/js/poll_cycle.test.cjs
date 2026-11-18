const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.resolve(__dirname, '../../web/static/js/poll-cycle.js'), 'utf8');
const context = vm.createContext({});
vm.runInContext(source, context);
const baseTime = Date.parse('2026-11-07T12:00:00Z');
const status = (extra = {}) => ({server_time: new Date(baseTime).toISOString(),
  next_poll_at: new Date(baseTime + 60000).toISOString(), poll_interval: 60,
  count: 1, last: 'cycle-1', is_polling: false, ...extra});
const meta = {poll_count: 1, last_poll: 'cycle-1'};
function fixture(extra = {}) {
  let time = baseTime, seq = 0, checks = 0, refreshes = 0, current = status();
  const tasks = new Map(), views = [];
  const api = context.PrintGuardPollCycle.create({now: () => time,
    setTimeout(fn, delay) { const id = ++seq; tasks.set(id, {fn, at: time + delay}); return id; },
    clearTimeout(id) { tasks.delete(id); },
    render(v) { views.push(v); },
    async readStatus() { checks++; return current; },
    async refresh() { refreshes++; api.accept(current, {poll_count: current.count, last_poll: current.last}); },
    ...extra,
  });
  return {api, tasks, views, get checks() { return checks; }, get refreshes() { return refreshes; },
    setTime(t) { time = t; }, setStatus(s) { current = s; },
    async tick(ms) {
      const target = time + ms;
      while (true) {
        const next = [...tasks.entries()].filter(([,t]) => t.at <= target).sort((a,b) => a[1].at - b[1].at)[0];
        if (!next) break;
        time = next[1].at; tasks.delete(next[0]); await next[1].fn();
      }
      time = target;
    },
  };
}
test('60-second bar follows server clock despite skewed browser clock', async () => {
  const f = fixture();
  f.setTime(baseTime + 3600000);
  f.api.accept(status(), meta);
  assert.equal(f.api.view().percent, 100);
  await f.tick(5000);
  assert.ok(Math.abs(f.api.view().percent - 55 / 60 * 100) < .01);
  f.api.stop();
});
test('running cycle is indeterminate, never a fake remaining-page percentage', () => {
  const f = fixture(); f.api.accept(status({is_polling: true}), meta);
  assert.equal(f.api.view().state, 'polling');
  assert.match(f.api.view().text, /در حال پایش/);
  f.api.stop();
});
test('unchanged cycle checks status without reloading printer data', async () => {
  const f = fixture(); f.api.accept(status(), meta); await f.tick(30000);
  assert.equal(f.checks, 3); assert.equal(f.refreshes, 0); f.api.stop();
});
test('automatic completion refreshes snapshot and then stays quiet', async () => {
  const f = fixture(); f.api.accept(status({is_polling: true}), meta);
  f.setStatus(status({count: 2, last: 'cycle-2'})); await f.tick(3000);
  assert.equal(f.refreshes, 1); await f.tick(10000); assert.equal(f.refreshes, 1); f.api.stop();
});
test('fast/manual cycle missed between status checks is caught by completion token', async () => {
  const f = fixture(); f.api.accept(status(), meta);
  f.setStatus(status({count: 2, last: 'manual-cycle'})); f.api.wake(); await f.tick(0);
  assert.equal(f.refreshes, 1); f.api.stop();
});
test('printer/status response race compares with rendered printer metadata', async () => {
  const f = fixture(); f.api.accept(status({count: 2, last: 'cycle-2'}), meta);
  f.setStatus(status({count: 2, last: 'cycle-2'})); await f.tick(10000);
  assert.equal(f.refreshes, 1); f.api.stop();
});
test('failed cycle leaving same counter still refreshes on running-to-idle', async () => {
  const f = fixture(); f.api.accept(status({is_polling: true}), meta);
  await f.tick(3000); assert.equal(f.refreshes, 1); f.api.stop();
});
test('skipped refresh is retried until actual printer snapshot is accepted', async () => {
  let refreshes = 0;
  const f = fixture({async refresh() { refreshes++; }});
  f.api.accept(status(), meta); f.setStatus(status({count: 2})); await f.tick(20000);
  assert.equal(refreshes, 2); f.api.stop();
});
test('network failures back off and recover without killing the watcher', async () => {
  let fail = true;
  const f = fixture({async readStatus() { if (fail) throw Error('offline'); return status(); }});
  f.api.accept(status(), meta); await f.tick(10000);
  assert.equal(f.api.view().state, 'error');
  fail = false; f.api.wake(); await f.tick(0);
  assert.equal(f.api.view().state, 'idle'); f.api.stop();
});
test('overdue or missing schedule never invents a fresh 60-second deadline', () => {
  const f = fixture(); f.api.accept(status({next_poll_at: null}), meta);
  assert.equal(f.api.view().state, 'waiting');
  f.api.accept(status({next_poll_at: new Date(baseTime - 1000).toISOString()}), meta);
  assert.equal(f.api.view().percent, 0); assert.equal(f.api.view().state, 'waiting'); f.api.stop();
});
test('only one status read can run, wake bursts cannot overlap it', async () => {
  let release, calls = 0;
  const pending = new Promise(resolve => { release = resolve; });
  const f = fixture({async readStatus() { calls++; return pending; }});
  f.api.accept(status(), meta); f.api.wake();
  const p = f.tick(0);
  f.api.wake(); f.api.wake();
  assert.equal(calls, 1); release(status()); await p; f.api.stop();
});
test('stop cancels all timers, including a status response arriving after logout', async () => {
  let release;
  const f = fixture({readStatus: () => new Promise(resolve => { release = resolve; })});
  f.api.accept(status(), meta); f.api.wake(); const pending = f.tick(0);
  f.api.stop(); release(status({count: 5})); await pending;
  assert.equal(f.tasks.size, 0); assert.equal(f.refreshes, 0);
});

test('dashboard wiring loads helper first, uses one watcher and has bounded network waits', () => {
  const dashboard = fs.readFileSync(path.resolve(__dirname, '../../web/static/js/dashboard.js'), 'utf8');
  const template = fs.readFileSync(path.resolve(__dirname, '../../web/templates/base.html'), 'utf8');
  assert.ok(template.indexOf('js/poll-cycle.js') < template.indexOf('js/dashboard.js'));
  assert.match(dashboard, /markRendered\(pr.meta\)/);
  assert.match(dashboard, /_ensurePollCycleSync\(\).wake\(\);\nfetchData\(\)/);
  assert.doesNotMatch(dashboard, /countdown = 40|_pollWatchTimer/);
  assert.match(dashboard, /controller.abort\(\), 15000/);
  assert.match(dashboard, /controller.abort\(\), 30000/);
});

test('production DOM wiring displays busy state, clock label and restores manual button', () => {
  const {JSDOM} = require('jsdom');
  const dom = new JSDOM('<!doctype html><div id="cbar"><div id="cfill"></div></div><span id="poll-cycle-label"></span><button onclick="triggerPoll()"></button>', {runScripts: 'outside-only'});
  const w = dom.window;
  const dashboard = fs.readFileSync(path.resolve(__dirname, '../../web/static/js/dashboard.js'), 'utf8');
  const functionSource = name => {
    const start = dashboard.indexOf(`function ${name}(`);
    return dashboard.slice(start, dashboard.indexOf('\n}', start) + 2);
  };
  let options, accepted;
  w.PrintGuardPollCycle = {create(o) { options = o; return {accept(s, m) { accepted = [s, m]; }}; }};
  w._syncConnBanner = () => {};
  w._cycleSync = null;
  for (const name of ['_pollBtn', '_setPollBtnBusy', '_ensurePollCycleSync', 'resetCountdown']) w.eval(functionSource(name));
  try {
    w.resetCountdown(status(), meta);
    assert.equal(accepted[0].poll_interval, 60);
    options.render({state: 'polling', text: 'در حال پایش دستگاه‌ها…', percent: 100});
    const bar = w.document.getElementById('cbar');
    assert.equal(bar.dataset.state, 'polling');
    assert.equal(bar.getAttribute('aria-busy'), 'true');
    assert.equal(w.document.querySelector('button').disabled, true);
    options.render({state: 'idle', text: 'پایش بعدی حدود ۳۰ ثانیه دیگر', percent: 50});
    assert.equal(w.document.querySelector('button').disabled, false);
    assert.equal(w.document.getElementById('cfill').style.width, '50%');
    assert.match(w.document.getElementById('poll-cycle-label').textContent, /۳۰/);
  } finally { w.close(); }
});


test('slow printer refresh never holds the status watcher or duplicates refreshes', async () => {
  let finish, refreshes = 0;
  const pending = new Promise(resolve => { finish = resolve; });
  const f = fixture({async refresh() { refreshes++; await pending; }});
  f.api.wake(); await f.tick(0);
  await f.tick(40000);
  assert.ok(f.checks >= 5);
  assert.equal(refreshes, 1);
  assert.equal(f.api.view().state, 'idle');
  finish(); await Promise.resolve(); f.api.stop();
});

test('rejected printer refresh leaves healthy status and retries without stale acknowledgement', async () => {
  let refreshes = 0;
  const f = fixture({async refresh() { refreshes++; throw Error('printer timeout'); }});
  f.api.wake(); await f.tick(0); await f.tick(30000);
  assert.ok(refreshes >= 2);
  assert.equal(f.api.view().state, 'idle');
  f.api.markRendered(meta);
  const previous = refreshes;
  await f.tick(10000);
  assert.equal(refreshes, previous);
  f.api.stop();
});

test('marking printer metadata does not rewind the server clock or clear status errors', async () => {
  const f = fixture({async readStatus() { throw Error('offline'); }});
  f.api.accept(status()); await f.tick(5000);
  const remaining = f.api.view().remaining;
  f.api.markRendered(meta);
  assert.equal(f.api.view().remaining, remaining);
  f.api.wake(); await f.tick(0);
  assert.equal(f.api.view().state, 'error');
  f.api.markRendered(meta);
  assert.equal(f.api.view().state, 'error');
  f.api.stop();
});
