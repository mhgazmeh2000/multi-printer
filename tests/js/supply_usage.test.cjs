// Exercise the production detail renderer in jsdom; no reimplementation of its supply markup.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const source = fs.readFileSync(path.resolve(__dirname, '../../web/static/js/dashboard.js'), 'utf8');
function productionFunction(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, name);
  const end = source.indexOf('\n}', start) + 2;
  return source.slice(start, end);
}
function render(toners, extra = {}) {
  const dom = new JSDOM('<!doctype html><body></body>', {runScripts: 'outside-only', url: 'https://printguard.example/'});
  const w = dom.window;
  w.eval(fs.readFileSync(path.resolve(__dirname, '../../web/static/js/supply-usage-info.js'), 'utf8'));
  // Unrelated permissions, accordion storage and paper/counter rendering dependencies.
  Object.assign(w, {canEditPrinters: () => false, canManage: () => false,
    buildPaperSizeBlock: () => '', getPaperSizeAccordionState: () => false,
    getExtraCountersAccordionState: () => false, renderExtraCounterLabel: s => s});
  for (const name of ['escapeHtml', 'jsArg', 'fmtN', 'hasNumericCounterValue', 'buildPrinterDetail']) {
    w.eval(productionFunction(name));
  }
  w.document.body.innerHTML = w.buildPrinterDetail({ip: '192.0.2.1', name: 'Test', online: true, toners, ...extra});
  return dom;
}

test('supply cards show independent counted pages and honest tracking dates', () => {
  const dom = render({black: {level: 50, supply_pages: 1234, first_install_date: '2026-11-01', last_use_date: '2026-11-05'},
    cyan: {level: 80, supply_pages: 0}});
  try {
    const blocks = dom.window.document.querySelectorAll('.supply-usage');
    assert.equal(blocks.length, 2);
    assert.equal(blocks[0].querySelector('strong').textContent, '1,234');
    assert.match(blocks[0].textContent, /شروع ردیابی این کارتریج/);
    assert.match(blocks[0].textContent, /2026-11-01/);
    assert.match(blocks[0].textContent, /2026-11-05/);
    assert.match(blocks[0].title, /لزوماً تاریخ نصب واقعی نیست/);
    assert.equal(blocks[1].querySelector('strong').textContent, '0');
    assert.equal((blocks[1].textContent.match(/—/g) || []).length, 2);
  } finally { dom.window.close(); }
});

test('missing or invalid usage displays placeholders, not fabricated counts', () => {
  for (const value of [undefined, null, NaN, Infinity, -1, '500', true]) {
    const dom = render({black: {level: 50, supply_pages: value}});
    try {
      const block = dom.window.document.querySelector('.supply-usage');
      assert.equal(block.querySelector('strong').textContent, '—');
      assert.equal((block.textContent.match(/—/g) || []).length, 3);
    } finally { dom.window.close(); }
  }
});

test('persisted date strings are inert text, never HTML or event attributes', () => {
  const payload = '<img src=x onerror="window.pwned=1"><script>window.pwned=1</script>';
  const dom = render({black: {level: 50, supply_pages: 12, first_install_date: payload, last_use_date: payload}});
  try {
    const block = dom.window.document.querySelector('.supply-usage');
    assert.equal(block.querySelector('img,script'), null);
    assert.equal(block.children[3].textContent, payload);
    assert.equal(block.children[5].textContent, payload);
    assert.equal(dom.window.pwned, undefined);
  } finally { dom.window.close(); }
});

function dialogAPI(dom) {
  // jsdom does not implement native modal behavior. Emulate the API/events only;
  // production code owns listeners/content/focus restoration, browsers own the focus trap.
  const w = dom.window;
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () {
    this.open = false;
    this.dispatchEvent(new w.Event('close'));
  };
}

test('information buttons appear only on CMYK supplies of explicitly color devices', () => {
  for (const device_type of ['mono', 'unknown', undefined, 'color']) {
    const dom = render({black: {level: 50}, cyan: {level: 40}, drum: {level: 90}}, {device_type});
    try {
      const buttons = dom.window.document.querySelectorAll('[data-supply-info]');
      assert.equal(buttons.length, device_type === 'color' ? 2 : 0);
      for (const button of buttons) {
        assert.equal(button.tagName, 'BUTTON');
        assert.equal(button.type, 'button');
        assert.equal(button.getAttribute('aria-haspopup'), 'dialog');
        assert.ok(button.getAttribute('aria-label'));
      }
    } finally { dom.window.close(); }
  }
});

test('raw usage wins over rounded legacy Mega Dots; rounding is display-only', () => {
  const toner = {level: 50, usage: 2479877, usage_m: 2.48, supply_pages: 7};
  const original = JSON.stringify(toner);
  const dom = render({cyan: toner}, {device_type: 'color', counters: {total: 1000, full_color: 200}});
  try {
    dialogAPI(dom);
    dom.window.document.querySelector('[data-supply-info]').click();
    const dialog = dom.window.document.querySelector('dialog');
    assert.equal(dialog.open, true);
    assert.match(dialog.textContent, /2,479,877/);
    assert.match(dialog.textContent, /2\.48M/);
    assert.match(dialog.textContent, /تمام‌رنگی/);
    assert.match(dialog.textContent, /مبنای مستقل مصرف ثبت نشده/);
    assert.match(dialog.textContent, /محاسبه نمی‌شود/);
    assert.match(dialog.textContent, /فرمول شمارش صفحات، تخمین فعلی و رفتار ریست را تغییر نمی‌دهد/);
    assert.equal(JSON.stringify(toner), original);
    assert.equal(dom.window.PrintGuardSupplyUsage.megaText(2479877, 999), '2.48');
    assert.equal(dom.window.PrintGuardSupplyUsage.megaText(1), '0.00');
    assert.equal(dom.window.PrintGuardSupplyUsage.megaText(0), '0.00');
    assert.equal(dom.window.PrintGuardSupplyUsage.megaText(null, 2.48), '2.48');
  } finally { dom.window.close(); }
});

test('missing/invalid dot readings stay unavailable; reported zero remains distinct', () => {
  for (const raw of [null, undefined, -1, NaN, Infinity, true, '123', 1.5, Number.MAX_SAFE_INTEGER + 1, 0]) {
    const dom = render({black: {level: 50, usage: raw}}, {device_type: 'color'});
    try {
      dialogAPI(dom);
      dom.window.document.querySelector('[data-supply-info]').click();
      const rows = dom.window.document.querySelectorAll('dialog dd');
      assert.equal(rows[6].textContent, raw === 0 ? '0' : 'ناموجود / نامعتبر');
      assert.equal(rows[7].textContent, raw === 0 ? '0.00M' : 'ناموجود / نامعتبر');
    } finally { dom.window.close(); }
  }
});

test('dialog safely renders hostile names/dates; all close paths restore focus and reuse one dialog', () => {
  const payload = '"><img src=x onerror="window.pwned=1"><script>window.pwned=1</script>';
  const dom = render({black: {level: 50, name: payload, first_install_date: payload, last_use_date: payload, usage: 727249},
    yellow: {level: 60, usage: 250749}}, {device_type: 'color', ip: '192.0.2.1', last_poll: payload});
  try {
    dialogAPI(dom);
    dom.window.fetch = () => { throw new Error('Diagnostics must not request the network'); };
    const {document, Event} = dom.window;
    const [black, yellow] = document.querySelectorAll('[data-supply-info]');
    for (const method of ['button', 'backdrop', 'cancel']) {
      black.focus();
      black.click();
      const dialog = document.querySelector('dialog');
      assert.equal(dialog.querySelector('img,script,iframe'), null);
      assert.ok(dialog.textContent.includes(payload));
      assert.equal(dialog.getAttribute('aria-labelledby'), 'supply-info-title');
      assert.equal(document.activeElement, dialog.querySelector('button'));
      assert.match(dialog.textContent, /کل صفحات/);
      if (method === 'button') dialog.querySelector('button').click();
      else if (method === 'backdrop') dialog.click();
      else dialog.dispatchEvent(new Event('cancel', {cancelable: true}));
      assert.equal(dialog.open, false);
      assert.equal(document.activeElement, black);
    }
    yellow.click();
    assert.equal(document.querySelectorAll('dialog').length, 1);
    assert.match(document.querySelector('dialog').textContent, /250,749/);
    assert.ok(!document.querySelector('dialog .supply-info-device').textContent.includes(payload));
    assert.ok(!document.querySelector('dialog').textContent.includes('727,249'));
    assert.equal(document.querySelector('dialog img, dialog script'), null);
    assert.equal(dom.window.pwned, undefined);
    assert.equal(dom.window.localStorage.length, 0);
  } finally { dom.window.close(); }
});

test('production template loads helper before dashboard renderer', () => {
  const template = fs.readFileSync(path.resolve(__dirname, '../../web/templates/base.html'), 'utf8');
  assert.ok(template.indexOf("filename='js/supply-usage-info.js'") < template.indexOf("filename='js/dashboard.js'"));
});
