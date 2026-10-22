"""
PrintGuard Alert Engine integration package.
"""

from core.alerting.alert_service import (
    get_active_alerts,
    get_alert_summary,
    get_alert_by_id,
)

__all__ = [
    "get_active_alerts",
    "get_alert_summary",
    "get_alert_by_id",
]
