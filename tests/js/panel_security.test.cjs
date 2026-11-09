// Real browser bundles + real panel renderers in a DOM, not a regex-only probe.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const root = path.resolve(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const renderer = process.env.PANEL_RENDERER_SOURCE
  ? fs.readFileSync(process.env.PANEL_RENDERER_SOURCE, 'utf8')
  : read('web/static/js/echarts-panels.js');
const purifier = read('web/static/vendor/dompurify/purify.min.js');
const markdown = read('web/static/vendor/marked/marked.umd.js');

function browser({sanitize = true, marked = true} = {}) {
  const dom = new JSDOM('<!doctype html><body><div id="panel"></div></body>', {
    url: 'https://printguard.example/dashboards/test', runScripts: 'outside-only',
  });
  if (sanitize) dom.window.eval(purifier);
  if (marked) dom.window.eval(markdown);
  dom.window.eval(renderer);
  return dom;
}
function assertInert(element) {
  assert.equal(element.querySelector('script,img,iframe,svg,math,form,input,object,embed,style,template'), null);
  for (const node of element.querySelectorAll('*')) {
    for (const attr of node.attributes) {
      assert.ok(!/^on/i.test(attr.name), `Executable attribute ${attr.name}`);
      assert.ok(!['style', 'id', 'name', 'src', 'srcdoc', 'formaction'].includes(attr.name), attr.name);
      if (attr.name === 'href') assert.ok(!/^(?:javascript|vbscript|data):/i.test(node.href), node.href);
    }
  }
}
const payloads = [
  '<img src=x onerror="window.pwned=1"><script>window.pwned=1</script>',
  '<svg><a xlink:href="javascript:alert(1)">svg</a></svg><math><mtext>x</mtext></math>',
  '<a href="java&#x73;cript:alert(1)" onclick="alert(1)">bad</a><a href="data:text/html,bad">data</a>',
  '<form id="DOMPurify"><input name="sanitize"></form><iframe srcdoc="<script>alert(1)</script>"></iframe>',
  '<div style="background:url(https://attacker.invalid/x)" data-attack="yes" onmouseover="alert(1)">متن</div>',
  '<math><mtext><table><mglyph><style><!--</style><img title="--><img src=x onerror=alert(1)>">',
];

for (const mode of ['html', 'markdown']) {
  test(`${mode}: untrusted saved content is sanitized by the real bundle`, () => {
    const dom = browser();
    try {
      const {document, PrintGuardPanels} = dom.window;
      const panel = document.getElementById('panel');
      for (const content of payloads) {
        PrintGuardPanels.render('text', panel, null, {mode, content});
        assertInert(panel.lastElementChild);
      }
      assert.equal(dom.window.pwned, undefined);
    } finally { dom.window.close(); }
  });
}

test('plain and unknown modes preserve markup as literal text, including Persian and line breaks', () => {
  const dom = browser();
  try {
    const panel = dom.window.document.getElementById('panel');
    const content = 'یادداشت O\'Reilly\n<strong>not bold</strong>' + payloads[0];
    for (const mode of ['plain', 'unknown', 'constructor']) {
      dom.window.PrintGuardPanels.render('text', panel, null, {mode, content, title: '<img src=x onerror=alert(1)>'});
      assert.equal(panel.lastElementChild.textContent, content);
      assert.equal(panel.lastElementChild.childElementCount, 0);
      assert.equal(panel.lastElementChild.style.whiteSpace, 'pre-wrap');
      assert.equal(panel.querySelector('img'), null);
    }
  } finally { dom.window.close(); }
});

test('markdown renders formatting, tables, safe links and code while HTML inside code stays literal', () => {
  const dom = browser();
  try {
    const panel = dom.window.document.getElementById('panel');
    dom.window.PrintGuardPanels.render('text', panel, null, {content:
      '# راهنما\n\n**bold** and *italic*\n\n- first\n- second\n\n[help](https://example.org/help)\n\n' +
      '| A | B |\n| - | - |\n| 1 | 2 |\n\n```html\n<img src=x onerror=alert(1)>\n```'});
    assert.equal(panel.querySelector('h1').textContent, 'راهنما');
    assert.equal(panel.querySelector('strong').textContent, 'bold');
    assert.equal(panel.querySelectorAll('li').length, 2);
    assert.equal(panel.querySelector('a').href, 'https://example.org/help');
    assert.ok(panel.querySelector('table'));
    assert.ok(panel.querySelector('pre code').textContent.includes('<img'));
    assertInert(panel.lastElementChild);
  } finally { dom.window.close(); }
});

test('HTML keeps only supported formatting; unsafe link schemes never survive', () => {
  const dom = browser();
  try {
    const panel = dom.window.document.getElementById('panel');
    dom.window.PrintGuardPanels.render('text', panel, null, {mode: 'html', content:
      '<p><b>متن</b> <a href="/help" title="help">local</a></p><a href="mailto:help@example.org">mail</a>' +
      '<a href="javascript:alert(1)">bad</a><a href="vbscript:msgbox(1)">bad</a>'});
    assert.equal(panel.querySelector('b').textContent, 'متن');
    assert.equal(panel.querySelector('a').getAttribute('href'), '/help');
    assert.equal(panel.querySelectorAll('a[href]').length, 2);
    assertInert(panel.lastElementChild);
  } finally { dom.window.close(); }
});

for (const options of [{sanitize: false}, {marked: false}]) {
  test(`missing dependency fails closed: ${JSON.stringify(options)}`, () => {
    const dom = browser(options);
    try {
      const panel = dom.window.document.getElementById('panel');
      for (const mode of options.sanitize === false ? ['html', 'markdown'] : ['markdown']) {
        dom.window.PrintGuardPanels.render('text', panel, null, {mode, content: payloads[0]});
        assert.equal(panel.lastElementChild.textContent, payloads[0]);
        assert.equal(panel.lastElementChild.childElementCount, 0);
      }
    } finally { dom.window.close(); }
  });
}

test('sanitizer/parser errors fail closed, empty content is not replaced by query data', () => {
  const dom = browser();
  try {
    const panel = dom.window.document.getElementById('panel');
    dom.window.marked = {parse() { throw new Error('parse failure'); }};
    dom.window.PrintGuardPanels.render('text', panel, null, {mode: 'markdown', content: payloads[0]});
    assert.equal(panel.lastElementChild.textContent, payloads[0]);
    assert.equal(panel.lastElementChild.childElementCount, 0);
    dom.window.DOMPurify = {isSupported: true, sanitize() { throw new Error('sanitize failure'); }};
    dom.window.PrintGuardPanels.render('text', panel, null, {mode: 'html', content: payloads[0]});
    assert.equal(panel.lastElementChild.childElementCount, 0);
    dom.window.PrintGuardPanels.render('text', panel, 'unwanted query data', {content: ''});
    assert.equal(panel.lastElementChild.textContent, '');
  } finally { dom.window.close(); }
});

test('alert list click and reused modal keep every alert field inert; close controls still work', () => {
  const dom = browser();
  try {
    const {document, PrintGuardPanels} = dom.window;
    const panel = document.getElementById('panel');
    const fields = ['title', 'message', 'printer_name', 'printer_ip', 'code', 'timestamp', 'time', 'suggested_action'];
    const bad = 'هشدار O\'Reilly "><img src=x onerror="window.pwned=1"><svg onload="alert(1)">';
    for (const field of fields) {
      const alert = {severity: 'critical', [field]: bad};
      PrintGuardPanels.render('alertlist', panel, [alert], {show_filters: false, poll_interval: false});
      panel.firstElementChild.firstElementChild.click();
      const modal = document.getElementById('pg-alert-detail-modal');
      assert.ok(modal.textContent.includes(bad), field);
      assert.equal(modal.querySelector('img,svg,script,iframe'), null, field);
      assert.equal(modal.querySelector('[onerror],[onload],[onclick]'), null, field);
      assert.equal(modal.style.display, 'flex');
      const close = modal.querySelectorAll('[data-alert-close]');
      assert.equal(close.length, 2);
      close[0].click();
      assert.equal(modal.style.display, 'none');
      panel.firstElementChild.firstElementChild.click();
      modal.querySelectorAll('[data-alert-close]')[1].click();
      assert.equal(modal.style.display, 'none');
      assert.equal(document.querySelectorAll('#pg-alert-detail-modal').length, 1);
    }
    assert.equal(dom.window.pwned, undefined);
  } finally { dom.window.close(); }
});

test('normal alert details, filters and severity continue to work', () => {
  const dom = browser();
  try {
    const {document, PrintGuardPanels} = dom.window;
    const panel = document.getElementById('panel');
    PrintGuardPanels.render('alertlist', panel, [
      {severity: 'critical', message: 'تونر تمام شد', printer_ip: '192.0.2.1', timestamp: '2026-11-07T12:00:00Z'},
      {severity: 'info', title: 'اطلاع'},
    ], {poll_interval: false});
    panel.firstElementChild.children[1].click(); // critical filter
    assert.equal(panel.lastElementChild.children.length, 1);
    panel.lastElementChild.firstElementChild.click();
    const modal = document.getElementById('pg-alert-detail-modal');
    assert.ok(modal.textContent.includes('تونر تمام شد'));
    assert.ok(modal.textContent.includes('192.0.2.1'));
    assert.ok(modal.textContent.includes('بحرانی'));
    panel.firstElementChild.children[2].click(); // warning filter, empty
    assert.ok(panel.textContent.includes('هیچ هشدار فعالی'));
  } finally { dom.window.close(); }
});

test('unknown or prototype panel types render an inert error', () => {
  const dom = browser();
  try {
    const panel = dom.window.document.getElementById('panel');
    for (const type of ['constructor', '__proto__', '<img src=x onerror=alert(1)>']) {
      dom.window.PrintGuardPanels.render(type, panel, null, {});
      assert.equal(panel.childElementCount, 0);
      assert.ok(panel.textContent.includes(type));
    }
  } finally { dom.window.close(); }
});

test('committed browser libraries match the lockfile-installed source and load before panel renderers', () => {
  assert.equal(purifier, read('node_modules/dompurify/dist/purify.min.js'));
  assert.equal(markdown, read('node_modules/marked/lib/marked.umd.js'));
  const template = read('web/templates/base.html');
  assert.ok(template.indexOf('vendor/dompurify/purify.min.js') < template.indexOf('js/echarts-panels.js'));
  assert.ok(template.indexOf('vendor/marked/marked.umd.js') < template.indexOf('js/echarts-panels.js'));
});
