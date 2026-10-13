# -*- coding: utf-8 -*-
"""تستهایِ tools/compare_snmp_sources.py (گام G.6).

سه چیزسنجی: (۱) کدِ خروجِ صحيح برای pair_ok / pair_diff / ورودیِ ناقص؛ (۲) آینه و snapshot
از دو مسیرِ نام‌گذاریِ مختلف به هم می‌رسند (سطرهایِ موردِ انتظارِ هر fixture الزاماً
برقرار می‌شوند)؛ (۳) هیچ عددی از دستگاه واقعی در repo ثبت نشده — هر سه fixture «مصنوعی»اند
و گزارش هم N/A را اختراع نمی‌کند.

اعدادِ موردِ انتظار همه از *اجرای همین سوئیت* روی fixtureهایِ مصنوعیِ G.5.1 به دست آمده‌اند،
نه از دستگاه واقعی؛ سنجهٔ واقعیِ دو منبع روی سرورِ مالک انجام می‌شود (§۱۰ از
docs/snmp-exporter-setup.md).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
TOOL = REPO_ROOT / "tools" / "compare_snmp_sources.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("compare_snmp_sources", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL_MODULE = load_tool()


class CompareToolTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)

    # ── helpers ──────────────────────────────────────────────────────────
    def fixture(self, name):
        path = FIXTURES / f"compare_snmp_pair_{name}.json"
        self.assertTrue(path.is_file(), f"fixture نیست: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def write_pair(self, doc):
        exp = self.tmp / "capture.txt"
        exp.write_text(doc["exporter_capture"], encoding="utf-8")
        snap = self.tmp / "snapshot.json"
        snap.write_text(json.dumps(doc["snmp_snapshot"], ensure_ascii=False), encoding="utf-8")
        return exp, snap

    def run_tool(self, argv):
        """main() را با stdout/stderr ضبط‌شده اجرا می‌کند → (rc, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = TOOL_MODULE.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def run_fixture(self, name, extra=(), capture_override=None, snapshot_override=None):
        doc = self.fixture(name)
        if capture_override is not None:
            doc = dict(doc, exporter_capture=capture_override)
        if snapshot_override is not None:
            doc = dict(doc, snmp_snapshot=snapshot_override)
        exp, snap = self.write_pair(doc)
        out_json, out_md = self.tmp / "report.json", self.tmp / "report.md"
        argv = ["--exporter-capture", str(exp), "--snmp-snapshot", str(snap),
                "--json", str(out_json), "--md", str(out_md), *extra]
        rc, stdout, stderr = self.run_tool(argv)
        rep = json.loads(out_json.read_text(encoding="utf-8")) if out_json.exists() else None
        md = out_md.read_text(encoding="utf-8") if out_md.exists() else ""
        return rc, rep, md, stdout, stderr, doc

    def row(self, rep, path):
        for r in rep["rows"]:
            if r["path"] == path:
                return r
        self.fail(f"سطر `{path}` در گزارش نیست")

    def assert_expect_rows(self, rep, doc):
        for path, verdict in doc.get("expect_rows", {}).items():
            self.assertEqual(self.row(rep, path)["verdict"], verdict, f"سطر {path}")


class FixturePairTests(CompareToolTestCase):
    def test_pair_ok_exits_zero_and_has_no_differences(self):
        rc, rep, md, stdout, stderr, doc = self.run_fixture("ok")
        self.assertEqual(rc, doc["expect_exit"], stderr)
        self.assertEqual(rep["summary"]["diff"], 0)
        self.assert_expect_rows(rep, doc)
        self.assertEqual(rep["brand"], "hp")
        self.assertEqual(rep["tolerance_pct"], 5.0)
        self.assertIn("**diff 0**", md)
        self.assertIn("**هیچ اختلافِ معناداری دیده نشد**", md)

    def test_pair_ok_matches_measured_row_counts(self):
        rc, rep, md, stdout, stderr, doc = self.run_fixture("ok")
        s = rep["summary"]
        # 30 مسیرِ مشترک از 32 سطر؛ دو سطرِ باقی‌مانده nickname/device_type هستند
        self.assertEqual((s["rows"], s["common_paths"], s["ok"], s["diff"], s["missing"]),
                         (32, 30, 30, 0, 2))
        self.assertEqual(s["only_in_snapshot"], ["device_type", "nickname"])
        self.assertEqual(s["only_in_exporter"], [])
        self.assertGreaterEqual(s["common_paths"], doc["expect_summary"]["common_paths_min"])

    def test_shape_gap_is_reported_as_missing_not_as_difference(self):
        """کاستیِ ساختاریِ آینه (۴+۲ کلیدِ collect_enhanced) باید «missing» باشد نه «diff»."""
        rc, rep, md, stdout, stderr, doc = self.run_fixture("ok")
        for r in rep["rows"]:
            if r["verdict"] == "missing":
                self.assertIn(r["note"], ("only_in_exporter", "only_in_snapshot"))
                self.assertIsNone(r["rel_diff_pct"])
                self.assertIsNone(r["exporter"] if r["note"] == "only_in_snapshot" else r["snapshot"])
        paths = {r["path"] for r in rep["rows"]}
        for path in doc["expect_no_rows"]:
            self.assertNotIn(path, paths, f"{path} نباید سطر بسازد (هر دو طرف None/تهی)")
        self.assertIn("کاستیِ ساختاریِ آینه", md)

    def test_pair_diff_flags_exactly_the_perturbed_fields(self):
        rc, rep, md, stdout, stderr, doc = self.run_fixture("diff")
        self.assertEqual(rc, doc["expect_exit"], stderr)
        self.assert_expect_rows(rep, doc)
        s = rep["summary"]
        self.assertEqual((s["rows"], s["common_paths"], s["ok"], s["diff"], s["missing"]),
                         (32, 30, 27, 3, 2))
        self.assertEqual(sorted(r["path"] for r in rep["diff_rows"]),
                         ["counters.total", "device.model", "toners.black.level"])
        for path, pct in doc["expect_rel_diff_pct"].items():
            self.assertAlmostEqual(self.row(rep, path)["rel_diff_pct"], pct, places=2,
                                   msg=path)
        # trays.0.level عمداً زیرِ تلورانس است تا «sensitiviy» آستانه سنجیده شود
        self.assertEqual(self.row(rep, "trays.0.level")["verdict"], "ok")
        self.assertAlmostEqual(self.row(rep, "trays.0.level")["rel_diff_pct"], 3.7, places=2)
        self.assertEqual(rep["summary"]["meaningful_common_paths"], 26)
        self.assertIn("## اختلاف‌هایِ معنادار", md)

    def test_canon_pair_agrees_and_fills_pdu_columns(self):
        rc, rep, md, stdout, stderr, doc = self.run_fixture("canon")
        self.assertEqual(rc, doc["expect_exit"], stderr)
        self.assertEqual(rep["brand"], "canon")
        self.assert_expect_rows(rep, doc)
        self.assertEqual(rep["summary"]["diff"], 0)
        self.assertEqual(rep["summary"]["rows"], 44)
        self.assertEqual(rep["summary"]["common_paths"], 41)
        # alerts رشته‌ایِ فارسی هم مسیرِ مشترک است (نه drop شده)
        self.assertEqual(self.row(rep, "alerts.0.message")["verdict"], "ok")
        self.assertEqual(self.row(rep, "alerts.0.code")["exporter"], 3)
        pdu = rep["pdu"]
        self.assertEqual(pdu["exporter"]["packets_sent"], doc["expect_pdu"]["exporter_packets_sent"])
        self.assertEqual(pdu["exporter"]["pdus_returned"], doc["expect_pdu"]["exporter_pdus_returned"])
        self.assertEqual(pdu["snapshot"], doc["expect_pdu"]["snapshot"])
        self.assertEqual(pdu["source"]["snapshot"], "SNMP_PDU_COUNT")


class ToleranceTests(CompareToolTestCase):
    def _only_counters_total_off(self):
        doc = self.fixture("ok")
        snap = dict(doc["snmp_snapshot"])
        snap["counters"] = dict(snap["counters"], total=13500)   # 8.56٪
        return doc, snap

    def test_tolerance_moves_the_verdict_of_the_same_numbers(self):
        doc, snap = self._only_counters_total_off()
        rc5, rep5, *_ = self.run_fixture("ok", snapshot_override=snap)
        self.assertEqual(rc5, 1)
        self.assertEqual([r["path"] for r in rep5["diff_rows"]], ["counters.total"])
        self.assertEqual(self.row(rep5, "counters.total")["rel_diff_pct"], 8.56)

        rc9, rep9, *_ = self.run_fixture("ok", extra=("--tolerance", "9"),
                                          snapshot_override=snap)
        self.assertEqual(rc9, 0)
        self.assertEqual(self.row(rep9, "counters.total")["verdict"], "ok")
        self.assertEqual(rep9["summary"]["diff"], 0)

    def test_verdict_is_ok_exactly_at_the_tolerance_not_below_only(self):
        """مرزِ *دقیق*: rel == tolerance ⇒ ok (شرطِ `<=`).

        سنجه‌ای که عمداً برای این نوشته شد: اولین دورِ جهش‌ها، `<=` را به `<` تغییر داد و
        **هیچ تستی نگرفت** (سطرهایِ موجود یا ۴.۹۹٪ بودند یا ۵.۰۰۱۹٪)؛ این سطر با
        اعدادی ساخته شد که rel دقیقاً ۵.۰۰ درمی‌آید: exporter 1900 در برابر snapshot 2000
        (|1900−2000|/2000 = ۵٪ دقیق، بدونِ خطایِ گردکردنِ float — ۱۹۰۰ و ۲۰۰۰ هر دو
        نمایندهٔ دقیق دارند).
        """
        doc = self.fixture("ok")
        cap = doc["exporter_capture"].replace("toner_max_1 3200", "toner_max_1 1900")
        self.assertNotEqual(cap, doc["exporter_capture"])
        snap = json.loads(json.dumps(doc["snmp_snapshot"]))
        snap["toners"]["black"]["max"] = 2000
        rc, rep, md, stdout, stderr, _ = self.run_fixture("ok", capture_override=cap,
                                                          snapshot_override=snap)
        row = self.row(rep, "toners.black.max")
        self.assertEqual(row["rel_diff_pct"], 5.0)
        self.assertEqual(row["verdict"], "ok")            # `<=` ⇒ مرز داخلِ ok است
        self.assertEqual(row["note"], "")

    def test_text_rows_ignore_numeric_tolerance(self):
        tol = self.fixture("diff")["expect_tolerate_at"]
        rc, rep, md, stdout, stderr, doc = self.run_fixture(
            "diff", extra=("--tolerance", str(tol["tolerance"])))
        self.assertEqual(rc, tol["expect_exit"])
        self.assertEqual([r["path"] for r in rep["diff_rows"]], tol["expect_diff_paths"])
        self.assertEqual(self.row(rep, "counters.total")["verdict"], "ok")

    def test_boundary_exactly_at_tolerance_is_ok(self):
        """rel == tolerance ⇒ ok (شرط `<=`)؛ کمی بالاتر ⇒ diff."""
        doc = self.fixture("ok")
        snap = json.loads(json.dumps(doc["snmp_snapshot"]))
        # exporter 12345؛ برای rel دقیقاً ۵٪: |x-12345|/max(x,12345)=0.05 ⇒ x=12345/0.95
        # rel = |x − 12345| / max(x, 12345) · با مخرجِ exporter (base بزرگ‌تر)
        snap["counters"]["total"] = 12994      # 649/12994 = 4.99٪ → ok (شرط `<=`)
        rc, rep, *_ = self.run_fixture("ok", snapshot_override=snap)
        self.assertEqual(rc, 0, rep["diff_rows"] if rep else "")
        self.assertAlmostEqual(self.row(rep, "counters.total")["rel_diff_pct"], 4.99, places=2)
        snap["counters"]["total"] = 12995      # 650/12995 = 5.0019٪ → diff
        rc2, rep2, *_ = self.run_fixture("ok", snapshot_override=snap)
        self.assertEqual(rc2, 1)
        self.assertAlmostEqual(self.row(rep2, "counters.total")["rel_diff_pct"], 5.0, places=2)
        self.assertEqual(self.row(rep2, "counters.total")["verdict"], "diff")


class InputGuardTests(CompareToolTestCase):
    def _args(self, exp, snap, extra=()):
        return ["--exporter-capture", str(exp), "--snmp-snapshot", str(snap), *extra]

    def test_missing_files_exit_two(self):
        doc = self.fixture("ok")
        exp, snap = self.write_pair(doc)
        rc, out, err = self.run_tool(self._args(self.tmp / "nope.txt", snap,
                                                 ("--json", str(self.tmp / "report.json"))))
        self.assertEqual(rc, 2)
        self.assertIn("exporter-capture پیدا نشد", err)
        rc2, _, err2 = self.run_tool(self._args(exp, self.tmp / "nope.json"))
        self.assertEqual(rc2, 2)
        self.assertIn("snmp-snapshot پیدا نشد", err2)
        self.assertFalse((self.tmp / "report.json").exists(), "ورودیِ خراب نباید گزارش بنویسد")

    def test_broken_json_exit_two(self):
        doc = self.fixture("ok")
        exp, _ = self.write_pair(doc)
        bad = self.tmp / "bad.json"
        bad.write_text("{ this is not json", encoding="utf-8")
        rc, _, err = self.run_tool(self._args(exp, bad))
        self.assertEqual(rc, 2)
        self.assertIn("JSON معتبر نیست", err)

    def test_snapshot_must_look_like_a_collector_shape(self):
        doc = self.fixture("ok")
        exp, _ = self.write_pair(doc)
        bad = self.tmp / "shape.json"
        bad.write_text(json.dumps({"brand": "hp", "hello": "world"}), encoding="utf-8")
        rc, _, err = self.run_tool(self._args(exp, bad))
        self.assertEqual(rc, 2)
        self.assertIn("ساختارِ snapshot", err)

    def test_unsupported_brand_exit_two_but_flag_can_supply_it(self):
        doc = self.fixture("ok")
        exp, _ = self.write_pair(doc)
        nosnap_brand = self.tmp / "nb.json"
        snap = dict(doc["snmp_snapshot"])
        snap.pop("brand")
        nosnap_brand.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        rc, _, err = self.run_tool(self._args(exp, nosnap_brand))
        self.assertEqual(rc, 2)
        self.assertIn("brand", err)
        # با --brand همان ورودی باید کار کند
        rc2, _, err2 = self.run_tool(self._args(exp, nosnap_brand, ("--brand", "hp")))
        self.assertEqual(rc2, 0, err2)
        # برندی که آینه ندارد ⇒ ۲ (نه fallbackِ بی‌صدا)
        brother = dict(doc["snmp_snapshot"], brand="brother")
        b = self.tmp / "bro.json"
        b.write_text(json.dumps(brother, ensure_ascii=False), encoding="utf-8")
        rc4, _, err4 = self.run_tool(self._args(exp, b))
        self.assertEqual(rc4, 2)
        self.assertIn("brand", err4)
        # --brand برندهایِ پشتیبانی‌شده را argparse می‌ساند (خروجیِ argparse هم ۲ است)
        with self.assertRaises(SystemExit) as cm:
            self.run_tool(self._args(exp, b, ("--brand", "brother")))
        self.assertEqual(cm.exception.code, 2)

    def test_capture_without_metrics_exit_two(self):
        doc = self.fixture("ok")
        _, snap = self.write_pair(doc)
        empty = self.tmp / "empty.txt"
        empty.write_text("# فقط کامنت\n# TYPE x gauge\n", encoding="utf-8")
        rc, _, err = self.run_tool(self._args(empty, snap))
        self.assertEqual(rc, 2)
        self.assertIn("هیچ متریکی", err)

    def test_zero_common_paths_exit_two(self):
        doc = self.fixture("ok")
        exp, _ = self.write_pair(doc)
        thin = self.tmp / "thin.json"
        thin.write_text(json.dumps({"brand": "hp", "counters": {}}), encoding="utf-8")
        rc, _, err = self.run_tool(self._args(exp, thin))
        self.assertEqual(rc, 2)
        self.assertIn("مسیرِ مشترکِ معنادار", err)

    def test_sentinel_in_capture_becomes_missing_not_invented_zero(self):
        """`toner_max_1 = -1` (sentinel) ⇒ level تولید نمی‌شود؛ سطر missing است، و max/status
        عملاً «اختلاف» گزارش می‌شوند — یعنی ابزار واگراییِ دو منبع را پنهان نمی‌کند."""
        doc = self.fixture("ok")
        cap = doc["exporter_capture"].replace("toner_max_1 3200", "toner_max_1 -1")
        self.assertNotEqual(cap, doc["exporter_capture"], "سنجهٔ پارسِ fixture خراب شد")
        rc, rep, md, stdout, stderr, _ = self.run_fixture("ok", capture_override=cap)
        self.assertEqual(rc, 1)
        level = self.row(rep, "toners.black.level")
        self.assertEqual(level["verdict"], "missing")
        self.assertEqual(level["note"], "only_in_snapshot")
        self.assertEqual(self.row(rep, "toners.black.remaining")["verdict"], "ok")
        self.assertEqual(sorted(r["path"] for r in rep["diff_rows"]),
                         ["toners.black.max", "toners.black.status"])
        self.assertEqual(self.row(rep, "toners.black.max")["rel_diff_pct"], 100.03)
        self.assertNotIn("`None`", md)

    def test_comments_and_labelled_text_metrics_are_parsed(self):
        """capture پر از کامنت + متریک‌هایِ متنی (label = مقدار) باید پارس شود و سطر بسازد."""
        doc = self.fixture("ok")
        self.assertIn("# TYPE", doc["exporter_capture"])
        rc, rep, md, stdout, stderr, _ = self.run_fixture("ok")
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(self.row(rep, "device.model")["kind"], "text")
        self.assertEqual(self.row(rep, "device.model")["exporter"], "HP LaserJet Pro M402dn")
        self.assertEqual(self.row(rep, "device.serial")["exporter"], "CNCVC0245A")
        self.assertEqual(self.row(rep, "toners.black.name")["exporter"], "Black Toner")


class VolatileAndDeterminismTests(CompareToolTestCase):
    def test_volatile_fields_are_skipped_by_default_and_listed(self):
        doc = self.fixture("ok")
        snap = json.loads(json.dumps(doc["snmp_snapshot"]))
        snap["last_poll"] = "1999-01-01T00:00:00Z"
        snap["poll_ms"] = 999999
        rc, rep, md, stdout, stderr, _ = self.run_fixture("ok", snapshot_override=snap)
        self.assertEqual(rc, 0, stderr)                      # پس diff نیستند
        self.assertEqual(rep["ignored_paths"], ["last_poll", "poll_ms"])
        self.assertIn("`last_poll`", md)
        self.assertNotIn("1999-01-01", json.dumps(rep["rows"], ensure_ascii=False))

    def test_include_volatile_flag_makes_them_differences(self):
        doc = self.fixture("ok")
        snap = json.loads(json.dumps(doc["snmp_snapshot"]))
        snap["poll_ms"] = 999999
        rc, rep, md, *_ = self.run_fixture("ok", extra=("--include-volatile",),
                                           snapshot_override=snap)
        self.assertEqual(rc, 1)
        self.assertEqual(rep["ignored_paths"], [])
        row = self.row(rep, "poll_ms")
        self.assertEqual(row["verdict"], "diff")
        self.assertGreater(row["rel_diff_pct"], 99.0)

    def test_reports_are_deterministic_and_sorted(self):
        _, rep1, md1, *_ = self.run_fixture("ok")
        _, rep2, md2, *_ = self.run_fixture("ok")
        self.assertEqual(md1, md2)
        self.assertEqual(json.dumps(rep1, sort_keys=True), json.dumps(rep2, sort_keys=True))
        paths = [r["path"] for r in rep1["rows"]]
        self.assertEqual(paths, sorted(paths))
        payload = json.dumps(rep1, ensure_ascii=False, sort_keys=True, indent=1, default=str)
        self.assertEqual(json.loads(payload), rep1)           # round-tripِ بدونِ تغییر

    def _args(self, exp, snap, extra=()):
        return ["--exporter-capture", str(exp), "--snmp-snapshot", str(snap), *extra]

    def test_cli_entry_point_returns_the_documented_exit_code(self):
        doc = self.fixture("ok")
        exp, snap = self.write_pair(doc)
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
        for args, expected in ((self._args(exp, snap, ("--json", str(self.tmp / "r.json"))), 0),
                               (self._args(self.tmp / "nope", snap), 2),
                               (["--help"], 0)):
            proc = subprocess.run([sys.executable, str(TOOL), *args], cwd=str(REPO_ROOT),
                                  capture_output=True, text=True, timeout=120, env=env)
            self.assertEqual(proc.returncode, expected,
                             f"args={args}\n{proc.stdout}\n{proc.stderr}")


class NoRealNumbersTests(CompareToolTestCase):
    def test_every_compare_fixture_declares_itself_synthetic(self):
        for name in ("ok", "diff", "canon"):
            doc = self.fixture(name)
            comment = doc.get("_comment", "")
            self.assertIn("مصنوعی", comment, f"{name}: برچسبِ مصنوعی‌بودن ندارد")
        tool_src = TOOL.read_text(encoding="utf-8")
        self.assertIn("هیچ عددی دربارهٔ PDU در repo ثبت نمی‌شود", tool_src)

    def test_tool_reuses_the_shared_parser_and_mirror_instead_of_new_logic(self):
        src = TOOL.read_text(encoding="utf-8")
        self.assertIn("from core.exporter_client import", src)
        self.assertIn("from core.collectors.exporter_adapter import exporter_to_collector_shape",
                      src)
        self.assertNotIn("def parse_prometheus_text", src, "پارسرِ دومِ موازی ساخته نشود")
        for banned in ("requests", "http.client", "urlopen"):
            self.assertNotIn(banned, src, f"ابزار نباید شبکه بزند: {banned}")

    def test_flatten_and_kind_helpers(self):
        flat = TOOL_MODULE.flatten({"b": {"y": 2, "a": None}, "l": [{"x": 1}, 5], "s": "z"})
        self.assertEqual(flat, {"b.y": 2, "b.a": None, "l.0.x": 1, "l.1": 5, "s": "z"})
        self.assertEqual(TOOL_MODULE.kind_of(True), "bool")
        self.assertEqual(TOOL_MODULE.kind_of(1), "num")
        self.assertEqual(TOOL_MODULE.kind_of(1.5), "num")
        self.assertEqual(TOOL_MODULE.kind_of("x"), "text")
        self.assertEqual(TOOL_MODULE.kind_of(None), "other")
        self.assertEqual(TOOL_MODULE._num(3.0), 3)
        self.assertIsNone(TOOL_MODULE._num(float("nan")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
