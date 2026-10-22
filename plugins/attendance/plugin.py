"""
Attendance Plugin Main Entry Point
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional
from core.plugins.base import BasePlugin, PluginContext
from plugins.attendance.routes import bp
from plugins.attendance.widgets.attendance_widget import AttendanceStatPanel

log = logging.getLogger("PrinterMonitor.Plugin.Attendance")


class AttendancePlugin(BasePlugin):
    """Integrates attendance tracking into PrintGuard without separate UI or login."""

    def on_enable(self, context: PluginContext) -> None:
        log.info("پلاگین حضور و غیاب پرسنل (Attendance) فعال شد.")

    def on_disable(self, context: PluginContext) -> None:
        log.info("پلاگین حضور و غیاب پرسنل (Attendance) غیرفعال شد.")

    def get_blueprint(self) -> Optional[Any]:
        return bp

    def get_widgets(self) -> List[Any]:
        return [AttendanceStatPanel]
