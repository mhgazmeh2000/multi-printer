"""Expose the existing scheduler, without changing automatic/manual poll cadence."""
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from core import poller, store


class PollCycleStatusTests(unittest.TestCase):
    def setUp(self):
        self.stats = dict(store.poll_stats)
        self.data = dict(store.printer_data)

    def tearDown(self):
        store.poll_stats.clear()
        store.poll_stats.update(self.stats)
        store.printer_data.clear()
        store.printer_data.update(self.data)

    def test_wait_publishes_actual_sleep_deadline_and_clears_it_on_exit(self):
        def sleep(seconds):
            self.assertEqual(seconds, poller.POLL_INTERVAL)
            due = datetime.fromisoformat(store.poll_stats['next_poll_at'].replace('Z', '+00:00'))
            self.assertEqual(due.timestamp(), 1000 + seconds)
        with patch.object(poller.time, 'time', return_value=1000), patch.object(poller.time, 'sleep', side_effect=sleep) as wait:
            poller._wait_for_next_poll()
        wait.assert_called_once_with(poller.POLL_INTERVAL)
        self.assertIsNone(store.poll_stats['next_poll_at'])

    def test_manual_poll_does_not_move_automatic_deadline(self):
        deadline = '2026-11-07T12:01:00Z'
        store.poll_stats['next_poll_at'] = deadline
        with patch.object(store, 'PRINTERS', []):
            poller.poll_all()
        self.assertEqual(store.poll_stats['next_poll_at'], deadline)
        self.assertFalse(store.poll_stats['is_polling'])
        self.assertIsNotNone(store.poll_stats['started_at'])

    def test_exception_clears_busy_flag(self):
        with patch.object(store, 'PRINTERS', None):
            with self.assertRaises(TypeError):
                poller.poll_all()
        self.assertFalse(store.poll_stats['is_polling'])

    def test_status_contains_server_clock_and_scheduler_snapshot(self):
        from flask import Flask
        from web.routes.system import bp
        app = Flask(__name__)
        app.secret_key = 'unit-test-only'
        app.register_blueprint(bp)
        store.poll_stats.update(next_poll_at='2026-11-07T12:01:00Z', is_polling=True,
                                started_at='2026-11-07T12:00:00Z')
        response = app.test_client().get('/api/status')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result['next_poll_at'], store.poll_stats['next_poll_at'])
        self.assertTrue(result['is_polling'])
        self.assertEqual(result['poll_interval'], poller.POLL_INTERVAL)
        self.assertEqual(datetime.fromisoformat(result['server_time'].replace('Z', '+00:00')).tzinfo, timezone.utc)
