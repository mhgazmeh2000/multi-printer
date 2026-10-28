# =============================================================================
# @file:         core/security_audit.py
# @brief:        Records security events such as failed logins and suspicious activity.
# @details:      Responsibilities: Write security events to the database and count recent failed
#                logins.
#                Main API: Severity, SecurityEvent, init_security_audit(), log_security_event(),
#                count_recent_failed_logins(), get_recent_events(), cleanup_old_events()
#                Side effects: database access
# @dependencies: internal: core.db, core.db.schema, core.time_utils
# @notes:        None.
# =============================================================================

"""
Security Audit Module
Log security events such as failed logins, brute-force attacks, and suspicious activity.
"""

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import List, Dict, Any

from core.db import db_connection as core_db_connection
from core.db.schema import (
    SECURITY_TABLES as _PG_SECURITY_TABLES,
    is_postgres,
    verify_postgres_tables,
)
from core.time_utils import to_filter_bound, to_utc_z, utc_now_iso

log = logging.getLogger("PrinterMonitor")

# - Constants - Constants
MAX_FAILED_ATTEMPTS_PER_USER = 5      # Maximum failed attempts per user
MAX_FAILED_ATTEMPTS_PER_IP = 10       # Maximum failed attempts per IP
FAILED_ATTEMPT_WINDOW_MINUTES = 15    # Check interval (minutes)

# Enums Enums
class Severity(str, Enum):
    """Severity levels of security events."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

class SecurityEvent(str, Enum):
    """One security event (type, severity and details) as recorded by the audit log."""

    SUCCESSFUL_LOGIN = "successful_login"
    FAILED_LOGIN = "failed_login"
    ACCOUNT_LOCKED = "account_locked"
    ACCOUNT_CREATED = "account_created"
    PASSWORD_RESET_REQUESTED = "password_reset_requested"
    PASSWORD_RESET_COMPLETED = "password_reset_completed"
    SUSPICIOUS_ACTIVITY = "suspicious_activity"
    OAUTH_LOGIN = "oauth_login"
    OAUTH_FAILURE = "oauth_failure"
    LOGOUT = "logout"
    RATE_LIMIT_HIT = "rate_limit_hit"
    USER_CREATED = "user_created"
    USER_ROLE_CHANGED = "user_role_changed"
    USER_VERIFICATION_CHANGED = "user_verification_changed"
    USER_ACCESS_CHANGED = "user_access_changed"
    USER_DELETED = "user_deleted"

# Database connection
# Note (Phase D): Implementation moved to core.db; This function is only a shell
# It is compatible so that existing names and calls (`_db_conn(...)`) remain intact.
@contextmanager
def _db_conn(timeout: float = 10.0, commit: bool = False):
    """Context manager for secure connection to security audit database (core.db shell)."""
    with core_db_connection(timeout=timeout, commit=commit) as conn:
        yield conn

# Setting up the table
def init_security_audit():
    """Create security_events table if it doesn't exist (on PG: verification only).

    On PostgreSQL (phase E.4.5) no DDL is executed — schema comes from Alembic —
    And in the absence of the table, we give a clear error. This error is **out** of the ``try'' below
    Do not suppress in the log as before (silent failure is the same as line 8 of the document
    had hidden for months).
    """
    if is_postgres():
        with _db_conn() as conn:
            verify_postgres_tables(conn, _PG_SECURITY_TABLES, context="init_security_audit")
        log.info("security_events: schema از Alembic تأیید شد (روی PG جدول ساخته نمی‌شود).")
        return

    try:
        with _db_conn(commit=True) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS security_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'info',
                    user_identifier TEXT,
                    user_id INTEGER,
                    ip_address TEXT,
                    user_agent TEXT,
                    endpoint TEXT,
                    success INTEGER DEFAULT 0,
                    details TEXT,
                    created_at TEXT NOT NULL
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_sec_timestamp ON security_events(timestamp)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_sec_event_type ON security_events(event_type)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_sec_ip ON security_events(ip_address)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_sec_user ON security_events(user_identifier)')
        log.info("Security audit table initialized")
    except Exception as e:
        log.exception("Failed to init security_audit table: %s", e)

# Security event registration
def log_security_event(
    event_type: SecurityEvent,
    severity: Severity = Severity.INFO,
    user_identifier: str = None,
    user_id: int = None,
    ip_address: str = None,
    user_agent: str = None,
    endpoint: str = None,
    success: bool = False,
    details: str = None,
):
    """
    Register a security event in the database.
    
    Parameters:
        event_type: event type (from SecurityEvent)
        severity: level of importance (from Severity)
        user_identifier: username or email
        user_id: User ID
        ip_address: IP address
        user_agent: Browser User-Agent
        endpoint: desired endpoint
        success: Was the operation successful?
        details: Additional details
    """
    try:
        now = utc_now_iso()
        with _db_conn(commit=True) as conn:
            conn.execute('''
                INSERT INTO security_events 
                (timestamp, event_type, severity, user_identifier, user_id, 
                 ip_address, user_agent, endpoint, success, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                now,
                event_type.value if isinstance(event_type, SecurityEvent) else str(event_type),
                severity.value if isinstance(severity, Severity) else str(severity),
                user_identifier,
                user_id,
                ip_address,
                user_agent,
                endpoint,
                # Phase E.4.1: Success column on PG is BOOLEAN ⇒ bool, not int.
                bool(success),
                details,
                now,
            ))
    except Exception as e:
        log.error(f"Failed to log security event: {e}")

# Counting recent failed attempts
def count_recent_failed_logins(
    ip_address: str = None,
    user_identifier: str = None,
    minutes: int = FAILED_ATTEMPT_WINDOW_MINUTES,
) -> int:
    """
    Count the number of failed login attempts in the specified time period.
    
    Parameters:
        ip_address: IP address for the filter
        user_identifier: username for the filter
        minutes: time interval (minutes)
    
    Returns: Number of failed attempts
    """
    try:
        # Phase E.3: Cutoff is the same format as the stored data (UTC/Z) so that string comparison remains correct.
        cutoff = to_utc_z(datetime.now(timezone.utc) - timedelta(minutes=minutes))
        # Phase E.4.1: Comparison of success is done with parameter, not literal "0":
        # On PG the column type is BOOLEAN and "success = 0" gives a type error
        # (`operator does not exist: boolean = integer`), while the parameter bool
        # It has the same meaning on both backends (SQLite: 0/1 · PG: false/true).
        # Filter meaning is not changed: NULL value does not match in both backends.
        conditions = ["event_type = ?", "success = ?", "timestamp >= ?"]
        params = [SecurityEvent.FAILED_LOGIN.value, False, cutoff]

        if ip_address:
            conditions.append("ip_address = ?")
            params.append(ip_address)
        if user_identifier:
            conditions.append("user_identifier = ?")
            params.append(user_identifier)

        where = " AND ".join(conditions)

        with _db_conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM security_events WHERE {where}",
                params
            ).fetchone()
            return int(row[0] or 0)
    except Exception as e:
        log.error(f"Failed to count failed logins: {e}")
        return 0

# Receive recent events
def get_recent_events(
    limit: int = 100,
    event_type: SecurityEvent = None,
    severity: Severity = None,
    user_identifier: str = None,
    user_id: int = None,
    ip_address: str = None,
    start: str = None,
    end: str = None,
) -> List[Dict[str, Any]]:
    """
    Get recent security events with different filters.
    
    Parameters:
        limit: maximum number of results
        event_type: event type filter
        severity: importance level filter
        user_identifier: User filter
        ip_address: IP filter
        start: start date (ISO format)
        end: end date (ISO format)
    
    Returns: List of event dictionaries
    """
    try:
        # Phase E.3: Interval filters like /api/logs/all come from user input ⇒ pre
        # String comparison with timestamp column are converted to standard Z format.
        start = to_filter_bound(start)
        end = to_filter_bound(end)
        conditions = []
        params = []

        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type.value if isinstance(event_type, SecurityEvent) else str(event_type))
        if severity:
            conditions.append("severity = ?")
            params.append(severity.value if isinstance(severity, Severity) else str(severity))
        if user_identifier:
            conditions.append("user_identifier = ?")
            params.append(user_identifier)
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if ip_address:
            conditions.append("ip_address = ?")
            params.append(ip_address)
        if start:
            conditions.append("timestamp >= ?")
            params.append(start)
        if end:
            conditions.append("timestamp <= ?")
            params.append(end)

        where = " WHERE " + " AND ".join(conditions) if conditions else ""

        with _db_conn() as conn:
            rows = conn.execute(f'''
                SELECT id, timestamp, event_type, severity, user_identifier,
                       user_id, ip_address, user_agent, endpoint, success, details
                FROM security_events {where}
                ORDER BY timestamp DESC LIMIT ?
            ''', params + [limit]).fetchall()

            return [
                {
                    "id": row[0],
                    "timestamp": row[1],
                    "event_type": row[2],
                    "severity": row[3],
                    "user_identifier": row[4],
                    "user_id": row[5],
                    "ip_address": row[6],
                    "user_agent": row[7],
                    "endpoint": row[8],
                    "success": bool(row[9]),
                    "details": row[10],
                }
                for row in rows
            ]
    except Exception as e:
        log.error(f"Failed to get recent events: {e}")
        return []

# Clearing old events
def cleanup_old_events(days: int = 90) -> int:
    """
    Delete security events older than specified number of days.
    
    Parameters:
        days: number of days (default: 90 days)
    
    Returns: The number of deleted records
    """
    try:
        cutoff = to_utc_z(datetime.now(timezone.utc) - timedelta(days=days))
        with _db_conn(commit=True) as conn:
            cur = conn.execute(
                "DELETE FROM security_events WHERE timestamp < ?",
                (cutoff,)
            )
            deleted = cur.rowcount
        if deleted:
            log.info(f"Cleaned up {deleted} old security events (>{days} days)")
        return deleted
    except Exception as e:
        log.error(f"Failed to cleanup old events: {e}")
        return 0
