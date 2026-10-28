# -*- coding: utf-8 -*-
# =============================================================================
# @file:         alembic/env.py
# @brief:        Alembic environment: resolves the target DB URL (SQLite or PostgreSQL) and runs
#                migrations.
# @details:      Responsibilities: Choose backend and DSN, mask secrets, configure Alembic
#                online/offline migration runs.
#                Main API: ConfigurationError, mask_dsn(), resolve_backend(), sqlite_url(),
#                get_url(), run_migrations_offline(), run_migrations_online()
#                Side effects: database access, network I/O
# @dependencies: internal: alembic, config.settings, core.db.connection | external: sqlalchemy
# @notes:        Targets PostgreSQL when DB_BACKEND=postgresql; otherwise uses the local SQLite
#                file.
# =============================================================================

"""
alembic/env.py — Alembic configuration for the PrintGuard project (Phase E.1).

Two backends, one migration set
-------------------------------
Migrations are **handwritten** (no ORM; `target_metadata = None`) and must be on both
Run the database:

  • `DB_BACKEND=sqlite` (default, the same as the current state of the application) ⇒ path from `config.settings.DB_PATH`
  • `DB_BACKEND=postgresql` ⇒ address of `PG_DSN`

Golden rule: **There is no silent fallback.** If the backend is on postgresql and `PG_DSN'
is empty, execution stops with an explicit error; Because running the migration on the wrong database
It is more dangerous than not coming up (the same rule as `core/db/connection.resolve_backend`).

Note the SQLite path
-------------------
The relative path of `DB_PATH` (default `logs.db`) is absolute relative to **project root**, not relative to
current folder; To run alembic from anywhere, it will see the desired file of the program and the file
Don't make unwanted blanks.

Override hook (only for test/special scenarios)
---------------------------------------------
`ALEMBIC_DB_URL' in the environment bypasses the entire detection (e.g. to run a migration on a copy
temporary: `ALEMBIC_DB_URL=sqlite://///tmp/copy.db`). When used, a warning is printed.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, event, pool

from alembic import context

# ─────────────────────────────────────────────────────────────
# Routes and project import
# ─────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: No ORM: No MetaData to autogenerate; Migrations are handwritten.
target_metadata = None

BACKEND_SQLITE = "sqlite"
BACKEND_POSTGRESQL = "postgresql"
#: the same alternative names that the data layer of the application knows (`core/db/connection.py`).
BACKEND_ALIASES = {"postgres": BACKEND_POSTGRESQL, "pg": BACKEND_POSTGRESQL}
ENV_DB_BACKEND = "DB_BACKEND"
ENV_PG_DSN = "PG_DSN"
ENV_OVERRIDE_URL = "ALEMBIC_DB_URL"


class ConfigurationError(RuntimeError):
    """The environment configuration for migration is incomplete or inconsistent."""


def mask_dsn(dsn: str) -> str:
    """Removes the password from the DSN (never print the password)."""
    out = []
    for token in str(dsn).split():
        out.append("password=***" if token.lower().startswith("password=") else token)
    return " ".join(out)


def resolve_backend(value: str | None = None) -> str:
    """Specifies the backend from the /`DB_BACKEND` parameter (empty ⇒ sqlite)."""
    raw = (value if value is not None else os.getenv(ENV_DB_BACKEND, "")) or ""
    backend = raw.strip().lower()
    backend = BACKEND_ALIASES.get(backend, backend)
    if not backend:
        return BACKEND_SQLITE
    if backend in (BACKEND_SQLITE, BACKEND_POSTGRESQL):
        return backend
    raise ConfigurationError(
        f"مقدار ناشناخته برای {ENV_DB_BACKEND}: «{raw}» "
        f"(مجاز: {BACKEND_SQLITE}، {BACKEND_POSTGRESQL})")


def sqlite_url() -> str:
    """Full SQLite URL from `config.settings.DB_PATH` (relative ⇒ to project root)."""
    from config.settings import DB_PATH  # noqa: PLC0415 — according to the project contract

    path = Path(DB_PATH)
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    return "sqlite:///" + path.as_posix()


def get_url() -> str:
    """Destination database address for migration (without silent fallback)."""
    override = os.getenv(ENV_OVERRIDE_URL, "").strip()
    if override:
        # Guaranteed to use psycopg v3 (postgresql+psycopg://) instead of psycopg2
        if "://" not in override:
            override = "postgresql+psycopg://" + _dsn_to_url(override)
        elif override.startswith("postgresql://"):
            override = "postgresql+psycopg://" + override[len("postgresql://"):]
        elif override.startswith("postgres://"):
            override = "postgresql+psycopg://" + override[len("postgres://"):]
        print(f"[alembic] ⚠ override فعال است ({ENV_OVERRIDE_URL}); URL = {mask_dsn(override)}",
              file=sys.stderr)
        return override

    backend = resolve_backend()
    if backend == BACKEND_POSTGRESQL:
        dsn = os.getenv(ENV_PG_DSN, "").strip()
        if not dsn:
            raise ConfigurationError(
                f"{ENV_DB_BACKEND}=postgresql است ولی {ENV_PG_DSN} خالی است. "
                f"یا {ENV_PG_DSN} را ست کنید یا {ENV_DB_BACKEND} را به sqlite برگردانید "
                f"(هیچ fallback خودکاری انجام نمی‌شود).")
        print(f"[alembic] backend = postgresql | DSN = {mask_dsn(dsn)}", file=sys.stderr)
        return "postgresql+psycopg://" + _dsn_to_url(dsn)

    url = sqlite_url()
    print(f"[alembic] backend = sqlite | URL = {url}", file=sys.stderr)
    return url


def _dsn_to_url(dsn: str) -> str:
    """Converts a libpq-style DSN (`host=... dbname=...`) to the part after `://`.

    Support: common libpq keys + URI form (`postgresql://...`) which is passed intact.
    """
    if "://" in dsn:
        # is already a URI (like postgresql://user:pass@host/db)
        return dsn.split("://", 1)[1]

    from core.db.connection import _tokenize_conninfo

    parts = {}
    for token in _tokenize_conninfo(dsn):
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        parts[key.strip().lower()] = value.strip().strip("'\"")

    host = parts.get("host") or parts.get("hostaddr") or "localhost"
    port = parts.get("port") or "5432"
    user = parts.get("user") or ""
    password = parts.get("password") or ""
    dbname = parts.get("dbname") or parts.get("database") or ""

    auth = ""
    if user:
        from urllib.parse import quote  # noqa: PLC0415 — only for password/user escape
        auth = quote(user, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    elif password:
        from urllib.parse import quote  # noqa: PLC0415
        auth = ":" + quote(password, safe="") + "@"

    tail = "/" + (dbname or "")
    extra = {k: v for k, v in parts.items()
             if k not in ("host", "hostaddr", "port", "user", "password", "dbname", "database")}
    if extra:
        from urllib.parse import urlencode  # noqa: PLC0415
        tail += "?" + urlencode(extra)
    return f"{auth}{host}:{port}{tail}"


def _is_sqlite(url: str) -> bool:
    """Return True when the Alembic target URL points to SQLite."""
    return url.startswith("sqlite")


def _install_sqlite_pragmas(engine) -> None:
    """Applies `PRAGMA foreign_keys=ON` to every new connection.

    Why event and not command execution in the body? Running PRAGMA on `Connection` causes
    becomes autobegin (open SQLAlchemy level transaction) and is the same as in E.1.4
    caused the `alembic_version' line to be missing; Here PRAGMA in the DBAPI layer and before any
    A transaction is executed.
    """
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):  # pragma: no cover - event
        """Apply SQLite connection pragmas on each new DB-API connection."""
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def _configure(connection=None, url: str | None = None) -> None:
    """Common online/offline configuration (with appropriate settings for each dialect)."""
    opts = {
        "target_metadata": target_metadata,
        # Comparing the type of columns in autogenerate (later phases that generate migration).
        "compare_type": True,
        # On SQLite, every ALTER must be executed as a batch (table reconstruction).
        "render_as_batch": True,
        "include_schemas": False,
    }
    if connection is not None:
        context.configure(connection=connection, **opts)
    else:
        context.configure(url=url, literal_binds=True,
                          dialect_opts={"paramstyle": "named"}, **opts)


def run_migrations_offline() -> None:
    """Offline mode: only SQL is generated (no connection)."""
    _configure(url=get_url())
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """online mode: real connection and execution of migrations."""
    url = get_url()
    # Initialization from code (not from alembic.ini): `%` in password/URL must be escaped.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(section, prefix="sqlalchemy.",
                                     poolclass=pool.NullPool)

    if _is_sqlite(url):
        # On SQLite, foreign key enforcement is off by default; for migration
        # We will clarify the future of this project (which does not have FK but can be added).
        _install_sqlite_pragmas(connectable)

    with connectable.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()

        # ⚠ Measured warning (E.1.4): on SQLite, alembic `transactional_ddl=False`
        # sees and only returns nullcontext for "outer transaction"; As a result of the transaction
        # The SQLAlchemy level remains open at the end, and `Connection.close()` will **rollback** it.
        # does The DDL remains because of the implicit-commit behavior of the sqlite3 driver, but the line
        # `alembic_version' (which is DML) **would be lost**: tables would be created and the version
        # It was not registered (proof: alembic_version is empty and `alembic current` has no output).
        # This explicit commit closes the issue and on PostgreSQL (which has transactional_ddl)
        # It is ineffective, because there the transaction has already been closed.
        if connection.in_transaction():
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
