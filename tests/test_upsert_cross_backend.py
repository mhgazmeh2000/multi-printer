# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_upsert_cross_backend.py
# @brief:        Tests for the cross-backend upsert helper (E.5).
# @details:      Responsibilities: Verify the upsert SQL for SQLite and PostgreSQL.
#                Main API: HelperFragmentTests, SourceContractTests, ColumnCompletenessTests,
#                _UpsertBehaviourMixin, SqliteUpsertBehaviourTests, PostgresUpsertBehaviourTests
#                Side effects: database access
# @dependencies: internal: core.database, core.db, core.db.schema, core.yield_engine | external:
#                psycopg
# @notes:        Can be run directly as a script.
# =============================================================================

"""
E.5 Tests — ``upsert_sql'' and three locations moved from ``INSERT OR REPLACE''.

Why is this file?
----------------
``INSERT OR REPLACE'' is the SQLite syntax; on PostgreSQL with
``syntax error at or near "OR"'' (42601) breaks. In the first measurement E.5 (the whole suite with
``DB_BACKEND=postgresql'') The 20 failures were from just this one pattern. helper
``core.db.upsert_sql'' makes the statement backend-aware:

  • SQLite — ``INSERT OR REPLACE INTO t (…) VALUES (…)'' ⇒ byte by byte today;
  • PG — ``INSERT … ON CONFLICT (key) DO UPDATE SET c = EXCLUDED.c …``.

This translation **only** does not change the meaning of the data when the sentence of *all* columns of the table
Write (``docs/insert-or-replace-inventory.md`` §3.1): SQLite with REPLACE row
deletes and rebuilds (so unwritten column goes back to default) and PG in-place UPDATE
does (the unwritten column keeps the previous value). So part of the same file, lock
The match is "insert column list == all table columns" — if one column to the table
is added, this breaks the test and reminds that the sentence is no longer "full pern".

Two procedures are evaluated: **insert new**, **conflict ⇒ update**, **idempotent**
(execute again ⇒ same result), and **maintain other columns** when they are actually written.
"""
import ast
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.db import (
    BACKEND_POSTGRESQL,
    BACKEND_SQLITE,
    ENV_PG_DSN,
    DatabaseError,
    connect_postgres,
    connect_sqlite,
    upsert_sql,
)
from core.db.schema import schema_columns

ROOT = Path(__file__).resolve().parent.parent
IP = "203.0.113.160"


def _pg_target():
    """``(dsn, None)'' if PG is available, otherwise ``(None, skip reason)''."""
    dsn = (os.getenv(ENV_PG_DSN, "") or "").strip()
    if not dsn:
        return None, (f"{ENV_PG_DSN} ست نشده است ⇒ تست‌های PGِ زنده‌ی این فایل اجرا نمی‌شوند "
                      f"(اجرا با: PG_DSN='host=… dbname=… user=… password=…' pytest)")
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        return None, (f"PG در دسترس نیست ({type(exc).__name__}: "
                      f"{str(exc).strip().splitlines()[0][:120]}) ⇒ تست‌های PG رد می‌شوند")
    return dsn, None


_PG_DSN, _PG_SKIP = _pg_target()


def _pin_sqlite(test):
    """
    Locks the environment of a SQLite-based test.

    Why it is needed: ``init_db()'' and ``ensure_yield_tables()'' backend from environment
    they sing; If the suite is run with ``DB_BACKEND=postgresql'' and it is not locked,
    Tables are created on PG and the temporary file remains empty ⇒ "no such table"
    (The same thing happened in step E.5). The PG_DSN lock also prevents accidental
    be written on the repository DB.
    """
    env = mock.patch.dict(os.environ, {"DB_BACKEND": BACKEND_SQLITE, ENV_PG_DSN: ""})
    env.start()
    test.addCleanup(env.stop)
    from core.db import resolve_backend
    assert resolve_backend() == BACKEND_SQLITE


# ─────────────────────────────────────────────────────────────────────
# A) Helper itself (without database)
# ─────────────────────────────────────────────────────────────────────
class HelperFragmentTests(unittest.TestCase):

    """Tests for helper fragment."""
    def test_sqlite_keeps_the_historic_statement(self):
        """Verifies sqlite keeps the historic statement."""
        sql = upsert_sql("t", ("a", "b"), ("a",), backend=BACKEND_SQLITE)
        self.assertEqual(sql, 'INSERT OR REPLACE INTO "t" ("a", "b") VALUES (?, ?)')

    def test_postgres_targets_the_conflict_key(self):
        """Verifies postgres targets the conflict key."""
        sql = upsert_sql("t", ("a", "b"), ("a",), backend=BACKEND_POSTGRESQL)
        self.assertEqual(
            sql,
            'INSERT INTO "t" ("a", "b") VALUES (?, ?) '
            'ON CONFLICT ("a") DO UPDATE SET "b" = EXCLUDED."b"')

    def test_conflict_columns_are_excluded_from_the_update_list(self):
        """Verifies conflict columns are excluded from the update list."""
        sql = upsert_sql("t", ("ip", "color", "n"), ("ip", "color"), backend=BACKEND_POSTGRESQL)
        self.assertIn('ON CONFLICT ("ip", "color")', sql)
        self.assertEqual(sql.count("= EXCLUDED."), 1, sql)
        self.assertIn('"n" = EXCLUDED."n"', sql)
        for key in ("ip", "color"):
            self.assertNotIn(f'"{key}" = EXCLUDED.', sql, f"کلید نباید در SET بیاید: {sql}")

    def test_no_updatable_column_falls_back_to_do_nothing(self):
        """Empty ``DO UPDATE SET'' is an invalid syntax if all columns are keys."""
        sql = upsert_sql("t", ("a",), ("a",), backend=BACKEND_POSTGRESQL)
        self.assertTrue(sql.endswith("DO NOTHING"), sql)

    def test_explicit_values_sql_is_passed_through(self):
        """The fixed value is kept in VALUES (like ``'high'') and the number of placeholders is not checked."""
        sql = upsert_sql("t", ("a", "b", "c"), ("a",), values_sql="?, 'x', ?",
                         backend=BACKEND_POSTGRESQL)
        self.assertIn("VALUES (?, 'x', ?)", sql)
        self.assertIn('"c" = EXCLUDED."c"', sql)

    def test_validation_is_strict(self):
        """Verifies validation is strict."""
        for kwargs in (
            {"table": "t", "columns": (), "conflict_columns": ("a",)},
            {"table": "t", "columns": ("a",), "conflict_columns": ()},
            {"table": "t", "columns": ("a", "b"), "conflict_columns": ("zz",)},
        ):
            with self.subTest(**{k: v for k, v in kwargs.items() if k != "table"}):
                with self.assertRaises(ValueError):
                    upsert_sql(**kwargs)

    def test_backend_default_follows_env(self):
        """Verifies backend default follows env."""
        with mock.patch.dict(os.environ, {"DB_BACKEND": "postgresql"}):
            self.assertIn("ON CONFLICT", upsert_sql("t", ("a", "b"), ("a",)))
        with mock.patch.dict(os.environ, {"DB_BACKEND": "sqlite"}):
            self.assertIn("INSERT OR REPLACE", upsert_sql("t", ("a", "b"), ("a",)))


# ─────────────────────────────────────────────────────────────────────
# B) Structural locks on code
# ─────────────────────────────────────────────────────────────────────
class SourceContractTests(unittest.TestCase):
    """No manual ``INSERT OR REPLACE'' is left in the program code (decision E.5)."""

    #: The only places allowed for "INSERT OR REPLACE" text are in core/ and web/ — knowingly and
    #Documentary. If a third is added, this test breaks.
    #: (In E.6.5 the third previous — TD-001 dead code in enhanced_collector — was removed, so
    #: allow-list was also shortened; The witness of "deletion" in the test below is the same file.)
    ALLOWED_SITES = {
        # Helper itself: Creates the SQLite statement right here.
        "core/db/upsert.py": "سازندهٔ جمله",
        # replace mode in import: it is made only for the SQLite destination and the path is PG
        # Previously closed with integral guard (E.4.8b) and retired in E.6.1.
        "web/routes/import_db.py": "جملهٔ پویا، زیر گارد PG",
    }

    def _docstring_linenos(self, source):
        """docstring lines (not code) so they don't count."""
        lines = set()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    doc = body[0].value
                    lines.update(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
        return lines

    def test_only_the_allowed_sites_mention_insert_or_replace(self):
        """
        Rule E.5: The program code does not write the ``INSERT OR REPLACE'' sentence by hand, except for three
        Sites that are registered in ``ALLOWED_SITES'' with reason. Counting with AST is up
        docstrings and comments should not count as "code".
        """
        found = {}
        for root in ("core", "web"):
            for path in sorted((ROOT / root).rglob("*.py")):
                rel = path.relative_to(ROOT).as_posix()
                source = path.read_text(encoding="utf-8")
                doc_lines = self._docstring_linenos(source)
                for lineno, line in enumerate(source.splitlines(), start=1):
                    if lineno in doc_lines:
                        continue
                    code = line.split("#", 1)[0]
                    if re.search(r"INSERT\s+OR\s+REPLACE", code, re.I):
                        found.setdefault(rel, []).append(lineno)
        self.assertEqual(sorted(found), sorted(self.ALLOWED_SITES),
                         f"محل‌های غیرمنتظره: {found}")

    def test_the_untouched_sites_are_still_documented(self):
        """
        A consciously intact location (golden fixtures, decision E.5), an under-guard location
        (import_db, decision E.6.1: retirement) and a **removed** location (TD-001 in E.6.5).
        If one day someone suddenly changes this situation, this test will ask why.
        """
        dead = (ROOT / "core" / "enhanced_collector.py").read_text(encoding="utf-8")
        self.assertNotIn("INSERT OR REPLACE", dead,
                         "TD-001 در E.6.5 حذف شد؛ بازگشتش باید با دلیل و کامیت جدا باشد")
        self.assertNotIn("_save_counters_to_db", dead, "تابع مرده نباید احیا شود")
        harness = (ROOT / "tests" / "golden_harness.py").read_text(encoding="utf-8")
        self.assertIn("INSERT OR REPLACE", harness, "fixtureهای SQLite golden دست‌نخورده‌اند")
        route = (ROOT / "web" / "routes" / "import_db.py").read_text(encoding="utf-8")
        self.assertIn("_reject_import_on_postgres", route, "import روی PG زیر گارد است")
        self.assertNotIn("upsert_sql", route)

    def test_three_live_sites_call_the_helper(self):
        """The three live locations must call the helper (not the manual SQL string)."""
        expected = {"core/database.py": 1, "core/yield_engine.py": 2}
        for rel, count in expected.items():
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_upsert_sql"
            ]
            self.assertEqual(len(calls), count,
                             f"{rel}: انتظار {count} فراخوانی upsert_sql داشتیم")


# ─────────────────────────────────────────────────────────────────────
# C) "all columns" lock — correct translation condition
# ─────────────────────────────────────────────────────────────────────
class ColumnCompletenessTests(unittest.TestCase):
    """The column lists of the moved three places should remain the same as "all table columns"."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)                 # Relative DB_PATH ⇒ Temporary DB
        self.addCleanup(lambda: os.chdir(self.old_cwd))
        _pin_sqlite(self)                      # init_db() should see this same file
        import core.database as database
        import core.yield_engine as yield_engine
        database.init_db()
        yield_engine.ensure_yield_tables()
        self.conn = connect_sqlite(os.path.join(self.tmp.name, "logs.db"))
        self.addCleanup(self.conn.close)

    def test_printer_counters_columns_are_complete(self):
        """Verifies printer counters columns are complete."""
        import core.database as database
        table_cols = schema_columns(self.conn, "printer_counters")
        # The *declaration* order of the table is not a condition (toner_overrides added with the last ALTER), but
        # "Cover all columns" is a condition — otherwise the translation changes the meaning of REPLACE.
        self.assertEqual(sorted(database._COUNTERS_UPSERT_COLUMNS), sorted(table_cols),
                         "فهرست درج باید همهٔ ستون‌های جدول را بنویسد (شرط §۳.۱)")
        # The same directory that the reader creates, without the "ip" ⇒ must be a subdirectory:
        read_cols = [c.strip() for c in database._COUNTERS_COLS.split(",")]
        self.assertEqual(sorted(read_cols), sorted(t for t in table_cols if t != "ip"))

    def test_cartridge_state_columns_are_complete(self):
        """Verifies cartridge state columns are complete."""
        import core.yield_engine as yield_engine
        self.assertEqual(list(yield_engine._STATE_UPSERT_COLUMNS),
                         schema_columns(self.conn, "cartridge_state"))

    def test_cartridge_yield_profiles_columns_are_complete(self):
        """Verifies cartridge yield profiles columns are complete."""
        import core.yield_engine as yield_engine
        self.assertEqual(list(yield_engine._PROFILE_UPSERT_COLUMNS),
                         schema_columns(self.conn, "cartridge_yield_profiles"))

    def test_upsert_param_counts_match_the_columns(self):
        """Every place in the ``VALUES'' view must have a parameter (except constant values)."""
        import core.database as database
        import core.yield_engine as yield_engine
        state_sql = upsert_sql("cartridge_state", yield_engine._STATE_UPSERT_COLUMNS,
                               ("printer_ip", "color"), backend=BACKEND_SQLITE)
        self.assertEqual(state_sql.count("?"), len(yield_engine._STATE_UPSERT_COLUMNS))
        prof_sql = upsert_sql("cartridge_yield_profiles", yield_engine._PROFILE_UPSERT_COLUMNS,
                               ("cartridge_key",), values_sql=yield_engine._PROFILE_UPSERT_VALUES,
                               backend=BACKEND_SQLITE)
        self.assertEqual(prof_sql.count("?"), 9, "۹ جای‌نما + یک مقدار ثابت 'high'")
        counters_sql = upsert_sql("printer_counters", database._COUNTERS_UPSERT_COLUMNS,
                                  ("ip",), backend=BACKEND_SQLITE)
        self.assertEqual(counters_sql.count("?"), len(database._COUNTERS_UPSERT_COLUMNS))


# ─────────────────────────────────────────────────────────────────────
# D) behavior on both backends — common ground
# ─────────────────────────────────────────────────────────────────────
class _UpsertBehaviourMixin:
    """insert new / conflict ⇒ update / idempotent, on connection ``self.conn''."""

    def _state(self, **over):
        """Helper: state."""
        state = {
            "printer_ip": IP, "color": "black", "printer_model": "M-1",
            "cartridge_name": "C-1", "cartridge_key": f"{IP}:M-1:C-1",
            "current_level": 55, "last_valid_level": 60, "last_counter": 1000,
            "anchor_counter": 900, "anchor_level": 70,
            "anchor_timestamp": "2026-10-04T00:00:00.000000Z",
            "yield_per_page": 1800, "yield_source": "auto_learn", "confidence": "high",
            "sample_count": 3, "total_weight": 2.5,
            "learned_at": "2026-10-04T00:00:00.000000Z", "last_refill_at": None,
            "last_refill_counter": None, "pending_refill_level": None,
            "pending_refill_counter": None, "pending_refill_timestamp": None,
            "cycle_start_counter": 800, "cycle_start_level": 80,
            "cycle_start_timestamp": "2026-10-04T00:00:00.000000Z",
            "zero_reached_counter": None, "zero_reached_timestamp": None,
            "pages_after_zero": 0, "cycle_status": "active", "force_estimate": 0,
            "updated_at": "2026-10-04T00:00:00.000000Z",
        }
        state.update(over)
        return state

    def test_fresh_insert_then_conflict_updates(self):
        """Verifies fresh insert then conflict updates."""
        import core.yield_engine as yield_engine
        self._delete_rows()

        yield_engine._upsert_state(self.conn, self._state(current_level=55))
        self.conn.commit()
        row = yield_engine._load_state(self.conn, IP, "black")
        self.assertIsNotNone(row, "ردیف تازه باید درج شود")
        self.assertEqual(row["current_level"], 55)

        # The same key (printer_ip, color) ⇒ should be **updated**, not the second row.
        yield_engine._upsert_state(self.conn, self._state(current_level=33, sample_count=4))
        self.conn.commit()
        row2 = yield_engine._load_state(self.conn, IP, "black")
        self.assertEqual(row2["current_level"], 33)
        self.assertEqual(row2["sample_count"], 4)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM cartridge_state WHERE printer_ip = ?", (IP,)).fetchone()[0]
        self.assertEqual(count, 1, "REPLACE/upsert باید یک ردیف نگه دارد")

    def test_upsert_is_idempotent(self):
        """Rerun with same data ⇒ same status (no error, no extra rows)."""
        import core.yield_engine as yield_engine
        self._delete_rows()
        state = self._state()
        for _ in range(3):
            yield_engine._upsert_state(self.conn, state)
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT current_level, sample_count, updated_at FROM cartridge_state "
            "WHERE printer_ip = ?", (IP,)).fetchall()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0], (55, 3, "2026-10-04T00:00:00.000000Z"))

    def test_profile_upsert_replaces_instead_of_accumulating(self):
        """
        ``_upsert_profile_if_high'' with the same ``cartridge_key'' should profile
        replace (the max/weighted average logic is inside the function; here it's just "one row"
        and "fresh value" is measured).
        """
        import core.yield_engine as yield_engine
        self._delete_rows()
        key = f"{IP}:M-1:C-1"
        self.conn.execute("DELETE FROM cartridge_yield_profiles WHERE cartridge_key = ?", (key,))
        self.conn.commit()

        yield_engine._upsert_profile_if_high(self.conn, self._state(confidence="high",
                                                                    yield_per_page=1500))
        self.conn.commit()
        prof = yield_engine._load_profile(self.conn, key)
        self.assertIsNotNone(prof)
        self.assertEqual(prof["yield_per_page"], 1500)
        self.assertEqual(prof["confidence"], "high")

        yield_engine._upsert_profile_if_high(self.conn, self._state(confidence="high",
                                                                    yield_per_page=1700))
        self.conn.commit()
        prof2 = yield_engine._load_profile(self.conn, key)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM cartridge_yield_profiles WHERE cartridge_key = ?",
            (key,)).fetchone()[0]
        self.assertEqual(rows, 1, "تعارض باید به جایگزینی ختم شود، نه ردیف دوم")
        self.assertEqual(prof2["yield_per_page"], 1700)

    def _delete_rows(self):
        """Clearing test rows (profile key is ``source_printer_ip'', not printer_ip)."""
        self.conn.execute("DELETE FROM cartridge_state WHERE printer_ip = ?", (IP,))
        self.conn.execute("DELETE FROM cartridge_yield_profiles WHERE source_printer_ip = ?", (IP,))
        self.conn.commit()


class SqliteUpsertBehaviourTests(_UpsertBehaviourMixin, unittest.TestCase):
    """On SQLite: exactly the same behavior as before (REPLACE).

    The environment is intentionally locked to SQLite: ``init_db()'' and ``ensure_yield_tables()''
    The backend is read from the environment, so if the suite runs with ``DB_BACKEND=postgresql''
    and it is not locked here, the tables are created on PG and the temporary file remains empty
    ("no such table: cartridge_state" — the actual occurrence of this step).
    """

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(lambda: os.chdir(self.old_cwd))
        _pin_sqlite(self)
        import core.database as database
        import core.yield_engine as yield_engine
        database.init_db()
        yield_engine.ensure_yield_tables()
        self.conn = connect_sqlite(os.path.join(self.tmp.name, "logs.db"))
        self.addCleanup(self.conn.close)


@unittest.skipUnless(_PG_DSN, _PG_SKIP or "")
class PostgresUpsertBehaviourTests(_UpsertBehaviourMixin, unittest.TestCase):
    """On real PG: same four measurements, via ``ON CONFLICT DO UPDATE''."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        if not _PG_DSN:
            self.skipTest(_PG_SKIP or "PG در دسترس نیست")
        # This class also calls application-level functions (save_printer_counters,
        # ensure_printer_counters_columns) and they read the backend from the environment.
        # Without the DB_BACKEND lock, those calls go silently to SQLite and **DB
        # Repository ** — same thing happened once and logs.db with reproduction
        # E.3.5 byte-byte migration reverted. So: env + temporary cwd + confirm.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(lambda: os.chdir(self.old_cwd))
        self.env = mock.patch.dict(os.environ, {
            "DB_BACKEND": "postgresql", ENV_PG_DSN: _PG_DSN})
        self.env.start()
        self.addCleanup(self.env.stop)
        from core.db import resolve_backend
        self.assertEqual(resolve_backend(), BACKEND_POSTGRESQL,
                         "محیط PG قفل نشد — خطر نوشتن روی DB مخزن")
        self.conn = connect_postgres(_PG_DSN)
        self.addCleanup(self._cleanup)
        self.addCleanup(self.conn.close)

    def _cleanup(self):
        """Helper: cleanup."""
        try:
            self.conn.rollback()
            self._delete_rows()
            self.conn.execute("DELETE FROM printer_counters WHERE ip = ?", (IP,))
            self.conn.commit()
        except Exception:
            pass

    def test_helper_sql_is_what_pg_accepts(self):
        """Double witness: the helper sentence is executed and the old sentence gives 42601."""
        import core.yield_engine as yield_engine
        new_sql = upsert_sql("cartridge_state", yield_engine._STATE_UPSERT_COLUMNS,
                             ("printer_ip", "color"), backend=BACKEND_POSTGRESQL)
        old_sql = new_sql.replace(
            'INSERT INTO "cartridge_state"', 'INSERT OR REPLACE INTO "cartridge_state"', 1)
        old_sql = old_sql.split(" ON CONFLICT", 1)[0]
        # The parameters are padded so that the error comes from the PG interpreter, not from the number check
        # placeholder in the adapter itself (that check is done before sending).
        params = (IP, "black") + (None,) * (len(yield_engine._STATE_UPSERT_COLUMNS) - 2)
        with self.assertRaises(DatabaseError) as ctx:
            self.conn.execute(old_sql, params)
        self.assertEqual(ctx.exception.sqlstate, "42601", str(ctx.exception))
        self.conn.rollback()
        self.assertIn("ON CONFLICT", new_sql)
        self.assertIn("DO UPDATE SET", new_sql)

    def test_save_printer_counters_round_trip_on_pg(self):
        """``save_printer_counters'' / ``load_printer_counters'' on live PG (location 1)."""
        import core.database as database
        self.conn.execute("DELETE FROM printer_counters WHERE ip = ?", (IP,))
        self.conn.commit()
        data = {"print_total": 120, "toner_level": 80, "device_type": "toshiba",
                "updated_at": "2026-10-04T00:00:00.000000Z"}
        database.save_printer_counters(IP, data)
        loaded = database.load_printer_counters(IP)
        self.assertIsNotNone(loaded, "پس از درج باید ردیف خوانده شود")
        self.assertEqual(loaded["print_total"], 120)

        data["print_total"] = 240
        database.save_printer_counters(IP, data)          # Conflict ⇒ update
        loaded2 = database.load_printer_counters(IP)
        self.assertEqual(loaded2["print_total"], 240)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM printer_counters WHERE ip = ?", (IP,)).fetchone()[0]
        self.assertEqual(rows, 1)
        # The second run shouldn't leave anything in the log (ensure_* is inactive on PG)
        with mock.patch("core.database.log") as lg:
            database.ensure_printer_counters_columns()
        lg.exception.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
