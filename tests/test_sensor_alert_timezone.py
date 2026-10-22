# -*- coding: utf-8 -*-
"""
tests/test_sensor_alert_timezone.py — تست‌های رفع نقص TD-006 (تطبیق منطقه زمانی سنسورها).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from core.sensor_alert import _in_time_range, check_and_send_sensor_alert


class TestSensorAlertTimezone(unittest.TestCase):
    def test_in_time_range_with_tehran_timezone_conversion(self):
        """
        تست تبدیل UTC به Asia/Tehran:
        ساعت 05:00 به وقت UTC برابر با 08:30 به وقت تهران است (اختلاف +03:30).
        اگر بازه مجاز 8 تا 17 باشد:
        - بر مبنای ساعت UTC (5) خارج از بازه است.
        - بر مبنای ساعت تهران (8) داخل بازه است.
        """
        utc_dt = datetime(2026, 10, 15, 5, 0, 0, tzinfo=timezone.utc)
        
        # با در نظر گرفتن منطقه زمانی تهران باید True باشد
        in_range = _in_time_range(8, 17, tz_name="Asia/Tehran", now_dt=utc_dt)
        self.assertTrue(in_range)

        # اگر همان زمان را برای UTC بسنجیم، ساعت 5 است و باید False باشد
        in_range_utc = _in_time_range(8, 17, tz_name="UTC", now_dt=utc_dt)
        self.assertFalse(in_range_utc)

    def test_in_time_range_overnight_window(self):
        """تست بازه شبانه که از نیمه‌شب عبور می‌کند (مثلا ۲۲ تا ۶)."""
        # ساعت ۲۳ به وقت تهران
        dt_night = datetime(2026, 10, 15, 23, 15, 0, tzinfo=ZoneInfo("Asia/Tehran"))
        self.assertTrue(_in_time_range(22, 6, tz_name="Asia/Tehran", now_dt=dt_night))

        # ساعت ۰۴ صبح به وقت تهران
        dt_early = datetime(2026, 10, 15, 4, 30, 0, tzinfo=ZoneInfo("Asia/Tehran"))
        self.assertTrue(_in_time_range(22, 6, tz_name="Asia/Tehran", now_dt=dt_early))

        # ساعت ۱۲ ظهر به وقت تهران (خارج از بازه شبانه)
        dt_noon = datetime(2026, 10, 15, 12, 0, 0, tzinfo=ZoneInfo("Asia/Tehran"))
        self.assertFalse(_in_time_range(22, 6, tz_name="Asia/Tehran", now_dt=dt_noon))

    @mock.patch("utils.send_email")
    def test_check_and_send_sensor_alert_respects_tehran_quiet_hours(self, mock_send_email):
        """تست اینکه check_and_send_sensor_alert در ساعات غیرمجاز ایمیل ارسال نمی‌کند."""
        config = {
            "enabled": True,
            "recipient_emails": ["admin@example.com"],
            "temp_critical": 35,
            "start_hour": 9,
            "end_hour": 17,
            "timezone": "Asia/Tehran",
        }
        # شبیه‌سازی زمانی که به وقت تهران ساعت ۲۳:۰۰ شب است
        night_tehran = datetime(2026, 10, 15, 23, 0, 0, tzinfo=ZoneInfo("Asia/Tehran"))

        with mock.patch("core.sensor_alert.load_config", return_value=config), \
             mock.patch("core.sensor_alert.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: night_tehran
            readings = [{"status": "active", "kind": "temperature", "value": 40.0, "port": 1}]
            check_and_send_sensor_alert("192.168.1.100", "Server Room Sensor", readings)

            mock_send_email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
