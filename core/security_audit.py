"""
Security Audit Module
ثبت رویدادهای امنیتی مانند ورود ناموفق، حملات brute-force، و فعالیت‌های مشکوک.
"""

import logging
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

from core.db import db_connection as core_db_connection
from core.db.schema import (
    SECURITY_TABLES as _PG_SECURITY_TABLES,
    is_postgres,
    verify_postgres_tables,
)
from core.time_utils import to_filter_bound, to_utc_z, utc_now_iso

log = logging.getLogger("PrinterMonitor")

# ─── ثابت‌ها ─────────────────────────────────────────────────────
MAX_FAILED_ATTEMPTS_PER_USER = 5      # حداکثر تلاش ناموفق برای هر کاربر
MAX_FAILED_ATTEMPTS_PER_IP = 10       # حداکثر تلاش ناموفق برای هر IP
FAILED_ATTEMPT_WINDOW_MINUTES = 15    # بازه زمانی بررسی (دقیقه)

# ─── Enumها ─────────────────────────────────────────────────────
class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

class SecurityEvent(str, Enum):
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

# ─── اتصال دیتابیس ─────────────────────────────────────────────
# توجه (فاز D): پیاده‌سازی به core.db منتقل شده است؛ این تابع فقط پوسته‌ی
# سازگاری است تا نام و فراخوانی‌های موجود (`_db_conn(...)`) دست‌نخورده بماند.
@contextmanager
def _db_conn(timeout: float = 10.0, commit: bool = False):
    """Context manager برای اتصال امن به دیتابیس security audit (پوسته‌ی core.db)."""
    with core_db_connection(timeout=timeout, commit=commit) as conn:
        yield conn

# ─── راه‌اندازی جدول ─────────────────────────────────────────────
def init_security_audit():
    """ایجاد جدول security_events اگر وجود نداشته باشد (روی PG: فقط تأیید).

    روی PostgreSQL (فاز E.4.5) هیچ DDLی اجرا نمی‌شود — اسکیما از Alembic می‌آید —
    و در نبود جدول، خطای روشن می‌دهیم. این خطا **بیرون** از ``try`` زیر است تا
    مثل گذشته در log سرکوب نشود (شکست بی‌صدا همان چیزی است که ردیف ۸ سند را
    ماه‌ها پنهان کرده بود).
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

# ─── ثبت رویداد امنیتی ─────────────────────────────────────────
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
    ثبت یک رویداد امنیتی در دیتابیس.
    
    پارامترها:
        event_type: نوع رویداد (از SecurityEvent)
        severity: سطح اهمیت (از Severity)
        user_identifier: نام کاربری یا ایمیل
        user_id: شناسه کاربر
        ip_address: آدرس IP
        user_agent: User-Agent مرورگر
        endpoint: endpoint مورد نظر
        success: آیا عملیات موفق بود
        details: توضیحات اضافی
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
                # فاز E.4.1: ستون success روی PG BOOLEAN است ⇒ bool، نه int.
                bool(success),
                details,
                now,
            ))
    except Exception as e:
        log.error(f"Failed to log security event: {e}")

# ─── شمارش تلاش‌های ناموفق اخیر ─────────────────────────────────
def count_recent_failed_logins(
    ip_address: str = None,
    user_identifier: str = None,
    minutes: int = FAILED_ATTEMPT_WINDOW_MINUTES,
) -> int:
    """
    شمارش تعداد تلاش‌های ناموفق ورود در بازه زمانی مشخص.
    
    پارامترها:
        ip_address: آدرس IP برای فیلتر
        user_identifier: نام کاربری برای فیلتر
        minutes: بازه زمانی (دقیقه)
    
    بازگشت: تعداد تلاش‌های ناموفق
    """
    try:
        # فاز E.3: cutoff هم‌فرمت با داده‌ی ذخیره‌شده (UTC/Z) تا مقایسه‌ی رشته‌ای درست بماند.
        cutoff = to_utc_z(datetime.now(timezone.utc) - timedelta(minutes=minutes))
        # فاز E.4.1: مقایسه‌ی success با پارامتر انجام می‌شود، نه literal «0»:
        # روی PG نوع ستون BOOLEAN است و «success = 0» خطای نوع می‌دهد
        # (`operator does not exist: boolean = integer`)، در حالی که پارامتر bool
        # روی هر دو backend یک معنا دارد (SQLite: 0/1 · PG: false/true).
        # معنای فیلتر عوض نشده است: مقدار NULL در هر دو backend مطابقت نمی‌کند.
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

# ─── دریافت رویدادهای اخیر ─────────────────────────────────────
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
    دریافت رویدادهای امنیتی اخیر با فیلترهای مختلف.
    
    پارامترها:
        limit: حداکثر تعداد نتایج
        event_type: فیلتر نوع رویداد
        severity: فیلتر سطح اهمیت
        user_identifier: فیلتر کاربر
        ip_address: فیلتر IP
        start: تاریخ شروع (ISO format)
        end: تاریخ پایان (ISO format)
    
    بازگشت: لیست دیکشنری‌های رویداد
    """
    try:
        # فاز E.3: فیلترهای بازه مثل /api/logs/all از ورودی کاربر می‌آیند ⇒ پیش از
        # مقایسه‌ی رشته‌ای با ستون timestamp به فرمت استاندارد Z تبدیل می‌شوند.
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

# ─── پاکسازی رویدادهای قدیمی ─────────────────────────────────────
def cleanup_old_events(days: int = 90) -> int:
    """
    حذف رویدادهای امنیتی قدیمی‌تر از تعداد روز مشخص.
    
    پارامترها:
        days: تعداد روز (پیش‌فرض: ۹۰ روز)
    
    بازگشت: تعداد رکوردهای حذف شده
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
