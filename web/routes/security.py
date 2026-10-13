"""
Endpoint های نمایش لاگ‌های امنیتی (فقط برای admin).
"""

import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request, render_template

from web.auth import admin_required
from core.security_audit import get_recent_events, SecurityEvent, _db_conn
from core.time_utils import to_utc_z
import json

#: E.6.7 (TD-004): مسیر خطای `api_security_stats` با `log.exception` صدا می‌زد در
#: حالی که این نام در ماژول تعریف نبود ⇒ خودِ handler خطا، `NameError` می‌داد و
#: پاسخ JSON برنامه‌ریزی‌شده (و رکورد لاگ) هرگز تولید نمی‌شد. نام، همان قرارداد
#: سایر routeهاست.
log = logging.getLogger("PrinterMonitor")

EVENT_TYPE_LABELS = {
    "successful_login": "ورود موفق",
    "failed_login": "ورود ناموفق",
    "account_locked": "قفل شدن حساب",
    "account_created": "ایجاد حساب",
    "password_reset_requested": "درخواست بازنشانی رمز",
    "password_reset_completed": "تکمیل بازنشانی رمز",
    "suspicious_activity": "فعالیت مشکوک",
    "oauth_login": "ورود با OAuth",
    "oauth_failure": "خطای OAuth",
    "logout": "خروج از حساب",
    "rate_limit_hit": "عبور از محدودیت درخواست",
    "user_created": "ایجاد کاربر",
    "user_role_changed": "تغییر نقش کاربر",
    "user_verification_changed": "تغییر تأیید کاربر",
    "user_access_changed": "تغییر دسترسی کاربر",
    "user_deleted": "حذف کاربر",
}


def _localize_event(event):
    event["event_type_fa"] = EVENT_TYPE_LABELS.get(event.get("event_type"), event.get("event_type", "—"))
    raw = event.get("details")
    try:
        details = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        details = {"متن": raw} if raw else {}
    labels = {
        "target_username": "کاربر هدف", "target_user_id": "شناسه هدف", "role": "نقش",
        "old_role": "نقش قبلی", "new_role": "نقش جدید", "is_verified": "تأیید شده",
        "allowed_offices": "دفاتر مجاز", "allowed_modules": "ماژول‌های مجاز",
        "email_sent": "ایمیل ارسال شد", "full_access": "دسترسی کامل", "deleted_user_id": "شناسه حذف‌شده",
    }
    event["details_text"] = "، ".join(f"{labels.get(k, k)}: {', '.join(v) if isinstance(v, list) else v}" for k, v in details.items())
    return event

bp = Blueprint("security_audit", __name__)


@bp.route("/api/security/events", methods=["GET"])
@admin_required
def api_security_events():
    """
    لیست رویدادهای امنیتی اخیر (فقط admin).
    Query params:
      - limit: حداکثر تعداد (پیش‌فرض 100، حداکثر 1000)
      - event_type: فیلتر بر اساس نوع رویداد (failed_login، ...)
      - severity: فیلتر بر اساس شدت (info، warning، critical)
      - user_id: فقط رویدادهای یک کاربر
    """
    limit = request.args.get("limit", type=int) or 100
    limit = max(1, min(limit, 1000))
    event_type = request.args.get("event_type") or None
    severity = request.args.get("severity") or None
    user_id = request.args.get("user_id", type=int)
    events = get_recent_events(
        limit=limit,
        event_type=event_type,
        severity=severity,
        user_id=user_id,
    )
    events = [_localize_event(event) for event in events]
    return jsonify({
        "events": events,
        "total": len(events),
        "available_types": [{"value": event.value, "label": EVENT_TYPE_LABELS.get(event.value, event.value)} for event in SecurityEvent],
    })


@bp.route("/api/security/stats", methods=["GET"])
@admin_required
def api_security_stats():
    """آمار خلاصه رویدادهای امنیتی اخیر."""
    try:
        # فاز E.3: cutoffها هم‌فرمت با داده‌ی ذخیره‌شده (UTC/Z) ساخته می‌شوند.
        now_utc = datetime.now(timezone.utc)
        cutoff_24h = to_utc_z(now_utc - timedelta(hours=24))
        cutoff_7d = to_utc_z(now_utc - timedelta(days=7))

        with _db_conn() as conn:
            failed_24h = conn.execute(
                "SELECT COUNT(*) FROM security_events WHERE event_type=? AND timestamp>=?",
                (SecurityEvent.FAILED_LOGIN.value, cutoff_24h),
            ).fetchone()[0]

            success_24h = conn.execute(
                "SELECT COUNT(*) FROM security_events WHERE event_type=? AND timestamp>=?",
                (SecurityEvent.SUCCESSFUL_LOGIN.value, cutoff_24h),
            ).fetchone()[0]

            top_ips = conn.execute(
                '''SELECT ip_address, COUNT(*) as cnt FROM security_events
                   WHERE event_type=? AND timestamp>=? AND ip_address IS NOT NULL
                   GROUP BY ip_address ORDER BY cnt DESC LIMIT 10''',
                (SecurityEvent.FAILED_LOGIN.value, cutoff_7d),
            ).fetchall()

            critical_7d = conn.execute(
                "SELECT COUNT(*) FROM security_events WHERE severity='critical' AND timestamp>=?",
                (cutoff_7d,),
            ).fetchone()[0]

            total_7d = conn.execute(
                "SELECT COUNT(*) FROM security_events WHERE timestamp>=?",
                (cutoff_7d,),
            ).fetchone()[0]

            warnings_7d = conn.execute(
                "SELECT COUNT(*) FROM security_events WHERE severity IN ('warning','critical') AND timestamp>=?",
                (cutoff_7d,),
            ).fetchone()[0]

        return jsonify({
            "failed_logins_24h": int(failed_24h or 0),
            "successful_logins_24h": int(success_24h or 0),
            "critical_events_7d": int(critical_7d or 0),
            "total_events_7d": int(total_7d or 0),
            "warnings_7d": int(warnings_7d or 0),
            "top_failed_ips_7d": [{"ip": row[0], "count": int(row[1] or 0)} for row in top_ips],
        })
    except Exception:
        log.exception("Error in security stats API")
        return jsonify({"error": "خطای داخلی سرور رخ داد"}), 500


@bp.route("/security", methods=["GET"])
@admin_required
def security_page():
    """صفحه امنیت"""
    return render_template("security.html", load_dashboard_scripts=False)
