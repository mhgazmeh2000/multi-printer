"""Collector package 3: real two-poll persistence, isolated DB and mocked device IO.

Never use production PG_DSN; this fixture forces SQLite in a temporary directory.
The collector, PrevStore, counters events and yield engine are NOT mocked out.
"""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import config.settings as settings
from core import store
import core.enhanced_collector as ec
from core.database import db_connection, init_db, load_printer_counters
from core.yield_engine import ensure_yield_tables, process_cartridge_snapshot, register_manual_refill


class EvidencePolicyTests(unittest.TestCase):
    def test_oid_enterprise_boundaries_and_leading_dot(self):
        for oid, vendor in [
            ('1.3.6.1.4.1.11', 'hp'), ('.1.3.6.1.4.1.11.2.3', 'hp'),
            ('1.3.6.1.4.1.1129.2', 'toshiba'), ('1.3.6.1.4.1.253.1', 'xerox'),
            ('1.3.6.1.4.1.11290.2', None), ('1.3.6.1.4.1.1150.1', None),
            ('1.3.6.1.4.1.2531.1', None), ('9.1.3.6.1.4.1.11.2', None),
        ]:
            with self.subTest(oid=oid):
                self.assertEqual(ec._vendor_hint_from_identity('', oid), vendor)

    def test_compact_and_separated_part_colors(self):
        for name, color in [('T-FC415C', 'cyan'), ('T-FC415M', 'magenta'),
                            ('T-FC505Y', 'yellow'), ('T-FC338EK', 'black'),
                            ('T-FC505U-Y', 'yellow'), ('TK-5240M', 'magenta'),
                            ('TN-247Y', 'yellow'), ('CLT-C506L', 'cyan'),
                            ('Unknown supply', None)]:
            with self.subTest(name=name):
                self.assertEqual(ec._detect_toner_color(name), color)

    def test_static_missing_and_generation_decrease_are_not_swaps(self):
        prev = {'cartridge_ids': {'black': 'toner_gen:3'}}
        for ids, quality in [({'black': 'toner_gen:2'}, {'black': 'generation'}),
                             ({'black': 'part:CF230A'}, {'black': 'static'}),
                             ({'black': ''}, {'black': 'genuine'}),
                             ({'black': 'other:4'}, {'black': 'generation'}),
                             ({'black': 'serial'}, {})]:
            self.assertEqual(ec._hardware_swapped_colors(prev, ids, {}, quality), set())
        self.assertEqual(ec._hardware_swapped_colors(
            prev, {'black': 'toner_gen:4'}, {}, {'black': 'generation'}), {'black'})

    def test_supply_page_reset_needs_continuous_printer_counter_and_no_reboot(self):
        prev = {'print_total': 10000, 'uptime': 50000, 'cart_supply_pages': {'black': 500}}
        for total, uptime in [(None, 51000), (0, 51000), (10010, 100)]:
            self.assertEqual(ec._hardware_swapped_colors(
                prev, {}, {'black': 0}, {}, total=total, uptime=uptime), set())
        self.assertEqual(ec._hardware_swapped_colors(
            prev, {}, {'black': 0}, {}, total=10010, uptime=51000), {'black'})
        for invalid in (-1, True, 0.5, "invalid"):
            self.assertEqual(ec._hardware_swapped_colors(
                prev, {}, {'black': invalid}, {}, total=10010, uptime=51000), set())

    def test_identity_signal_upgrade_is_not_a_physical_replacement(self):
        for prev in [
            {'cartridge_ids': {'black': 'part:CF230A'}},
            {'cartridge_ids': {'black': 'old'},
             'cartridge_state': {'black': {'signal_quality': 'static'}}},
        ]:
            self.assertEqual(ec._hardware_swapped_colors(
                prev, {'black': 'new-serial'}, {}, {'black': 'genuine'}), set())


class CollectorPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, old_cwd)
        self.start_patch(patch.object(settings, 'DB_PATH', str(Path(self.tmp.name) / 'collector.db')))
        self.start_patch(patch.dict(os.environ, {
            'DB_BACKEND': 'sqlite', 'PG_DSN': '', 'REPORTING_PG_DSN': '',
            'YIELD_ESTIMATOR': 'v1', 'CARTRIDGE_YIELD_CATALOG': str(Path(self.tmp.name) / 'catalog.json'),
        }))
        Path('catalog.json').write_text('{"version":1,"entries":[]}', encoding='utf8')
        init_db()
        ensure_yield_tables()
        store._prev._cache.clear()
        self.addCleanup(store._prev._cache.clear)
        self.printer = {'ip': '192.0.2.43', 'name': 'Test', 'brand': 'hp', 'device_type': 'mono'}
        self.model = 'HP LaserJet MFP M227fdn'
        self.total = 10000
        self.uptime = 50000
        self.identity = {'ids': {'black': 'serial-1'}, 'supply_pages': {'black': 500},
                         'signal_quality': {'black': 'genuine'}, 'source': 'test'}
        self.supplies = [self.supply('Black Cartridge HP CF230A', 75)]
        self.start_patch(patch.object(ec, 'detect_snmp_version', return_value=2))
        self.start_patch(patch.object(ec, 'snmp_get_with_fallback', side_effect=self.snmp))
        self.start_patch(patch.object(ec, 'walk_supplies_table', side_effect=lambda *a, **k: copy.deepcopy(self.supplies)))
        self.start_patch(patch.object(ec, 'walk_input_trays', return_value=[]))
        self.start_patch(patch.object(ec, '_save_oid_profile'))
        self.start_patch(patch.object(ec, '_log_to_toner_report'))
        self.start_patch(patch('core.collectors.cartridge_id.get_cartridge_identity_data',
                               side_effect=lambda **k: copy.deepcopy(self.identity)))
        self.start_patch(patch('core.collectors.cartridge_discovery.queue_discovery'))
        self.start_patch(patch('core.collectors.hp._scrape_hp_toners', return_value={}))

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    @staticmethod
    def supply(name, percent):
        return {'name': name, 'type': 3, 'type_name': 'toner', 'percent': percent,
                'remaining': percent, 'max': 100, 'status': 'ok' if percent is not None else 'unknown',
                'unit_code': 19, 'unit': '%', 'index': 1}

    def snmp(self, ip, oid, *args, **kwargs):
        return {
            '1.3.6.1.2.1.1.1.0': 'HP LaserJet MFP M227fdn',
            '1.3.6.1.2.1.43.10.2.1.4.1.1': self.total,
            '1.3.6.1.2.1.43.5.1.1.16.1': self.model,
            '1.3.6.1.2.1.43.5.1.1.17.1': 'printer-serial',
            '1.3.6.1.2.1.1.3.0': self.uptime,
        }.get(oid)

    def poll(self):
        # Force reading the normalized persisted representation, not a warm Python object.
        store._prev._cache.clear()
        return ec.collect_enhanced(dict(self.printer), save_to_db=True)

    def state(self, color='black'):
        with db_connection() as conn:
            cur = conn.execute('SELECT * FROM cartridge_state WHERE printer_ip=? AND color=?',
                               (self.printer['ip'], color))
            return dict(zip([d[0] for d in cur.description], cur.fetchone()))

    def override(self, colors=('black',)):
        entries = {color: {'start_total': self.total, 'start_toner': 10, 'yield_per_page': 2000,
                          'mode': 'manual_reset', 'chip_quarantined': True,
                          'chip_level_at_reset': 75} for color in colors}
        store._prev.set(self.printer['ip'], {'toner_overrides': entries, 'manual_override': 1,
                        'override_color': 'black', 'override_start_total': self.total, 'override_start_toner': 10})

    def test_production_model_alias_roundtrip_keeps_key_anchor_and_learned_state(self):
        self.poll()
        with db_connection(commit=True) as conn:
            conn.execute("UPDATE cartridge_state SET yield_per_page=4321, yield_source='auto_learn', "
                         "sample_count=7, confidence='high', total_weight=8 WHERE printer_ip=?",
                         (self.printer['ip'],))
        before = self.state()
        self.total += 20
        self.uptime += 10000
        self.model = 'NPI9E8319'
        self.poll()
        self.model = 'HP LaserJet MFP M227fdn'
        self.total += 20
        self.uptime += 10000
        self.poll()
        after = self.state()
        for key in ['cartridge_key', 'anchor_counter', 'anchor_level', 'cycle_start_counter',
                    'yield_per_page', 'yield_source', 'sample_count', 'total_weight']:
            self.assertEqual(after[key], before[key], key)

    def test_initial_network_alias_enriches_to_real_model_without_replacement(self):
        self.model = 'NPI9E8319'
        self.poll()
        before = self.state()
        self.model = 'HP LaserJet MFP M227fdn'
        self.total += 10
        self.poll()
        after = self.state()
        self.assertIn('hp laserjet mfp m227fdn', after['cartridge_key'])
        self.assertEqual(after['anchor_counter'], before['anchor_counter'])
        self.assertEqual(after['anchor_level'], before['anchor_level'])

    def test_generic_missing_supply_name_is_not_a_new_cartridge_identity(self):
        self.poll()
        before = self.state()
        self.supplies = []
        self.total += 10
        self.poll()
        after = self.state()
        self.assertEqual(after['cartridge_key'], before['cartridge_key'])
        self.assertEqual(after['anchor_counter'], before['anchor_counter'])

    def test_same_part_new_serial_releases_only_its_override_and_resets_anchor(self):
        self.poll()
        self.override(('black', 'cyan'))
        self.total += 10
        self.identity['ids']['black'] = 'serial-2'
        self.supplies[0]['percent'] = 85  # below the old 20% level-jump heuristic
        result = self.poll()
        saved = load_printer_counters(self.printer['ip'])
        self.assertNotIn('black', saved['toner_overrides'])
        self.assertIn('cyan', saved['toner_overrides'])
        self.assertEqual(result['toners']['black']['level'], 85)
        self.assertFalse(any(a['code'] == 'toner:black' for a in result['alerts']))
        self.assertEqual(self.state()['anchor_counter'], self.total)
        self.assertEqual(self.state()['anchor_level'], 85)
        # No resurrection through legacy single-slot keys on the following poll.
        self.poll()
        self.assertNotIn('black', load_printer_counters(self.printer['ip'])['toner_overrides'])

    def test_supply_counter_reset_through_real_collector_resets_anchor(self):
        self.poll()
        self.override()
        self.total += 10
        self.identity['supply_pages']['black'] = 0
        self.poll()
        self.assertEqual(self.state()['anchor_counter'], self.total)
        self.assertFalse(load_printer_counters(self.printer['ip'])['toner_overrides'])

    def test_static_id_change_keeps_manual_override(self):
        self.identity['ids']['black'] = 'part:CF230A'
        self.identity['signal_quality']['black'] = 'static'
        self.poll()
        self.override()
        self.identity['ids']['black'] = 'part:CF230X'
        result = self.poll()
        self.assertIn('black', load_printer_counters(self.printer['ip'])['toner_overrides'])
        self.assertEqual(result['toners']['black']['level'], 10)

    def test_new_chip_without_sensor_retires_old_override_until_real_level_arrives(self):
        self.poll()
        self.override()
        self.identity['ids']['black'] = 'serial-2'
        self.identity['supply_pages']['black'] = 0
        self.total += 10
        self.supplies[0]['percent'] = None
        result = self.poll()
        self.assertFalse(load_printer_counters(self.printer['ip'])['toner_overrides'])
        self.assertIsNone(result['toners']['black']['level'])
        self.assertIsNone(self.state()['anchor_level'])
        self.total += 10
        self.supplies[0]['percent'] = 80
        self.poll()
        self.assertEqual(self.state()['anchor_level'], 80)
        self.assertEqual(self.state()['anchor_counter'], self.total)

    def test_compact_color_rows_and_unknown_supply_cannot_overwrite_black(self):
        self.printer['device_type'] = 'color'
        self.supplies = [self.supply('T-FC415K', 60), self.supply('T-FC415C', 30),
                         self.supply('Unidentified supply', 90)]
        result = self.poll()
        self.assertEqual(result['device_type'], 'color')
        self.assertEqual(result['toners']['black']['raw_level'], 60)
        self.assertEqual(result['toners']['cyan']['raw_level'], 30)
        self.assertEqual(set(result['toners']), {'black', 'cyan'})

    def test_single_unlabelled_mono_supply_keeps_legacy_black_fallback(self):
        self.supplies = [self.supply('Cartridge 137', 60)]
        self.assertEqual(self.poll()['toners']['black']['raw_level'], 60)

    def test_genuine_part_name_change_still_resets_learned_state(self):
        self.poll()
        with db_connection(commit=True) as conn:
            conn.execute('UPDATE cartridge_state SET sample_count=7 WHERE printer_ip=?', (self.printer['ip'],))
        self.supplies[0]['name'] = 'Black Cartridge HP CF230X'
        self.total += 10
        self.poll()
        self.assertEqual(self.state()['sample_count'], 0)
        self.assertEqual(self.state()['anchor_counter'], self.total)

    def test_v2_keeps_alias_state_and_honors_serial_swap(self):
        with patch.dict(os.environ, {"YIELD_ESTIMATOR": "v2"}):
            self.test_production_model_alias_roundtrip_keeps_key_anchor_and_learned_state()
            self.override()
            self.total += 10
            self.identity['ids']['black'] = 'serial-v2'
            self.supplies[0]['percent'] = 85
            self.poll()
            self.assertFalse(load_printer_counters(self.printer['ip'])['toner_overrides'])
            self.assertEqual(self.state()['anchor_counter'], self.total)

    def test_swap_with_missing_supply_row_retires_old_anchor_without_fake_display(self):
        self.poll()
        self.override()
        self.identity['ids']['black'] = 'serial-2'
        self.supplies = [self.supply('Cyan Toner', 60)]
        self.total += 10
        result = self.poll()
        self.assertNotIn('black', result['toners'])
        self.assertFalse(load_printer_counters(self.printer['ip'])['toner_overrides'])
        self.assertIsNone(self.state()['anchor_level'])
        self.supplies = [self.supply('Black Cartridge HP CF230A', 80)]
        self.total += 10
        self.poll()
        self.assertEqual(self.state()['anchor_counter'], self.total)

    def test_failed_identity_read_does_not_clear_override(self):
        self.poll()
        self.override()
        self.identity = {}
        result = self.poll()
        self.assertIn('black', load_printer_counters(self.printer['ip'])['toner_overrides'])
        self.assertEqual(result['toners']['black']['level'], 10)

    def test_swap_with_synthetic_level_does_not_seed_new_anchor(self):
        self.poll()
        self.identity['ids']['black'] = 'serial-2'
        self.supplies[0]['percent'] = 20
        self.supplies[0]['percent_source'] = 'canon_status_code'
        self.total += 10
        self.poll()
        self.assertIsNone(self.state()['anchor_level'])
        del self.supplies[0]['percent_source']
        self.supplies[0]['percent'] = 80
        self.total += 10
        self.poll()
        self.assertEqual(self.state()['anchor_level'], 80)
        self.assertEqual(self.state()['anchor_counter'], self.total)

    def test_manual_refill_with_network_alias_keeps_canonical_key(self):
        self.poll()
        key = self.state()['cartridge_key']
        register_manual_refill(ip=self.printer['ip'], color='black', printer_model='NPI9E8319',
                               cartridge_name=self.supplies[0]['name'], new_level=100,
                               counters={'total': self.total}, device_type='mono')
        self.assertEqual(self.state()['cartridge_key'], key)


if __name__ == '__main__':
    unittest.main()
