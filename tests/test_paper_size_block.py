# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_paper_size_block.py
# @brief:        Tests for buildPaperSizeBlock() and its accordion toggle in web/static/js/dashboard.js.
# @details:      Responsibilities: shares add up to 100, derived "copy and other" values,
#                hiding of inconsistent splits, unknown counter note, bad input,
#                accordion markup (closed by default, ids, sanitised ip) and toggle logic.
#                Main API: PaperSizeBlockTests
#                Side effects: spawns `node` (tests are skipped when node is missing)
# @dependencies: internal: web/static/js/dashboard.js | external: node, unittest
# @notes:        The function is extracted from the real source, so the test follows the code.
# =============================================================================

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

DASHBOARD_JS = Path(__file__).resolve().parents[1] / "web" / "static" / "js" / "dashboard.js"
SAMPLE = {
    "A4": {"total": 171820, "fc": 55120, "bw": 116700},
    "A3": {"total": 12410, "fc": 6420, "bw": 5990},
    "A4R": {"total": 9310, "fc": 2210, "bw": 7100},
    "A5": {"total": 98440, "fc": 30110, "bw": 68330},
    "B4": {"total": 0, "fc": 0, "bw": 0},
}


def _function_source(name: str = "buildPaperSizeBlock") -> str:
    src = DASHBOARD_JS.read_text(encoding="utf-8")
    start = src.index(f"function {name}(")
    end = src.index("\n}\n", start) + 3
    return src[start:end]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PaperSizeBlockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fn = _function_source()

    def _node(self, script: str) -> str:
        # Bytes in, bytes out: the default text encoding differs per OS (cp1252 on Windows) and the
        # markup contains Persian text. The script goes through stdin so no command-line encoding applies.
        out = subprocess.run(["node", "-"], input=script.encode("utf-8"), capture_output=True, timeout=30, check=True)
        return out.stdout.decode("utf-8")

    def render(self, pz, opts=None) -> str:
        return self._node(
            "const fmtN = v => Number(v).toLocaleString('en-US');\n"
            + self.fn
            + f"\nprocess.stdout.write(buildPaperSizeBlock({json.dumps(pz)}, {json.dumps(opts)}));"
        )

    @staticmethod
    def text(html: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()

    def test_sample_numbers_and_shares(self):
        text = self.text(self.render(SAMPLE))
        self.assertIn("171,820", text)
        self.assertIn("93.3%", text)
        self.assertIn("6.7%", text)               # 93.3 + 6.7 = 100
        self.assertIn("چاپ از رایانه 98,440 57%", text)
        self.assertIn("کپی و سایر 73,380 43%", text)    # 171,820 - 98,440
        self.assertIn("کپی و سایر 3,100 25%", text)     # 12,410 - 9,310
        self.assertIn("رنگی 55,120 32%", text)
        self.assertIn("سیاه‌سفید 5,990 48%", text)

    def test_legacy_row_labels_are_gone(self):
        text = self.text(self.render(SAMPLE))
        for legacy in ("چاپ·بزرگ", "چاپ·کوچک", "FC:", "BW:", "نامشخص"):
            self.assertNotIn(legacy, text)

    def test_nothing_to_show_returns_empty_string(self):
        self.assertEqual(self.render({}), "")
        self.assertEqual(self.render({"A4": {"total": 0}, "A3": {"total": 0}}), "")

    def test_inconsistent_print_counter_hides_the_split(self):
        text = self.text(self.render({"A4": {"total": 100, "fc": 10, "bw": 90}, "A5": {"total": 150}}))
        self.assertNotIn("منبع چاپ", text)        # prints (150) > class total (100): nothing is guessed
        self.assertIn("نوع رنگ", text)

    def test_black_and_white_only_hides_the_color_split(self):
        text = self.text(self.render({"A4": {"total": 1000, "fc": 0, "bw": 1000}, "A5": {"total": 400}}))
        self.assertIn("منبع چاپ", text)
        self.assertNotIn("نوع رنگ", text)

    def test_single_class_is_100_percent(self):
        text = self.text(self.render({"A3": {"total": 500, "fc": 0, "bw": 500}}))
        self.assertIn("100%", text)
        self.assertNotIn("کاغذ کوچک", text)

    def test_unknown_counter_is_a_footnote_only_when_non_zero(self):
        with_unknown = self.text(self.render({"A4": {"total": 1000}, "B4": {"total": 42}}))
        self.assertIn("شاخه ۲۲۷", with_unknown)
        self.assertIn("42", with_unknown)
        self.assertNotIn("شاخه ۲۲۷", self.text(self.render({"A4": {"total": 1000}, "B4": {"total": 0}})))

    def test_accordion_is_closed_by_default_and_open_on_request(self):
        closed = self.render(SAMPLE, {"ip": "172.16.25.10"})
        self.assertIn('aria-expanded="false"', closed)
        self.assertNotRegex(closed, r'class="extra-counters-accordion-header open"')
        self.assertNotRegex(closed, r'class="extra-counters-accordion-body ps-acc-body open"')
        self.assertIn("93.3%", closed)                     # content stays in the DOM while collapsed

        opened = self.render(SAMPLE, {"ip": "172.16.25.10", "open": True})
        self.assertIn('aria-expanded="true"', opened)
        self.assertIn('class="extra-counters-accordion-header open"', opened)
        self.assertIn('class="extra-counters-accordion-body ps-acc-body open"', opened)

    def test_header_ids_handler_and_summary(self):
        html = self.render(SAMPLE, {"ip": "172.16.25.10"})
        self.assertIn('id="paper-size-header-172-16-25-10"', html)
        self.assertIn('id="paper-size-body-172-16-25-10"', html)
        self.assertIn('aria-controls="paper-size-body-172-16-25-10"', html)
        self.assertIn("togglePaperSizeAccordion('172.16.25.10')", html)
        summary = self.text(re.search(r'<span class="ps-acc-summary">(.*?)</span>\s*<span class="extra-counters-accordion-icon"', html, re.S).group(1))
        self.assertEqual(summary, "کوچک 93.3% · بزرگ 6.7%")

    def test_ip_is_sanitised_before_it_reaches_the_handler(self):
        html = self.render(SAMPLE, {"ip": "1.2.3.4');alert(1);//"})
        self.assertNotIn("alert(1)", html)
        self.assertIn("togglePaperSizeAccordion('1.2.3.4alert1')", html)

    def test_toggle_updates_classes_aria_and_persists(self):
        script = (
            _function_source("toggleAccordionSection")
            + """
            const mk = () => { const s = new Set(); return { classList: { contains: c => s.has(c),
              toggle: (c, on) => { on ? s.add(c) : s.delete(c); }}, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } }; };
            const els = { 'paper-size-body-1-2-3-4': mk(), 'paper-size-header-1-2-3-4': mk() };
            global.document = { getElementById: id => els[id] || null };
            const saved = [];
            toggleAccordionSection('paper-size', '1.2.3.4', (ip, open) => saved.push([ip, open]));
            toggleAccordionSection('paper-size', '1.2.3.4', (ip, open) => saved.push([ip, open]));
            toggleAccordionSection('paper-size', '9.9.9.9', (ip, open) => saved.push([ip, open]));   // unknown ip: no-op
            process.stdout.write(JSON.stringify({ saved, aria: els['paper-size-header-1-2-3-4'].attrs['aria-expanded'] }));
            """
        )
        result = json.loads(self._node(script))
        self.assertEqual(result["saved"], [["1.2.3.4", True], ["1.2.3.4", False]])
        self.assertEqual(result["aria"], "false")

    def test_garbage_values_do_not_break_rendering(self):
        text = self.text(self.render({"A4": {"total": "abc"}, "A3": {"total": "1200", "fc": "300", "bw": "900"}}))
        self.assertIn("1,200", text)
        self.assertNotIn("NaN", text)
        self.assertNotIn("undefined", text)


if __name__ == "__main__":
    unittest.main()
