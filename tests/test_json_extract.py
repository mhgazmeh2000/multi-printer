# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_json_extract.py
# @brief:        Tests for backend-independent JSON extraction (E.4.9).
# @details:      Responsibilities: Check the JSON fragment helpers on both backends.
#                Main API: _StubRaw, FragmentTests, SqliteDedupeBehaviourTests, SourceGuardTests,
#                PostgresLiveJsonExtractTests
#                Side effects: database access
# @dependencies: internal: core.database, core.db | external: psycopg
# @notes:        Can be run directly as a script. PostgreSQL-live tests skip automatically when no
#                server is reachable.
# =============================================================================

"""
E.4.9 tests — backend-independent ``json_extract''.

the problem
-----
``json_extract(col, '$.key')'' is a SQLite function (JSON1 extension) on PostgreSQL
Does not exist: ``function json_extract(text, unknown) does not exist''
(measured in E.4.6 — the ``/metrics'' path on PG breaks here). Equivalent to PostgreSQL
is the ``->>'' operator, with one important difference: ``->>'' always returns **text**, while
SQLite's ``json_extract'' returns a JSON value of its own type (number ⇒ INTEGER).

Solution (E.4.9): ``ConnectionAdapter.json_extract(column, key, *, as_number)'' helper
Builds the SQL snippet — on SQLite byte-by-byte the same as ``json_extract'' today, and on PG
``->>'' (and if needed ``::numeric'' for numerical comparison).

Four things that this file locks
--------------------------------
1) The form of the SQL fragment on both backends (and column name quoting);
2) Key Validation — This helper generates SQL text, so an invalid key should throw a ``ValueError''
   give (security border);
3) Actual behavior of dedupe paths in ``core.database.add_event'' on SQLite
   (CARTRIDGE_CHANGED / PRINT / COUNTER_RESET) — that is, the migration did not change the behavior;
4) No ``json_extract('' text left in ``core/'' and ``web/'' (except the helper itself) — regression lock.
"""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.db import (
    BACKEND_POSTGRESQL,
    ENV_PG_DSN,
    ConnectionAdapter,
    connect_postgres,
    connect_sqlite,
)

ROOT = Path(__file__).resolve().parent.parent
IP = "203.0.113.99"


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


class _StubRaw:
    """Dummy connection: only to create adapter with specific backend (without running SQL)."""

    def execute(self, sql, params=()):  # pragma: no cover - does not run
        """Helper: execute."""
        raise AssertionError("این استاب نباید SQL اجرا کند")


# ─────────────────────────────────────────────────────────────────────
# A) The shape of the SQL fragment on both backends
# ─────────────────────────────────────────────────────────────────────
class FragmentTests(unittest.TestCase):
    """The SQL fragment must provide valid and expected syntax on any backend."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sqlite = connect_sqlite(os.path.join(self.tmp.name, "frag.db"))
        self.addCleanup(self.sqlite.close)
        self.pg = ConnectionAdapter(_StubRaw(), backend=BACKEND_POSTGRESQL)

    def test_sqlite_fragment_is_the_historic_expression(self):
        """SQLite remains the same as today's ``json_extract'' (only the quoted column)."""
        self.assertEqual(self.sqlite.json_extract("details", "event_id"),
                         "json_extract(\"details\", '$.event_id')")
        self.assertEqual(self.sqlite.json_extract("details", "prev_total", as_number=True),
                         "json_extract(\"details\", '$.prev_total')",
                         "روی SQLite نوع از قبل درست است ⇒ as_number چیزی اضافه نمی‌کند")

    def test_postgres_fragment_uses_arrow_operator(self):
        """On the PG, the ``->>'' operator (and if needed, a numeric cast)."""
        self.assertEqual(self.pg.json_extract("details", "event_id"),
                         "(\"details\"::jsonb)->>'event_id'")
        self.assertEqual(self.pg.json_extract("details", "prev_total", as_number=True),
                         "((\"details\"::jsonb)->>'prev_total')::numeric")

    def test_column_name_is_quoted(self):
        """The column name is quoted (standard syntax; the same as the ``quote_ident'' rule)."""
        self.assertEqual(self.pg.json_extract("weird name", "k"),
                         '("weird name"::jsonb)->>\'k\'')
        self.assertEqual(self.pg.json_extract('has"quote', "k"),
                         '("has""quote"::jsonb)->>\'k\'')

    def test_invalid_key_is_rejected(self):
        """Invalid key ⇒ ``ValueError'' (helper constructs SQL text; security boundary)."""
        bad_keys = ("a.b", "x' OR 1=1 --", "x; DROP TABLE logs", "", "1abc",
                    "با فاصله", None, 3, "k)", "$.k")
        for key in bad_keys:
            for adapter in (self.sqlite, self.pg):
                with self.subTest(key=key, backend=adapter.backend):
                    with self.assertRaises(ValueError):
                        adapter.json_extract("details", key)

    def test_fragment_runs_on_a_real_sqlite_table(self):
        """The built-in snippet works on real SQLite (text and numbers)."""
        self.sqlite.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, details TEXT)")
        self.sqlite.execute("INSERT INTO t (details) VALUES (?)",
                            (json.dumps({"event_id": "e-1", "prev_total": 12}),))
        text_frag = self.sqlite.json_extract("details", "event_id")
        num_frag = self.sqlite.json_extract("details", "prev_total", as_number=True)
        self.assertEqual(
            self.sqlite.execute(f"SELECT 1 FROM t WHERE {text_frag} = ?", ("e-1",)).fetchone(),
            (1,))
        self.assertEqual(
            self.sqlite.execute(f"SELECT 1 FROM t WHERE {num_frag} = ?", (12,)).fetchone(),
            (1,))
        self.assertIsNone(
            self.sqlite.execute(f"SELECT 1 FROM t WHERE {num_frag} = ?", (13,)).fetchone())


# ─────────────────────────────────────────────────────────────────────
# B) dedupe behavior on SQLite (migration did not change domain logic)
# ─────────────────────────────────────────────────────────────────────
class SqliteDedupeBehaviourTests(unittest.TestCase):
    """Three dedupe paths that used this statement, on real SQLite."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)                 # Relative DB_PATH ⇒ Temporary DB
        self.addCleanup(self._restore)
        from core.database import init_db
        init_db()

    def _restore(self):
        """Helper: restore."""
        os.chdir(self.old_cwd)

    def _rows(self, etype):
        """Helper: rows."""
        from core.database import db_connection
        with db_connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM logs WHERE printer_ip = ? AND type = ?",
                (IP, etype)).fetchone()[0]

    def test_cartridge_changed_same_event_id_is_skipped(self):
        """Same ``event_id'' ⇒ second event is not registered (text key)."""
        from core.database import add_event
        add_event(IP, "CARTRIDGE_CHANGED", {"event_id": "ev-1", "color": "black"})
        add_event(IP, "CARTRIDGE_CHANGED", {"event_id": "ev-1", "color": "black"})
        self.assertEqual(self._rows("CARTRIDGE_CHANGED"), 1)
        add_event(IP, "CARTRIDGE_CHANGED", {"event_id": "ev-2", "color": "black"})
        self.assertEqual(self._rows("CARTRIDGE_CHANGED"), 2,
                         "رویداد با event_id دیگر باید ثبت شود")

    def test_print_duplicate_transition_is_skipped(self):
        """``PRINT'' is not recorded with the same prev/current ⇒ second record (numerical comparison)."""
        from core.database import add_event
        add_event(IP, "PRINT", {"prev_total": 100, "current_total": 105})
        add_event(IP, "PRINT", {"prev_total": 100, "current_total": 105})
        self.assertEqual(self._rows("PRINT"), 1)
        add_event(IP, "PRINT", {"prev_total": 105, "current_total": 110})
        self.assertEqual(self._rows("PRINT"), 2)

    def test_counter_reset_duplicate_transition_is_skipped(self):
        """``COUNTER_RESET'' is not registered with the same prev/current ⇒ second record."""
        from core.database import add_event
        add_event(IP, "COUNTER_RESET", {"prev_total": 5000, "current_total": 3})
        add_event(IP, "COUNTER_RESET", {"prev_total": 5000, "current_total": 3})
        self.assertEqual(self._rows("COUNTER_RESET"), 1)
        add_event(IP, "COUNTER_RESET", {"prev_total": 5000, "current_total": 9})
        self.assertEqual(self._rows("COUNTER_RESET"), 2)


# ─────────────────────────────────────────────────────────────────────
# C) Regression lock on code text
# ─────────────────────────────────────────────────────────────────────
class SourceGuardTests(unittest.TestCase):
    """After E.4.9, no text ``json_extract('') remains in the program code."""

    def test_no_inline_json_extract_in_app_code(self):
        """Verifies no inline json extract in app code."""
        offenders = []
        for root in ("core", "web"):
            for path in sorted((ROOT / root).rglob("*.py")):
                if path.relative_to(ROOT).as_posix() == "core/db/connection.py":
                    continue                        # The helper itself (and its docstrings)
                for lineno, line in enumerate(
                        path.read_text(encoding="utf-8").splitlines(), start=1):
                    code = line.split("#", 1)[0]     # Comments are not counted
                    # ``conn.json_extract(...)'' is true; What is prohibited is SQL text
                    # Manual (``json_extract(`` without dot at the beginning of the call).
                    if re.search(r"(?<![\.\w])json_extract\(", code):
                        offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "باید از conn.json_extract(...) استفاده شود، نه متن SQL دستی")

    def test_helper_is_used_by_both_consumers(self):
        """Two consumers (core/database.py and web/routes/metrics.py) use the helper."""
        for rel, minimum in (("core/database.py", 5), ("web/routes/metrics.py", 1)):
            source = (ROOT / rel).read_text(encoding="utf-8")
            self.assertGreaterEqual(source.count("conn.json_extract("), minimum, rel)


# ─────────────────────────────────────────────────────────────────────
# D) Real PostgreSQL
# ─────────────────────────────────────────────────────────────────────
@unittest.skipUnless(_PG_DSN, _PG_SKIP or "")
class PostgresLiveJsonExtractTests(unittest.TestCase):
    """On real PG 16: the ``->>'' fragment works and the dedupe/`metrics' paths are intact."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        if not _PG_DSN:
            self.skipTest(_PG_SKIP or "PG در دسترس نیست")
        self.env = mock.patch.dict(os.environ, {
            "DB_BACKEND": "postgresql", ENV_PG_DSN: _PG_DSN})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = connect_postgres(_PG_DSN)
        self.addCleanup(self.conn.close)

    def _cleanup_logs(self):
        """Helper: cleanup logs."""
        self.conn.rollback()
        self.conn.execute("DELETE FROM logs WHERE printer_ip = ?", (IP,))
        self.conn.commit()

    def test_fragment_queries_the_real_logs_table(self):
        """The same pieces work on the real ``logs'' (text + number) table."""
        from core.database import add_event
        from core.database import db_connection
        self._cleanup_logs()
        add_event(IP, "CARTRIDGE_CHANGED", {"event_id": "pg-ev-1", "color": "black"})
        add_event(IP, "PRINT", {"prev_total": 100, "current_total": 105})

        with db_connection() as conn:
            text_frag = conn.json_extract("details", "event_id")
            num_frag = conn.json_extract("details", "prev_total", as_number=True)
            hit = conn.execute(
                f"SELECT COUNT(*) FROM logs WHERE printer_ip = ? AND {text_frag} = ?",
                (IP, "pg-ev-1")).fetchone()[0]
            num_hit = conn.execute(
                f"SELECT COUNT(*) FROM logs WHERE printer_ip = ? AND {num_frag} = ?",
                (IP, 100)).fetchone()[0]
        self.assertEqual(hit, 1)
        self.assertEqual(num_hit, 1)
        self._cleanup_logs()

    def test_dedupe_is_alive_on_postgres(self):
        """
        Three dedupe paths also work on PG (before E.4.9, all with
        They break "function json_extract(text, unknown) does not exist" and
        ``add_event'' only logs the error ⇒ silent failure).
        """
        from core.database import add_event, db_connection
        self._cleanup_logs()
        add_event(IP, "CARTRIDGE_CHANGED", {"event_id": "pg-dup", "color": "black"})
        add_event(IP, "CARTRIDGE_CHANGED", {"event_id": "pg-dup", "color": "black"})
        add_event(IP, "PRINT", {"prev_total": 7, "current_total": 9})
        add_event(IP, "PRINT", {"prev_total": 7, "current_total": 9})
        with db_connection() as conn:
            rows = dict(conn.execute(
                "SELECT type, COUNT(*) FROM logs WHERE printer_ip = ? GROUP BY type",
                (IP,)).fetchall())
        self.assertEqual(rows, {"CARTRIDGE_CHANGED": 1, "PRINT": 1},
                         "ثبت دوباره باید skip شود، ولی رکورد اول باید باشد")

        # normalization from the side of the project and symmetric by inserting: the relational column belongs to the test.
        self._cleanup_logs()

    # ``/metrics'' on PG is not measured here: that query has a **independent** bug
    # (``printer_name'' column without ``GROUP BY'' ⇒ ``GroupingError'' on PG). decision
    # Malik: fix in separate step ``E.4.9b'' with its own test file (``tests/test_metrics_pg.py'')
    # and independent commit. The live test of this file only measures ``json_extract''/dedupe.


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
