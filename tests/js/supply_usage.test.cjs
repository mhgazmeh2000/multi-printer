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
function render(toners) {
  const dom = new JSDOM('<!doctype html><body></body>', {runScripts: 'outside-only'});
  const w = dom.window;
  // Unrelated permissions, accordion storage and paper/counter rendering dependencies.
  Object.assign(w, {canEditPrinters: () => false, canManage: () => false,
    buildPaperSizeBlock: () => '', getPaperSizeAccordionState: () => false,
    getExtraCountersAccordionState: () => false, renderExtraCounterLabel: s => s});
  for (const name of ['escapeHtml', 'jsArg', 'fmtN', 'hasNumericCounterValue', 'buildPrinterDetail']) {
    w.eval(productionFunction(name));
  }
  w.document.body.innerHTML = w.buildPrinterDetail({ip: '192.0.2.1', name: 'Test', online: true, toners});
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
