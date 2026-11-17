"""Additive evidence only: arithmetic, snapshot guards and existing reads stay intact."""
import copy
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from core import store
from core.collectors import base
from core.counter_diagnostics import timed_counter_read
from tests.golden_harness import isolated_db


class CounterDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DB_BACKEND': 'sqlite', 'PG_DSN': ''})
        self.env.start()
        self.db = isolated_db(self.tmp.name)
        self.db.__enter__()
        self.events = []
        self.record = patch.object(base, 'add_event', side_effect=lambda ip, kind, data:
                                  self.events.append((kind, copy.deepcopy(data))))
        self.record.start()
        self.ip = '192.0.2.90'

    def tearDown(self):
        self.record.stop()
        self.db.__exit__(None, None, None)
        self.env.stop()
        self.tmp.cleanup()

    def run_counter(self, total, fc, bw, readings=None, prev=None):
        anchor = dict(print_total=10000, full_color=2000, black_white=8000,
                      uptime=500000000, alert_codes=[], last_alert_codes=[])
        anchor.update(prev or {})
        store._prev.set(self.ip, anchor)
        base._counters_event(self.ip, total, store._prev.get(self.ip), [], [],
                             full_color=fc, black_white=bw, uptime=500006000,
                             poll_timestamp='2026-11-07T12:00:00Z', counter_readings=readings)
        return next(data for kind, data in self.events if kind == 'PRINT')

    def test_one_page_invariant_preserves_original_deltas_and_pages(self):
        reads = [{'source': 'toshiba:print_bw', 'raw_value': 8004,
                  'started_at': '2026-11-07T12:00:01Z', 'finished_at': '2026-11-07T12:00:02Z'}]
        e = self.run_counter(10005, 2000, 8004, reads)
        self.assertEqual((e['pages'], e['color'], e['split_delta']), (5, 'نامشخص', 4))
        d = e['counter_diagnostic']
        self.assertEqual(d['previous'], {'total': 10000, 'full_color': 2000, 'black_white': 8000})
        self.assertEqual(d['current'], {'total': 10005, 'full_color': 2000, 'black_white': 8004})
        self.assertEqual(d['delta'], {'total': 5, 'full_color': 0, 'black_white': 4})
        self.assertEqual(d['reads'], reads)
        self.assertEqual([k for k, _ in self.events], ['PRINT'])
        self.assertEqual(store._prev.get(self.ip)['print_total'], 10005)
        json.dumps(d)  # API/JSON-compatible payload

    def test_negative_split_records_raw_decline_without_rolling_anchor_back(self):
        e = self.run_counter(10001, 2000, 7926)
        self.assertEqual((e['pages'], e['split_delta']), (1, -74))
        self.assertEqual(e['counter_diagnostic']['delta']['black_white'], -74)
        self.assertEqual(store._prev.get(self.ip)['black_white'], 8000)

    def test_missing_split_keeps_nulls_and_no_fabricated_read_times(self):
        e = self.run_counter(10005, None, None)
        self.assertIsNone(e['counter_diagnostic']['current']['full_color'])
        self.assertIsNone(e['counter_diagnostic']['current']['black_white'])
        self.assertEqual(e['counter_diagnostic']['reads'], [])

    def test_consistent_mixed_print_gets_no_diagnostic(self):
        e = self.run_counter(10005, 2002, 8003)
        self.assertEqual((e['pages'], e['color']), (5, 'مختلط'))
        self.assertNotIn('counter_diagnostic', e)

    def test_diagnostic_survives_real_log_storage_and_authenticated_api(self):
        from core.database import add_event
        from web import create_app
        from models import User
        e = self.run_counter(10005, 2000, 8004)
        add_event(self.ip, 'PRINT', e)
        app = create_app()
        user = User.create(username='diag_admin', email='diag@test.local',
                           password='StrongPassword!123', role='admin', is_verified=True)
        client = app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True
        response = client.get(f'/api/printer/{self.ip}/log')
        self.assertEqual(response.status_code, 200)
        item = next(item for item in response.get_json()['events'] if item['type'] == 'PRINT')
        self.assertEqual(item['details']['counter_diagnostic'], e['counter_diagnostic'])

    def test_timed_read_is_single_call_and_keeps_return_value(self):
        read = Mock(return_value='42')
        reads = []
        with patch('core.counter_diagnostics.utc_now_iso', side_effect=['start', 'end']):
            self.assertEqual(timed_counter_read(reads, 'toshiba:print_total', read), '42')
        read.assert_called_once_with()
        self.assertEqual(reads, [{'source': 'toshiba:print_total', 'raw_value': 42,
                                 'started_at': 'start', 'finished_at': 'end'}])

    def test_timed_read_preserves_exception_without_logging_sensitive_message(self):
        reads = []
        read = Mock(side_effect=RuntimeError('secret community'))
        with self.assertRaises(RuntimeError):
            timed_counter_read(reads, 'toshiba:print_total', read)
        read.assert_called_once_with()
        self.assertTrue(reads[0]['read_failed'])
        self.assertNotIn('secret', json.dumps(reads))

    def test_supplied_timings_do_not_change_existing_event_or_snapshot_fields(self):
        e1 = self.run_counter(10005, 2000, 8004)
        snapshot1 = store._prev.get(self.ip)
        self.events.clear()
        e2 = self.run_counter(10005, 2000, 8004, [{'source': 'test', 'raw_value': 8004}])
        snapshot2 = store._prev.get(self.ip)
        e1.pop('counter_diagnostic'); e2.pop('counter_diagnostic')
        snapshot1.pop('updated_at', None); snapshot2.pop('updated_at', None)
        self.assertEqual(e1, e2)
        self.assertEqual(snapshot1, snapshot2)

    def test_enhanced_path_times_existing_toshiba_reads(self):
        from tests.golden_harness import FIXTURE_DIR, replay_enhanced
        from core import enhanced_collector as enhanced
        fixture = json.loads((FIXTURE_DIR / 'enhanced_04_toshiba_offline_then_online.json').read_text())
        with patch.object(enhanced, '_counters_event', wraps=base._counters_event) as event:
            replay_enhanced(fixture)
        reads = event.call_args.kwargs['counter_readings']
        sources = [r['source'] for r in reads]
        for source in ('toshiba:print_total', 'toshiba:print_fc', 'toshiba:print_bw'):
            self.assertEqual(sources.count(source), 1)
        self.assertTrue(all('started_at' in r and 'finished_at' in r for r in reads))
        self.assertLess(sources.index('toshiba:print_total'), sources.index('toshiba:print_bw'))

    def test_confirmed_gap_has_evidence_but_keeps_estimated_event_type(self):
        anchor = dict(print_total=10000, full_color=2000, black_white=8000,
                      uptime=500000000, alert_codes=[], last_alert_codes=[])
        store._prev.set(self.ip, anchor)
        for _ in range(2):
            base._counters_event(self.ip, 200000, store._prev.get(self.ip), [], [],
                                 full_color=2000, black_white=198000, uptime=500006000)
        self.assertEqual([kind for kind, _ in self.events], ['PRINT_OVERFLOW', 'PRINT_GAP'])
        gap = self.events[-1][1]
        self.assertEqual(gap['pages'], 190000)
        self.assertTrue(gap['estimated'])
        self.assertEqual(gap['counter_diagnostic']['reason'], 'overflow_confirmed')
        self.assertEqual(gap['counter_diagnostic']['delta']['black_white'], 190000)
