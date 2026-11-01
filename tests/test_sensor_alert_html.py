# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_sensor_alert_html.py
# @brief:        Regression test: sensor alert e-mail must be built and sent.
# @details:      Responsibilities: Guard against the '%' formatting bug in the HTML body
#                (a CSS 'width:100%' inside a %-format string raised ValueError before
#                send_email was reached, so no alert mail was ever sent).
#                Main API: test_* functions only
#                Side effects: none (send_email, config and cooldown are patched)
# @dependencies: internal: core.sensor_alert | external: pytest
# @notes:        None.
# =============================================================================

from unittest.mock import patch

import pytest

import core.sensor_alert as sa


@pytest.fixture
def enabled_config():
    """Config that enables alerts with one recipient and no time window or cooldown."""
    return {
        "enabled": True,
        "recipient_emails": ["ops@example.com"],
        "user_ids": [],
        "temp_warning": 30,
        "temp_critical": 35,
        "hum_warning_high": 70,
        "hum_critical_high": 80,
        "hum_warning_low": 20,
        "cooldown_minutes": 0,
        "start_hour": 0,
        "end_hour": 24,
    }


def test_alert_mail_is_sent_for_critical_temperature(enabled_config):
    """A critical temperature reading must produce exactly one e-mail with the alert table."""
    readings = [{"kind": "temperature", "value": 40.0, "status": "active", "port": 1}]
    with patch.object(sa, "load_config", return_value=enabled_config), \
         patch.object(sa, "_check_cooldown", return_value=True), \
         patch.object(sa, "_mark_sent"), \
         patch.object(sa, "_in_time_range", return_value=True), \
         patch("utils.send_email") as send:
        sa.check_and_send_sensor_alert("192.0.2.10", "Room-A", readings)

    assert send.call_count == 1, "alert e-mail was not sent (HTML build probably raised)"
    sent_payload = repr(send.call_args)
    assert "width:100%" in sent_payload
    assert "192.0.2.10" in sent_payload
    assert "40.0" in sent_payload


def test_no_mail_when_reading_is_normal(enabled_config):
    """Normal readings must not send mail."""
    readings = [{"kind": "temperature", "value": 22.0, "status": "active", "port": 1}]
    with patch.object(sa, "load_config", return_value=enabled_config), \
         patch.object(sa, "_check_cooldown", return_value=True), \
         patch.object(sa, "_mark_sent"), \
         patch.object(sa, "_in_time_range", return_value=True), \
         patch("utils.send_email") as send:
        sa.check_and_send_sensor_alert("192.0.2.10", "Room-A", readings)
    send.assert_not_called()
