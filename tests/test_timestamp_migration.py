# -*- coding: utf-8 -*-
"""
تست‌های ابزار مهاجرت timestamp (فاز E.3 — کار E.3.2).

سناریوی خواسته‌شده در گام E.3.2 روی یک DB موقت ساخته می‌شود:
  • ۳ ردیف naive local
  • ۲ ردیف ``Z`` پیش از cutover (برچسب جعلی)
  • ۲ ردیف ``Z`` پس از cutover (UTC واقعی)
  • ۱ ردیف ``+03:30``
  • ۱ ردیف خراب

و بررسی می‌شود که همه به استاندارد ``…Z`` تبدیل شوند (خراب دست‌نخورده بماند)،
idempotent باشد، rollback/بکاپ درست کار کند و ``meta.value`` دست نخورد.

تست‌ها مستقل از TZ سیستم‌اند: همه‌ی تفسیرها با ``Asia/Tehran`` صریح انجام می‌شود.
"""
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_tool():
    """ابزار را به‌صورت ماژول مستقل بار می‌کند (پوشهٔ tools پکیج نیست)."""
    spec = importlib.util.spec_from_file_location(
        "migrate_timestamps_to_utc",
        PROJECT_ROOT / "tools" / "migrate_timestamps_to_utc.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load_tool()

CUTOVER = "2026-10-03T06:35:11+00:00"          # همان نشانهٔ ثبت‌شده در logs.db پروژه
CUTOVER_DT = datetime(2026, 10, 3, 6, 35, 11, tzinfo=timezone.utc)


class _DbFixture(unittest.TestCase):
    """DB موقت با جدول‌های `logs`، `security_events`، `meta` و ردیف‌های سناریو."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "logs.db")
        self.manifest = os.path.join(self.tmp.name, "manifest.json")
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE logs (id INTEGER PRIMARY KEY, printer_ip TEXT, type TEXT,
                               pages INT, details TEXT, timestamp TEXT);
            CREATE TABLE security_events (id INTEGER PRIMARY KEY, timestamp TEXT,
                                          event_type TEXT, created_at TEXT);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL,
                               updated_at TEXT NOT NULL);
        """)
        # ۳ ردیف naive local (ساعت تهران)
        conn.executemany(
            "INSERT INTO logs (id, printer_ip, type, pages, timestamp) VALUES (?,?,?,?,?)",
            [(1, "10.0.0.1", "PRINT", 10, "2026-10-01T08:51:39.937749"),
             (2, "10.0.0.2", "PRINT", 20, "2026-10-01T09:00:00.000000"),
             (3, "10.0.0.3", "REFILL", 0, "2026-10-02T23:30:00")])
        # ۲ ردیف Z پیش از cutover (برچسب جعلی؛ فرمت قدیمی موتور کارتریج)
        conn.executemany(
            "INSERT INTO logs (id, printer_ip, type, pages, timestamp) VALUES (?,?,?,?,?)",
            [(4, "10.0.0.4", "CARTRIDGE_CHANGED", 0, "2026-10-02T22:00:00Z"),
             (5, "10.0.0.5", "CARTRIDGE_CHANGED", 0, "2026-10-03T06:00:00Z")])
        # ۲ ردیف Z پس از cutover (UTC واقعی؛ دومی درست روز بعد)
        conn.executemany(
            "INSERT INTO logs (id, printer_ip, type, pages, timestamp) VALUES (?,?,?,?,?)",
            [(6, "10.0.0.6", "STATUS", 0, "2026-10-03T07:00:00Z"),
             (7, "10.0.0.7", "STATUS", 0, "2026-10-04T10:00:00Z")])
        # ۱ ردیف offset دار و ۱ ردیف خراب
        conn.execute("INSERT INTO logs (id, printer_ip, type, pages, timestamp) "
                     "VALUES (8, '10.0.0.8', 'PRINT', 5, '2026-10-03T11:00:00+03:30')")
        conn.execute("INSERT INTO logs (id, printer_ip, type, pages, timestamp) "
                     "VALUES (9, '10.0.0.9', 'PRINT', 5, 'not-a-timestamp')")
        # security_events: دو ستون زمانی + یک مقدار NULL
        conn.executemany(
            "INSERT INTO security_events (id, timestamp, event_type, created_at) "
            "VALUES (?,?,?,?)",
            [(1, "2026-10-01T08:50:13.110514", "login", "2026-10-01T08:50:13.110514"),
             (2, "2026-10-03T09:55:51+00:00", "login_failed", "2026-10-03T09:55:51+00:00"),
             (3, None, "logout", "2026-10-01T10:00:00")])
        # meta: نشانهٔ cutover (نباید تبدیل شود) + updated_at (باید تبدیل شود)
        conn.execute("INSERT INTO meta (key, value, updated_at) VALUES (?,?,?)",
                     ("timezone_cutover_at", CUTOVER, "2026-10-03T09:55:51+00:00"))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, table="logs", col="timestamp"):
        conn = sqlite3.connect(self.db)
        try:
            return {r[0]: r[1] for r in conn.execute(f'SELECT id, "{col}" FROM {table}')}
        finally:
            conn.close()

    def run_tool(self, *extra):
        # E.6.2: ابزار بکاپ PG را از محیط می‌فهمد؛ این سوئیت روی کپیِ موقت SQLite
        # کار می‌کند، پس محیط را قفل می‌کنیم تا اجرای سوئیت PG (DB_BACKEND=postgresql)
        # در کنار هر تست، یک pg_dump واقعی از دیتابیس زنده نگیرد.
        with mock.patch.dict(os.environ, {"DB_BACKEND": "sqlite", "PG_DSN": ""}):
            return tool.main(["--db", self.db, "--manifest", self.manifest, *extra])


class ConversionUnitTests(unittest.TestCase):
    """قاعدهٔ تبدیل در سطح تابع (بدون DB) — سریع و صریح."""

    def test_naive_becomes_tehran_to_utc(self):
        new, cat, changed = tool.convert_value("2026-10-01T08:51:39.937749", CUTOVER_DT)
        self.assertEqual(new, "2026-10-01T05:21:39.937749Z")
        self.assertEqual(cat, "naive_local")
        self.assertTrue(changed)

    def test_z_before_cutover_is_fake_label(self):
        new, cat, changed = tool.convert_value("2026-10-03T06:00:00Z", CUTOVER_DT)
        self.assertEqual(new, "2026-10-03T02:30:00.000000Z")     # 06:00 تهران = 02:30 UTC
        self.assertEqual(cat, "z_before_cutover")
        self.assertTrue(changed)

    def test_known_blind_spot_of_the_cutover_rule(self):
        """
        نقطهٔ کور مستندشده: ردیف جعلی‌ای که در ۳:۳۰ پیش از cutover نوشته شده،
        برچسبش از نشانه عبور می‌کند و «UTC واقعی» خوانده می‌شود. اینجا فقط
        **ثبت** می‌شود تا رفتار کد و سند یکی بمانند (تصمیم سند، نه باگ این ابزار).
        """
        new, cat, _ = tool.convert_value("2026-10-03T07:09:06Z", CUTOVER_DT)
        self.assertEqual(cat, "z_after_cutover")
        self.assertEqual(new, "2026-10-03T07:09:06.000000Z")

    def test_z_after_cutover_is_true_utc(self):
        new, cat, changed = tool.convert_value("2026-10-03T07:00:00Z", CUTOVER_DT)
        self.assertEqual(new, "2026-10-03T07:00:00.000000Z")
        self.assertEqual(cat, "z_after_cutover")
        self.assertTrue(changed)

    def test_offset_is_interpreted_as_is(self):
        new, cat, changed = tool.convert_value("2026-10-03T11:00:00+03:30", CUTOVER_DT)
        self.assertEqual(new, "2026-10-03T07:30:00.000000Z")
        self.assertEqual(cat, "offset_aware")

    def test_unparsable_and_empty_are_untouched(self):
        self.assertEqual(tool.convert_value("not-a-timestamp", CUTOVER_DT),
                         ("not-a-timestamp", "unparsable", False))
        self.assertEqual(tool.convert_value(None, CUTOVER_DT), (None, "empty", False))
        self.assertEqual(tool.convert_value("", CUTOVER_DT), ("", "empty", False))

    def test_already_standard_is_idempotent(self):
        std = "2026-10-01T05:21:39.937749Z"
        new, cat, changed = tool.convert_value(std, CUTOVER_DT)
        self.assertEqual(new, std)
        self.assertEqual(cat, "already_standard")
        self.assertFalse(changed)

    def test_standard_z_before_cutover_is_not_reinterpreted(self):
        """
        گارد اصلی idempotency: ردیف مهاجرت‌شده‌ای که زمانش پیش از cutover است
        نباید دوباره «جعلی» فرض شود (وگرنه هر اجرا ۳:۳۰ جابه‌جا می‌کرد).
        """
        migrated = "2026-10-03T03:39:06.000000Z"     # نتیجهٔ مهاجرت ردیف جعلی
        self.assertLess(datetime.fromisoformat(migrated.replace("Z", "+00:00")), CUTOVER_DT)
        _, cat, changed = tool.convert_value(migrated, CUTOVER_DT)
        self.assertEqual(cat, "already_standard")
        self.assertFalse(changed)

    def test_dst_era_values_use_real_zone_not_fixed_offset(self):
        """تابستان ۲۰۲۲ تهران +04:30 بود؛ offset ثابت +03:30 نتیجه را خراب می‌کرد."""
        new, _, _ = tool.convert_value("2022-07-01T12:00:00", CUTOVER_DT)
        self.assertEqual(new, "2022-07-01T07:30:00.000000Z")     # 12:00 − 4:30


class MigrationRunTests(_DbFixture):

    def test_dry_run_changes_nothing_and_writes_manifest(self):
        before = self.rows()
        self.assertEqual(self.run_tool("--dry-run", "--quiet"), 0)
        self.assertEqual(self.rows(), before)
        with open(self.manifest, encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["mode"], "dry-run")
        self.assertFalse(os.path.exists(self.db + ".e3bak") or
                         [f for f in os.listdir(self.tmp.name) if f.endswith(".e3bak")])
        self.assertNotIn("sha256_after", manifest)

    def test_full_run_converts_all_categories(self):
        self.assertEqual(self.run_tool("--quiet"), 0)
        rows = self.rows()
        self.assertEqual(rows[1], "2026-10-01T05:21:39.937749Z")   # naive
        self.assertEqual(rows[3], "2026-10-02T20:00:00.000000Z")   # naive نیمه‌شب‌شکن
        self.assertEqual(rows[4], "2026-10-02T18:30:00.000000Z")   # Z جعلی
        self.assertEqual(rows[5], "2026-10-03T02:30:00.000000Z")   # Z جعلی
        self.assertEqual(rows[6], "2026-10-03T07:00:00.000000Z")   # Z واقعی
        self.assertEqual(rows[8], "2026-10-03T07:30:00.000000Z")   # offset
        self.assertEqual(rows[9], "not-a-timestamp")               # خراب: دست‌نخورده
        for value in rows.values():
            if value != "not-a-timestamp":
                self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

    def test_multi_column_table_and_null_values(self):
        self.assertEqual(self.run_tool("--quiet"), 0)
        seconds = self.rows("security_events", "created_at")
        self.assertEqual(seconds[1], "2026-10-01T05:20:13.110514Z")
        self.assertEqual(seconds[3], "2026-10-01T06:30:00.000000Z")
        stamps = self.rows("security_events", "timestamp")
        self.assertEqual(stamps[2], "2026-10-03T09:55:51.000000Z")

    def test_meta_value_is_protected_but_updated_at_migrates(self):
        self.assertEqual(self.run_tool("--quiet"), 0)
        conn = sqlite3.connect(self.db)
        try:
            value, updated = conn.execute(
                "SELECT value, updated_at FROM meta WHERE key = 'timezone_cutover_at'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(value, CUTOVER)                          # مرجع: دست‌نخورده
        self.assertEqual(updated, "2026-10-03T09:55:51.000000Z")   # داده: استانداردشده

    def test_backup_is_created_and_ignored_name(self):
        self.assertEqual(self.run_tool("--quiet"), 0)
        backups = [f for f in os.listdir(self.tmp.name) if f.endswith(".e3bak")]
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].startswith("logs.db-"))

    def test_second_run_is_noop(self):
        self.assertEqual(self.run_tool("--quiet"), 0)
        after_first = self.rows()
        with open(self.manifest, encoding="utf-8") as fh:
            first_manifest = json.load(fh)
        self.assertEqual(self.run_tool("--quiet"), 0)
        self.assertEqual(self.rows(), after_first)
        with open(self.manifest, encoding="utf-8") as fh:
            second_manifest = json.load(fh)
        self.assertEqual(second_manifest["mode"], "no-op")
        self.assertEqual(second_manifest["sha256_before"], first_manifest["sha256_after"])

    def test_missing_cutover_marker_stops_before_writing(self):
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM meta")
        conn.commit()
        conn.close()
        before = self.rows()
        self.assertEqual(self.run_tool("--quiet"), 2)
        self.assertEqual(self.rows(), before)

    def test_unparsable_value_is_warned_but_run_succeeds(self):
        """زبالهٔ دست‌نیافتنی: هشدار + ثبت در manifest، ولی کد خروج ۰ (تصمیم انسانی لازم نیست)."""
        self.assertEqual(self.run_tool("--quiet"), 0)
        self.assertEqual(self.rows()[1], "2026-10-01T05:21:39.937749Z")
        with open(self.manifest, encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["totals"]["skipped"], 1)
        self.assertEqual(manifest["totals"]["needs_decision"], 0)
        self.assertEqual(manifest["skipped_samples"][0]["value"], "not-a-timestamp")

    def test_manifest_totals_reconcile(self):
        self.run_tool("--quiet")
        with open(self.manifest, encoding="utf-8") as fh:
            manifest = json.load(fh)
        logs_entry = manifest["tables"]["logs"]
        counted = sum(sum(c.values()) for c in logs_entry["columns"].values())
        self.assertEqual(counted, logs_entry["rows"])
        self.assertEqual(manifest["totals"]["rows"],
                         sum(t.get("rows", 0) for t in manifest["tables"].values()))

    def test_write_error_rolls_back_everything(self):
        """
        اگر نوشتن وسط راه بشکند، هیچ سلی تغییر نمی‌کند (کل مهاجرت یک تراکنش است).

        ``sqlite3.Connection.execute`` فقط-خواندنی است، پس با یک پروکسی نازک
        جایگزین می‌شود که بعد از سه UPDATE خطا می‌دهد.
        """
        before = self.rows()

        class _Boom(Exception):
            pass

        real_connect = sqlite3.connect

        class _FailingConnection:
            def __init__(self, raw):
                self._raw = raw
                self._updates = 0

            def execute(self, sql, *params):
                if sql.strip().upper().startswith("UPDATE"):
                    self._updates += 1
                    if self._updates > 3:
                        raise _Boom("شکست شبیه‌سازی‌شده")
                return self._raw.execute(sql, *params)

            def __getattr__(self, name):
                return getattr(self._raw, name)

        def fake_connect(path, *a, **kw):
            return _FailingConnection(real_connect(path, *a, **kw))

        with mock.patch.object(tool.sqlite3, "connect", side_effect=fake_connect):
            with self.assertRaises(_Boom):
                tool.migrate_rows(self.db, CUTOVER_DT, ["logs"])
        self.assertEqual(self.rows(), before)

    def test_needs_decision_when_z_row_has_no_marker(self):
        """ردیف `Z` بدون نشانهٔ قابل‌اعتماد ⇒ کد خروج ۲ (تصمیم انسانی)، بدون نوشتن."""
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM meta")
        conn.commit()
        conn.close()
        before = self.rows()
        self.assertEqual(self.run_tool("--cutover", "", "--quiet"), 2)
        self.assertEqual(self.rows(), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
