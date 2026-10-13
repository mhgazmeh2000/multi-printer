# -*- coding: utf-8 -*-
"""
تست‌های E.4.9b — برچسب ``printer_name`` در ``/metrics`` روی هر دو backend.

مسئله
-----
کوئری ``cartridge_changes_total`` روی ``logs`` گروه‌بندی
``(printer_ip, detection, color)`` دارد و ``printer_name`` را بدون ``GROUP BY``
برمی‌گرداند. SQLite این را می‌پذیرد (مقدارش غیرقطعی است: «هر ردیفی که افتاد»)،
PostgreSQL نه: خطای **42803** «column logs.printer_name must appear in the
GROUP BY clause or be used in an aggregate function» (سنجیده‌شده، E.4.9 روی
PG ۱۶؛ ``/metrics`` آن‌جا ۲۰۰ برمی‌گرداند ولی با کامنت
``# ERROR generating metrics`` و بدون نمونه — یعنی شکست بی‌صدا).

تصمیم (مالک، E.4.9b)
--------------------
``MAX(printer_name) AS printer_name`` با ``GROUP BY`` بدون تغییر، تا **تعداد سری
امروز ثابت بماند**؛ افزودن ``printer_name`` به ``GROUP BY`` اگر پرینتری تغییر نام
داده باشد سری اضافه می‌کند. مزیت جانبی: مقدار برچسب قطعی می‌شود. بدهی باقی‌مانده
(«نام تاریخی» به‌جای «نام زنده») در ``docs/tech-debt.md`` به‌عنوان TD-010 ثبت شده.

چه چیزی قفل می‌شود
------------------
۱) شاهد معکوس: همان کوئری **بدون** ``MAX`` روی PG واقعی ``DatabaseError`` می‌دهد؛
۲) شمارش سری‌ها روی هر دو backend یکسان است (برابر تعداد متمایزهای
   ``(printer_ip, detection, color)``) — یعنی تصمیم، رفتار مصرف‌کننده را عوض نکرد؛
۳) مقدار برچسب با دادهٔ نام‌دار متفاوت قطعی است (``MAX``، نه «هر ردیفی که افتاد»).
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from core.db import ENV_PG_DSN, DatabaseError, connect_postgres

ROOT = Path(__file__).resolve().parent.parent
IP = "203.0.113.77"
#: همان کوئری امروز **منهای ``MAX(...)``** — برای شاهد معکوس. قطعهٔ ``detection``
#: عمداً از هِلپر cross-backend می‌آید (E.4.9)، تا تنها تفاوتِ کوئری همان
#: ``printer_name`` بدون تابع تجمعی باشد و خطای مشاهده‌شده دقیقاً 42803 شود.
RAW_SQL_TEMPLATE = """
    SELECT printer_ip, printer_name,
           {detection} as detection,
           color, COUNT(*) as cnt
    FROM logs
    WHERE type = 'CARTRIDGE_CHANGED'
    GROUP BY printer_ip, detection, color
    ORDER BY printer_ip
"""


def _pg_target():
    """``(dsn, None)`` اگر PG در دسترس باشد، وگرنه ``(None, دلیلِ skip)``."""
    dsn = (os.getenv(ENV_PG_DSN, "") or "").strip()
    if not dsn:
        return None, (f"{ENV_PG_DSN} ست نشده است ⇒ تست‌های PGِ زنده‌ی این فایل اجرا "
                      f"نمی‌شوند (اجرا با: PG_DSN='host=… dbname=… user=… password=…' pytest)")
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        return None, (f"PG در دسترس نیست ({type(exc).__name__}: "
                      f"{str(exc).strip().splitlines()[0][:120]}) ⇒ تست‌های PG رد می‌شوند")
    return dsn, None


_PG_DSN, _PG_SKIP = _pg_target()


def _seed_two_names(ip=IP):
    """
    دو رخداد ``CARTRIDGE_CHANGED`` با همان ``(ip, detection, color)`` ولی دو نام
    متفاوت می‌سازد (شبیه‌سازی تغییر نام پرینتر بین دو رخداد).

    Returns:
        ``(نام کوچک‌تر, نام بزرگ‌تر)`` طبق ترتیب رشته‌ای.
    """
    from core import store
    from core.database import add_event

    names = ["Zeta", "Alpha"]                    # MAX ⇒ "Zeta" (ترتیب ASCII)
    for i, name in enumerate(names):
        with mock.patch.object(store, "PRINTERS", [{"ip": ip, "name": name}]):
            add_event(ip, "CARTRIDGE_CHANGED",
                      {"event_id": f"ev-{i}", "detection": "snmp", "color": "black"})
    return max(names)


def _parse_series(body):
    """سری‌های ``cartridge_changes_total`` را از بدنهٔ Prometheus بیرون می‌کشد."""
    series = []
    for line in body.splitlines():
        if line.startswith("cartridge_changes_total{") and "printer_ip=" in line:
            labels, _, value = line.rpartition(" ")
            parsed = dict(
                (k, v) for k, _, v in
                (item.partition("=") for item in
                 labels[labels.index("{") + 1:labels.rindex("}")].replace('"', "").split(","))
            )
            series.append((parsed, float(value)))
    return series


class _MetricsAppMixin:
    """اپلیکیشن Flask واقعی + کلاینت تست (بدون توکن: ``METRICS_TOKEN`` خالی)."""

    def _make_client(self):
        from core.database import init_db
        from web import create_app
        init_db()
        app = create_app()
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()
        patcher = mock.patch.dict(os.environ, {"METRICS_TOKEN": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _get_metrics(self):
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)


# ─────────────────────────────────────────────────────────────────────
# A) SQLite — تعداد سری و مقدار برچسب
# ─────────────────────────────────────────────────────────────────────
# marker `sqlite_only`: این تست SQL/اسکیمای مختص SQLite می‌نویسد ⇒ زیر
# `DB_BACKEND=postgresql` با auto-skip رد می‌شود (tests/conftest.py، docs/phase-e-plan.md §۵.۳).
@pytest.mark.sqlite_only
class SqliteMetricsSeriesTests(_MetricsAppMixin, unittest.TestCase):
    """روی SQLite: شمارش سری‌ها همان تعریف ``GROUP BY`` است و مقدار قطعی شده."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)                  # DB_PATH نسبی ⇒ DB موقت
        self.addCleanup(lambda: os.chdir(self.old_cwd))
        self._make_client()
        self.expected_name = _seed_two_names()

    def _distinct_groups(self, ip=IP):
        from core.database import db_connection
        with db_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT printer_ip, color, "
                "json_extract(details, '$.detection') FROM logs WHERE printer_ip = ?",
                (ip,)).fetchall()
        return len(rows)

    def test_series_count_equals_distinct_groups(self):
        """تعداد سری = تعداد متمایزهای ``(ip, detection, color)`` (تصمیم: ثابت بماند)."""
        series = [s for s, _ in _parse_series(self._get_metrics())
                  if s.get("printer_ip") == IP]
        self.assertEqual(len(series), 1, series)
        self.assertEqual(len(series), self._distinct_groups())

    def test_label_uses_max_printer_name(self):
        """برچسب = ``MAX(نام‌ها)`` ⇒ قطعی، نه «هر ردیفی که افتاد»."""
        series = [s for s, _ in _parse_series(self._get_metrics())
                  if s.get("printer_ip") == IP]
        self.assertEqual(series[0]["printer_name"], self.expected_name)
        self.assertEqual(series[0]["detection"], "snmp")
        self.assertEqual(series[0]["color"], "black")

    def test_counter_value_counts_all_events(self):
        """هر دو رخداد در ``count`` همان یک سری شمرده می‌شوند."""
        values = [v for s, v in _parse_series(self._get_metrics())
                  if s.get("printer_ip") == IP]
        self.assertEqual(values, [2.0])


# ─────────────────────────────────────────────────────────────────────
# B) PostgreSQL واقعی
# ─────────────────────────────────────────────────────────────────────
@unittest.skipUnless(_PG_DSN, _PG_SKIP or "")
class PostgresMetricsTests(_MetricsAppMixin, unittest.TestCase):
    """روی PG ۱۶ واقعی: بدون ``GroupingError``، با همان تعداد سری و همان برچسب."""

    def setUp(self):
        if not _PG_DSN:
            self.skipTest(_PG_SKIP or "PG در دسترس نیست")
        self.env = mock.patch.dict(os.environ, {
            "DB_BACKEND": "postgresql", ENV_PG_DSN: _PG_DSN})
        self.env.start()
        self.addCleanup(self.env.stop)
        self._cleanup_logs()
        self._make_client()
        self.expected_name = _seed_two_names()

    def _cleanup_logs(self, ip=IP):
        conn = connect_postgres(_PG_DSN)
        try:
            conn.rollback()
            conn.execute("DELETE FROM logs WHERE printer_ip = ?", (ip,))
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self._cleanup_logs()

    def test_metrics_has_no_generation_error(self):
        """بدنه نباید کامنت ``# ERROR generating metrics`` داشته باشد (شکست بی‌صدای قبلی)."""
        body = self._get_metrics()
        self.assertNotIn("# ERROR generating metrics", body)

    def test_series_matches_sqlite_semantics(self):
        """یک سری با مقدار ۲ و برچسب ``MAX(نام)`` — همان چیزی که SQLite می‌دهد."""
        series = [(s, v) for s, v in _parse_series(self._get_metrics())
                  if s.get("printer_ip") == IP]
        self.assertEqual(len(series), 1, series)
        labels, value = series[0]
        self.assertEqual(labels["printer_name"], self.expected_name)
        self.assertEqual(labels["detection"], "snmp")
        self.assertEqual(value, 2.0)

    def test_raw_sql_still_fails_on_pg(self):
        """شاهد معکوس: همان کوئری **بدون** ``MAX`` روی PG خطای 42803 می‌دهد."""
        conn = connect_postgres(_PG_DSN)
        self.addCleanup(conn.close)
        raw_sql = RAW_SQL_TEMPLATE.format(
            detection=conn.json_extract("details", "detection"))
        with self.assertRaises(DatabaseError) as ctx:
            conn.execute(raw_sql)
        self.assertEqual(ctx.exception.sqlstate, "42803", str(ctx.exception))
        conn.rollback()


# ─────────────────────────────────────────────────────────────────────
# C) قفل روی متن کوئری (بدون DB)
# ─────────────────────────────────────────────────────────────────────
class QueryShapeTests(unittest.TestCase):
    """``printer_name`` باید در قالب ``MAX(...)`` بیاید و ``GROUP BY`` دست‌نخورده بماند."""

    def setUp(self):
        self.source = (ROOT / "web" / "routes" / "metrics.py").read_text(encoding="utf-8")

    def test_printer_name_is_aggregated(self):
        self.assertIn("MAX(printer_name) AS printer_name", self.source)

    def test_group_by_columns_unchanged(self):
        """``printer_name`` به ``GROUP BY`` اضافه **نشده** (تصمیم: حفظ تعداد سری)."""
        self.assertIn("GROUP BY printer_ip, detection, color", self.source)
        self.assertNotIn("GROUP BY printer_ip, printer_name", self.source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
