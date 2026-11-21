"""Real alert composition and delivery with mocked SMTP, never actual mail."""
from html import escape
import unittest
from unittest.mock import patch

from core import sensor_alert


class SensorAlertDeliveryTests(unittest.TestCase):
    def setUp(self):
        config = {'enabled': True, 'recipient_emails': ['ops@example.invalid'],
                  'user_ids': [], 'temp_warning': 30, 'temp_critical': 35,
                  'hum_warning_high': 70, 'hum_critical_high': 80,
                  'hum_warning_low': 20, 'cooldown_minutes': 30}
        for context in (patch.object(sensor_alert, 'load_config', return_value=config),
                        patch.object(sensor_alert, '_in_time_range', return_value=True),
                        patch.object(sensor_alert, '_last_sent', {})):
            context.start()
            self.addCleanup(context.stop)
        mail_patch = patch('utils.send_email', return_value=True)
        self.send = mail_patch.start()
        self.addCleanup(mail_patch.stop)

    def test_critical_temperature_renders_percent_css_and_sends_once(self):
        readings = [{'status': 'active', 'kind': 'temperature', 'value': 41, 'port': 1}]
        sensor_alert.check_and_send_sensor_alert('192.0.2.50', 'Server room', readings)
        self.send.assert_called_once()
        subject, recipient, text, html = self.send.call_args.args
        self.assertEqual(recipient, 'ops@example.invalid')
        self.assertIn('Server room', subject)
        self.assertIn('41.0', text)
        self.assertIn('width:100%;border-collapse', html)
        self.assertNotIn('100%%', html)
        self.assertIn('sensor_192.0.2.50', sensor_alert._last_sent)
        sensor_alert.check_and_send_sensor_alert('192.0.2.50', 'Server room', readings)
        self.send.assert_called_once()

    def test_humidity_and_all_external_html_values_are_escaped(self):
        name = '<img src=x onerror=alert(1)>'
        ip = '192.0.2.50<script>x</script>'
        unit = '%<b>unit</b>'
        port = '"><svg onload=x>'
        readings = [{'status': 'active', 'kind': 'humidity', 'value': 90,
                     'port': port, 'unit': unit}]
        sensor_alert.check_and_send_sensor_alert(ip, name, readings)
        _, _, text, html = self.send.call_args.args
        for value in (name, ip, unit, port):
            self.assertIn(escape(value), html)
            self.assertNotIn(value, html)
        self.assertIn(unit, text)
        self.assertIn('90.0', html)

    def test_disabled_or_normal_or_inactive_readings_send_nothing(self):
        sensor_alert.check_and_send_sensor_alert('192.0.2.1', 'Room', [
            {'status': 'active', 'kind': 'temperature', 'value': 20},
            {'status': 'inactive', 'kind': 'temperature', 'value': 99}])
        with patch.object(sensor_alert, 'load_config', return_value={'enabled': False}):
            sensor_alert.check_and_send_sensor_alert('192.0.2.1', 'Room', [])
        self.send.assert_not_called()
        self.assertEqual(sensor_alert._last_sent, {})

    def test_mail_exception_is_contained_and_not_marked_sent(self):
        self.send.side_effect = RuntimeError('mock transport failure')
        with patch.object(sensor_alert.log, 'exception') as logged:
            sensor_alert.check_and_send_sensor_alert('192.0.2.1', 'Room', [
                {'status': 'active', 'kind': 'temperature', 'value': 41}])
        logged.assert_called_once()
        self.assertEqual(sensor_alert._last_sent, {})
