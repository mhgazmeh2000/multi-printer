# =============================================================================
# @file:         core/database.py
# @brief:        Legacy SQLite data-access layer: schema init and event, user and counter queries.
# @details:      Responsibilities: Initialise the SQLite schema and provide CRUD helpers used by
#                the web and poller code.
#                Main API: db_connection(), update_missing_yield_list(), init_db(), count_users(),
#                get_user_by_id(), get_user_by_identifier(), get_user_by_email(),
#                get_user_by_google_id() (+26 more)
#                Side effects: database access, starts threads, file I/O
# @dependencies: internal: config.settings, core, core.db, core.db.schema, core.security_audit,
#                core.time_utils, core.yield_engine
# @notes:        Legacy SQLite helpers. New code should use core.db, which also supports
#                PostgreSQL. Reads environment variables: DB_ALERT_DEBOUNCE_SECS,
#                DB_DEBOUNCE_TYPES, DB_DEBOUNCE_WINDOW_SECS.
# =============================================================================

"""
Functions of working with SQLite:
- init_db: create logs table (with paper_size field) and printer_counters table
- add_event: event registration (with paper_size support)
- get_log: get the log of a printer
- get_all_logs: get all logs with filter
- clear_logs: clear logs other than PRINT, SERVICE and REFILL
- prune_old_print_logs: automatic cleaning of old PRINT logs
- load_printer_counters, save_printer_counters: save and restore previous values of counters
"""

import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

# NOTE: DB_PATH is intentionally omitted — tests/golden_harness.py returns the DB path from
# The same attribute reads to create the "Temporary DB" guard. Do not delete.
from config.settings import DB_PATH  # noqa: F401
from core.db import BACKEND_POSTGRESQL, ColumnAlreadyExists, DatabaseOperationalError
from core.db import upsert_sql as _upsert_sql
from core.db import db_connection as core_db_connection
from core.db import resolve_backend
from core.db import get_connection as core_db_get_connection
from core.db.schema import APP_TABLES as _PG_APP_TABLES, verify_postgres_tables
from core.time_utils import (to_filter_bound, to_local_day, to_sort_key,
                             to_utc_z, utc_now_iso)
# NOTE: from core import store has been removed (dynamic import inside the add_event function)

log = logging.getLogger("PrinterMonitor")

_missing_yield_lock = threading.Lock()

# ─── Context manager for secure SQLite connection ─────────────────
@contextmanager
def db_connection(timeout: float = 10.0, commit: bool = False):
    """
    Context manager for secure connection to the database.
    Guarantees that the connection is closed in all conditions (success or error).

    Note (Phase D): The actual implementation has been moved to ``core.db.db_connection''
    (thin adapter layer). This function is just a compatibility shell rather than the previous signature
    (timeout/commit) and the existing import location remains intact.

    Usage:
        # read only:
        with db_connection() as conn:
            row = conn.execute("SELECT ... WHERE id=?", (uid,)).fetchone()
            return _row_to_dict(row)

        # Write (with automatic commit):
        with db_connection(commit=True) as conn:
            conn.execute("UPDATE users SET ... WHERE id=?", (uid,))
    """
    with core_db_connection(timeout=timeout, commit=commit) as conn:
        yield conn


# Top-level fields that are stored directly in table columns (the rest go to JSON details)
_LOG_TOP_LEVEL_FIELDS = frozenset(
    ("timestamp", "message", "pages", "color", "code", "severity", "paper_size", "username")
)

MISSING_YIELD_FILE = "missing_yield_printers.txt"

_USER_JSON_FIELDS = frozenset(("allowed_offices", "allowed_modules"))


def update_missing_yield_list(ip: str, current_yield: int, source: str = None):
    """Record the yield_per_page of each printer in the `missing_yield_printers.txt' legacy file.

    Note: The main source of Yield Engine is database tables. This file is only for
    Compatibility and reporting are kept fast.

    Format of each line:
        <ip> yield_per_page=<value> status=<default|catalog|device_capacity|shared_profile|auto_learn|learned>

    If current_yield == -1, the IP is removed from the file.
    The old lines that were only IP are converted to the new format when writing.
    """
    try:
        with _missing_yield_lock:
            rows = {}
            if os.path.exists(MISSING_YIELD_FILE):
                with open(MISSING_YIELD_FILE, 'r', encoding='utf-8') as f:
                    for raw_line in f:
                        line = raw_line.strip()
                        if not line or line.startswith('#'):
                            continue
                        parts = line.split()
                        row_ip = parts[0]
                        row_yield = 2000
                        row_source = 'default'
                        for part in parts[1:]:
                            if part.startswith('yield_per_page='):
                                try:
                                    row_yield = int(part.split('=', 1)[1])
                                except (TypeError, ValueError):
                                    row_yield = 2000
                            elif part.startswith('status='):
                                row_source = part.split('=', 1)[1] or row_source
                        rows[row_ip] = {"yield": row_yield, "source": row_source}

            if int(current_yield or 0) == -1:
                rows.pop(ip, None)
            else:
                y = int(current_yield or 2000)
                status = (source or '').strip() or ('default' if y == 2000 else 'learned')
                rows[ip] = {"yield": y, "source": status}

            lines = []
            for row_ip in sorted(rows):
                row = rows[row_ip]
                y = int(row.get("yield") or 2000)
                status = row.get("source") or ('default' if y == 2000 else 'learned')
                lines.append(f"{row_ip} yield_per_page={y} status={status}")

            with open(MISSING_YIELD_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
                if lines:
                    f.write('\n')
    except Exception as e:
        log.exception(f"Error updating missing yield list: {e}")


USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


def _dump_json_list(value) -> str:
    """Serialize a list to a JSON string; empty values become '[]'."""
    if not value:
        return "[]"
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                value = parsed
            else:
                value = [parsed]
        except Exception:
            value = [value]
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    cleaned = []
    for item in value:
        text = str(item).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return json.dumps(cleaned, ensure_ascii=False)


def _load_cartridge_id_state(value):
    """Pars cartridge status including identifiers, counters and per-slot state."""
    out = {}
    if not value:
        return out
    try:
        data = json.loads(value)
        if isinstance(data, dict):
            ids = data.get("ids")
            pages = data.get("supply_pages")
            if isinstance(ids, dict) and ids:
                out["cartridge_ids"] = ids
            if isinstance(pages, dict) and pages:
                out["cart_supply_pages"] = pages
            state = data.get("cartridge_state")
            if isinstance(state, dict) and state:
                out["cartridge_state"] = state
    except Exception:
        pass
    return out


def _dump_cartridge_id_state(data: dict):
    """Build JSON column from cartridge snapshot for full state round-trip."""
    ids = data.get("cartridge_ids")
    pages = data.get("cart_supply_pages")
    if not isinstance(ids, dict):
        ids = {}
    if not isinstance(pages, dict):
        pages = {}
    state = data.get("cartridge_state")
    if not isinstance(state, dict):
        state = {}
    if not ids and not pages and not state:
        return None
    try:
        return json.dumps({"ids": ids, "supply_pages": pages, "cartridge_state": state}, ensure_ascii=False)
    except Exception:
        return None


def _load_json_list(value):
    """Deserialize a JSON list from storage; empty values become an empty list."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    try:
        parsed = json.loads(value)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _load_json_dict(value):
    """Loading a JSON column as a dict; In the absence of data, an empty dict is returned."""
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def init_db():
    # Phase D: The connection is made from the adapter layer (core.db); The same timeout as before.
    # Phase E.4.6: All commands are executed with ``conn.execute'' (we don't take raw cursor)
    # so that the PG path passes through the translation of the placeholder and the common behavior of the adapter.
    """Create the SQLite schema and run its migrations."""

    conn = core_db_get_connection(timeout=30.0)

    if conn.backend == BACKEND_POSTGRESQL:
        # Phase E.4.5: On PostgreSQL **Alembic is the only source of DDL**. This function is there
        # does not build any tables/indexes (no PRAGMA, no AUTOINCREMENT which is not PG syntax) and
        # It only verifies that the migration schema exists; In its absence, it gives a clear error
        # So that the program does not load with an incomplete schema. Replace SQLite PRAGMAs
        # (`busy_timeout`/`journal_mode`/`synchronous`) Level `lock_timeout` setting
        # It is a server - paragraph 12 of the tightening document.
        try:
            verify_postgres_tables(conn, _PG_APP_TABLES, context="init_db")
        finally:
            conn.close()
        return


    conn.execute("PRAGMA busy_timeout=30000")
    # Changing the journal mode on a network share may be temporarily locked;
    # Keep the current state and let the startup continue.
    # Phase E.4.4: Error comes from adapter with common type (DatabaseOperationalError).
    # This PRAGMA only runs on the SQLite path (on PG, init_db used to be no-op
    # and "temporary lock on share" should not stop startup; Other errors above
    # they go Checking the message text of the driver is intact (SQLite does not have the SQLSTATE code).
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
    except DatabaseOperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        log.warning("Database is temporarily locked; keeping current journal mode: %s", exc)
    try:
        conn.execute("PRAGMA synchronous=FULL")
    except DatabaseOperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        log.warning("Could not set SQLite synchronous mode during startup: %s", exc)

    conn.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_ip TEXT NOT NULL,
            printer_name TEXT,
            timestamp TEXT NOT NULL,
            type TEXT,
            message TEXT,
            pages INTEGER,
            color TEXT,
            code TEXT,
            severity TEXT,
            paper_size TEXT,
            username TEXT,
            details TEXT
        )
    ''')
    # Phase E.4.4: "Column already exists" on both backends of the same class
    # (SQLite: duplicate column name; PG: SQLSTATE 42701).
    try:
        conn.execute("ALTER TABLE logs ADD COLUMN paper_size TEXT")
    except ColumnAlreadyExists:
        pass
    try:
        conn.execute("ALTER TABLE logs ADD COLUMN username TEXT")
    except ColumnAlreadyExists:
        pass

    conn.execute('CREATE INDEX IF NOT EXISTS idx_printer_ip ON logs(printer_ip)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON logs(timestamp)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_type ON logs(type)')

    # Base columns are made from _COUNTER_BASE_COLUMNS and columns
    # Added with _counter_column_alters() (single source).
    conn.execute(f'''
        CREATE TABLE IF NOT EXISTS printer_counters (
            ip TEXT PRIMARY KEY,
{_counter_columns_create_sql()}
        )
    ''')
    # Complete old table columns — single source: _counter_column_alters()
    for _column_sql in _counter_column_alters():
        try:
            conn.execute(_column_sql)
        except ColumnAlreadyExists:      # Phase E.4.4: Common type of both backends
            pass  # The column already exists

    conn.execute('''
        CREATE TABLE IF NOT EXISTS toner_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_ip TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            print_total INTEGER,
            toner_level INTEGER,
            yield_per_page INTEGER,
            source TEXT DEFAULT 'poll'
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_toner_history_ip_ts ON toner_history(printer_ip, timestamp)')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS sensor_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_ip TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            port INTEGER NOT NULL,
            kind TEXT NOT NULL,
            value REAL,
            unit TEXT,
            status TEXT
        )
    ''')
    # If a copy of index is corrupted in an incomplete stop, rebuild it atomically.
    conn.execute('DROP INDEX IF EXISTS idx_sensor_readings_kind')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_sensor_readings_ip_ts ON sensor_readings(printer_ip, timestamp)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_sensor_readings_kind ON sensor_readings(kind)')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT,
            google_id TEXT UNIQUE,
            reset_token_hash TEXT,
            reset_token_expires TEXT,
            role TEXT NOT NULL DEFAULT 'viewer',
            is_verified INTEGER NOT NULL DEFAULT 0,
            email_verified INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT,
            allowed_offices TEXT NOT NULL DEFAULT '[]',
            allowed_modules TEXT NOT NULL DEFAULT '[]'
        )
    ''')
    for column_sql in (
        "ALTER TABLE users ADD COLUMN password_hash TEXT",
        "ALTER TABLE users ADD COLUMN google_id TEXT",
        "ALTER TABLE users ADD COLUMN reset_token_hash TEXT",
        "ALTER TABLE users ADD COLUMN reset_token_expires TEXT",
        "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'viewer'",
        "ALTER TABLE users ADD COLUMN is_verified INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE users ADD COLUMN created_at TEXT",
        "ALTER TABLE users ADD COLUMN updated_at TEXT",
        "ALTER TABLE users ADD COLUMN last_login_at TEXT",
        "ALTER TABLE users ADD COLUMN allowed_offices TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE users ADD COLUMN allowed_modules TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE users ADD COLUMN sensor_alert_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN sensor_temp_warning REAL DEFAULT 30",
        "ALTER TABLE users ADD COLUMN sensor_temp_critical REAL DEFAULT 35",
        "ALTER TABLE users ADD COLUMN sensor_hum_warning_high REAL DEFAULT 70",
        "ALTER TABLE users ADD COLUMN sensor_hum_critical_high REAL DEFAULT 80",
        "ALTER TABLE users ADD COLUMN sensor_hum_warning_low REAL DEFAULT 20",
        "ALTER TABLE users ADD COLUMN sensor_alert_cooldown_minutes INTEGER DEFAULT 30",
        "ALTER TABLE users ADD COLUMN sensor_alert_start_hour INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN sensor_alert_end_hour INTEGER DEFAULT 24",
    ):
        try:
            conn.execute(column_sql)
        except ColumnAlreadyExists:      # Phase E.4.4: Common type of both backends
            pass

    conn.commit()
    conn.close()

    # Setting up the security audit table
    try:
        from core.security_audit import init_security_audit
        init_security_audit()
    except Exception as e:
        log.exception("Failed to init security_audit: %s", e)

    # Setting up the Yield Engine tables
    # This import is intentionally inside the function so that circular import is not created.
    try:
        from core.yield_engine import ensure_yield_tables
        ensure_yield_tables()
    except Exception as e:
        log.exception("Failed to init yield_engine tables: %s", e)

    # Setting up the dashboard, panel and permission tables (phase J.3)
    # Same reason: a function-level import avoids a circular import.
    try:
        from core.dashboard.manager import bootstrap_dashboard_tables
        with core_db_connection(commit=True) as conn_dash:
            bootstrap_dashboard_tables(conn_dash)
    except Exception as e:
        log.exception("Failed to init dashboard tables: %s", e)


def _user_row_to_dict(row) -> Optional[dict]:
    """Convert a user database row to a dictionary, or None for an empty row."""
    if not row:
        return None
    role = row[7] or "viewer"
    is_verified = bool(row[8])
    return {
        "id": row[0],
        "username": row[1],
        "email": row[2],
        "password_hash": row[3],
        "google_id": row[4],
        "reset_token_hash": row[5],
        "reset_token_expires": row[6],
        "role": role,
        "is_verified": is_verified,
        "email_verified": is_verified,
        "is_active": bool(row[9]),
        "created_at": row[10],
        "updated_at": row[11],
        "last_login_at": row[12],
        "allowed_offices": _load_json_list(row[13] if len(row) > 13 else None),
        "allowed_modules": _load_json_list(row[14] if len(row) > 14 else None),
        "sensor_alert_enabled": bool(row[15]) if len(row) > 15 else False,
        "sensor_temp_warning": row[16] if len(row) > 16 else 30,
        "sensor_temp_critical": row[17] if len(row) > 17 else 35,
        "sensor_hum_warning_high": row[18] if len(row) > 18 else 70,
        "sensor_hum_critical_high": row[19] if len(row) > 19 else 80,
        "sensor_hum_warning_low": row[20] if len(row) > 20 else 20,
        "sensor_alert_cooldown_minutes": row[21] if len(row) > 21 else 30,
        "sensor_alert_start_hour": row[22] if len(row) > 22 else 0,
        "sensor_alert_end_hour": row[23] if len(row) > 23 else 24,
    }


def count_users() -> int:
    """Return the number of user accounts."""

    try:
        with db_connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
            return int(row[0] or 0)
    except Exception as e:
        log.exception(f"Error counting users: {e}")
        return 0


# Common SELECT for the users table (to avoid duplication)
_USER_SELECT_COLS = '''
    id, username, email, password_hash, google_id, reset_token_hash,
    reset_token_expires, role, is_verified, is_active, created_at,
    updated_at, last_login_at, allowed_offices, allowed_modules,
    sensor_alert_enabled, sensor_temp_warning, sensor_temp_critical,
    sensor_hum_warning_high, sensor_hum_critical_high, sensor_hum_warning_low,
    sensor_alert_cooldown_minutes, sensor_alert_start_hour, sensor_alert_end_hour
'''


def _fetch_user(where_clause: str, params: tuple) -> Optional[dict]:
    """Helper function: execute SELECT on users with desired WHERE."""
    try:
        with db_connection() as conn:
            row = conn.execute(
                f"SELECT {_USER_SELECT_COLS} FROM users WHERE {where_clause}",
                params,
            ).fetchone()
            return _user_row_to_dict(row)
    except Exception as e:
        log.exception(f"Error fetching user with {where_clause!r}: {e}")
        return None


def _execute_user_update(sql: str, params: tuple, action_desc: str = "user") -> bool:
    """Helper function: execute UPDATE/DELETE on users and return True if >0 rows are affected."""
    try:
        with db_connection(commit=True) as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount > 0
    except Exception as e:
        log.exception(f"Error updating {action_desc}: {e}")
        return False


def get_user_by_id(user_id: int) -> Optional[dict]:
    """Return the user row with this id as a dict, or None."""

    return _fetch_user("id = ?", (user_id,))


def get_user_by_identifier(identifier: str) -> Optional[dict]:
    """Return the user matching a username or e-mail address, or None."""

    identifier = (identifier or "").strip().lower()
    if not identifier:
        return None
    return _fetch_user("lower(username) = ? OR lower(email) = ?", (identifier, identifier))


def get_user_by_email(email: str) -> Optional[dict]:
    """Return the user with this e-mail address, or None."""

    email = (email or "").strip().lower()
    if not email:
        return None
    return _fetch_user("lower(email) = ?", (email,))


def get_user_by_google_id(google_id: str) -> Optional[dict]:
    """Return the user linked to this Google account id, or None."""

    google_id = (google_id or "").strip()
    if not google_id:
        return None
    return _fetch_user("google_id = ?", (google_id,))


def create_user(username: str, email: str, password_hash: str = None, google_id: str = None,
                role: str = "viewer", is_verified: bool = False,
                allowed_offices=None, allowed_modules=None) -> Optional[dict]:
    """Insert a new user row and return its id."""

    now = utc_now_iso()
    username = (username or "").strip()
    if not USERNAME_RE.fullmatch(username):
        log.warning("Rejecting invalid username during user creation: %s", username)
        return None
    role = role if role in ("admin", "manager", "viewer") else "viewer"
    try:
        with db_connection(commit=True) as conn:
            # Phase E.4.3: Row ID is taken from adapter, not ``cur.lastrowid''
            # (which is absent on PG). ``insert_returning_id'' on SQLite same
            # ``lastrowid'' and on PG it uses ``INSERT... RETURNING id''.
            user_id = conn.insert_returning_id(
                '''
                INSERT INTO users (username, email, password_hash, google_id, role,
                                   is_verified, email_verified, is_active, created_at, updated_at,
                                   allowed_offices, allowed_modules)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                # Phase E.4.1: These three columns on PG have BOOLEAN type ⇒ Python value must
                # be bool (writing int on boolean gives a type error). On SQLite none
                # No difference: sqlite3 stores the bool value as 0.1.
                # is_active used to be literal "1" in SQL and became a parameter.
                (username, email.strip().lower(), password_hash, google_id,
                 role, bool(is_verified), bool(is_verified), True, now, now,
                 _dump_json_list(allowed_offices), _dump_json_list(allowed_modules)),
            )
        return get_user_by_id(user_id)
    except Exception as e:
        log.exception(f"Error creating user {username}: {e}")
        return None


def update_user_password(user_id: int, password_hash: str) -> bool:
    """Replace the stored password hash of a user."""

    return _execute_user_update(
        'UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?',
        (password_hash, utc_now_iso(), user_id),
        f"password for user {user_id}",
    )


def set_user_google_id(user_id: int, google_id: str) -> bool:
    """Link a Google account id to a user."""

    return _execute_user_update(
        'UPDATE users SET google_id = ?, updated_at = ? WHERE id = ?',
        (google_id, utc_now_iso(), user_id),
        f"google_id for user {user_id}",
    )


def touch_user_login(user_id: int) -> None:
    """Record the current time as the user's last login."""

    now = utc_now_iso()
    _execute_user_update(
        'UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?',
        (now, now, user_id),
        f"login timestamp for user {user_id}",
    )


def list_users() -> list:
    """Return all user rows as a list of dicts."""

    try:
        with db_connection() as conn:
            rows = conn.execute(
                f'''
                SELECT {_USER_SELECT_COLS}
                FROM users
                ORDER BY
                    CASE role
                        WHEN 'admin' THEN 0
                        WHEN 'manager' THEN 1
                        ELSE 2
                    END,
                    -- فاز E.4.11a (ردیف ۳ سند سخت‌گیری): ``COLLATE NOCASE`` نحو
                    -- SQLite است و روی PG با «collation "nocase" for encoding
                    -- "UTF8" does not exist» (42704) می‌شکند — و چون این تابع
                    -- استثنا را می‌گیرد، خطا بی‌صدا «[]» می‌داد (سنجیده‌شده).
                    -- ``lower(username)`` روی هر دو backend کار می‌کند و همان
                    -- ترتیب ASCII بی‌توجه به بزرگی/کوچکی را می‌دهد.
                    lower(username) ASC
                '''
            ).fetchall()
            return [_user_row_to_dict(r) for r in rows]
    except Exception as e:
        log.exception(f"Error listing users: {e}")
        return []


def update_user_role(user_id: int, role: str) -> bool:
    """Set a user's role (admin, manager or viewer)."""

    role = role if role in ("admin", "manager", "viewer") else None
    if not role:
        return False
    return _execute_user_update(
        'UPDATE users SET role = ?, updated_at = ? WHERE id = ?',
        (role, utc_now_iso(), user_id),
        f"role for user {user_id}",
    )


def update_user_verified(user_id: int, is_verified: bool) -> bool:
    # Phase E.4.1: Both columns on PG are of BOOLEAN type ⇒ Python value must be bool
    # (int gives a type error on boolean). The same 0.1 is stored on SQLite.
    """Set the verified flag of a user."""

    verified = bool(is_verified)
    return _execute_user_update(
        'UPDATE users SET is_verified = ?, email_verified = ?, updated_at = ? WHERE id = ?',
        (verified, verified, utc_now_iso(), user_id),
        f"verification for user {user_id}",
    )


def update_user_access(user_id: int, allowed_offices=None, allowed_modules=None) -> bool:
    """Set the allowed offices and modules of a user."""

    return _execute_user_update(
        '''
        UPDATE users
        SET allowed_offices = ?, allowed_modules = ?, updated_at = ?
        WHERE id = ?
        ''',
        (_dump_json_list(allowed_offices), _dump_json_list(allowed_modules),
         utc_now_iso(), user_id),
        f"access for user {user_id}",
    )


def update_user_sensor_alert(user_id: int, enabled=False, temp_warning=30, temp_critical=35,
                             hum_warning_high=70, hum_critical_high=80, hum_warning_low=20,
                             cooldown_minutes=30, start_hour=0, end_hour=24) -> bool:
    """Store the sensor-alert settings (enabled flag and temperature thresholds) of a user."""

    return _execute_user_update(
        '''
        UPDATE users
        SET sensor_alert_enabled = ?, sensor_temp_warning = ?, sensor_temp_critical = ?,
            sensor_hum_warning_high = ?, sensor_hum_critical_high = ?, sensor_hum_warning_low = ?,
            sensor_alert_cooldown_minutes = ?, sensor_alert_start_hour = ?, sensor_alert_end_hour = ?,
            updated_at = ?
        WHERE id = ?
        ''',
        (int(enabled), temp_warning, temp_critical,
         hum_warning_high, hum_critical_high, hum_warning_low,
         cooldown_minutes, start_hour, end_hour,
         utc_now_iso(), user_id),
        f"sensor alert settings for user {user_id}",
    )


def delete_user(user_id: int) -> bool:
    """Delete a user row."""

    return _execute_user_update(
        'DELETE FROM users WHERE id = ?',
        (user_id,),
        f"delete user {user_id}",
    )


def set_password_reset_token(user_id: int, token_hash: str, expires_at: str) -> bool:
    """Store a hashed password-reset token and its expiry time for a user."""

    return _execute_user_update(
        '''
        UPDATE users
        SET reset_token_hash = ?, reset_token_expires = ?, updated_at = ?
        WHERE id = ?
        ''',
        (token_hash, expires_at, utc_now_iso(), user_id),
        f"reset token for user {user_id}",
    )


def get_user_by_reset_token_hash(token_hash: str) -> Optional[dict]:
    """Return the user that owns this password-reset token hash, or None."""

    if not token_hash:
        return None
    return _fetch_user("reset_token_hash = ?", (token_hash,))


def clear_password_reset_token(user_id: int) -> bool:
    """Remove the password-reset token of a user."""

    return _execute_user_update(
        '''
        UPDATE users
        SET reset_token_hash = NULL, reset_token_expires = NULL, updated_at = ?
        WHERE id = ?
        ''',
        (utc_now_iso(), user_id),
        f"clear reset token for user {user_id}",
    )


def add_event(ip: str, etype: str, details: dict):
    """Insert one event row (type, details and timestamp) for a printer."""

    try:
        from core import store
        timestamp = details.get("timestamp", utc_now_iso())
        message = details.get("message", "")
        pages = details.get("pages")
        color = details.get("color")
        code = details.get("code")
        severity = details.get("severity", "info")
        paper_size = details.get("paper_size")
        username = details.get("username")
        # Keep the complete contract payload in JSON. The relational columns
        # are indexes for existing consumers; details remains the lossless
        # source for evidence and future event fields.
        other = dict(details)
        printer_name = None
        with store.printers_lock:
            for p in store.PRINTERS:
                if p["ip"] == ip:
                    printer_name = p["name"]
                    break
        # ----- Debounce noisy/duplicate events -----
        try:
            # configurable via env var `DB_DEBOUNCE_WINDOW_SECS` (seconds)
            # default: 60s (short window). Set to 600 for 10 minutes if desired.
            DEBOUNCE_WINDOW_SECS = int(os.getenv("DB_DEBOUNCE_WINDOW_SECS", "60"))
            # ALERT events get a longer debounce (4 hours) to prevent repeated
            # toner low/empty notifications for the same printer+color
            ALERT_DEBOUNCE_SECS = int(os.getenv("DB_ALERT_DEBOUNCE_SECS", "14400"))
            raw_types = os.getenv(
                "DB_DEBOUNCE_TYPES",
                "SNMP_COUNTER_READ_ERROR,SNMP_ERROR,COUNTER_ANOMALY,PRINT_OVERFLOW,PRINT_GAP,SNMP_TIMEOUT,SNMP_RETRY,TONER_ESTIMATE"
            ) or ""
            DEBOUNCE_TYPES = set([s.strip() for s in raw_types.split(",") if s.strip()])

            if etype in DEBOUNCE_TYPES or etype == 'ALERT':
                debounce_secs = ALERT_DEBOUNCE_SECS if etype == 'ALERT' else DEBOUNCE_WINDOW_SECS
                cutoff = to_utc_z(datetime.now(timezone.utc) - timedelta(seconds=debounce_secs))
                with db_connection() as conn:
                    # prefer code match when available (faster indexable comparison)
                    if code is not None:
                        cur = conn.execute(
                            "SELECT COUNT(1) FROM logs WHERE printer_ip=? AND type=? AND code=? AND timestamp>=?",
                            (ip, etype, str(code), cutoff),
                        ).fetchone()
                        if cur and cur[0] > 0:
                            log.debug(f"Debounced event {etype} for {ip} (code={code})")
                            return
                    # fallback to exact message match
                    if message:
                        cur = conn.execute(
                            "SELECT COUNT(1) FROM logs WHERE printer_ip=? AND type=? AND message=? AND timestamp>=?",
                            (ip, etype, message, cutoff),
                        ).fetchone()
                        if cur and cur[0] > 0:
                            log.debug(f"Debounced event {etype} for {ip} (same message)")
                            return
        except Exception:
            # on any error in debounce logic, proceed with inserting the event
            log.debug("debounce check failed, proceeding to add event")

        with db_connection(commit=True) as conn:
            if etype == "CARTRIDGE_CHANGED":
                event_id = details.get("event_id") or details.get("id")
                if event_id:
                    # Phase E.4.9: ``json_extract'' is a SQLite function ⇒ cross-backend helper.
                    event_id_sql = conn.json_extract("details", "event_id")
                    existing = conn.execute(
                        f"SELECT 1 FROM logs WHERE printer_ip=? AND type='CARTRIDGE_CHANGED' AND {event_id_sql}=? LIMIT 1",
                        (ip, str(event_id)),
                    ).fetchone()
                    if existing:
                        log.debug("Skipped duplicate CARTRIDGE_CHANGED for %s with event_id=%s", ip, event_id)
                        return
            if etype == "PRINT":
                prev_total = details.get("prev_total")
                current_total = details.get("current_total")
                if prev_total is not None and current_total is not None:
                    cutoff = to_utc_z(datetime.now(timezone.utc) - timedelta(seconds=300))
                    prev_sql = conn.json_extract("details", "prev_total", as_number=True)
                    curr_sql = conn.json_extract("details", "current_total", as_number=True)
                    duplicate = conn.execute(
                        f"""
                        SELECT 1 FROM logs
                        WHERE printer_ip = ? AND type = 'PRINT' AND timestamp >= ?
                          AND {prev_sql} = ?
                          AND {curr_sql} = ?
                        LIMIT 1
                        """,
                        (ip, cutoff, prev_total, current_total),
                    ).fetchone()
                    if duplicate:
                        log.debug(
                            "Skipped duplicate PRINT for %s (%s -> %s)",
                            ip, prev_total, current_total,
                        )
                        return
                    overlap = conn.execute(
                        f"""
                        SELECT 1 FROM logs
                        WHERE printer_ip = ? AND type = 'PRINT' AND timestamp >= ?
                          AND {prev_sql} IS NOT NULL
                          AND {curr_sql} IS NOT NULL
                          AND CAST({prev_sql} AS INTEGER) < ?
                          AND CAST({curr_sql} AS INTEGER) > ?
                        LIMIT 1
                        """,
                        (ip, cutoff, current_total, prev_total),
                    ).fetchone()
                    if overlap:
                        log.warning(
                            "Skipped overlapping PRINT for %s (%s -> %s); recent transition already covers this range",
                            ip, prev_total, current_total,
                        )
                        return
            # ✅ Semantic dedup for COUNTER_RESET: same printer + same transition = skip
            if etype == "COUNTER_RESET":
                prev_total_cr = details.get("prev_total")
                current_total_cr = details.get("current_total")
                if prev_total_cr is not None and current_total_cr is not None:
                    cutoff_cr = to_utc_z(datetime.now(timezone.utc) - timedelta(seconds=300))
                    cr_prev_sql = conn.json_extract("details", "prev_total", as_number=True)
                    cr_curr_sql = conn.json_extract("details", "current_total", as_number=True)
                    dup_cr = conn.execute(
                        f"""
                        SELECT 1 FROM logs
                        WHERE printer_ip = ? AND type = 'COUNTER_RESET' AND timestamp >= ?
                          AND {cr_prev_sql} = ?
                          AND {cr_curr_sql} = ?
                        LIMIT 1
                        """,
                        (ip, cutoff_cr, prev_total_cr, current_total_cr),
                    ).fetchone()
                    if dup_cr:
                        log.debug(
                            "Skipped duplicate COUNTER_RESET for %s (%s -> %s)",
                            ip, prev_total_cr, current_total_cr,
                        )
                        return

            conn.execute('''
                INSERT INTO logs (printer_ip, printer_name, timestamp, type, message,
                                  pages, color, code, severity, paper_size, username, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (ip, printer_name, timestamp, etype, message,
                  pages, color, code, severity, paper_size, username,
                  json.dumps(other, ensure_ascii=False)))
    except Exception as e:
        log.exception(f"Error adding event to DB: {e}")


def _row_to_dict(row) -> dict:
    # Phase E.6.4: ``logs.details'' on PostgreSQL is of type ``jsonb'' and psycopg
    # returns **decoded** (dict); On SQLite, it stays the same as TEXT and ``json.loads''
    # it is necessary The SQLite path is intentionally left untouched — including "malformed JSON ⇒ error", which
    # It is documented behavior today and is not swallowed silently.
    """Convert a logs database row to a dictionary and decode its details column."""
    _raw_details = row[11]
    if isinstance(_raw_details, dict):
        details = _raw_details
    elif _raw_details:
        details = json.loads(_raw_details)
    else:
        details = {}
    # Phase E.3: The order is explicit — ``details'' come first before additional contract keys
    # be available as event fields, but **table columns** (esp
    # ``timestamp'') always win: previously ``**details'' was last and if payload
    # The key had a ``timestamp'' (like cartridge engine events), a JSON value instead of a value
    # Gets the column in the API response.
    return {
        **details,
        "printer_ip":   row[0],
        "printer_name": row[1],
        "timestamp":    row[2],
        "type":         row[3],
        "message":      row[4],
        "pages":        row[5],
        "color":        row[6],
        "code":         row[7],
        "severity":     row[8],
        "paper_size":   row[9],
        "username":     row[10],
        "details":      details,
    }


def get_log(ip: str, limit: int = 500, ips=None) -> list:
    """Return the most recent log rows of one printer."""

    try:
        params = []
        if ips:
            ips = [str(item).strip() for item in ips if str(item).strip()]
            if not ips:
                return []
            placeholders = ",".join(["?"] * len(ips))
            where = f"WHERE printer_ip IN ({placeholders})"
            params.extend(ips)
        else:
            where = "WHERE printer_ip = ?"
            params.append(ip)
        with db_connection() as conn:
            rows = conn.execute(
                f'''
                SELECT printer_ip, printer_name, timestamp, type, message,
                       pages, color, code, severity, paper_size, username, details
                FROM logs {where}
                ORDER BY timestamp DESC LIMIT ?
                ''',
                params + [limit],
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
    except Exception as e:
        log.exception(f"Error reading logs from DB: {e}")
        return []


def get_all_logs(start=None, end=None, limit: int = 1000, ip=None, ips=None) -> list:
    """Return log rows across printers, optionally filtered by time range and printer."""

    try:
        # Phase E.3: Range parameters come from query string (local/naive/aware date) and
        # Before comparing a string with a timestamp column, the format of the stored data must be the same
        # become Rule and boundary conditions in core/time_utils.to_filter_bound.
        start = to_filter_bound(start)
        end = to_filter_bound(end)
        params = []
        conditions = []
        if ips:
            ips = [str(item).strip() for item in ips if str(item).strip()]
            if not ips:
                return []
            placeholders = ",".join(["?"] * len(ips))
            conditions.append(f"printer_ip IN ({placeholders})")
            params.extend(ips)
        elif ip:
            conditions.append("printer_ip = ?")
            params.append(ip)
        if start and end:
            conditions.append("timestamp BETWEEN ? AND ?")
            params.extend([start, end])
        elif start:
            conditions.append("timestamp >= ?")
            params.append(start)
        elif end:
            conditions.append("timestamp <= ?")
            params.append(end)

        query = '''
            SELECT printer_ip, printer_name, timestamp, type, message,
                   pages, color, code, severity, paper_size, username, details
            FROM logs
        '''
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with db_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [_row_to_dict(r) for r in rows]
    except Exception as e:
        log.exception(f"Error reading all logs: {e}")
        return []


def clear_logs(ip=None, ips=None, types=None) -> int:
    """
    Delete events.
    If types are specified, only those types are deleted.
    Otherwise, events other than PRINT, SERVICE and REFILL are cleared.
    """
    try:
        params = []
        where_clauses = []

        if ips:
            ips = [str(item).strip() for item in ips if str(item).strip()]
            if not ips:
                return 0
            ip_placeholders = ','.join(['?'] * len(ips))
            where_clauses.append(f"printer_ip IN ({ip_placeholders})")
            params.extend(ips)
        elif ip:
            where_clauses.append("printer_ip = ?")
            params.append(ip)

        if types:
            # If certain types are selected, delete exactly those
            type_placeholders = ','.join(['?'] * len(types))
            where_clauses.append(f"type IN ({type_placeholders})")
            params.extend(types)
        else:
            # Previous behavior: Clear all but critical items
            keep_types = ('PRINT', 'SERVICE', 'REFILL')
            type_placeholders = ','.join(['?'] * len(keep_types))
            where_clauses.append(f"type NOT IN ({type_placeholders})")
            params.extend(keep_types)

        sql = "DELETE FROM logs"
        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)

        with db_connection(commit=True) as conn:
            cur = conn.execute(sql, params)
            deleted = cur.rowcount

        log.info(f"clear_logs: {deleted} رویداد پاک شد. فیلتر نوع: {types if types else 'auto'}")
        return deleted
    except Exception as e:
        log.exception(f"Error clearing logs: {e}")
        return 0


def ensure_printer_counters_columns():
    """Ensure that all printer_counters columns exist.

    The only source of columns is ``_counter_column_alters()'' (the same list as
    uses ``init_db()''); So the column in ``_COUNTERS_COLS''
    It is read that he can no longer stay on the migration path.
    """
    if resolve_backend() == BACKEND_POSTGRESQL:
        # Rule E.4.5 (Alembic only source DDL on PG): These ALTERs are not on PG
        # Required, not safe — any ALTER error aborts the entire transaction (25P02) and
        # As a result, only one exception was recorded in the log (measurement E.5: 3 failed
        # test_db_migration_columns). So it is inactive on PG.
        return
    try:
        with db_connection(commit=True) as conn:
            for column_sql in _counter_column_alters():
                try:
                    conn.execute(column_sql)
                except ColumnAlreadyExists:   # Phase E.4.4: Common type of both backends
                    pass  # The column already exists
    except Exception as e:
        log.exception(f"Error ensuring printer_counters columns: {e}")


def prune_old_print_logs(days=90) -> int:
    """Automatic deletion of PRINT type logs that are older than days."""
    cutoff = to_utc_z(datetime.now(timezone.utc) - timedelta(days=days))
    try:
        with db_connection(commit=True) as conn:
            cur = conn.execute("DELETE FROM logs WHERE type='PRINT' AND timestamp < ?", (cutoff,))
            deleted = cur.rowcount
        if deleted:
            log.info(f"prune_old_print_logs: {deleted} رکورد PRINT قدیمی پاک شد")
        return deleted
    except Exception as e:
        log.exception(f"Error pruning old logs: {e}")
        return 0


# The definition of the printer_counters column unit
# The only source of schema truth: both the ``create new table'' path (init_db) and the path
# "Migrating the old table" (ensure_printer_counters_columns) from these two lists
# are fed to the migration slot (a column that is read but never added)
# Do not repeat again.
_COUNTER_BASE_COLUMNS = (
    ("device_type", "TEXT"),
    ("print_total", "INTEGER"),
    ("full_color", "INTEGER"),
    ("black_white", "INTEGER"),
    ("toner_level", "INTEGER"),
    ("manual_override", "INTEGER DEFAULT 0"),
    ("override_color", "TEXT"),
    ("override_base_level", "INTEGER"),
    ("override_start_total", "INTEGER"),
    ("override_start_toner", "INTEGER"),
    ("yield_per_page", "INTEGER DEFAULT 2000"),
    ("force_estimate", "INTEGER DEFAULT 0"),
    ("yield_learning_failures", "INTEGER DEFAULT 0"),
    ("last_alert_codes", "TEXT"),
    ("a3_total", "INTEGER"),
    ("a4_total", "INTEGER"),
    ("alert_codes", "TEXT"),
    ("cartridge_id_state", "TEXT"),
    ("updated_at", "TEXT"),
)

# Columns that were added after the first version of the table (only come from the migration path).
_COUNTER_ADDED_COLUMNS = (
    ("toner_overrides", "TEXT DEFAULT NULL"),
)

_COUNTER_COLUMNS = _COUNTER_BASE_COLUMNS + _COUNTER_ADDED_COLUMNS

# Names of columns for SELECT — ⚠ order of this string to row indices
# load_printer_counters/save_printer_counters is tied; Do not change the order.
_COUNTERS_COLS = (
    "device_type, print_total, full_color, black_white, toner_level, manual_override, "
    "override_color, override_base_level, override_start_total, override_start_toner, "
    "yield_per_page, force_estimate, yield_learning_failures, last_alert_codes, "
    "a3_total, a4_total, alert_codes, cartridge_id_state, toner_overrides, updated_at"
)


#: columns inserted in ``save_printer_counters'' — **all** columns of the table.
#: The name of this list is the same as ``_COUNTERS_COLS'' and with the schema of the table
#: ``tests/test_upsert_cross_backend.py'' is locked because ``INSERT OR REPLACE''
#: Only when "ON CONFLICT DO UPDATE" means that all columns are written
#: (``docs/insert-or-replace-inventory.md`` §3.1).
_COUNTERS_UPSERT_COLUMNS = (
    "ip", "device_type", "print_total", "full_color", "black_white", "toner_level",
    "manual_override", "override_color", "override_base_level", "override_start_total",
    "override_start_toner", "yield_per_page", "force_estimate", "yield_learning_failures",
    "last_alert_codes", "a3_total", "a4_total", "alert_codes", "cartridge_id_state",
    "toner_overrides", "updated_at",
)


def _counter_column_alters() -> tuple:
    """ALTERs needed to complete the printer_counters table.

    Order: Basic columns first, then added columns. On the new table or
    Semi-migrated, duplicate ALTERs give ``duplicate column name'' error
    and are ignored by the caller (idempotent).
    """
    return tuple(
        f"ALTER TABLE printer_counters ADD COLUMN {name} {decl}"
        for name, decl in _COUNTER_COLUMNS
    )


def _counter_columns_create_sql() -> str:
    """Body of CREATE TABLE printer_counters (without ``ip'' which is the PRIMARY KEY)."""
    return ",\n".join(f"            {name} {decl}" for name, decl in _COUNTER_BASE_COLUMNS)


def load_printer_counters(ip: str) -> Optional[dict]:
    """Load the latest counter values from the database"""
    try:
        with db_connection() as conn:
            row = conn.execute(
                f"SELECT {_COUNTERS_COLS} FROM printer_counters WHERE ip = ?",
                (ip,),
            ).fetchone()
        if not row:
            return None
        return {
            "device_type": row[0],
            "print_total": row[1],
            "full_color": row[2],
            "black_white": row[3],
            "toner_level": row[4],
            "manual_override": row[5] or 0,
            "override_color": row[6],
            "override_base_level": row[7],
            "override_start_total": row[8],
            "override_start_toner": row[9],
            "yield_per_page": row[10] if row[10] is not None else 2000,
            "force_estimate": row[11] or 0,
            "yield_learning_failures": row[12] or 0,
            "last_alert_codes": _load_json_list(row[13]),
            "a3_total": row[14],
            "a4_total": row[15],
            "alert_codes": _load_json_list(row[16]),
            **_load_cartridge_id_state(row[17]),
            "toner_overrides": _load_json_dict(row[18]),
            "updated_at": row[19],
        }
    except Exception as e:
        log.exception(f"Error loading counters for {ip}: {e}")
        return None


def save_printer_counters(ip: str, data: dict):
    """Save the counter values in the database"""
    ensure_printer_counters_columns()
    updated_at = data.get("updated_at") or utc_now_iso()
    try:
        with db_connection(commit=True) as conn:
            # Phase E.5: The sentence is generated from the cross-backend helper (previously on PG
            # breaks with "syntax error at or near "OR"" (42601). The order of the columns and
            # The parameters are intact.
            conn.execute(
                _upsert_sql("printer_counters", _COUNTERS_UPSERT_COLUMNS, ("ip",),
                            backend=conn.backend), (
                ip,
                data.get("device_type"),
                data.get("print_total"),
                data.get("full_color"),
                data.get("black_white"),
                data.get("toner_level"),
                data.get("manual_override", 0),
                data.get("override_color"),
                data.get("override_base_level"),
                data.get("override_start_total"),
                data.get("override_start_toner"),
                data.get("yield_per_page", 2000),
                data.get("force_estimate", 0),
                data.get("yield_learning_failures", 0),
                json.dumps(data.get("last_alert_codes", [])),
                data.get("a3_total"),
                data.get("a4_total"),
                json.dumps(data.get("alert_codes", [])),
                _dump_cartridge_id_state(data),
                json.dumps(data.get("toner_overrides") or {}),
                updated_at,
            ))
    except Exception as e:
        log.exception(f"Error saving counters for {ip}: {e}")


def delete_printer_counters(ip: str) -> bool:
    """remove baseline/counters of a printer; To remove the device or prevent IP reuse."""
    try:
        with db_connection(commit=True) as conn:
            conn.execute("DELETE FROM printer_counters WHERE ip = ?", (ip,))
        return True
    except Exception as e:
        log.exception(f"Error deleting counters for {ip}: {e}")
        return False


def record_sensor_readings(ip: str, readings: list, timestamp: str = None):
    """Save sensor readings for daily average chart.

    readings: [{"port": 1, "kind": "temperature"|"humidity", "value": 25.3, "unit": "°C", "status": "active"}]
    """
    if not readings:
        return
    ts = timestamp or utc_now_iso()
    rows = []
    for item in readings:
        try:
            value = item.get("value")
            if value is None:
                continue
            rows.append((
                ip,
                ts,
                int(item.get("port") or 0),
                str(item.get("kind") or ""),
                float(value),
                item.get("unit"),
                item.get("status"),
            ))
        except (TypeError, ValueError):
            continue
    if not rows:
        return
    try:
        with db_connection(commit=True) as conn:
            conn.executemany(
                """
                INSERT INTO sensor_readings (printer_ip, timestamp, port, kind, value, unit, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
    except Exception as e:
        log.exception(f"Error recording sensor readings for {ip}: {e}")


def record_toner_snapshot(ip: str, print_total: int = None, toner_level: int = None,
                         yield_per_page: int = None, timestamp: str = None,
                         source: str = "poll"):
    """Save a snapshot of the counter/toner for historical yield learning."""
    if print_total is None and toner_level is None:
        return
    try:
        with db_connection(commit=True) as conn:
            conn.execute(
                """
                INSERT INTO toner_history (printer_ip, timestamp, print_total, toner_level, yield_per_page, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    ip,
                    timestamp or utc_now_iso(),
                    print_total,
                    toner_level,
                    yield_per_page,
                    source,
                ),
            )
    except Exception as e:
        log.exception(f"Error recording toner snapshot for {ip}: {e}")


def estimate_yield_from_history(ip: str, days: int = 7, min_points: int = 3, min_pages: int = 500):
    """Estimation of yield_per_page from historical snapshots of toner_history."""
    cutoff = to_utc_z(datetime.now(timezone.utc) - timedelta(days=days))
    try:
        with db_connection() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, print_total, toner_level
                FROM toner_history
                WHERE printer_ip = ? AND timestamp >= ?
                ORDER BY timestamp ASC
                """,
                (ip, cutoff),
            ).fetchall()
        if len(rows) < 2:
            return None

        total_pages = 0
        total_drop = 0
        sample_points = 0
        days_seen = set()
        prev_total = None
        prev_toner = None
        for ts, print_total, toner_level in rows:
            if ts:
                # Phase E.3: "day" means the local day (like the old substr on the local naive data),
                # Nine days of UTC. The unmigrated row does not break the estimate; The raw day is recorded.
                try:
                    days_seen.add(to_local_day(ts))
                except ValueError:
                    days_seen.add(str(ts)[:10])
            if print_total is None or toner_level is None:
                prev_total, prev_toner = print_total, toner_level
                continue
            if prev_total is None or prev_toner is None:
                prev_total, prev_toner = print_total, toner_level
                continue
            delta_pages = int(print_total) - int(prev_total)
            toner_drop = int(prev_toner) - int(toner_level)
            if delta_pages > 0 and toner_drop > 0:
                total_pages += delta_pages
                total_drop += toner_drop
                sample_points += 1
            prev_total, prev_toner = print_total, toner_level

        if total_drop <= 0:
            return None
        # To avoid false yield, both the number of samples and the number of pages should be sufficient.
        if sample_points < min_points or total_pages < min_pages:
            return None

        estimated_yield = int(round(total_pages * 100.0 / total_drop))
        if not 300 <= estimated_yield <= 20000:
            return None
        return {
            "yield_per_page": estimated_yield,
            "sample_points": sample_points,
            "total_pages": total_pages,
            "total_drop": total_drop,
            "days_seen": len(days_seen),
        }
    except Exception as e:
        log.exception(f"Error estimating historical yield for {ip}: {e}")
        return None


def get_reset_history(ip=None, limit: int = 1000, ips=None, current_totals=None) -> list:
    """Get cartridge reset history for Excel reporting."""
    try:
        params = ['REFILL']
        conditions = ["type = ?"]
        if ips:
            ips = [str(item).strip() for item in ips if str(item).strip()]
            if not ips:
                return []
            placeholders = ",".join(["?"] * len(ips))
            conditions.append(f"printer_ip IN ({placeholders})")
            params.extend(ips)
        elif ip:
            conditions.append("printer_ip = ?")
            params.append(ip)

        query = f'''
            SELECT printer_ip, printer_name, timestamp, type, message,
                   pages, color, code, severity, paper_size, username, details
            FROM logs
            WHERE {' AND '.join(conditions)}
            ORDER BY printer_ip ASC, timestamp ASC
        '''
        with db_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        events = [_row_to_dict(r) for r in rows]
        events = [e for e in events if e.get('manual_reset')]
        if not events:
            return []

        current_totals = current_totals or {}
        grouped = {}
        for ev in events:
            grouped.setdefault(ev.get('printer_ip'), []).append(ev)

        history = []
        for printer_ip, printer_events in grouped.items():
            # Phase E.3: Real time sorting (not string comparison) — one row
            # Unmigrated is enough to break string order.
            printer_events.sort(key=lambda ev: to_sort_key(ev.get('timestamp')))
            for idx, event in enumerate(printer_events):
                total_at_reset = event.get('total_at_reset')
                next_total = None
                if idx + 1 < len(printer_events):
                    next_total = printer_events[idx + 1].get('total_at_reset')
                else:
                    next_total = current_totals.get(printer_ip)
                    if next_total is None:
                        counters = load_printer_counters(printer_ip) or {}
                        next_total = counters.get('print_total')
                try:
                    printed_after = int(next_total) - int(total_at_reset) if next_total is not None and total_at_reset is not None else None
                except (TypeError, ValueError):
                    printed_after = None
                if printed_after is not None and printed_after < 0:
                    printed_after = 0
                history.append({
                    'timestamp': event.get('timestamp'),
                    'printer_ip': printer_ip,
                    'printer_name': event.get('printer_name'),
                    'color': event.get('reset_color') or event.get('color') or '',
                    'set_level': event.get('set_level'),
                    'total_pages_at_reset': total_at_reset,
                    'pages_printed_after_reset': printed_after,
                    'pages_per_1pct': event.get('yield_per_page_at_reset') or event.get('yield_per_page'),
                })

        history.sort(key=lambda item: to_sort_key(item.get('timestamp')), reverse=True)
        return history[:limit]
    except Exception as e:
        log.exception(f"Error building reset history: {e}")
        return []



