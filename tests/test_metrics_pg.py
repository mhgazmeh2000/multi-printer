# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_metrics_pg.py
# @brief:        Tests for the printer_name label on /metrics on both backends (E.4.9b).
# @details:      Responsibilities: Check label output and query shapes.
#                Main API: _MetricsAppMixin, SqliteMetricsSeriesTests, PostgresMetricsTests,
#                QueryShapeTests
#                Side effects: database access
# @dependencies: internal: core, core.database, core.db, web | external: psycopg, pytest
# @notes:        Can be run directly as a script.
# =============================================================================

"""
E.4.9b tests — ``printer_name'' tag in ``/metrics'' on both backends.

the problem
-----
Query ``cartridge_changes_total'' on ``logs'' grouping
has ``(printer_ip, detection, color)'' and ``printer_name'' without ``GROUP BY''
returns SQLite accepts this (its value is indeterminate: "any row dropped"),
PostgreSQL not: error **42803** "column logs.printer_name must appear in the
GROUP BY clause or be used in an aggregate function" (measured, E.4.9 on
PG 16; ``/metrics'' returns 200 there, but with comments
``# ERROR generating metrics'' and no examples — that is, silent failure).

Decision (Owner, E.4.9b)
--------------------
``MAX(printer_name) AS printer_name'' with ``GROUP BY'' unchanged, up to **number of series
Stay constant today**; Add ``printer_name'' to ``GROUP BY'' if the printer is renamed
It adds series. Side benefit: The amount of the label becomes definitive. outstanding debt
('historical name' instead of 'living name') is registered in ``docs/tech-debt.md'' as TD-010.

What is locked?
------------------
1) Reverse witness: the same query **without** ``MAX'' on real PG gives ``DatabaseError'';
2) The number of series on both backends is the same (equal to the number of distinct
   ``(printer_ip, detection, color)'') — that is, the decision did not change the consumer's behavior;
3) The tag value is definite with different named data (``MAX'', not ``every row dropped'').
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
#: Same query as today **minus ``MAX(...)''** — for reverse witness. ``detection'' fragment
#: intentionally comes from the cross-backend helper (E.4.9), until the only difference is the same query
#: ``printer_name'' without cumulative function and the observed error becomes exactly 42803.
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
    """``(dsn, None)'' if PG is available, otherwise ``(None, skip reason)''."""
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
    Two ``CARTRIDGE_CHANGED'' events with the same ``(ip, detection, color)'' but two names
    Makes it different (simulating changing the printer name between two events).

    Returns:
        ``(smaller name, larger name)'' according to string order.
    """
    from core import store
    from core.database import add_event

    names = ["Zeta", "Alpha"]                    # MAX ⇒ "Zeta" (ASCII format)
    for i, name in enumerate(names):
        with mock.patch.object(store, "PRINTERS", [{"ip": ip, "name": name}]):
            add_event(ip, "CARTRIDGE_CHANGED",
                      {"event_id": f"ev-{i}", "detection": "snmp", "color": "black"})
    return max(names)


def _parse_series(body):
    """Extracts the ``cartridge_changes_total'' series from the Prometheus body."""
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
    """Real Flask app + test client (no token: empty ``METRICS_TOKEN'')."""

    def _make_client(self):
        """Helper: make client."""
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
        """Helper: get metrics."""
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)


# ─────────────────────────────────────────────────────────────────────
# A) SQLite — serial number and tag value
# ─────────────────────────────────────────────────────────────────────
# marker `sqlite_only`: This test writes SQLite/schema specific to SQLite ⇒ below
# `DB_BACKEND=postgresql` is skipped with auto-skip (tests/conftest.py, docs/phase-e-plan.md §5.3).
@pytest.mark.sqlite_only
class SqliteMetricsSeriesTests(_MetricsAppMixin, unittest.TestCase):
    """On SQLite: the series count is the same as the ``GROUP BY'' definition and the value is fixed."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)                  # Relative DB_PATH ⇒ Temporary DB
        self.addCleanup(lambda: os.chdir(self.old_cwd))
        self._make_client()
        self.expected_name = _seed_two_names()

    def _distinct_groups(self, ip=IP):
        """Helper: distinct groups."""
        from core.database import db_connection
        with db_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT printer_ip, color, "
                "json_extract(details, '$.detection') FROM logs WHERE printer_ip = ?",
                (ip,)).fetchall()
        return len(rows)

    def test_series_count_equals_distinct_groups(self):
        """Number of series = number of distinct ``(ip, detection, color)'' (decision: keep constant)."""
        series = [s for s, _ in _parse_series(self._get_metrics())
                  if s.get("printer_ip") == IP]
        self.assertEqual(len(series), 1, series)
        self.assertEqual(len(series), self._distinct_groups())

    def test_label_uses_max_printer_name(self):
        """label = ``MAX(names)'' ⇒ deterministic, not "every row that falls"."""
        series = [s for s, _ in _parse_series(self._get_metrics())
                  if s.get("printer_ip") == IP]
        self.assertEqual(series[0]["printer_name"], self.expected_name)
        self.assertEqual(series[0]["detection"], "snmp")
        self.assertEqual(series[0]["color"], "black")

    def test_counter_value_counts_all_events(self):
        """Both occurrences are counted in ``count'' of the same series."""
        values = [v for s, v in _parse_series(self._get_metrics())
                  if s.get("printer_ip") == IP]
        self.assertEqual(values, [2.0])


# ─────────────────────────────────────────────────────────────────────
# B) Real PostgreSQL
# ─────────────────────────────────────────────────────────────────────
@unittest.skipUnless(_PG_DSN, _PG_SKIP or "")
class PostgresMetricsTests(_MetricsAppMixin, unittest.TestCase):
    """On real PG 16: no ``GroupingError'', with the same number of series and the same label."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
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
        """Helper: cleanup logs."""
        conn = connect_postgres(_PG_DSN)
        try:
            conn.rollback()
            conn.execute("DELETE FROM logs WHERE printer_ip = ?", (ip,))
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        """Release the resources that each test in this class created."""
        self._cleanup_logs()

    def test_metrics_has_no_generation_error(self):
        """The body should not have the comment ``# ERROR generating metrics'' (previous silent failure)."""
        body = self._get_metrics()
        self.assertNotIn("# ERROR generating metrics", body)

    def test_series_matches_sqlite_semantics(self):
        """A string with a value of 2 and a label of ``MAX(name)'' — the same as SQLite gives."""
        series = [(s, v) for s, v in _parse_series(self._get_metrics())
                  if s.get("printer_ip") == IP]
        self.assertEqual(len(series), 1, series)
        labels, value = series[0]
        self.assertEqual(labels["printer_name"], self.expected_name)
        self.assertEqual(labels["detection"], "snmp")
        self.assertEqual(value, 2.0)

    def test_raw_sql_still_fails_on_pg(self):
        """Reverse witness: the same query **without** ``MAX'' on PG gives error 42803."""
        conn = connect_postgres(_PG_DSN)
        self.addCleanup(conn.close)
        raw_sql = RAW_SQL_TEMPLATE.format(
            detection=conn.json_extract("details", "detection"))
        with self.assertRaises(DatabaseError) as ctx:
            conn.execute(raw_sql)
        self.assertEqual(ctx.exception.sqlstate, "42803", str(ctx.exception))
        conn.rollback()


# ─────────────────────────────────────────────────────────────────────
# C) lock on the query text (without DB)
# ─────────────────────────────────────────────────────────────────────
class QueryShapeTests(unittest.TestCase):
    """``printer_name'' must come in the form ``MAX(...)'' and leave ``GROUP BY'' intact."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.source = (ROOT / "web" / "routes" / "metrics.py").read_text(encoding="utf-8")

    def test_printer_name_is_aggregated(self):
        """Verifies printer name is aggregated."""
        self.assertIn("MAX(printer_name) AS printer_name", self.source)

    def test_group_by_columns_unchanged(self):
        """``printer_name'' **not** added to ``GROUP BY'' (decision: preserve serial number)."""
        self.assertIn("GROUP BY printer_ip, detection, color", self.source)
        self.assertNotIn("GROUP BY printer_ip, printer_name", self.source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
