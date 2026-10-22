from core.dashboard.panel_registry import BasePanel


class AttendanceStatPanel(BasePanel):
    type_name = "attendance_stat"
    display_name = "آمار حضور و غیاب (Attendance)"
    description = "نمایش تعداد پرسنل حاضر، مرخصی و شیفت کاری فعال"
    icon = "users"

    @classmethod
    def config_schema(cls):
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "shift": {"type": "string", "enum": ["all", "morning", "evening", "night"], "default": "all"},
                "show_absent": {"type": "boolean", "default": True},
            },
            "required": ["title"],
        }

    @classmethod
    def default_config(cls):
        cfg = super().default_config()
        cfg.update({
            "title": "پرسنل حاضر در شیفت",
            "shift": "all",
            "show_absent": True,
        })
        return cfg
