# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_strictness_pg.py
# @brief:        End-to-end PostgreSQL strictness tests with no silent failures (E.4.10).
# @details:      Responsibilities: Cover boolean strictness, RETURNING ids, schema and timezone
#                handling.
#                Main API: _PgBase, BooleanStrictnessTests, InsertReturningIdTests,
#                SchemaAndTimezoneTests, ExecutescriptTests, SavepointContentsTests
#                Side effects: database access
# @dependencies: internal: core.database, core.db, core.db.schema, tests.migration_graph |
#                external: psycopg
# @notes:        Can be run directly as a script.
# =============================================================================

"""
E.4.10 tests — PostgreSQL hardening, end-to-end and without silent failures.

This file is **E.4 Phase Acceptance Lock**: any functionality built in E.4.1 to E.4.9
Done, this is measured once integrally and on the actual PG.

The express rule of the owner
----------------
"If it is PG, this file should not be skipped." Implementation:
  • The entire module without ``PG_DSN'' is skipped (with a clear reason and execution order);
  • ``NoSilentSkipsTests'' same tests as this file when PG is available
    It runs again and if it sees **even** a skip or an error, the test fails.

Coverage (each item is rooted in one step)
-----------------------------------
| Subject | Step | Evaluation of this file
|---|---|---|
| BOOLEAN columns and hard binding of type | E.4.1 `information_schema` + actual insertion of `bool` and rejection of `int` |
| `insert_returning_id` (RETURNING) | E.4.3 insert in `logs` and read the same `id`
| DDL only from Alembic | E.4.5 `alembic_version` = `0003` and the completeness of the tables
| split `executescript` | E.4.7 Two-statement script on PG |
| SAVEPOINT and continue the transaction E.4.8 Error in a row ⇒ return to SAVEPOINT and continue |
| `json_extract` independent of backend | E.4.9 Reading JSON from `logs` with both |
| Mapping common errors E.4.4 42P01/42703/23505/42701 ⇒ `core.db` classes |
| TimeZone locked | E.2/E.3 | `SHOW TimeZone` = `UTC` |
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.db import (
    BACKEND_POSTGRESQL,
    ENV_PG_DSN,
    ColumnAlreadyExists,
    DatabaseError,
    DatabaseOperationalError,
    UndefinedColumn,
    UndefinedTable,
    UniqueViolation,
    connect_postgres,
)
from core.db.schema import BOOLEAN_COLUMNS, APP_TABLES, present_tables
from tests.migration_graph import migration_head

ROOT = Path(__file__).resolve().parent.parent
LOG_IP = "203.0.113.150"


def _pg_target():
    """``(dsn, None)'' if PG is available, otherwise ``(None, skip reason)''."""
    dsn = (os.getenv(ENV_PG_DSN, "") or "").strip()
    if not dsn:
        return None, (f"{ENV_PG_DSN} ست نشده است ⇒ کل فایل سخت‌گیری روی PG اجرا نمی‌شود "
                      f"(اجرا با: PG_DSN='host=… dbname=… user=… password=…' pytest "
                      f"tests/test_strictness_pg.py)")
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        return None, (f"PG در دسترس نیست ({type(exc).__name__}: "
                      f"{str(exc).strip().splitlines()[0][:120]}) ⇒ سخت‌گیری PG رد می‌شود")
    return dsn, None


_PG_DSN, _PG_SKIP = _pg_target()
TEMPLATE_TABLES = ("strictness_t",)


# ─────────────────────────────────────────────────────────────────────
# base
# ─────────────────────────────────────────────────────────────────────
@unittest.skipUnless(_PG_DSN, _PG_SKIP or "")
class _PgBase(unittest.TestCase):
    """PG connection + temporary table + cleanup (without relying on existing data)."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        if not _PG_DSN:
            self.skipTest(_PG_SKIP or "PG در دسترس نیست")
        self.conn = connect_postgres(_PG_DSN)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        """Helper: cleanup."""
        try:
            self.conn.rollback()
            for table in TEMPLATE_TABLES:
                self.conn.execute(f"DROP TABLE IF EXISTS {table}")
            self.conn.execute("DELETE FROM logs WHERE printer_ip = ?", (LOG_IP,))
            self.conn.commit()
        except Exception:                     # Cleaning should not invalidate the result
            pass
        try:
            self.conn.close()
        except Exception:
            pass

    def _expect_error(self, cls, sql, params=()):
        """execution of the statement expecting an error; The transaction is rolled back after an error."""
        try:
            with self.assertRaises(cls) as ctx:
                self.conn.execute(sql, params)
            return ctx.exception
        finally:
            self.conn.rollback()


# ─────────────────────────────────────────────────────────────────────
# 1) BOOLEAN columns and hard connection type (E.4.1)
# ─────────────────────────────────────────────────────────────────────
class BooleanStrictnessTests(_PgBase):
    """On PG the columns are really BOOLEAN and do not accept ``int''."""

    def test_all_five_columns_are_boolean(self):
        """Verifies all five columns are boolean."""
        rows = self.conn.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND data_type = 'boolean' "
            "ORDER BY table_name, column_name").fetchall()
        found = {(t, c) for t, c, _d in rows}
        self.assertEqual(found, set(BOOLEAN_COLUMNS))

    def test_int_is_rejected_by_a_boolean_column(self):
        """``yield_samples.accepted'' rejects an ``int'' value (type hard binding)."""
        exc = self._expect_error(
            DatabaseError,
            "INSERT INTO yield_samples (printer_ip, color, created_at, accepted) "
            "VALUES (?, ?, ?, ?)",
            (LOG_IP, "black", "2026-10-04T00:00:00.000000Z", 1))
        self.assertEqual(exc.sqlstate, "42804", str(exc))     # datatype_mismatch
        self.assertIn("boolean", str(exc))
        self.assertIs(type(exc), DatabaseError,
                      "42804 عمداً ترجمه نمی‌شود؛ خطای نوع، خطای integrity نیست")

    def test_bool_is_accepted_and_read_back_as_boolean(self):
        """``True/False'' goes real and returns boolean."""
        try:
            self.conn.execute(
                "INSERT INTO yield_samples (printer_ip, color, created_at, accepted) "
                "VALUES (?, ?, ?, ?)",
                (LOG_IP, "black", "2026-10-04T00:00:00.000000Z", True))
            self.conn.commit()
            value = self.conn.execute(
                "SELECT accepted FROM yield_samples WHERE printer_ip = ?",
                (LOG_IP,)).fetchone()[0]
            self.assertIs(value, True)
        finally:
            self.conn.rollback()
            self.conn.execute("DELETE FROM yield_samples WHERE printer_ip = ?", (LOG_IP,))
            self.conn.commit()


# ─────────────────────────────────────────────────────────────────────
# 2) insert_returning_id (E.4.3)
# ─────────────────────────────────────────────────────────────────────
class InsertReturningIdTests(_PgBase):
    """``lastrowid'' has no meaning on PG; ``insert_returning_id'' is the placeholder."""

    def test_insert_returns_the_new_id(self):
        """Verifies insert returns the new id."""
        new_id = self.conn.insert_returning_id(
            "INSERT INTO logs (printer_ip, timestamp, type, message) VALUES (?, ?, ?, ?)",
            (LOG_IP, "2026-10-04T00:00:00.000000Z", "PRINT", "strictness"))
        self.conn.commit()
        self.assertIsInstance(new_id, int, f"id برگشتی: {new_id!r}")
        fetched = self.conn.execute("SELECT id FROM logs WHERE printer_ip = ?",
                                    (LOG_IP,)).fetchone()[0]
        self.assertEqual(fetched, new_id, "id برگشتی باید همان ردیف درج‌شده باشد")

    def test_id_is_generated_by_identity(self):
        """``logs.id'' on the PG is the IDENTITY column (E.4.2) and creates a value by itself."""
        kind = self.conn.execute(
            "SELECT is_identity FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'logs' "
            "AND column_name = 'id'").fetchone()[0]
        self.assertEqual(kind, "YES")


# ─────────────────────────────────────────────────────────────────────
# 3) DDL only from Alembic (E.4.5) and TimeZone locked (E.2/E.3)
# ─────────────────────────────────────────────────────────────────────
class SchemaAndTimezoneTests(_PgBase):
    """The PG schema is made by Alembic and the session is locked to UTC."""

    def test_revision_is_the_migration_head(self):
        """
        The executed schema must be on the **real head** of the chain.

        The number was not hardcoded (it used to be ``0003'' and was broken with every migration):
        The head is calculated from ``alembic/versions'' files, so this lock is always
        Tests "migrations all applied" — also from 0004_jsonb_columns (E.6.4).
        """
        revision = self.conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        self.assertEqual(revision, migration_head(),
                         "اسکیما روی سرِ migrationها نیست (`alembic upgrade head` runs)")

    def test_all_app_tables_exist(self):
        """Verifies all app tables exist."""
        missing = [t for t in APP_TABLES if t not in present_tables(self.conn)]
        self.assertEqual(missing, [], "اسکیمای Alembic کامل نیست")

    def test_session_timezone_is_utc(self):
        """Verifies session timezone is utc."""
        self.assertEqual(self.conn.execute("SHOW TimeZone").fetchone()[0], "UTC")

    def test_timestamp_is_text_and_details_is_jsonb_as_documented(self):
        """
        Column type convention on PG:

        • ``timestamp'' is still ``text'' (E.1/E.3 — **not** TIMESTAMPTZ; sit and
          string format are locked in ``docs/timezone-contract.md'');
        • ``details'' is ``jsonb'' from E.6.4 (migration ``0004'') — only JSON column
          on which SQL reads content; The rest of the JSON columns (``users.allowed_*'',
          ``printer_counters.cartridge_id_state``/``toner_overrides``,
          ``security_events.details'') are still ``text'' and in
          ``tests/test_jsonb_columns.py'' are locked.
        """
        rows = dict(self.conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'logs' "
            "AND column_name IN ('timestamp', 'details')").fetchall())
        self.assertEqual(rows, {"timestamp": "text", "details": "jsonb"})


# ─────────────────────────────────────────────────────────────────────
# 4) split executescript (E.4.7)
# ─────────────────────────────────────────────────────────────────────
class ExecutescriptTests(_PgBase):
    """Multi-statement script is executed on PG (divided into statements)."""

    def test_two_statement_script_creates_both_tables(self):
        """Verifies two statement script creates both tables."""
        self.conn.executescript(
            "CREATE TABLE strictness_t (id integer primary key, name text);"
            "INSERT INTO strictness_t (id, name) VALUES (1, 'a');"
        )
        count = self.conn.execute("SELECT COUNT(*) FROM strictness_t").fetchone()[0]
        self.assertEqual(count, 1)

    def test_multi_command_with_params_is_why_we_split(self):
        """
        Reason for split: psycopg does not accept multiple statements with a non-empty parameter
        (``cannot insert multiple commands into a prepared statement``).
        This test keeps that reason alive.
        """
        exc = self._expect_error(
            DatabaseError,
            "CREATE TABLE strictness_t (id integer); SELECT ?",
            (1,))
        self.assertIn("multiple commands", str(exc).lower(), str(exc))


# ─────────────────────────────────────────────────────────────────────
# 5) SAVEPOINT in import (E.4.8)
# ─────────────────────────────────────────────────────────────────────
class SavepointContentsTests(_PgBase):
    """On PG a single row error only goes back to the SAVEPOINT and the transaction survives."""

    def test_savepoint_keeps_the_transaction_usable(self):
        """Verifies savepoint keeps the transaction usable."""
        self.conn.execute("CREATE TABLE strictness_t (ip text PRIMARY KEY, n integer)")
        self.conn.execute("INSERT INTO strictness_t (ip, n) VALUES (?, ?)", ("a", 1))
        self.conn.commit()

        # Duplicate row: Without SAVEPOINT, the entire transaction would have been aborted (25P02).
        self.conn.execute("SAVEPOINT sp_row")
        try:
            self.conn.execute("INSERT INTO strictness_t (ip, n) VALUES (?, ?)", ("a", 2))
            self.fail("درج تکراری باید خطا بدهد")
        except UniqueViolation as exc:
            self.assertEqual(exc.sqlstate, "23505")
            self.conn.execute("ROLLBACK TO SAVEPOINT sp_row")
        finally:
            self.conn.execute("RELEASE SAVEPOINT sp_row")

        self.conn.execute("INSERT INTO strictness_t (ip, n) VALUES (?, ?)", ("b", 3))
        self.conn.commit()
        rows = self.conn.execute("SELECT ip FROM strictness_t ORDER BY ip").fetchall()
        self.assertEqual([r[0] for r in rows], ["a", "b"],
                         "ردیف سالم بعد از خطای ردیف تکراری باید ثبت شود")

    def test_without_savepoint_the_transaction_dies(self):
        """Reverse Witness: Without SAVEPOINT the same scenario loses the transaction."""
        self.conn.execute("CREATE TABLE strictness_t (ip text PRIMARY KEY, n integer)")
        self.conn.execute("INSERT INTO strictness_t (ip, n) VALUES (?, ?)", ("a", 1))
        self.conn.commit()

        try:
            self.conn.execute("INSERT INTO strictness_t (ip, n) VALUES (?, ?)", ("a", 2))
        except UniqueViolation:
            pass
        exc = self._expect_error(
            DatabaseError,
            "INSERT INTO strictness_t (ip, n) VALUES (?, ?)", ("b", 3))
        self.assertEqual(exc.sqlstate, "25P02", str(exc))


# ─────────────────────────────────────────────────────────────────────
# 6) json_extract independent of backend (E.4.9)
# ─────────────────────────────────────────────────────────────────────
class JsonExtractStrictnessTests(_PgBase):
    """JSON snippet on PG returns text and number correctly."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        super().setUp()
        self.conn.execute(
            "INSERT INTO logs (printer_ip, timestamp, type, details) VALUES (?, ?, ?, ?)",
            (LOG_IP, "2026-10-04T00:00:00.000000Z", "PRINT",
             '{"prev_total": 41, "detection": "snmp", "note": "متن"}'))
        self.conn.commit()

    def test_text_and_number_fragments(self):
        """Verifies text and number fragments."""
        text_frag = self.conn.json_extract("details", "detection")
        num_frag = self.conn.json_extract("details", "prev_total", as_number=True)
        self.assertEqual(self.conn.execute(
            f"SELECT {text_frag} FROM logs WHERE printer_ip = ?", (LOG_IP,)).fetchone()[0],
            "snmp")
        self.assertEqual(self.conn.execute(
            f"SELECT {num_frag} FROM logs WHERE printer_ip = ?", (LOG_IP,)).fetchone()[0],
            41)

    def test_invalid_json_is_rejected_at_write_time(self):
        """
        Change E.6.4: On PG the corrupted JSON error was moved to **write time**.

        Before 0004, the column was ``text'' and this test played a malformed row
        Expected ``SELECT'' to explode with error ``22P02'' (ie a bad row, query
        aggregate breaks the one that has nothing to do with that row). Now ``jsonb''
        It rejects that it is written ⇒ neither ``22P02'' in reading, nor data
        Semi-finished on the table. On SQLite, the same reading behavior remains
        (``json_extract'' on the malformed string) and in
        ``tests/test_jsonb_columns.py::ReaderToleranceTests'' locked.
        """
        exc = self._expect_error(
            DatabaseError,
            "INSERT INTO logs (printer_ip, timestamp, type, details) VALUES (?, ?, ?, ?)",
            (LOG_IP, "2026-10-04T00:00:01.000000Z", "PRINT", "{not json"))
        self.assertEqual(exc.sqlstate, "22P02", str(exc))
        left = self.conn.execute(
            "SELECT count(*) FROM logs WHERE printer_ip = ? AND details::text LIKE ?",
            (LOG_IP, "{not json%")).fetchone()[0]
        self.assertEqual(left, 0, "ردیفِ ردشده نباید باقی مانده باشد")
        # And the same reading query (which burned with 22P02 before E.6.4) now runs without error.
        # becomes; The witness of this test is **running**, not the count value (rows of tests
        # The same class is in the table).
        frag = self.conn.json_extract("details", "prev_total", as_number=True)
        counted = self.conn.execute(
            f"SELECT count(*) FROM logs WHERE {frag} IS NOT NULL").fetchone()[0]
        self.assertGreaterEqual(int(counted), 0)


# ─────────────────────────────────────────────────────────────────────
# 6.5) The order regardless of the upper/lower case of the username (row 3 of the strict document)
# ─────────────────────────────────────────────────────────────────────
class CollationOrderingTests(unittest.TestCase):
    """
    ``list_users'' used to have ``COLLATE NOCASE'': the SQLite syntax used on PG with
    ``42704 collation "nocase" does not exist'' breaks — and because the error function
    takes, "[]" returns silently. It is now ``lower(username)'' (E.4.11a).

    **Hermeticity is mandatory:** This tests application-level functions (``create_user`` /
    calls ``list_users'') which reads the backend from ``DB_BACKEND''; so
    Both environment variable is locked on PG and ``cwd'' is moved to a temporary folder. without
    Both, a silent fallback to SQLite could write to the **logs.db" repository**
    (The same thing happened once; the file was restored with sha256 witness E.3).
    """

    NAMES = ("zz_alpha", "zz_Bravo", "zz_charlie")

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        if not _PG_DSN:
            self.skipTest(_PG_SKIP or "PG در دسترس نیست")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)                  # Even the fallback does not reach the repository
        self.addCleanup(lambda: os.chdir(self.old_cwd))
        self.env = mock.patch.dict(os.environ, {
            "DB_BACKEND": "postgresql", ENV_PG_DSN: _PG_DSN})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = connect_postgres(_PG_DSN)
        # Registry order is important: addCleanup runs in reverse, so first close the registry connection
        # and then clearing the rows — otherwise clearing on the execution package connection
        # and it fails silently (the same error happened once).
        self.addCleanup(self.conn.close)
        self.addCleanup(self._delete_users)

    def _delete_users(self):
        """Helper: delete users."""
        try:
            self.conn.rollback()
            for name in self.NAMES:
                self.conn.execute("DELETE FROM users WHERE username = ?", (name,))
            self.conn.commit()
        except Exception:
            pass

    def _seed(self):
        """Helper: seed."""
        from core.database import create_user
        for name in self.NAMES:
            # ``create_user'' takes a hash (not a raw password) — the same convention as the data layer.
            user = create_user(username=name, email=f"{name}@strictness.test",
                               password_hash="x" * 32, role="viewer", is_verified=True)
            self.assertIsNotNone(user, f"ساخت کاربر {name} ناموفق بود")
        # Anti-fallback lock: Rows must be in the **same** PG.
        for name in self.NAMES:
            found = self.conn.execute("SELECT 1 FROM users WHERE username = ?",
                                      (name,)).fetchone()
            self.assertIsNotNone(found, f"کاربر {name} در PG نیست (fallback بی‌صدا؟)")

    def test_ordering_is_case_insensitive_and_not_silently_empty(self):
        """Verifies ordering is case insensitive and not silently empty."""
        from core.database import list_users
        self._seed()
        rows = list_users()
        self.assertTrue(rows, "روی PG نباید بی‌صدا «[]» برگردد (باگ خطای collation)")
        ours = [r["username"] for r in rows if r["username"].startswith("zz_")]
        self.assertEqual(ours, ["zz_alpha", "zz_Bravo", "zz_charlie"],
                         "ترتیب ASCII بی‌توجه به بزرگی/کوچکی (مثل NOCASE)")


# ─────────────────────────────────────────────────────────────────────
# 7) Mapping common errors (E.4.4)
# ─────────────────────────────────────────────────────────────────────
class SharedErrorMappingTests(_PgBase):
    """The same mapping table as E.4.4, this time as an acceptance lock."""

    def test_mapping_table_on_live_pg(self):
        """Verifies mapping table on live pg."""
        cases = (
            (UndefinedTable, "SELECT * FROM no_such_table_strictness", "42P01"),
            (UndefinedColumn, "SELECT no_such_column FROM logs", "42703"),
            (UniqueViolation,
             "INSERT INTO yield_samples (printer_ip, color, created_at, accepted) "
             "VALUES (NULL, 'black', '2026-10-04T00:00:00.000000Z', true)", "23502"),
            (ColumnAlreadyExists, "ALTER TABLE logs ADD COLUMN printer_ip text", "42701"),
        )
        for cls, sql, sqlstate in cases:
            with self.subTest(sql=sql.split()[0], expected=sqlstate):
                exc = self._expect_error(cls, sql)
                self.assertEqual(exc.sqlstate, sqlstate)
                self.assertEqual(exc.backend, BACKEND_POSTGRESQL)
                self.assertIsNotNone(exc.original)

    def test_all_shared_errors_are_one_hierarchy(self):
        """Verifies all shared errors are one hierarchy."""
        for cls in (UndefinedTable, UndefinedColumn, UniqueViolation,
                    ColumnAlreadyExists, DatabaseOperationalError):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, DatabaseError))


class SkipReasonContractTests(unittest.TestCase):
    """
    The skip message convention — has meaning on both execution modes (so gete doesn't get stuck).

    • Without PG: the message must clearly state the name of the environment variable and the execution command;
    • With PG: there should be no reason for skip (``_PG_SKIP is None'').
    """

    def test_either_pg_is_available_or_the_skip_reason_is_actionable(self):
        """Verifies either pg is available or the skip reason is actionable."""
        if _PG_DSN:
            self.assertIsNone(_PG_SKIP, "وقتی PG در دسترس است، دلیلی برای skip نیست")
            return
        self.assertIn(ENV_PG_DSN, _PG_SKIP or "")
        self.assertIn("PG_DSN=", _PG_SKIP or "")


# ─────────────────────────────────────────────────────────────────────
# 8) without silent failure (Owner's express rule)
# ─────────────────────────────────────────────────────────────────────
@unittest.skipUnless(_PG_DSN, _PG_SKIP or "")
class NoSilentSkipsTests(unittest.TestCase):
    """
    When there is PG, no test in this file should be skipped.

    The list of classes is **explicit** (not the module scan) until this test reruns itself
    Don't do it and don't make endless returns.
    """

    #: classes to run on real PG (without skip guard).
    REQUIRED_CLASSES = (
        BooleanStrictnessTests,
        InsertReturningIdTests,
        SchemaAndTimezoneTests,
        ExecutescriptTests,
        SavepointContentsTests,
        JsonExtractStrictnessTests,
        CollationOrderingTests,
        SharedErrorMappingTests,
    )

    def test_no_skips_or_errors_when_pg_is_available(self):
        """Verifies no skips or errors when pg is available."""
        import unittest as _unittest

        import io as _io

        loader = _unittest.TestLoader()
        suite = _unittest.TestSuite(
            loader.loadTestsFromTestCase(cls) for cls in self.REQUIRED_CLASSES)
        output = _io.StringIO()
        result = _unittest.TextTestRunner(verbosity=2, stream=output).run(suite)

        skipped = [str(t) for t, _r in getattr(result, "skipped", [])]
        failures = [str(t) for t, _tb in result.failures]
        errors = [str(t) for t, _tb in result.errors]
        # The output of the internal execution is in the failure message so that the immediate cause can be seen.
        self.assertNotIn("None", "")   # Hold output for messages
        if failures or errors:
            raise AssertionError(
                "اجرای دوباره خطا داد:\n" + output.getvalue()[-4000:])
        self.assertEqual(skipped, [], "با وجود PG هیچ تستی نباید skip شود")
        self.assertEqual(failures, [], "خطای تست در اجرای دوباره")
        self.assertEqual(errors, [], "استثنا در اجرای دوباره")
        self.assertGreater(result.testsRun, 0, "هیچ تستی اجرا نشد؟")

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
