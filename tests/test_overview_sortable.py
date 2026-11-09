# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_overview_sortable.py
# @brief:        Tests for overview printer card SortableJS UX and mobile disabling.
# @details:      Responsibilities: Verify desktop Sortable configuration, RTL grid options,
#                drag-grip markup, mobile/coarse-pointer disabling, dynamic resize toggling,
#                mid-drag poll protection, post-drop feedback/click guard, and CSS rules.
#                Main API: OverviewSortableTests
#                Side effects: spawns `node` for JS behavioural tests (skipped if node missing)
# @dependencies: internal: web/static/js/dashboard.js, web/static/css/style.css | external: node, unittest
# =============================================================================

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
DASHBOARD_JS = ROOT_DIR / "web" / "static" / "js" / "dashboard.js"
STYLE_CSS = ROOT_DIR / "web" / "static" / "css" / "style.css"


def _extract_block(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class OverviewSortableCssTests(unittest.TestCase):
    """Static verification of Dark/Light Sortable styles and mobile rules in style.css."""

    @classmethod
    def setUpClass(cls):
        cls.css = STYLE_CSS.read_text(encoding="utf-8")

    def test_drag_grip_and_sortable_states_exist_for_dark_and_light_themes(self):
        for selector in (
            ".oc-drag-grip",
            ".overview-grid.sortable-enabled .overview-card:not(.sensor-banner)",
            ".overview-grid.sortable-disabled .oc-drag-grip",
            ".overview-card.sortable-fallback",
            ".overview-card.sortable-ghost",
            ".overview-card.sortable-ghost::after",
            ".overview-card.card-just-dropped",
            "html.light .overview-card.sortable-fallback",
            "html.light .overview-card.sortable-ghost",
            "html.light .overview-card.sortable-ghost::after",
        ):
            self.assertIn(selector, self.css, f"Missing CSS selector: {selector}")

    def test_entrance_animation_is_scoped_so_sortable_and_refreshes_do_not_flicker(self):
        self.assertIn(".overview-grid.cards-entering .overview-card", self.css)
        unscoped_fade = re.search(
            r"(?m)^\.overview-card\s*\{\s*animation:\s*cardFadeIn",
            self.css,
        )
        self.assertIsNone(unscoped_fade, "Unscoped .overview-card cardFadeIn animation must not remain")

    def test_mobile_and_coarse_pointer_css_disable_drag_affordances(self):
        self.assertIn("@media (hover: none) and (pointer: coarse)", self.css)
        mobile_idx = self.css.index("@media (max-width: 768px)")
        mobile_slice = self.css[mobile_idx : mobile_idx + 1400]
        self.assertIn(".overview-grid .oc-drag-grip", mobile_slice)
        self.assertIn("touch-action: manipulation", mobile_slice)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class OverviewSortableJsTests(unittest.TestCase):
    """Behavioural tests executing real dashboard.js Sortable logic in Node."""

    @classmethod
    def setUpClass(cls):
        src = DASHBOARD_JS.read_text(encoding="utf-8")
        cls.sortable_helpers = _extract_block(
            src,
            "let sortableInstance = null;",
            "// ══════════════════════════════════════════════════\n// FETCH & UPDATE",
        )
        cls.overview_render = _extract_block(
            src,
            "function renderOverviewCards(printers) {",
            "// ══════════════════════════════════════════════════\n// PRINTER DETAIL & SENSOR DETAIL PANELS",
        )

    def _run_scenario(self, scenario_js: str) -> dict:
        harness = f"""
const OFFICE_GROUPS = [{{ id: 'hq', name: 'ستاد', subnet: '172.16.25' }}];
let activeGroupFilter = null;
let allData = [];
let switchedTabs = [];
let toasts = [];
let rebuiltTabsCount = 0;
let fetchDataCount = 0;
let isAdmin = true;

function canAdmin() {{ return isAdmin; }}
function getOfficeGroup(ip) {{ return ip.startsWith('172.16.25.') ? 'hq' : 'other'; }}
function escapeHtml(s) {{ return String(s ?? ''); }}
function fmtN(n) {{ return Number(n || 0).toLocaleString('en-US'); }}
function signalQualityBadge() {{ return ''; }}
function sensorValueHtml(o) {{ return `<span>${{o.value ?? '-'}}</span>`; }}
function switchTab(ip) {{ switchedTabs.push(ip); }}
function toast(msg, type) {{ toasts.push({{ msg, type }}); }}
function rebuildTabs() {{ rebuiltTabsCount++; }}
function fetchData() {{ fetchDataCount++; }}

const storageMap = new Map();
const localStorage = {{
  getItem: (k) => storageMap.has(k) ? storageMap.get(k) : null,
  setItem: (k, v) => storageMap.set(k, String(v)),
  removeItem: (k) => storageMap.delete(k),
}};

function makeClassList(initial = []) {{
  const set = new Set(initial);
  return {{
    add: (...cls) => cls.forEach(c => set.add(c)),
    remove: (...cls) => cls.forEach(c => set.delete(c)),
    contains: (c) => set.has(c),
    toArray: () => Array.from(set),
  }};
}}

let currentViewportWidth = 1440;
let currentCoarsePointer = false;
const mqlListeners = [];
const resizeListeners = [];

globalThis.window = {{
  get innerWidth() {{ return currentViewportWidth; }},
  matchMedia: (q) => ({{
    get matches() {{
      return currentViewportWidth <= 768 || currentCoarsePointer;
    }},
    media: q,
    addEventListener: (ev, fn) => {{ if (ev === 'change') mqlListeners.push(fn); }},
  }}),
  addEventListener: (ev, fn) => {{ if (ev === 'resize') resizeListeners.push(fn); }},
}};

function parseCardsFromHtml(html) {{
  const cards = [];
  const re = /<div class="overview-card([^"]*)" data-ip="([^"]+)"/g;
  let m;
  while ((m = re.exec(html)) !== null) {{
    const clsTokens = ('overview-card ' + m[1]).trim().split(/\\s+/).filter(Boolean);
    cards.push({{
      ip: m[2],
      classList: makeClassList(clsTokens),
      offsetWidth: 320,
      getAttribute: function(attr) {{ return attr === 'data-ip' ? this.ip : null; }},
    }});
  }}
  return cards;
}}

const gridEl = {{
  _html: '',
  cards: [],
  classList: makeClassList(),
  set innerHTML(v) {{
    this._html = String(v);
    this.cards = parseCardsFromHtml(this._html);
  }},
  get innerHTML() {{ return this._html; }},
  querySelectorAll: function(sel) {{
    if (sel === '.overview-card') return this.cards;
    if (sel === '.overview-card:not(.sensor-banner)') {{
      return this.cards.filter(c => !c.classList.contains('sensor-banner'));
    }}
    return [];
  }},
}};

globalThis.document = {{
  body: {{ classList: makeClassList() }},
  getElementById: (id) => id === 'overview-grid' ? gridEl : null,
}};

let sortableCreated = [];
globalThis.Sortable = function(el, options) {{
  this.el = el;
  this.options = options;
  this.destroyed = false;
  this.destroy = () => {{ this.destroyed = true; }};
  sortableCreated.push(this);
}};

const SAMPLE_PRINTERS = [
  {{ ip: '172.16.25.10', name: 'Thermometer', device_type: 'sensor', online: true, counters: {{ temp1: 23, hum1: 40 }} }},
  {{ ip: '172.16.25.58', name: 'HP P2055', device_type: 'mono', online: true, counters: {{ total: 1200, black_white: 1200 }}, toners: {{ black: {{ level: 80 }} }} }},
  {{ ip: '172.16.25.60', name: 'Toshiba 2515AC', device_type: 'color', online: true, counters: {{ total: 5400, full_color: 2000, black_white: 3400 }}, toners: {{ cyan: {{ level: 60 }}, magenta: {{ level: 50 }}, yellow: {{ level: 70 }}, black: {{ level: 90 }} }} }},
];

{self.sortable_helpers}
{self.overview_render}

{scenario_js}
"""
        proc = subprocess.run(
            ["node", "-"],
            input=harness.encode("utf-8"),
            capture_output=True,
            timeout=30,
            check=True,
        )
        return json.loads(proc.stdout.decode("utf-8"))

    def test_desktop_enables_sortable_with_visual_options_and_grip_markup(self):
        res = self._run_scenario(
            """
renderOverviewCards(SAMPLE_PRINTERS);
const inst = sortableInstance;
const moveOnSensor = inst.options.onMove({ related: { classList: makeClassList(['overview-card', 'sensor-banner']) } });
const moveOnPrinter = inst.options.onMove({ related: { classList: makeClassList(['overview-card', 'oc-online']) } });
const firstEntrance = gridEl.classList.contains('cards-entering');
renderOverviewCards(SAMPLE_PRINTERS);
const secondEntrance = gridEl.classList.contains('cards-entering');

process.stdout.write(JSON.stringify({
  hasInstance: Boolean(sortableInstance),
  gridEnabled: gridEl.classList.contains('sortable-enabled'),
  gridDisabled: gridEl.classList.contains('sortable-disabled'),
  forceFallback: inst.options.forceFallback,
  fallbackOnBody: inst.options.fallbackOnBody,
  swap: inst.options.swap,
  direction: inst.options.direction,
  draggable: inst.options.draggable,
  animation: inst.options.animation,
  moveOnSensor,
  moveOnPrinter,
  firstEntrance,
  secondEntrance,
  gripCount: (gridEl.innerHTML.match(/class="oc-drag-grip"/g) || []).length,
}));
"""
        )
        self.assertTrue(res["hasInstance"])
        self.assertTrue(res["gridEnabled"])
        self.assertFalse(res["gridDisabled"])
        self.assertTrue(res["forceFallback"])
        self.assertTrue(res["fallbackOnBody"])
        self.assertFalse(res["swap"])
        self.assertEqual(res["direction"], "vertical")
        self.assertEqual(res["draggable"], ".overview-card:not(.sensor-banner)")
        self.assertEqual(res["animation"], 260)
        self.assertFalse(res["moveOnSensor"])
        self.assertTrue(res["moveOnPrinter"])
        self.assertTrue(res["firstEntrance"])
        self.assertFalse(res["secondEntrance"])
        # 2 printer cards get .oc-drag-grip; the 1 sensor-banner does not
        self.assertEqual(res["gripCount"], 2)

    def test_mobile_and_coarse_pointer_disable_sortable_and_resize_toggles_cleanly(self):
        res = self._run_scenario(
            """
// 1. Start on mobile width (375px)
currentViewportWidth = 375;
renderOverviewCards(SAMPLE_PRINTERS);
const mobileInitialInstance = sortableInstance;
const mobileGridDisabled = gridEl.classList.contains('sortable-disabled');
onOverviewCardClick(null, '172.16.25.58');

// 2. Resize up to desktop (1440px) and notify matchMedia listener
currentViewportWidth = 1440;
mqlListeners.forEach(fn => fn());
const desktopHasInstance = Boolean(sortableInstance);
const desktopGridEnabled = gridEl.classList.contains('sortable-enabled');

// 3. Switch to coarse-pointer landscape phone (844px touch device)
currentViewportWidth = 844;
currentCoarsePointer = true;
const prevInst = sortableInstance;
mqlListeners.forEach(fn => fn());

process.stdout.write(JSON.stringify({
  mobileInitialInstance,
  mobileGridDisabled,
  switchedTabs,
  desktopHasInstance,
  desktopGridEnabled,
  prevDestroyed: prevInst.destroyed,
  coarseInstanceAfter: sortableInstance,
  coarseGridDisabled: gridEl.classList.contains('sortable-disabled'),
}));
"""
        )
        self.assertIsNone(res["mobileInitialInstance"])
        self.assertTrue(res["mobileGridDisabled"])
        self.assertEqual(res["switchedTabs"], ["172.16.25.58"])
        self.assertTrue(res["desktopHasInstance"])
        self.assertTrue(res["desktopGridEnabled"])
        self.assertTrue(res["prevDestroyed"])
        self.assertIsNone(res["coarseInstanceAfter"])
        self.assertTrue(res["coarseGridDisabled"])

    def test_mid_drag_poll_is_deferred_and_drop_saves_order_and_guards_synthetic_click(self):
        res = self._run_scenario(
            """
currentViewportWidth = 1440;
currentCoarsePointer = false;
renderOverviewCards(SAMPLE_PRINTERS);
const activeInst = sortableInstance;

// Start dragging
activeInst.options.onStart();
const bodyDraggingDuring = document.body.classList.contains('is-dragging');

// Simulate background fetchData() calling renderOverviewCards mid-drag
renderOverviewCards(SAMPLE_PRINTERS);
const notDestroyedMidDrag = !activeInst.destroyed && sortableInstance === activeInst;

// Simulate reordering the two printer cards in the DOM (swap index 1 and 2)
const tmp = gridEl.cards[1];
gridEl.cards[1] = gridEl.cards[2];
gridEl.cards[2] = tmp;
const droppedCard = gridEl.cards[1];

activeInst.options.onEnd({ oldIndex: 1, newIndex: 2, item: droppedCard });

// Immediate synthetic click right after mouseup must be ignored
let prevented = false;
onOverviewCardClick({ preventDefault: () => { prevented = true; }, stopPropagation: () => {} }, '172.16.25.60');

process.stdout.write(JSON.stringify({
  bodyDraggingDuring,
  bodyDraggingAfter: document.body.classList.contains('is-dragging'),
  notDestroyedMidDrag,
  savedOrder: JSON.parse(localStorage.getItem('printer_order')),
  justDroppedClass: droppedCard.classList.contains('card-just-dropped'),
  toasts,
  prevented,
  switchedTabs,
}));
"""
        )
        self.assertTrue(res["bodyDraggingDuring"])
        self.assertFalse(res["bodyDraggingAfter"])
        self.assertTrue(res["notDestroyedMidDrag"])
        self.assertEqual(res["savedOrder"], ["172.16.25.10", "172.16.25.60", "172.16.25.58"])
        self.assertTrue(res["justDroppedClass"])
        self.assertEqual(res["toasts"], [{"msg": "چیدمان کارت‌ها ذخیره شد", "type": "s"}])
        self.assertTrue(res["prevented"])
        self.assertEqual(res["switchedTabs"], [])


if __name__ == "__main__":
    unittest.main()
