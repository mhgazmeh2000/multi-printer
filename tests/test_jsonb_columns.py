# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_jsonb_columns.py
# @brief:        Tests for the jsonb conversion of logs.details on PostgreSQL (E.6.4).
# @details:      Responsibilities: Verify that only the intended columns change type.
#                Main API: MigratedSchemaMixin, MigrationScopeTests, SqliteSchemaUnchangedTests,
#                ReaderToleranceTests, PgColumnTypeTests, PgAppRoundTripTests
#                Side effects: database access, runs external processes
# @dependencies: internal: core, core.database, core.db, tests.conftest, tests.migration_graph |
#                external: psycopg
# @notes:        Can be run directly as a script.
# =============================================================================

"""
E.6.4 tests — ``logs.details'' on PostgreSQL ⇒ ``jsonb''; The rest of the columns are TEXT.

The owner criterion for this decision was ``grep json_extract'': only one column goes to JSONB
on which **SQL** reads JSON content. That grep returned one column (``logs.details'' — in
``web/routes/metrics.py'' and in dedup events ``core/database.py''); four columns
JSON is only serialized in Python anymore ⇒ **deliberately** remain TEXT. This file is the same
Locks the domain so that "unreasonable uniformity" is not automatically added later.

Three things are tested — the three things decided upon:
  1) column type on PG = ``jsonb'', on SQLite = ``TEXT'' (aware divergence); four
     The other column remains ``text'' on PG;
  2) Readers work with both value formats (psycopg ``jsonb'' value
     gives decoded) and **SQLite meaning not changed**: malformed JSON still error there
     It gives, silently ``{}`` no;
  3) migration preflight is brutal: an invalid row ⇒ stop with counts and ids;
     Empty string ⇒ warning only (because it will be NULL and this has changed).

Measured cost/benefit (PG 16.15, 200,000 rows) in ``docs/phase-e-decisions.md''
Row E.6.4 shows: query metrics 119.3ms ⇒ 46.8ms (2.5x), negligible dedup.

``PG_DSN'' is used on PGs; "Program Behavior" tests with ``skipUnless'' on
``DB_BACKEND=postgresql'' are locked so they don't "false green" when running SQLite silently.
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tests.migration_graph import migration_head

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "alembic" / "versions" / "0004_jsonb_columns.py"

ENV_PG_DSN = "PG_DSN"
_PG_DSN = (os.getenv(ENV_PG_DSN, "") or "").strip()


def _pg_ready():
    """Helper: pg ready."""
    if not _PG_DSN:
        return False, f"{ENV_PG_DSN} ست نشده ⇒ رفتار JSONB روی PG آزموده نشد"
    try:
        import psycopg
        with psycopg.connect(_PG_DSN, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        return False, f"PG در دسترس نیست ({type(exc).__name__}) ⇒ رفتار JSONB آزموده نشد"
    return True, None


_PG_OK, _PG_SKIP = _pg_ready()

def _migration():
    """Helper: migration."""
    spec = importlib.util.spec_from_file_location("migration_0004_under_test", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dsn() -> str:
    """
    The DSN is read **at runtime**: the suite fixture, ``PG_DSN'' to the address
    Overwrite the isolated schema (with ``options=-csearch_path=…'') and import this
    The file happens earlier than the fixture.
    """
    return (os.getenv(ENV_PG_DSN, "") or "").strip() or _PG_DSN


def _dsn_for(schema: str) -> str:
    """Raw DSN + ``options=-csearch_path'' (template tests/conftest.py; no spaces allowed)."""
    from tests.conftest import _dsn_with_search_path
    return _dsn_with_search_path(_PG_DSN, schema)


def _pg_conn(dsn=None):
    """Independent connection with autocommit (temp table / DDL inside tests)."""
    import psycopg
    return psycopg.connect(dsn or _dsn(), autocommit=True)


class MigratedSchemaMixin:
    """
    An isolated schema that has an ``alembic upgrade head'' implemented.

    Why the test builds its own schema and doesn't rely on the fixture suite: the claim of this
    classes is "migration 0004 changes column type"; So it should be independent of the situation
    The schema of the environment should be measured and **search** in the implementation of SQLite (not skip - the rule
    "silent skip" is prohibited in this suite).
    """

    _schema = None

    @classmethod
    def setUpClass(cls):
        """Prepare the fixtures shared by all tests in this class."""
        if not _PG_OK:
            return
        cls._schema = f"printguard_e64_{uuid.uuid4().hex[:10]}"
        with _pg_conn(_PG_DSN) as conn:
            conn.execute(f"CREATE SCHEMA {cls._schema}")
        env = dict(os.environ)
        env["DB_BACKEND"] = "postgresql"
        env[ENV_PG_DSN] = _dsn_for(cls._schema)
        proc = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                              cwd=str(ROOT), env=env, capture_output=True, text=True,
                              timeout=300)
        if proc.returncode != 0:
            raise AssertionError("alembic upgrade head روی "
                                 f"{cls._schema} شکست:\n"
                                 + (proc.stdout + proc.stderr)[-2500:])

    @classmethod
    def tearDownClass(cls):
        """Release the fixtures shared by all tests in this class."""
        if cls._schema:
            with _pg_conn(_PG_DSN) as conn:
                conn.execute(f"DROP SCHEMA IF EXISTS {cls._schema} CASCADE")
            cls._schema = None

    @classmethod
    def dsn(cls) -> str:
        """Helper: dsn."""
        return _dsn_for(cls._schema)


class MigrationScopeTests(unittest.TestCase):
    """Scope of decision + chain of revisions."""

    def test_only_logs_details_is_converted(self):
        """Verifies only logs details is converted."""
        m = _migration()
        self.assertEqual(m.JSONB_COLUMNS, (("logs", "details"),),
                         "دامنهٔ E.6.4 فقط logs.details است؛ هر ستون تازه باید "
                         "دلیل و اندازه‌گیری خودش را داشته باشد")

    def test_revision_chain_is_linear_after_identity_columns(self):
        """Verifies revision chain is linear after identity columns."""
        m = _migration()
        self.assertEqual((m.revision, m.down_revision), ("0004", "0003"))

    def test_0004_is_the_head_of_the_chain(self):
        """The chain head is 0005 (0004 was 0005 before migration)."""
        self.assertEqual(migration_head(), "0005")


class SqliteSchemaUnchangedTests(unittest.TestCase):
    """SQLite side: Nothing is converted — checked against the actual schema of the repository."""

    @staticmethod
    def _decls(table):
        """Helper: decls."""
        conn = sqlite3.connect(f"file:{ROOT / 'logs.db'}?mode=ro", uri=True)
        try:
            return {r[1]: (r[2] or "").upper() for r in conn.execute(
                f"PRAGMA table_info({table})")}
        finally:
            conn.close()

    def test_logs_details_is_still_text_on_sqlite(self):
        """Verifies logs details is still text on sqlite."""
        self.assertEqual(self._decls("logs").get("details"), "TEXT")

    def test_the_python_side_columns_are_text_on_both_backends(self):
        """Verifies the python side columns are text on both backends."""
        for table, column in (("users", "allowed_offices"), ("users", "allowed_modules"),
                              ("printer_counters", "cartridge_id_state"),
                              ("printer_counters", "toner_overrides"),
                              ("security_events", "details")):
            with self.subTest(column=f"{table}.{column}"):
                self.assertEqual(self._decls(table).get(column), "TEXT")


class ReaderToleranceTests(unittest.TestCase):
    """``core.database._row_to_dict'' accepts both forms of value."""

    ROW_HEAD = ("1.2.3.4", "PRN", "2026-10-04T00:00:00Z", "PRINT", "msg",
                7, "black", "C1", "info", "A4", "user1")

    def _row(self, details):
        """Helper: row."""
        return self.ROW_HEAD + (details,)

    def test_dict_from_jsonb_is_used_as_is(self):
        """Verifies dict from jsonb is used as is."""
        from core.database import _row_to_dict
        out = _row_to_dict(self._row({"prev_total": 10, "current_total": 12}))
        self.assertEqual(out["details"], {"prev_total": 10, "current_total": 12})
        self.assertEqual(out["pages"], 7)
        self.assertEqual(out["timestamp"], "2026-10-04T00:00:00Z")

    def test_string_from_sqlite_is_parsed(self):
        """Verifies string from sqlite is parsed."""
        from core.database import _row_to_dict
        self.assertEqual(_row_to_dict(self._row('{"prev_total": 10}'))["details"],
                         {"prev_total": 10})

    def test_empty_and_none_yield_empty_dict(self):
        """Verifies empty and none yield empty dict."""
        from core.database import _row_to_dict
        for value in ("", None):
            with self.subTest(value=repr(value)):
                self.assertEqual(_row_to_dict(self._row(value))["details"], {})

    def test_malformed_string_still_raises_on_the_sqlite_path(self):
        """
        The "SQLite behavior did not change" clause: Today, malformed JSON throws an exception in this function.
        If anyone wants to swallow it with ``_load_json_dict'' for ``compatibility'', this test
        Stops — Error swallowing means a screen that silently shows low data.
        """
        from core.database import _row_to_dict
        with self.assertRaises(json.JSONDecodeError):
            _row_to_dict(self._row("{oops"))


@unittest.skipUnless(_PG_OK, _PG_SKIP or "")
class PgColumnTypeTests(MigratedSchemaMixin, unittest.TestCase):
    """Type columns on PG, as migration 0004 made."""

    @staticmethod
    def _type(conn, table, column):
        """Helper: type."""
        return conn.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s "
            "AND column_name = %s", (table, column)).fetchone()

    def test_schema_under_test_is_at_head(self):
        """Verifies schema under test is at head."""
        with _pg_conn(self.dsn()) as conn:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        self.assertEqual(row[0], migration_head(), "اسکیمای ایزوله به سرِ زنجیره نرسیده")

    def test_logs_details_is_jsonb_after_the_migration(self):
        """Verifies logs details is jsonb after the migration."""
        with _pg_conn(self.dsn()) as conn:
            row = self._type(conn, "logs", "details")
        self.assertIsNotNone(row, "جدول logs در اسکیمای ایزوله نیست (alembic اجرا نشده؟)")
        self.assertEqual(row[0], "jsonb")

    def test_the_four_python_side_columns_stay_text(self):
        """Verifies the four python side columns stay text."""
        with _pg_conn(self.dsn()) as conn:
            for table, column in (("users", "allowed_offices"),
                                  ("users", "allowed_modules"),
                                  ("printer_counters", "cartridge_id_state"),
                                  ("printer_counters", "toner_overrides"),
                                  ("security_events", "details")):
                with self.subTest(column=f"{table}.{column}"):
                    row = self._type(conn, table, column)
                    self.assertIsNotNone(row, f"{table}.{column} در اسکیمای ایزوله نیست")
                    self.assertEqual(row[0], "text",
                                     "E.6.4 فقط logs.details را تبدیل کرد")

    def test_helper_fragment_still_evaluates_on_a_jsonb_value(self):
        """
        helper constructs ``(col::jsonb)->>'key''; On the ``jsonb'' column, this is a free cast
        Immutable means — that's what made "intact domain code" possible.
        """
        from core.db import connect_postgres
        conn = connect_postgres(_dsn())
        try:
            frag = conn.json_extract("details", "prev_total", as_number=True)
            self.assertIn("::jsonb", frag)
            row = conn.execute(
                f"SELECT {frag} FROM (SELECT '{{\"prev_total\": 41}}'::jsonb AS details) t"
            ).fetchone()
            self.assertEqual(float(row[0]), 41.0)
        finally:
            conn.close()


@unittest.skipUnless(_PG_OK, _PG_SKIP or "")
class PgAppRoundTripTests(MigratedSchemaMixin, unittest.TestCase):
    """Actual program path: write event ⇒ read row ⇒ same dict on jsonb column."""

    IP = "10.99.99.98"

    def setUp(self):
        # We lock the backend and DSN ourselves (CollationOrderingTests pattern in
        # test_strictness_pg.py): A test that calls the program's top-level functions and
        # It does not create the environment, it can write to the logs.db of the repository.
        """Prepare the fixtures that each test in this class uses."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"DB_BACKEND": "postgresql",
                                               ENV_PG_DSN: self.dsn()})
        patcher.start()
        self.addCleanup(patcher.stop)
        cwd = os.getcwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, cwd)

    def _clear(self):
        """Helper: clear."""
        from core.db import get_connection
        get_connection().execute("DELETE FROM logs WHERE printer_ip = ?", (self.IP,))

    def test_add_event_and_get_log_round_trip(self):
        """Verifies add event and get log round trip."""
        from core import database as db
        self._clear()
        self.addCleanup(self._clear)
        db.add_event(self.IP, "PRINT", {
            "printer_name": "JSONB PROBE", "message": "round trip",
            "prev_total": 3, "current_total": 9, "probe": "e64",
        })
        rows = db.get_log(self.IP, limit=5)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["details"].get("probe"), "e64")
        self.assertEqual(rows[0]["details"].get("prev_total"), 3)
        self.assertEqual(rows[0]["message"], "round trip")
        # E.3 convention that must be maintained on jsonb: table columns on keys
        # payload are won (this IP is not in store.PRINTERS ⇒ column is None, even if
        # ``printer_name'' key should have a value inside the details).
        self.assertIsNone(rows[0]["printer_name"])
        self.assertEqual(rows[0]["details"]["printer_name"], "JSONB PROBE")

    def test_write_rejects_malformed_json_at_insert_time(self):
        """
        Second benefit of the decision: JSON validation at **write** time. Today on the text, one row
        Badformat breaks everything while reading; Here the insert itself is rejected.
        """
        from core.db import get_connection
        conn = get_connection()
        with self.assertRaises(Exception) as ctx:
            conn.execute("INSERT INTO logs (printer_ip, type, timestamp, details) "
                         "VALUES (?, 'PRINT', '2026-10-04T00:00:00Z', ?)",
                         (self.IP, "{oops"))
        self.assertIn("json", str(ctx.exception).lower())


@unittest.skipUnless(_PG_OK, _PG_SKIP or "")
class PreflightSafetyTests(unittest.TestCase):
    """preflight migration: invalid ⇒ stop with count; empty ⇒ warning; Clear ⇒ not rejected."""

    TABLE = "probe_logs_e64"

    #: **temporary** table (pg_temp) — preflight should not build anything in the actual schema.
    def _setup(self, conn, values):
        """Helper: setup."""
        conn.execute(f"CREATE TEMP TABLE {self.TABLE} "
                     f"(id serial primary key, details text)")
        for v in values:
            conn.execute(f"INSERT INTO {self.TABLE} (details) VALUES (%s)", (v,))

    def _teardown(self, conn):
        """Helper: teardown."""
        conn.execute(f"DROP TABLE IF EXISTS {self.TABLE}")  # temp ⇒ session end is sufficient

    def test_malformed_row_aborts_with_count_and_ids(self):
        """Verifies malformed row aborts with count and ids."""
        m = _migration()
        with _pg_conn(_PG_DSN) as conn:
            self._setup(conn, ['{"a":1}', "{oops", "not json", '{"b":2}'])
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    m._preflight(conn, self.TABLE, "details")
            finally:
                self._teardown(conn)
        msg = str(ctx.exception)
        self.assertIn("2 ردیف", msg, "پیام باید بشمارد که چند ردیف خراب است")
        self.assertIn("0004_jsonb_columns", msg)
        self.assertIn("نمونه idها", msg)
        self.assertIn("!~ '^[{[]'", msg, "کوئری راهنما باید روی PG معتبر باشد")

    def test_clean_data_passes(self):
        """Verifies clean data passes."""
        m = _migration()
        with _pg_conn(_PG_DSN) as conn:
            self._setup(conn, ['{"a":1}', "[1,2]", "13", '"str"', "null", None])
            try:
                m._preflight(conn, self.TABLE, "details")
            finally:
                self._teardown(conn)

    def test_empty_string_only_warns_because_it_becomes_null(self):
        """Verifies empty string only warns because it becomes null."""
        m = _migration()
        with _pg_conn(_PG_DSN) as conn:
            self._setup(conn, ["", "   ", '{"a":1}'])
            try:
                with self.assertLogs("alembic.runtime.migration", level="WARNING") as logs:
                    m._preflight(conn, self.TABLE, "details")
            finally:
                self._teardown(conn)
        joined = "\n".join(logs.output)
        self.assertIn("2 ردیف", joined)
        self.assertIn("NULL", joined)

    def test_validator_function_is_dropped_afterwards(self):
        """The pg_temp function should not persist; We did not create "permanent undocumented object" in schema."""
        m = _migration()
        with _pg_conn(_PG_DSN) as conn:
            self._setup(conn, ['{"a":1}'])
            try:
                m._preflight(conn, self.TABLE, "details")
                left = conn.execute(
                    "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname LIKE 'pg_temp%' AND p.proname = 'pg_json_is_valid'").fetchone()[0]
            finally:
                self._teardown(conn)
        self.assertEqual(int(left), 0, "تابع موقتِ اعتبارسنجی پاک نشد")


class MigrationHygieneTests(unittest.TestCase):
    """Structurally, from the lessons of these phases (silent swallowing = the biggest risk of migration)."""

    def test_no_bare_except_or_silent_pass(self):
        """Verifies no bare except or silent pass."""
        src = MIGRATION.read_text(encoding="utf-8")
        lines = src.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("except") and stripped.endswith(":"):
                self.assertNotEqual(lines[i].strip(), "pass",
                                    f"سطر {i + 1}: بلعیدن بی‌صدای استثنا در migration ممنوع")
                self.assertNotEqual(stripped, "except:",
                                    f"سطر {i + 1}: except بدون نوع استثنا")

    def test_sqlite_branch_returns_without_writing(self):
        """
        The SQLite path must be "check and return": no-op log and no op.execute before
        return. (It locks so that someone doesn't accidentally execute ALTER on SQLite as well.)
        """
        import ast
        tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
        upgrade = next(n for n in tree.body
                       if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
        guard = next((n for n in upgrade.body if isinstance(n, ast.If)), None)
        self.assertIsNotNone(guard, "شاخهٔ بررسی backend در upgrade پیدا نشد")
        test = guard.test
        self.assertIsInstance(test, ast.UnaryOp)
        self.assertIsInstance(getattr(test, "operand", None), ast.Call)
        self.assertEqual(test.operand.func.id, "_is_postgres")
        for node in ast.walk(guard):
            if isinstance(node, ast.Attribute) and node.attr == "execute":
                self.fail("در شاخهٔ SQLite نباید op.execute باشد (no-op بودن را نقض می‌کند)")
        self.assertTrue(any(isinstance(n, ast.Return) for n in ast.walk(guard)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
