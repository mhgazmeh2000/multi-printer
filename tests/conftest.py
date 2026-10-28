# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/conftest.py
# @brief:        Pytest fixtures and PostgreSQL isolation for the test suite.
# @details:      Responsibilities: Create per-test isolated schemas and summarise results.
#                Main API: none (no public functions or classes)
#                Side effects: database access, runs external processes
# @dependencies: internal: core.db, core.db.connection | external: dotenv, psycopg, pytest
# @notes:        None.
# =============================================================================

"""
tests/conftest.py — PostgreSQL isolation for the test suite (Phase E.5).

Why is this file?
----------------
Previously, any test that connected to Postgres with ``PG_DSN'' was on **a shared schema**
(``public'') worked. It had three practical consequences (E.5 initialization, ``DB_BACKEND=postgresql''):

  1. ``relation "t" already exists'' — some tests for temporary tables of the same name in ``public''
     were making; The second performance collided with the first performance.
  2. The remaining data from such executions would change the results (must be manual
     ``TRUNCATE'' to clean up the situation).
  3. Two parallel executions (for example, ``pytest -n'' or two checkouts on the same machine)
     They were ruining it.

Solution: A dedicated **schema for each pytest run**. with ``CREATE SCHEMA'' (no
``CREATE DATABASE'' — creating a database requires the ``CREATEDB'' right and is slower)
and lock ``search_path'' on the same empty schema, the above three problems are solved together.

The rule of this file: "No test should hardcode ``public''." Because everything is out of the way
``PG_DSN'' (which will be rewritten later in this file) are connected, the existing codes without
Change to isolated schema. Surface module tests that ``PG_DSN'' during import
They read that they work correctly, because this rewriting is done **at the level of the conftest module**
and conftest is imported before each test module.

skip logic (silent, according to the owner's decision)
----------------------------------
  • ``PG_DSN'' is not set ⇒ nothing; SQLite tests run intact.
  • Postgres server is not available ⇒ nothing (PG tests skip themselves; we
    We do not make a fake error).
  • The role does not have ``CREATE'' rights on the database (read-only role) ⇒ skip with warning.
  • There is ``PG_DSN'' and the scheme was created, but ``alembic upgrade head'' failed ⇒
    **explicit error**; There is no silent fallback to ``public'' (rule
    ``no silent fallback'' in ``alembic/env.py'').

Temporary shutdown (for troubleshooting): ``PRINTGUARD_PG_ISOLATE=0''.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
import warnings
from pathlib import Path

import pytest

ENV_PG_DSN = "PG_DSN"
ENV_DB_BACKEND = "DB_BACKEND"
ENV_SWITCH = "PRINTGUARD_PG_ISOLATE"
#: scheme name prefix — to be visible by eye in ``pg_stat_activity''.
SCHEMA_PREFIX = "printguard_test"

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Status of this implementation (main dsn + created scheme).
_state = {"dsn": None, "schema": None}

#: If a data reset breaks once, its message will remain here and will be turned off after that.
_reset_broken = None


def _dsn_with_search_path(dsn: str, schema: str) -> str:
    """
    Adds ``options=-csearch_path=<schema>'' to the DSN (no spaces).
    Full support for both structures: URI (`postgresql://...`) and conninfo (`host=...`).
    """
    try:
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        params = conninfo_to_dict(dsn)
        existing = params.get("options", "")
        opts = f"-csearch_path={schema}"
        if existing:
            opts = f"{existing} {opts}"
        params["options"] = opts
        return make_conninfo(**params)
    except Exception:
        from core.db.connection import _conninfo_value

        existing = _conninfo_value(dsn, "options")
        opts = f"-csearch_path={schema}"
        if existing:
            opts = f"{existing} {opts}"
        tokens = [t for t in dsn.split() if not t.lower().startswith("options=")]
        tokens.append(f"options={opts}")
        return " ".join(tokens)


def _maintenance_connect(dsn: str):
    """Autocommit connection for administrative tasks (CREATE/DROP SCHEMA)."""
    import psycopg

    return psycopg.connect(dsn, autocommit=True, connect_timeout=5)


def _isolate() -> None:
    """It isolates the schema, overwrites the DSN, and performs the migration."""
    env_file = PROJECT_ROOT / ".env"
    if env_file.is_file():
        try:
            from dotenv import load_dotenv
            load_dotenv(dotenv_path=env_file, override=False)
        except ImportError:
            pass

    dsn = (os.environ.get(ENV_PG_DSN, "") or "").strip()
    if not dsn or os.environ.get(ENV_SWITCH, "1").strip() == "0":
        return                                   # Normal mode: SQLite only
    from importlib.util import find_spec
    if find_spec("psycopg") is None:
        return                                   # Without driver ⇒ PG tests skip by themselves

    schema = f"{SCHEMA_PREFIX}_{uuid.uuid4().hex[:12]}"
    _state["dsn"] = dsn
    try:
        with _maintenance_connect(dsn) as conn:
            conn.execute(f"CREATE SCHEMA {schema}")
    except Exception as exc:                       # noqa: BLE001 — any reason (including insufficient right)
        sqlstate = getattr(getattr(exc, "sqlstate", None), "value", None) or getattr(
            exc, "sqlstate", None)
        _state["dsn"] = None
        if sqlstate in ("3D000", "08001", "08006", "28P01"):
            return                                 # Server/Authentication ⇒ Tests skip themselves
        warnings.warn(
            f"ایزولاسیون PG خاموش ماند: CREATE SCHEMA مجاز نبود "
            f"({type(exc).__name__}: {str(exc).strip().splitlines()[0][:160]}). "
            f"برای ایزوله‌شدن، به نقش اجرا «GRANT CREATE ON DATABASE …» بدهید.",
            stacklevel=2)
        return

    _state["schema"] = schema
    os.environ[ENV_PG_DSN] = _dsn_with_search_path(dsn, schema)
    _run_alembic_upgrade(dsn, schema)


def _run_alembic_upgrade(dsn: str, schema: str) -> None:
    """
    Runs ``alembic upgrade head'' **on an isolated schema**.

    Why subprocess: alembic at the module level logging and sys.path hands and execution
    Its in-process can change the global state in the middle of a pytest session.
    The cost is ~1 second and is paid once per run.
    """
    env = os.environ.copy()
    env[ENV_DB_BACKEND] = "postgresql"
    env[ENV_PG_DSN] = _dsn_with_search_path(dsn, schema)
    env.pop("ALEMBIC_DB_URL", None)                # Do not leave the override unread
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        # We don't swallow silently: if half of the constructed scheme remains empty, it means hundreds of failures
        # Irrelevant in tests.
        raise RuntimeError(
            "ایزولاسیون PG: alembic upgrade head روی اسکیمای تازه شکست.\n"
            f"schema = {schema}\n"
            f"--- stdout ---\n{proc.stdout[-2000:]}\n"
            f"--- stderr ---\n{proc.stderr[-2000:]}")


def _cleanup() -> None:
    """Helper: cleanup."""
    schema, dsn = _state.get("schema"), _state.get("dsn")
    if not schema or not dsn:
        return
    try:
        with _maintenance_connect(dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    except Exception as exc:                       # noqa: BLE001 — teardown should not break the test
        warnings.warn(f"پاک‌سازی اسکیمای تست نشد ({schema}): {type(exc).__name__}: {exc}",
                      stacklevel=2)
    finally:
        _state["schema"] = None


_isolate()


#: application tables that are emptied before each test (``core.db.schema.APP_TABLES'').
_APP_TABLES = ("logs", "printer_counters", "toner_history", "sensor_readings", "users",
               "security_events", "yield_samples", "toner_snapshots_v2", "cartridge_state",
               "cartridge_yield_profiles", "meta")


def _app_backend_is_postgres() -> bool:
    """
    Does the program itself run on PG?

    A data reset is only required in that case; If ``DB_BACKEND'' is empty/sqlite,
    Tests have their own temporary files and touching the common PG is useless
    It also slows down SQLite execution (one TRUNCATE per test).
    """
    from core.db import BACKEND_POSTGRESQL, resolve_backend

    try:
        return resolve_backend() == BACKEND_POSTGRESQL
    except Exception:                            # noqa: BLE001 - Unknown settings ⇒ Do not touch
        return False


def _quote(name: str) -> str:
    """Helper: quote."""
    return '"' + str(name).replace('"', '""') + '"'


def _reset_state() -> None:
    """
    Returns the isolated schema to a "fresh" state: empty application tables,
    Sequences are reset, and manual tables of tests are deleted.

    Why it's needed: "Every test is a temporary SQLite file" is the default assumption for hundreds of tests. On the PG that
    All tests see a common session schema, breaking this assumption; example
    Measured: ``duplicate key value violates unique constraint
    "users_username_key"`` (admin1 who created the previous file) ⇒ ``'viewer'!= 'admin'``,
    and ``'t' already exists'' (temporary table of the same name). Data reset **before each test**
    It reproduces the same "fresh temporary file", without a single line in the test files
    be changed and without running the migration again.
    """
    from core.db import connect_postgres

    conn = connect_postgres()
    try:
        try:
            conn.execute("SET LOCAL lock_timeout = '5s'")
        except Exception:                       # noqa: BLE001 — Out of transaction is meaningless
            pass
        found = {row[0] for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()").fetchall()}
        present_app = [t for t in _APP_TABLES if t in found]
        foreign = sorted(found - set(_APP_TABLES) - {"alembic_version"})
        if present_app:
            conn.execute("TRUNCATE TABLE " + ", ".join(_quote(t) for t in present_app)
                         + " RESTART IDENTITY CASCADE")
        for name in foreign:                     # The temp table of one test should not reach the next test
            conn.execute(f"DROP TABLE IF EXISTS {_quote(name)} CASCADE")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@pytest.fixture(scope="session", autouse=True)
def pg_isolated_schema():
    """
    Session global fixture: Connects teardown only.

    Scheme construction was done at the module level (before importing the test modules) until the tests that
    ``PG_DSN'' is read during collection to benefit from isolation.
    """
    yield {"schema": _state.get("schema"), "dsn": _state.get("dsn")}
    _cleanup()


@pytest.fixture(autouse=True)
def pg_clean_db_per_test(pg_isolated_schema):
    """
    Isolation of **data** at the scale of each test (spending the cost of migration once per session).

    It is inactive if PG isolation is not enabled (empty PG_DSN / read-only role).
    A reset error will alert once and then shut down, up to hundreds of errors
    Do not duplicate the original source.
    """
    global _reset_broken
    if _app_backend_is_postgres() and pg_isolated_schema.get("schema") and not _reset_broken:
        try:
            _reset_state()
        except Exception as exc:                 # noqa: BLE001
            _reset_broken = f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:140]}"
            warnings.warn(f"بازنشانی دادهٔ PG انجام نشد ⇒ از این پس بدون بازنشانی: "
                          f"{_reset_broken}", stacklevel=2)
    yield


#: count sqlite_only skips for the final report.
_sqlite_only_skipped = 0

SKIP_SQLITE_ONLY_REASON = (
    "sqlite_only: این تست SQL/اسکیمای مختص SQLite می‌نویسد (CREATE TABLE … "
    "AUTOINCREMENT، INSERT OR REPLACE، json_extract، یا فرض «DB خالیِ هر chdir»)؛ "
    "زیر DB_BACKEND=postgresql معنا ندارد ⇒ docs/phase-e-plan.md بند ۵.۳")


def pytest_collection_modifyitems(config, items):
    """
    auto-skip `sqlite_only` tests — **only** when the app is on PG.

    Why hook and not `-m "not sqlite_only"`: The request was to run `pytest tests/`
    Green on both backends without special arguments. skip is "explicit": both in
    `-rs' is seen, also counted in the final summary, and none on the SQLite path
    It has no effect (so the "0 skipped" rule of that route is valid).
    """
    global _sqlite_only_skipped
    if not _app_backend_is_postgres():
        return
    marker = pytest.mark.skip(reason=SKIP_SQLITE_ONLY_REASON)
    for item in items:
        if "sqlite_only" in item.keywords:
            item.add_marker(marker)
            _sqlite_only_skipped += 1


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """One line of clarification: some sqlite_only tests were skipped (so that skip is not silent)."""
    if _sqlite_only_skipped:
        terminalreporter.write_line(
            f"printguard: {_sqlite_only_skipped} تست با marker sqlite_only رد شد "
            f"(اجرای PG) — فهرست و دلیل: docs/phase-e-plan.md بند ۵.۳")


def pytest_report_header(config):
    """One line of clarification: which schema do the tests run on?"""
    schema = _state.get("schema")
    if schema:
        return f"printguard: PG ایزوله روی اسکیمای {schema} (هر اجرا یکی، teardown با CASCADE)"
    if not (os.environ.get(ENV_PG_DSN, "") or "").strip():
        return None
    return "printguard: PG در دسترس/ایزوله نیست ⇒ تست‌های PG روی همان تنظیمات قبلی"
