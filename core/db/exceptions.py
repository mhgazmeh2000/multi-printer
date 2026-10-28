# -*- coding: utf-8 -*-
# =============================================================================
# @file:         core/db/exceptions.py
# @brief:        Common exception family for the data layer, shared by both backends.
# @details:      Responsibilities: Translate driver-specific errors into DatabaseError subclasses.
#                Main API: DatabaseError, UnsupportedBackendError, ConfigurationError,
#                DatabaseConnectionError, DatabaseOperationalError, ColumnAlreadyExists,
#                UndefinedTable, UndefinedColumn (+3 more)
#                Side effects: none at import time
# @dependencies: stdlib only
# @notes:        None.
# =============================================================================

"""
Common data layer errors (phase D — thin adapter on SQLite).

Goal: The upstream code can catch database errors **independently of the backend**. From E.4.4
``ConnectionAdapter'' translates the driver error before reaching the consumer
(``core/db/connection.py::_translate_error'') and application code (``core/`` and ``web/``)
No more ``except sqlite3.*''.

Naming note: ``DatabaseConnectionError'' intentionally instead of ``ConnectionError''
Chosen not to be confused with Python's built-ins.
"""

from __future__ import annotations

from typing import Optional


class DatabaseError(Exception):
    """
    The root of all data layer errors.

    Attributes:
        backend: the backend from which the error came (``sqlite''/``postgresql''/``None'').
        sqlstate: the source driver's SQLSTATE code, if any (PG only; in SQLite ``None'').
        original: the driver exception itself (to accurately detect/retrieve the original message).
    """

    def __init__(self, message: str = "", *, backend: Optional[str] = None,
                 sqlstate: Optional[str] = None, original: Optional[BaseException] = None):
        """Create the error with a message and an optional backend name."""
        super().__init__(message)
        self.backend = backend
        self.sqlstate = sqlstate
        self.original = original


class UnsupportedBackendError(DatabaseError):
    """The requested backend is unknown or not supported at this stage."""


class ConfigurationError(DatabaseError):
    """
    Configuration is incomplete/inconsistent (eg ``DB_BACKEND=postgresql`` without ``PG_DSN``).

    It's separate from ``UnsupportedBackendError'': backend is **invalid**, here
    The backend is valid, but **no information required for connection**. Intentionally no fallback
    It is not done silently (working on the wrong database is more dangerous than not coming up).
    """


class DatabaseConnectionError(DatabaseError):
    """It was not possible to establish or maintain a connection to the database."""


class DatabaseOperationalError(DatabaseError):
    """
    Database execution error: locked, disk full, connection disconnected, timeout.

    Common equivalent of ``sqlite3.OperationalError'' and ``psycopg.OperationalError''.
    Consumers should get the same, not the type of driver. To make a more accurate decision
    (eg ``temporary lock'' vs ``disk full'') both ``sqlstate'' and the driver message
    (intact) is available — Note: The message is the driver's own text and is a string check
    The surface is still brittle (note E.4.4; candidate for the next phase).
    """


class ColumnAlreadyExists(DatabaseError):
    """
    Column already exists — idempotent path ``ALTER TABLE … ADD COLUMN''.

    The common equivalent of ``sqlite3.OperationalError: duplicate column name'' and
    ``psycopg.errors.DuplicateColumn`` (SQLSTATE 42701).
    """


class UndefinedTable(DatabaseError):
    """The table does not exist (SQLSTATE 42P01 / ``sqlite3'': "no such table")."""


class UndefinedColumn(DatabaseError):
    """The column does not exist (SQLSTATE 42703 / ``sqlite3``: "no such column")."""


class UniqueViolation(DatabaseError):
    """
    Unity/existence conflict — **the whole family of integrity**.

    Decision E.4.4 (Owner): In SQLite any ``sqlite3.IntegrityError'' (UNIQUE, NOT NULL,
    FOREIGN KEY, CHECK) and in PG every SQLSTATE with prefix ``23'' is mapped to the same class
    can be up to today's behavior of ``web/routes/import_db.py'' (counting ``skipped'' and so on
    ring) to remain the same on both backends; No behavioral failure occurs.

    ⚠ Known Tech Debt (registered at ``docs/tech-debt.md''): this name for the whole family
    It is not accurate. In the phase where a product decision is made, an ``IntegrityError'' must be raised
    With subclasses ``NotNullViolation''/``ForeignKeyViolation''/``UniqueViolation''
    be added and ``import_db'' explicitly chooses which family to skip.
    """


class PlaceholderTranslationError(DatabaseError):
    """It was not possible to translate the query placeholders for the destination backend."""


class UnsupportedFeatureError(DatabaseError):
    """
    The function that the code calls is not implemented on this backend.

    The difference with ``UnsupportedBackendError'' is clear: there *backend* is invalid;
    Here, the backend is valid and healthy, but a specific feature is not translated on it
    (Live example: ``replace'' mode in import on PostgreSQL — E.4.8 with guard
    Fast and made decision E.6.1 **retire**, not translation; Reasons and rejected alternatives in
    ``docs/phase-e-decisions.md'' row 1.)

    It is deliberately in this hierarchy so that consumers get a root, and deliberately
    **It's a clear and early error** rather than an obscure driver syntax error
    (``42601 syntax error at or near "OR"'') do not replace it.
    """


__all__ = [
    "DatabaseError",
    "DatabaseOperationalError",
    "ColumnAlreadyExists",
    "UndefinedTable",
    "UndefinedColumn",
    "UniqueViolation",
    "UnsupportedBackendError",
    "UnsupportedFeatureError",
    "ConfigurationError",
    "DatabaseConnectionError",
    "PlaceholderTranslationError",
]
