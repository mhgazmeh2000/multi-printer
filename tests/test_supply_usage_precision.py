"""Unrounded consumption telemetry: real collector and API, no change to page tracking."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from core import store
from core.collectors import toshiba
from core.snmp.oid_map import OIDS
from tests.golden_harness import FIXTURE_DIR, isolated_db, replay_enhanced

COUNTS = {'black': 727249, 'cyan': 2479877, 'magenta': 80149, 'yellow': 250749}


class SupplyUsagePrecisionTests(unittest.TestCase):
    def test_fallback_collector_retains_raw_integer_and_unrounded_megadots(self):
        raw = {'print_total': 1000, 'print_fc': 200, 'print_bw': 800}
        counts = dict(COUNTS, black=2**53 + 7)
        raw.update({f'toner_{color}_usage': value for color, value in counts.items()})
        with mock.patch.object(toshiba, 'snmp_get_bulk', return_value=raw), \
             mock.patch.object(toshiba, '_scrape_toshiba_toners', return_value={c: 50 for c in counts}), \
             mock.patch.object(toshiba, '_read_supply_names', return_value={}), \
             mock.patch.object(toshiba, '_counters_event'), \
             mock.patch.object(store._prev, 'get', return_value=None):
            result = toshiba.collect_toshiba('192.0.2.90', 'Test', 'public', time.time())
        for color, count in counts.items():
            self.assertEqual(result['toners'][color]['usage'], count)
            self.assertIsInstance(result['toners'][color]['usage'], int)
            self.assertEqual(result['toners'][color]['usage_m'], count / 1000000)
        self.assertEqual(result['toners']['cyan']['usage_m'], 2.479877)

    def test_enhanced_collector_and_authenticated_api_keep_all_digits(self):
        from web import create_app
        from models import User
        fixture = json.loads((FIXTURE_DIR / 'enhanced_04_toshiba_offline_then_online.json').read_text())
        for phase in fixture['input']['phases']:
            for device in phase.get('devices', {}).values():
                oids = device.get('oids', {})
                for color, count in COUNTS.items():
                    oids[OIDS[f'toner_{color}_usage']] = count
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {'DB_BACKEND': 'sqlite', 'PG_DSN': ''}), isolated_db(tmp):
            result = replay_enhanced(fixture)['steps'][-1]['return_value']
            for color, count in COUNTS.items():
                self.assertEqual(result['toners'][color]['usage'], count)
                self.assertEqual(result['toners'][color]['usage_m'], count / 1000000)
            # Raw consumption does not replace/rebase any page tracking records.
            records = store._prev.get(result['ip'])['supply_usage']
            self.assertEqual(records['black']['start_pages'], result['counters']['total'])
            for color in ('cyan', 'magenta', 'yellow'):
                self.assertEqual(records[color]['start_pages'], result['counters']['full_color'])
            with store.data_lock:
                store.printer_data[result['ip']] = result
            app = create_app()
            app.config['WTF_CSRF_ENABLED'] = False
            user = User.create(username='precision_admin', email='precision@test.local',
                               password='StrongPassword!123', role='admin', is_verified=True)
            client = app.test_client()
            with client.session_transaction() as session:
                session['_user_id'] = str(user.id)
                session['_fresh'] = True
            response = client.get('/api/printers')
            self.assertEqual(response.status_code, 200)
            printer = next(p for p in response.get_json()['printers'] if p['ip'] == result['ip'])
            for color, count in COUNTS.items():
                self.assertEqual(printer['toners'][color]['usage'], count)
                self.assertEqual(printer['toners'][color]['usage_m'], count / 1000000)
                self.assertEqual(printer['toners'][color]['supply_pages'], 0)
            self.assertEqual(store._prev.get(result['ip'])['supply_usage'], records)
