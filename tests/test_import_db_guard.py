# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_import_db_guard.py
# @brief:        Tests for the database import guard on PostgreSQL (E.4.8).
# @details:      Responsibilities: Check savepoint-per-row import and the boolean guard.
#                Main API: _Rows, _RecordingAdapter, _ScriptedAdapter, SqliteImportBehaviourTests,
#                PostgresSavepointStubTests, ImportGuardUnitTests
#                Side effects: database access, file I/O
# @dependencies: internal: core.database, core.db, core.db.schema, models, web, web.routes |
#                external: flask, psycopg, pytest
# @notes:        Can be run directly as a script. PostgreSQL-live tests skip automatically when no
#                server is reachable.
# =============================================================================

"""
E.4.8 tests — import on PostgreSQL: SAVEPOINT every row + integrated guard + boolean.

The four rules that lock this file
------------------------------------
1) **SAVEPOINT EACH ROW (only on PG).** On PostgreSQL first insert error, total
   It aborts the transaction (``25P02'') and the healthy rows of the same category are lost
   they go Assay E.4.4 showed a three-row stack with a repeat to
   ``rows_now=0'' is reached. So each row is inserted in ``SAVEPOINT sp_row'' itself
   The error with ``ROLLBACK TO SAVEPOINT sp_row'' is limited to the same row.
   On SQLite **no SAVEPOINT is opened** (today's behavior, byte by byte).

2) **Unified endpoint guard.** Owner's decision (E.4.8): instead of several scattered guards,
   ``_reject_import_on_postgres()'' as the first ``confirm_import'' task of all
   Closes import routes on PG with an ``UnsupportedFeatureError''
   (TD-007 + TD-009 + line 11 strictness, all three are fixed in E.6).
   On SQLite the guard is **not enabled** and today's behavior is intact.
   There is no ON CONFLICT test in this file — that's E.6 work.

3) **Boolean normalization.** ``yield_samples.accepted'' on PG has type ``BOOLEAN''
   (migration 0002, E.4.1) and rejects ``int'' with a type error; The starting value
   (SQLite file, ``0/1'') is converted to ``bool'' and ``NULL'' before insertion
   It remains intact.

4) **The ``analyze'' path is not blocked** (it only reads the source file and the source always
   SQLite) — the guard is only on the endpoint of the import execution.

Without ``PG_DSN'', live PG classes are skipped with clear message (the rest are executed).
"""
import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from core.db import (
    BACKEND_POSTGRESQL,
    BACKEND_SQLITE,
    ENV_PG_DSN,
    DatabaseError,
    UniqueViolation,
    UnsupportedFeatureError,
    connect_postgres,
    connect_sqlite,
)
from core.db.schema import BOOLEAN_COLUMNS, boolean_columns_of, schema_columns
from web.routes import import_db

ROOT = Path(__file__).resolve().parent.parent
# Decision E.6.1: Guard message should announce "decision", not "deadline" — these four signs
# That's what a system administrator needs to figure out the cause and workaround.
GUARD_MARKERS = ("E.6.1", "TD-007", "TD-009", "ردیف ۱۱")


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


# ─────────────────────────────────────────────────────────────────────
# Test tool
# ─────────────────────────────────────────────────────────────────────
class _Rows:
    """Fake result: It has both ``fetchall'' and ``fetchone'' (like cursor driver)."""

    def __init__(self, rows):
        """Initialize the instance."""
        self._rows = list(rows)

    def fetchall(self):
        """Helper: fetchall."""
        return list(self._rows)

    def fetchone(self):
        """Helper: fetchone."""
        return self._rows[0] if self._rows else None

    def __iter__(self):
        """Iterate over the items."""
        return iter(self._rows)


class _RecordingAdapter:
    """
    A thin overlay on an actual connection (adapter) that records SQLs.

    To test "No SAVEPOINT opens on SQLite" — without touching the
    Module's private helpers.
    """

    def __init__(self, conn):
        """Initialize the instance."""
        self._conn = conn
        self.sql = []

    @property
    def backend(self):
        """Helper: backend."""
        return self._conn.backend

    def execute(self, sql, params=()):
        """Helper: execute."""
        self.sql.append(sql)
        return self._conn.execute(sql, params)


class _ScriptedAdapter:
    """
    A dummy PG connection that returns specific rows for each SQL pattern
    (no server). ``fail_by_needle'' can throw a specified exception on the given pattern.
    """

    backend = BACKEND_POSTGRESQL

    def __init__(self, rows_by_needle=None, fail_by_needle=None):
        """Initialize the instance."""
        self.rows_by_needle = dict(rows_by_needle or {})
        self.fail_by_needle = dict(fail_by_needle or {})
        self.log = []           # (sql, params)

    def execute(self, sql, params=()):
        """Helper: execute."""
        self.log.append((sql, params))
        for needle, exc in self.fail_by_needle.items():
            if needle in sql:
                raise exc
        for needle, rows in self.rows_by_needle.items():
            if needle in sql:
                return _Rows(rows)
        return _Rows([])

    def commit(self):
        """Helper: commit."""
        pass

    def rollback(self):
        """Helper: rollback."""
        pass

    def close(self):
        """Helper: close."""
        pass

    # ── assistants ──
    def statements(self):
        """Helper: statements."""
        return [sql for sql, _params in self.log]


def _unique_violation(message="duplicate key value violates unique constraint"):
    """Helper: unique violation."""
    return UniqueViolation(message, backend=BACKEND_POSTGRESQL, sqlstate="23505")


def _make_source_db(tmpdir, table="printer_counters", ddl=None, rows=()):
    """Creates the source SQLite file (the same one that import uploads)."""
    path = os.path.join(tmpdir, "source.db")
    raw = sqlite3.connect(path)
    try:
        raw.execute(ddl or f"CREATE TABLE {table} (ip TEXT PRIMARY KEY, print_total INTEGER)")
        for row in rows:
            raw.execute(
                f"INSERT INTO {table} VALUES ({','.join(['?'] * len(row))})", row)
        raw.commit()
    finally:
        raw.close()
    return path


def _new_admin_user(username):
    """An admin creates an active database (temporary SQLite or PG)."""
    from models import User
    return User.create(username=username, email=f"{username}@test.io",
                       password="secret123", role="admin", is_verified=True)


def _login(client, user):
    """Helper: login."""
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


# ─────────────────────────────────────────────────────────────────────
# A) SQLite — today's behavior is intact
# ─────────────────────────────────────────────────────────────────────
class SqliteImportBehaviourTests(unittest.TestCase):
    """The SQLite path does not take a SAVEPOINT or change a boolean."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_replace_mode_still_works_on_sqlite(self):
        """The replace mode on SQLite does the same as usual (without guard)."""
        target = connect_sqlite(os.path.join(self.tmp.name, "target.db"))
        self.addCleanup(target.close)
        target.execute("CREATE TABLE printer_counters (ip TEXT PRIMARY KEY, print_total INTEGER)")
        target.execute("INSERT INTO printer_counters (ip, print_total) VALUES (?, ?)",
                       ("203.0.113.10", 100))
        target.commit()

        source = connect_sqlite(_make_source_db(
            self.tmp.name, rows=[("203.0.113.10", 555), ("203.0.113.11", 7)]))
        self.addCleanup(source.close)

        result = import_db._import_table(source, target, "printer_counters", {}, "replace")
        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["skipped"], 0)

        got = dict(target.execute(
            "SELECT ip, print_total FROM printer_counters").fetchall())
        self.assertEqual(got["203.0.113.10"], 555, "ردیف قبلی باید جایگزین شده باشد")
        self.assertEqual(got["203.0.113.11"], 7)

    def test_sqlite_never_opens_savepoints(self):
        """No ``SAVEPOINT'' is opened on SQLite (E.4.8 behavior is PG only)."""
        target = connect_sqlite(os.path.join(self.tmp.name, "target2.db"))
        self.addCleanup(target.close)
        target.execute("CREATE TABLE printer_counters (ip TEXT PRIMARY KEY, print_total INTEGER)")
        recording = _RecordingAdapter(target)

        source = connect_sqlite(_make_source_db(
            self.tmp.name, table="printer_counters",
            rows=[("203.0.113.20", 1), ("203.0.113.21", 2)]))
        self.addCleanup(source.close)

        result = import_db._import_table(
            source, recording, "printer_counters", {}, "skip")
        self.assertEqual(result["imported"], 2)
        savepoints = [s for s in recording.sql if "SAVEPOINT" in s.upper()]
        self.assertEqual(savepoints, [], f"روی SQLite نباید SAVEPOINT باشد: {savepoints}")


# ─────────────────────────────────────────────────────────────────────
# B) PostgreSQL with dummy connection (no server) — SAVEPOINT
# ─────────────────────────────────────────────────────────────────────
class PostgresSavepointStubTests(unittest.TestCase):
    """SAVEPOINT/RELEASE sequence and limit the error to one row, on the PG path."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = connect_sqlite(_make_source_db(
            self.tmp.name, rows=[("203.0.113.30", 3), ("203.0.113.31", 4)]))
        self.addCleanup(self.source.close)
        # Dummy PG destination: its column list comes from information_schema, so
        # The same cross-backend helper works (the E.4.8 helper prepared the same for E.6).
        self._columns_patch = mock.patch.object(
            import_db, "_columns",
            side_effect=lambda conn, table: schema_columns(conn, table))
        self._columns_patch.start()
        self.addCleanup(self._columns_patch.stop)

    def test_plain_insert_uses_one_savepoint_per_row_on_pg(self):
        """On the PG of each row: ``SAVEPOINT'' ⇒ ``INSERT'' ⇒ ``RELEASE'' (in the same order)."""
        target = _ScriptedAdapter(rows_by_needle={
            "information_schema.columns": [("ip",), ("print_total",)],
            "SELECT 1 FROM": [],                      # dedupe: The row does not exist
        })
        result = import_db._import_table(self.source, target, "printer_counters", {}, "skip")
        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["skipped"], 0)

        sqls = target.statements()
        self.assertEqual([s for s in sqls if "INSERT OR REPLACE" in s.upper()], [])
        inserts = [i for i, s in enumerate(sqls) if s.upper().startswith("INSERT INTO")]
        self.assertEqual(len(inserts), 2, sqls)
        for i in inserts:
            self.assertEqual(sqls[i - 1].upper(), "SAVEPOINT SP_ROW", sqls)
            self.assertIn("RELEASE SAVEPOINT SP_ROW", sqls[i + 1].upper(), sqls)
        self.assertEqual(len([s for s in sqls if "SAVEPOINT" in s.upper()]), 4)

    def test_duplicate_row_does_not_stop_the_batch_on_pg(self):
        """The duplicate row returns only its own SAVEPOINT; The next rows go."""
        target = _ScriptedAdapter(
            rows_by_needle={"information_schema.columns": [("ip",), ("print_total",)],
                            "SELECT 1 FROM": []},
            fail_by_needle={"INSERT INTO": _unique_violation()})
        result = import_db._import_table(self.source, target, "printer_counters", {}, "skip")
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["skipped"], 2, "هر دو ردیف تکراری‌اند ⇒ هر دو skip")

        sqls = [s.upper() for s in target.statements()]
        self.assertEqual(len([s for s in sqls if s == "ROLLBACK TO SAVEPOINT SP_ROW"]), 2)
        # No error is thrown from the loop and every SAVEPOINT is closed.
        self.assertEqual(len([s for s in sqls if s == "SAVEPOINT SP_ROW"]),
                         len([s for s in sqls if s.startswith("RELEASE SAVEPOINT")]))


# ─────────────────────────────────────────────────────────────────────
# C) Integrated guard (no DB)
# ─────────────────────────────────────────────────────────────────────
class ImportGuardUnitTests(unittest.TestCase):
    """``_reject_import_on_postgres'' only comes up on PG and the message has help."""

    def test_guard_raises_on_postgresql(self):
        """Verifies guard raises on postgresql."""
        with mock.patch.dict(os.environ, {"DB_BACKEND": "postgresql"}):
            with self.assertRaises(UnsupportedFeatureError) as ctx:
                import_db._reject_import_on_postgres()
        message = str(ctx.exception)
        for marker in GUARD_MARKERS:
            self.assertIn(marker, message, f"پیام گارد باید «{marker}» را داشته باشد")
        self.assertEqual(ctx.exception.backend, BACKEND_POSTGRESQL)
        self.assertIsInstance(ctx.exception, DatabaseError)
        self.assertIn("SQLite", message, "پیام باید مسیر سالم را هم روشن کند")

    def test_guard_message_states_retirement_not_a_deadline(self):
        """
        The guard message after decision E.6.1 should say "Retire" and safe routes.

        Why this test: Before E.6.1 the message was "not supported until E.6" - one
        Timeout, which after E.6 could still look right and delay the user.
        """
        with mock.patch.dict(os.environ, {"DB_BACKEND": "postgresql"}):
            with self.assertRaises(UnsupportedFeatureError) as ctx:
                import_db._reject_import_on_postgres()
        message = str(ctx.exception)
        self.assertIn("بازنشستگی", message, "پیام باید بگوید این مسیر **ترجمه نمی‌شود**")
        self.assertIn("analyze", message, "پیام باید بگوید پیش‌نمایش فایل کار می‌کند")
        self.assertNotIn("تا E.6 ", message, "مهلتِ منقضی‌شده نباید در پیام بماند")

    def test_guard_is_silent_on_sqlite(self):
        """Verifies guard is silent on sqlite."""
        for value in ("sqlite", "", "postgres", "pg"):     # Two alternative PG names are also tested
            with self.subTest(DB_BACKEND=value):
                with mock.patch.dict(os.environ, {"DB_BACKEND": value}):
                    if value in ("postgres", "pg"):
                        with self.assertRaises(UnsupportedFeatureError):
                            import_db._reject_import_on_postgres()
                    else:
                        self.assertIsNone(import_db._reject_import_on_postgres())

    def test_guard_is_the_first_statement_of_confirm_import(self):
        """Guard must be run **before** any file/write check (owner note)."""
        import ast
        tree = ast.parse((ROOT / "web" / "routes" / "import_db.py").read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "confirm_import")
        # A leading docstring is not executable code; the guard must be the first real statement.
        body = fn.body[1:] if ast.get_docstring(fn) else fn.body
        first = body[0]
        self.assertIsInstance(first, ast.Try, "نخستین دستور باید بلوک گارد باشد")
        called = [n.func.id for n in ast.walk(first)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        self.assertIn("_reject_import_on_postgres", called)

    def test_guard_does_not_touch_the_analyze_endpoint(self):
        """``analyze'' only reads the source ⇒ must not have guards."""
        source = (ROOT / "web" / "routes" / "import_db.py").read_text(encoding="utf-8")
        analyze = source.split("def analyze_db()", 1)[1].split("@bp.route", 1)[0]
        self.assertNotIn("_reject_import_on_postgres", analyze)


# ─────────────────────────────────────────────────────────────────────
# C2) origin is always SQLite — backend lock on connect_to (decision E.6.1)
# ─────────────────────────────────────────────────────────────────────
class SqliteSourceUnderPostgresEnvTests(unittest.TestCase):
    """
    ``analyze'' should not look at ``DB_BACKEND'': what is being opened is a **file**.

    Before E.6.1, ``connect_to'' reads the backend from the environment and below
    ``DB_BACKEND=postgresql'' ``connect()'' itself with ``ConfigurationError''
    ("path with postgresql") was rejected ⇒ endpoint with 500 "error in database analysis"
    would return This class tests the same without the need for a live PG — and because
    leaves ``PG_DSN'' empty, any return of the lock breaks the **loud** test.
    """

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"DB_BACKEND": "postgresql", ENV_PG_DSN: ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_connect_to_opens_the_sqlite_file_under_postgres_env(self):
        """Verifies connect to opens the sqlite file under postgres env."""
        from core.db import connect_to
        path = _make_source_db(self.tmp.name, table="logs", ddl=(
            "CREATE TABLE logs (printer_ip TEXT, timestamp TEXT, type TEXT, message TEXT)"),
            rows=[("10.9.9.20", "2026-10-04T00:00:00.000000Z", "PRINT", "x")])
        conn = connect_to(path, timeout=5.0)
        self.addCleanup(conn.close)
        self.assertEqual(conn.backend, BACKEND_SQLITE,
                         "فایل SQLite باید با adapter SQLite باز شود، فارغ از DB_BACKEND")
        self.assertIn("logs", import_db._tables(conn))
        self.assertEqual(import_db._table_count(conn, "logs"), 1)
        rng = import_db._table_range(conn, "logs", "timestamp")
        self.assertEqual(rng["start"], "2026-10-04T00:00:00.000000Z")
        self.assertEqual([p["ip"] for p in import_db._table_printers(conn, "logs", "printer_ip")],
                         ["10.9.9.20"])

    def test_the_same_call_without_the_pin_would_still_fail(self):
        """Reverse witness: connect(path) without locking the backend gives the same ConfigurationError."""
        from core.db import ConfigurationError, connect
        path = _make_source_db(self.tmp.name)
        with self.assertRaises(ConfigurationError):
            connect(path, timeout=5.0)


class DiscardUploadTests(unittest.TestCase):
    """``_discard_upload'' deletes the rejected file and removes the session key (E.6.1)."""

    def test_removes_file_and_session_key(self):
        """Verifies removes file and session key."""
        from flask import Flask, session
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = _make_source_db(tmp.name)
        app = Flask(__name__)
        app.secret_key = "test-only"              # session is required for this helper
        with app.test_request_context("/"):
            session["import_temp_path"] = path
            import_db._discard_upload()
            self.assertFalse(os.path.exists(path), "آپلودِ ردشده نباید روی دیسک بماند")
            self.assertNotIn("import_temp_path", dict(session))

    def test_missing_file_is_not_an_error(self):
        """Verifies missing file is not an error."""
        from flask import Flask, session
        app = Flask(__name__)
        app.secret_key = "test-only"
        with app.test_request_context("/"):
            session["import_temp_path"] = "/nonexistent/temp_import_gone.db"
            self.assertIsNone(import_db._discard_upload())   # Silent and flawless


# ─────────────────────────────────────────────────────────────────────
# D) Boolean normalization (no DB)
# ─────────────────────────────────────────────────────────────────────
class BooleanCoercionTests(unittest.TestCase):
    """``yield_samples.accepted'' on PG requires ``bool''; On SQLite same 0/1."""

    def test_schema_boolean_list_matches_migration_0002(self):
        """``BOOLEAN_COLUMNS'' list should not deviate from migration 0002."""
        spec = importlib.util.spec_from_file_location(
            "_mig_0002", ROOT / "alembic" / "versions" / "0002_boolean_columns.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual([(t, c) for t, c, _new, _old in module.BOOLEAN_COLUMNS],
                         list(BOOLEAN_COLUMNS))

    def test_only_yield_samples_boolean_is_importable(self):
        """Out of 5 boolean columns, only ``yield_samples.accepted'' can be imported in tables."""
        self.assertEqual(boolean_columns_of("yield_samples"), ("accepted",))
        self.assertEqual(boolean_columns_of("users"), ("is_verified", "email_verified", "is_active"))
        for table in import_db.ALLOWED_IMPORT_TABLES:
            if table != "yield_samples":
                self.assertEqual(boolean_columns_of(table), (), table)

    def test_values_become_bool_for_postgres(self):
        """``1/0'' ⇒ ``True/False'' and ``NULL'' intact (for PG destination only)."""
        cols = ["printer_ip", "color", "accepted"]
        for raw, expected in ((1, True), (0, False), (2, True), (None, None)):
            with self.subTest(raw=raw):
                out = import_db._prepare_row(
                    "yield_samples", cols, ("10.0.0.1", "black", raw), for_postgres=True)
                self.assertIs(out[2], expected)

    def test_values_are_untouched_for_sqlite(self):
        """No conversion is done on SQLite (0 behavioral change)."""
        cols = ["printer_ip", "color", "accepted"]
        for raw in (1, 0, 2, None):
            with self.subTest(raw=raw):
                row = ("10.0.0.1", "black", raw)
                self.assertIs(
                    import_db._prepare_row("yield_samples", cols, row, for_postgres=False), row)

    def test_non_boolean_table_is_identity(self):
        """A table with no boolean columns is returned exactly (same tuple, no copy)."""
        row = ("203.0.113.40", 5)
        self.assertIs(
            import_db._prepare_row("printer_counters", ["ip", "print_total"], row,
                                   for_postgres=True),
            row)


# ─────────────────────────────────────────────────────────────────────
# E) endpoint on real SQLite — guard not enabled
# ─────────────────────────────────────────────────────────────────────
# marker `sqlite_only`: This test writes SQLite/schema specific to SQLite ⇒ below
# `DB_BACKEND=postgresql` is skipped with auto-skip (tests/conftest.py, docs/phase-e-plan.md §5.3).
@pytest.mark.sqlite_only
class ImportEndpointSqliteTests(unittest.TestCase):
    """On SQLite, ``confirm_import'' goes the same way as today (without the guard message)."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)                    # Relative DB_PATH ⇒ Temporary DB
        self.addCleanup(self._restore)

        from core.database import init_db
        from web import create_app
        init_db()
        self.app = create_app()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        _login(self.client, _new_admin_user("imp_admin_sqlite"))

    def _restore(self):
        """Helper: restore."""
        os.chdir(self.old_cwd)

    def test_confirm_without_uploaded_file_is_the_old_error(self):
        """No temp file ⇒ same as usual message, not PG guard message."""
        response = self.client.post("/api/import/confirm",
                                    json={"tables": ["logs"], "options": {}})
        self.assertEqual(response.status_code, 400)
        error = response.get_json()["error"]
        self.assertIn("فایل موقت یافت نشد", error)
        self.assertNotIn("TD-007", error)


# ─────────────────────────────────────────────────────────────────────
# F) True PostgreSQL — SAVEPOINT, Boolean and end-to-end guard
# ─────────────────────────────────────────────────────────────────────
@unittest.skipUnless(_PG_DSN, _PG_SKIP or "")
class PostgresLiveImportTests(unittest.TestCase):
    """
    Same behaviors, this time on real PG 16.

    ``_columns'' breaks the module on PG with ``PRAGMA'' (row 11 strictness, task E.6),
    So this is replaced by the ``schema_columns'' cross-backend helper — ie
    The substitution that E.6 will make.

    ``duplicate_mode="allow"'' is used (one of the three allowed API values:
    ``skip | allow | replace``) because these tests measure "simple insertion" behavior.
    The path ``skip'' on PG goes to ``_exists_by_cols'' and there ``col IS $1''
    It is made that PostgreSQL syntax is not — the same TD-009 that today under the guard
    It is an integrated endpoint and will be fixed in E.6.
    """

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        if not _PG_DSN:                     # second guard
            self.skipTest(_PG_SKIP or "PG در دسترس نیست")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = connect_postgres(_PG_DSN)
        self.addCleanup(self.target.close)
        patcher = mock.patch.object(
            import_db, "_columns",
            side_effect=lambda conn, table: schema_columns(conn, table))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cleanup_ips(self, table, column, ips):
        """Clearing the test rows (after any possible error, the transaction is revived)."""
        self.target.rollback()
        for ip in ips:
            self.target.execute(f"DELETE FROM {table} WHERE {column} = ?", (ip,))
        self.target.commit()

    def test_good_rows_survive_a_bad_row(self):
        """
        Three rows, middle ``ip = NULL'' (NOT NULL violation ⇒ ``UniqueViolation'' on PG):
        Without the SAVEPOINT, the entire batch would have perished; With SAVEPOINT, two rows remain intact.
        """
        good_a, good_b = "203.0.113.240", "203.0.113.241"
        source = connect_sqlite(_make_source_db(
            self.tmp.name, rows=[(good_a, 11), (None, 12), (good_b, 13)]))
        self.addCleanup(source.close)
        self._cleanup_ips("printer_counters", "ip", [good_a, good_b])

        result = import_db._import_table(source, self.target, "printer_counters", {}, "allow")
        self.target.commit()
        try:
            self.assertEqual(result["imported"], 2, result)
            self.assertEqual(result["skipped"], 1, result)
            rows = dict(self.target.execute(
                "SELECT ip, print_total FROM printer_counters WHERE ip IN (?, ?)",
                (good_a, good_b)).fetchall())
            self.assertEqual(rows, {good_a: 11, good_b: 13},
                             "ردیف‌های سالم باید کامیت شده باشند (SAVEPOINT کار کند)")
        finally:
            self._cleanup_ips("printer_counters", "ip", [good_a, good_b])

    def test_boolean_accepted_is_inserted_as_real_boolean(self):
        """``accepted'' returns a true ``BOOLEAN'' from the SQLite file (``0/1'') on the PG."""
        ip = "203.0.113.242"
        ddl = ("CREATE TABLE yield_samples (printer_ip TEXT, color TEXT, created_at TEXT, "
               "start_counter INTEGER, end_counter INTEGER, start_level INTEGER, "
               "end_level INTEGER, accepted INTEGER)")
        source = connect_sqlite(_make_source_db(
            self.tmp.name, table="yield_samples", ddl=ddl,
            rows=[(ip, "black", "2026-10-04T00:00:00.000000Z", 100, 200, 90, 80, 1),
                  (ip, "cyan", "2026-10-04T00:00:01.000000Z", 100, 200, 90, 80, 0)]))
        self.addCleanup(source.close)
        self._cleanup_ips("yield_samples", "printer_ip", [ip])

        result = import_db._import_table(source, self.target, "yield_samples", {}, "allow")
        self.target.commit()
        try:
            self.assertEqual(result["imported"], 2, result)
            self.assertEqual(result["skipped"], 0, result)
            rows = self.target.execute(
                "SELECT color, accepted FROM yield_samples WHERE printer_ip = ? ORDER BY color",
                (ip,)).fetchall()
            self.assertEqual(rows, [("black", True), ("cyan", False)])
            self.assertIsInstance(rows[0][1], bool, "روی PG نوع ستون BOOLEAN است، نه int")
        finally:
            self._cleanup_ips("yield_samples", "printer_ip", [ip])


@unittest.skipUnless(_PG_DSN, _PG_SKIP or "")
class PostgresLiveEndpointGuardTests(unittest.TestCase):
    """``POST /api/import/confirm'' on real PG: all three modes ⇒ 400 with guard message."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        if not _PG_DSN:
            self.skipTest(_PG_SKIP or "PG در دسترس نیست")
        self.env = mock.patch.dict(os.environ, {
            "DB_BACKEND": "postgresql", ENV_PG_DSN: _PG_DSN})
        self.env.start()
        self.addCleanup(self.env.stop)

        from core.database import init_db
        from web import create_app
        init_db()                                   # On PG = verification of Alembic schema
        self.app = create_app()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        self.user = _new_admin_user("imp_admin_pg")
        self.addCleanup(self._delete_user)
        _login(self.client, self.user)

    def _delete_user(self):
        """Helper: delete user."""
        from core.database import db_connection
        try:
            with db_connection(commit=True) as conn:
                conn.execute("DELETE FROM users WHERE username = ?", ("imp_admin_pg",))
        except Exception:                            # Clearing should not invalidate the test
            pass

    def test_all_modes_are_blocked_before_any_file_check(self):
        """Guard files before checking: 400 guards without a single file upload."""
        for mode in ("skip", "allow", "replace"):
            with self.subTest(mode=mode):
                response = self.client.post(
                    "/api/import/confirm",
                    json={"tables": ["logs"], "options": {"duplicate_mode": mode}})
                self.assertEqual(response.status_code, 400)
                error = response.get_json()["error"]
                for marker in GUARD_MARKERS:
                    self.assertIn(marker, error)
                self.assertNotIn("خطای داخلی", error, "نباید ۵۰۰ عمومی شود")

    def test_rejected_confirm_discards_the_uploaded_file(self):
        """E.6.1: The retired path must not leave the upload file on disk."""
        import io as _io
        old_cwd = os.getcwd()
        tmp = tempfile.TemporaryDirectory()
        os.chdir(tmp.name)                       # The upload path is relative ⇒ temporary folder
        try:
            ddl = ("CREATE TABLE logs (printer_ip TEXT, timestamp TEXT, type TEXT, "
                   "message TEXT)")
            rows = [("10.9.9.20", "2026-10-04T00:00:00.000000Z", "PRINT", "x")]
            source_path = _make_source_db(tmp.name, table="logs", ddl=ddl, rows=rows)
            with open(source_path, "rb") as fh:
                payload = fh.read()
            resp = self.client.post(
                "/api/import/analyze",
                data={"file": (_io.BytesIO(payload), "old.db")},
                content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 200, resp.get_json())
            self.assertTrue([f for f in os.listdir(".") if f.startswith("temp_import_")],
                            "analyze باید فایل موقت را ساخته باشد")

            blocked = self.client.post("/api/import/confirm", json={"tables": ["logs"]})
            self.assertEqual(blocked.status_code, 400)
            for marker in GUARD_MARKERS:
                self.assertIn(marker, blocked.get_json()["error"])
            self.assertEqual([f for f in os.listdir(".") if f.startswith("temp_import_")], [],
                             "پس از رد گارد نباید آپلودی بماند")
        finally:
            os.chdir(old_cwd)
            tmp.cleanup()

    def test_analyze_is_not_blocked_on_pg(self):
        """``analyze'' is not blocked on PG; It only works on the source file (SQLite)."""
        import io as _io
        response = self.client.post(
            "/api/import/analyze",
            data={"file": (_io.BytesIO(b"not a database"), "old.db")},
            content_type="multipart/form-data")
        self.assertEqual(response.status_code, 500, "فایل نامعتبر ⇒ خطای تحلیل، نه گارد")
        self.assertNotIn("TD-007", response.get_json().get("error", ""))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
