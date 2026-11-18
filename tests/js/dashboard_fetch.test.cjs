// Execute the real fetch/watcher/banner wiring, with controlled network and clocks.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const dashboard = fs.readFileSync(path.resolve(__dirname, '../../web/static/js/dashboard.js'), 'utf8');
const watcher = fs.readFileSync(path.resolve(__dirname, '../../web/static/js/poll-cycle.js'), 'utf8');
function functionSource(name) {
  const start = dashboard.indexOf(`function ${name}(`);
  assert.ok(start >= 0, name);
  return dashboard.slice(dashboard.slice(start - 6, start) === 'async ' ? start - 6 : start,
    dashboard.indexOf('\n}', start) + 2);
}
function fixture() {
  const dom = new JSDOM('<!doctype html><div id="cbar"><div id="cfill"></div></div><span id="poll-cycle-label"></span><button onclick="triggerPoll()"></button>', {runScripts: 'outside-only', url: 'https://printguard.example/'});
  const w = dom.window;
  let time = Date.parse('2026-11-07T12:00:00Z'), sequence = 0;
  const tasks = new Map(), requests = [], views = [];
  let failPrinters = true, failStatus = false;
  const status = () => ({server_time: new Date(time).toISOString(), is_polling: true,
    poll_interval: 60, count: 7, last: 'new-cycle', csrf_token: 'fresh-token'});
  w.Date.now = () => time;
  w.setTimeout = (fn, ms) => { const id = ++sequence; tasks.set(id, {fn, at: time + ms}); return id; };
  w.clearTimeout = id => tasks.delete(id);
  Object.assign(w, {API: '', _cycleSync: null, _fetchInFlight: false,
    _connBanner: null, _printerFetchFailed: false, _statusFetchFailed: false,
    _retryTimer: null, _retryDelay: 3000, RETRY_DELAY_BASE: 3000, RETRY_DELAY_MAX: 60000,
    _errToastShown: false, _lastFetchOkAt: 0, _dataRevision: 0,
    _startupWarmupTimer: null, allData: [{ip: 'old-snapshot'}], serverInfo: {}, isFirst: true,
    updateMeta() {}, rebuildTabs() {}, renderOverviewCards(data) { views.push(data); },
    renderAllPrinterPanels() {}, renderAccessPanel() {}, populatePrinterSelect() {},
    setDot(s) { w.dot = s; }, toast() {}, fetchLogsInBackground() { w.logs++; }, logs: 0,
    _scheduleStartupWarmupIfNeeded() {}, updateCsrfToken(token) { w.token = token; },
  });
  w.console.warn = () => {};
  w.fetch = (url, options) => {
    requests.push(url);
    if (url.endsWith('/api/status')) return failStatus ? Promise.reject(Error('status offline'))
      : Promise.resolve({ok: true, status: 200, json: async () => status()});
    assert.ok(url.endsWith('/api/printers'));
    if (!failPrinters) return Promise.resolve({ok: true, status: 200, json: async () => ({
      printers: [{ip: 'fresh-snapshot'}], meta: {poll_count: 7, last_poll: 'new-cycle'},
    })});
    return new Promise((resolve, reject) => options.signal.addEventListener('abort', () =>
      reject(new w.DOMException('aborted', 'AbortError')), {once: true}));
  };
  w.eval(watcher);
  for (const name of ['_getConnBanner', '_showConnBanner', '_hideConnBanner', '_syncConnBanner',
    'scheduleFetchRetry', '_pollBtn', '_setPollBtnBusy', '_ensurePollCycleSync', 'fetchData']) {
    w.eval(functionSource(name));
  }
  const flush = async () => { for (let i = 0; i < 25; i++) await Promise.resolve(); };
  return {w, requests, views, flush,
    recoverPrinters() { failPrinters = false; }, failStatus() { failStatus = true; },
    async tick(ms) {
      const target = time + ms;
      while (true) {
        const next = [...tasks].filter(([, t]) => t.at <= target).sort((a,b) => a[1].at - b[1].at)[0];
        if (!next) break;
        time = next[1].at; tasks.delete(next[0]); next[1].fn(); await flush();
      }
      time = target; await flush();
    },
    close() { w._cycleSync?.stop(); dom.window.close(); },
  };
}

test('cold start status/cbar/CSRF work while first printer request hangs, then times out honestly', async () => {
  const f = fixture();
  try {
    f.w._ensurePollCycleSync().wake();
    const initial = f.w.fetchData();
    await f.tick(29000);
    assert.ok(f.requests.filter(p => p.endsWith('/api/status')).length >= 9);
    assert.equal(f.w.document.getElementById('cbar').dataset.state, 'polling');
    assert.equal(f.w.token, 'fresh-token');
    assert.equal(f.views.length, 0);
    assert.equal(f.requests.filter(p => p.endsWith('/api/printers')).length, 1);
    await f.tick(2000);
    assert.equal(await initial, false);
    assert.equal(f.w.allData[0].ip, 'old-snapshot');
    assert.equal(f.w._dataRevision, 0);
    assert.equal(f.w.dot, 'err');
    const banner = f.w.document.getElementById('conn-retry-banner');
    assert.equal(banner.style.display, 'block');
    assert.match(banner.textContent, /اطلاعات چاپگرها تازه نشده/);
    assert.equal(f.w.document.getElementById('cbar').dataset.state, 'polling');
  } finally { f.close(); }
});

test('retry recovery renders once, clears printer warning even for silent success, and preserves status state', async () => {
  const f = fixture();
  try {
    f.w._ensurePollCycleSync().wake(); f.w.fetchData(); await f.tick(30000);
    f.recoverPrinters();
    // A watcher refresh must respect the scheduled printer backoff.
    assert.equal(await f.w.fetchData({silent: true}), false);
    await f.tick(3000);
    assert.equal(f.w.allData[0].ip, 'fresh-snapshot');
    assert.equal(f.views.length, 1);
    assert.equal(f.w.document.getElementById('conn-retry-banner').style.display, 'none');
    assert.equal(f.w.document.getElementById('cbar').dataset.state, 'polling');
    f.w._printerFetchFailed = true;
    assert.equal(await f.w.fetchData({silent: true}), true);
    assert.equal(f.w._printerFetchFailed, false);
    assert.equal(f.w._retryTimer, null);
  } finally { f.close(); }
});

test('successful printer response cannot conceal failure of the independent status connection', async () => {
  const f = fixture();
  try {
    f.failStatus(); f.recoverPrinters();
    f.w._ensurePollCycleSync().wake(); await f.tick(0);
    assert.equal(await f.w.fetchData(), true);
    assert.equal(f.w.document.getElementById('cbar').dataset.state, 'error');
    const banner = f.w.document.getElementById('conn-retry-banner');
    assert.equal(banner.style.display, 'block');
    assert.match(banner.textContent, /وضعیت سرور/);
  } finally { f.close(); }
});
