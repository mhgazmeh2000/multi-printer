"""Operational v2 regressions, synthetic/anonymized fixtures and isolated SQLite only."""
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import unittest

from core.yield_estimator import SupplyReading, estimate_supply_series, segment_readings
from tests import test_yield_v2_audit as audit


class CounterRecoveryTests(unittest.TestCase):
    def test_counter_excursion_cannot_become_sixty_thousand_printed_pages(self):
        readings = [SupplyReading(60228, 30), SupplyReading(60257, 27),
                    SupplyReading(60259, 100), SupplyReading(0, 30),
                    SupplyReading(60262, 27), SupplyReading(60263, 27)]
        segments = segment_readings(readings)
        self.assertTrue(any(s.break_reason == 'counter_recovery' for s in segments))
        self.assertTrue(all(s.observations[-1].usage_counter - s.observations[0].usage_counter < 100
                            for s in segments))
        dec = estimate_supply_series(readings, vendor_or_model='hp', prior_yield=1000,
                                     prior_source='catalog', current_level=27)
        self.assertLess(dec.capacity_pages, 2000)
        self.assertLess(dec.eta_pages, 540)

    def test_new_counter_zero_is_valid_and_real_reset_keeps_learning(self):
        for counts in ((0, 100, 200, 300), (60000, 0, 100, 200, 300)):
            readings = [SupplyReading(c, 80 - i) for i, c in enumerate(counts)]
            segments = segment_readings(readings)
            self.assertEqual(segments[-1].observations[0].usage_counter, 0)
            self.assertEqual(segments[-1].observations[-1].usage_counter, 300)
            self.assertFalse(any(s.break_reason == 'counter_recovery' for s in segments))


class OperationalReplayTests(unittest.TestCase):
    setUp = audit.V2ReplayAuditTests.setUp
    start_patch = audit.V2ReplayAuditTests.start_patch
    rows = audit.V2ReplayAuditTests.rows
    estimate = audit.V2ReplayAuditTests.estimate

    def insert(self, conn, counter, level, minute, **kwargs):
        from core.yield_engine import _record_snapshot
        _record_snapshot(conn, self.state['printer_ip'], 'black', self.state['printer_model'],
                         self.state['cartridge_name'], kwargs.pop('key', self.state['cartridge_key']),
                         (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=minute)).isoformat(), {'total': counter}, counter,
                         level, level, kwargs.pop('source', 'poll'), **kwargs)

    def test_plateau_history_survives_more_than_four_hundred_idle_polls(self):
        from core.database import db_connection
        from core.yield_engine import estimate_cartridge_interval
        with db_connection(commit=True) as conn:
            for i in range(8):
                self.insert(conn, 1000 + i * 100, 80 - i, i)
            before = estimate_cartridge_interval(conn, ip=self.state['printer_ip'], color='black', state=self.state)
            for i in range(1500):
                self.insert(conn, 1700 + i // 10, 73, i + 8)
            after = estimate_cartridge_interval(conn, ip=self.state['printer_ip'], color='black', state=self.state)
        self.assertNotEqual(after.learning_state, 'prior')
        self.assertAlmostEqual(after.capacity_pages, before.capacity_pages, delta=2000)
        self.assertEqual(after.n_plateaus, before.n_plateaus)

    def test_identical_idle_polls_do_not_change_fit_or_ci(self):
        from core.database import db_connection
        from core.yield_engine import estimate_cartridge_interval
        with db_connection(commit=True) as conn:
            for i in range(8):
                self.insert(conn, 1000 + i * 100, 80 - i, i)
            before = estimate_cartridge_interval(conn, ip=self.state['printer_ip'], color='black', state=self.state)
            for i in range(1500):
                self.insert(conn, 1700, 73, i + 8)
            after = estimate_cartridge_interval(conn, ip=self.state['printer_ip'], color='black', state=self.state)
        self.assertEqual(before, after)

    def test_loader_is_bounded_and_bulk_matches_individual_slot(self):
        from core.database import db_connection
        from core.yield_engine import _load_interval_snapshots
        with db_connection(commit=True) as conn:
            for i in range(20):
                self.insert(conn, 1000 + i * 10, 90 - i, i)
            all_rows = _load_interval_snapshots(conn, limit=3)
            one_slot = _load_interval_snapshots(conn, ip=self.state['printer_ip'], color='BLACK', limit=3)
        self.assertEqual([tuple(r) for r in all_rows], [tuple(r) for r in one_slot])
        self.assertLessEqual(len(one_slot), 6)
        self.assertEqual(one_slot[-1][2], 1190)

    def test_loader_preserves_rejections_manual_reset_and_identity_changes(self):
        from core.database import db_connection
        from core.yield_engine import _load_interval_snapshots
        with db_connection(commit=True) as conn:
            self.insert(conn, 1000, 70, 0)
            self.insert(conn, 1000, 70, 1, valid=False, reject_reason='quarantined_chip')
            self.insert(conn, 1000, 70, 2, source='manual_reset:empty')
            self.insert(conn, 1000, 70, 3, key='other|part|black')
            rows = _load_interval_snapshots(conn, ip=self.state['printer_ip'], color='black')
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1][7], 'quarantined_chip')
        self.assertEqual(rows[2][5], 'manual_reset:empty')
        self.assertEqual(rows[3][9], 'other|part|black')

    def test_loaded_history_and_uncompressed_history_make_same_decision(self):
        from core.database import db_connection
        from core.yield_engine import estimate_cartridge_interval
        with db_connection(commit=True) as conn:
            for i in range(8):
                for j in range(20):
                    self.insert(conn, 1000 + i * 100 + j, 80 - i, i * 20 + j)
            raw = conn.execute("""SELECT usage_counter, toner_level, raw_level, source,
                      valid, reject_reason, timestamp, cartridge_key, full_color, print_total
                      FROM toner_snapshots_v2 ORDER BY id""").fetchall()
            loaded = estimate_cartridge_interval(conn, ip=self.state['printer_ip'], color='black', state=self.state)
            original = estimate_cartridge_interval(conn, ip=self.state['printer_ip'], color='black',
                                                   state=self.state, snap_rows=raw)
        self.assertEqual(loaded, original)

    def test_loader_preserves_endpoints_and_reset_boundaries(self):
        from core.database import db_connection
        from core.yield_engine import _load_interval_snapshots
        with db_connection(commit=True) as conn:
            for i, c in enumerate((1000, 1100, 1200, 0, 10, 1201, 1202)):
                self.insert(conn, c, 70, i)
            rows = _load_interval_snapshots(conn, ip=self.state['printer_ip'], color='black')
        self.assertIn(1200, [r[2] for r in rows])
        self.assertIn(0, [r[2] for r in rows])
        self.assertIn(1202, [r[2] for r in rows])

    def test_stored_catalog_reference_is_not_lost_when_identity_lookup_misses(self):
        from core.yield_engine import _resolve_interval_prior
        self.state.update(yield_source='catalog', yield_per_page=23800)
        with patch('core.yield_engine._catalog_lookup', return_value=None), \
             patch('core.yield_engine.estimate_yield_fallback', return_value=-1):
            self.assertEqual(_resolve_interval_prior(self.state), (23800, 'catalog', 23800))
            self.state['yield_source'] = 'auto_learn'
            self.assertEqual(_resolve_interval_prior(self.state), (2000, 'default', None))

    def test_live_catalog_takes_precedence_over_stored_catalog(self):
        from core.yield_engine import _resolve_interval_prior
        self.state.update(yield_source='catalog', yield_per_page=23800)
        with patch('core.yield_engine._catalog_lookup', return_value={'yield_per_page': 25000}):
            self.assertEqual(_resolve_interval_prior(self.state), (25000, 'catalog', 25000))

    def test_invalid_stored_catalog_capacity_cannot_become_prior(self):
        from core.yield_engine import _resolve_interval_prior
        self.state.update(yield_source='catalog', yield_per_page=-100)
        with patch('core.yield_engine._catalog_lookup', return_value=None), \
             patch('core.yield_engine.estimate_yield_fallback', return_value=-1):
            self.assertEqual(_resolve_interval_prior(self.state), (2000, 'default', None))

    def test_v2_status_fields_and_public_metadata_use_same_decision(self):
        from core.database import db_connection
        from core.yield_engine import _upsert_state, get_yield_status, _state_public_dict
        self.state.update(yield_per_page=27700, yield_source='auto_learn', confidence='high',
                          current_level=56)
        with db_connection(commit=True) as conn:
            _upsert_state(conn, self.state)
            for i in range(3):
                self.insert(conn, 1000 + i, 56, i)
        row = get_yield_status(ip=self.state['printer_ip'])[0]
        dec = self.estimate([(1000, 56, 56, 'poll', 1, None, '', self.state['cartridge_key'], None, 1000)])
        meta = _state_public_dict(self.state, interval_decision=dec)
        for item in (row, meta):
            self.assertEqual(item['confidence'], 'low')
            self.assertEqual(item['yield_per_page'], item['interval_capacity_pages'])
            self.assertEqual(item['yield_source'], 'default')
            self.assertEqual(item['stored_yield_per_page'], 27700)
            self.assertEqual(item['stored_confidence'], 'high')
            self.assertEqual(item['estimator'], 'v2')
            self.assertEqual(item['eta_pages'], 1120)

    def test_diagnostic_tool_uses_only_in_memory_export_even_with_pg_config(self):
        from tools.yield_diagnostic_replay import replay_export
        exported = {'devices': {'192.0.2.80': {
            'state': [self.state], 'profiles': [],
            'snapshots': [{'id': i + 1, 'usage_counter': 1000 + i * 100,
                           'toner_level': 80 - i, 'raw_level': 80 - i,
                           'source': 'poll', 'valid': 1, 'timestamp': '',
                           'cartridge_key': self.state['cartridge_key']}
                          for i in range(8)],
        }}}
        with patch.dict(os.environ, DB_BACKEND='postgresql', PG_DSN='must-not-be-used'), \
             patch('core.yield_engine.db_connection', side_effect=AssertionError('external DB access')):
            report = replay_export(exported)
        decision = report['devices']['192.0.2.80']['decision']
        self.assertNotEqual(decision['learning_state'], 'prior')
        self.assertLess(decision['capacity_pages'], 15000)

    def test_legacy_and_forced_values_are_not_replaced_by_shadow_decision(self):
        from core.yield_engine import _state_public_dict
        self.state.update(yield_per_page=10000, yield_source='forced_estimate', confidence='medium', current_level=70)
        dec = self.estimate([])
        for enabled, forced in (('', False), ('v2', True)):
            with patch.dict(os.environ, YIELD_ESTIMATOR=enabled):
                self.state['force_estimate'] = forced
                meta = _state_public_dict(self.state, interval_decision=dec)
                self.assertEqual(meta['yield_per_page'], 10000)
                self.assertEqual(meta['eta_pages'], 7000)


@unittest.skipUnless(os.getenv('YIELD_TEST_PG_DSN'), 'isolated PostgreSQL DSN not supplied')
class PostgresHistoryTests(unittest.TestCase):
    def test_postgres_and_sqlite_history_endpoints_match(self):
        from psycopg.conninfo import conninfo_to_dict
        from core.db import connect_postgres, connect_sqlite
        from core.yield_engine import _load_interval_snapshots
        dsn = os.environ['YIELD_TEST_PG_DSN']
        # Never run against an operational database, even when explicitly passed.
        if not conninfo_to_dict(dsn).get('dbname', '').startswith('printguard_test_'):
            self.fail('YIELD_TEST_PG_DSN must name a printguard_test_ database')
        pg = connect_postgres(dsn)
        lite = connect_sqlite(':memory:')
        self.addCleanup(pg.close)
        self.addCleanup(lite.close)
        results = []
        for conn in (pg, lite):
            # TEMP table shadows public without modifying an existing schema.
            conn.execute("""CREATE TEMP TABLE toner_snapshots_v2 (
                id INTEGER PRIMARY KEY, printer_ip TEXT, color TEXT,
                usage_counter INTEGER, toner_level INTEGER, raw_level INTEGER,
                source TEXT, valid INTEGER, reject_reason TEXT, timestamp TEXT,
                cartridge_key TEXT, full_color INTEGER, print_total INTEGER)""")
            counts = (60228, 60257, 0, 60262, 60263)
            for i, counter in enumerate(counts):
                conn.execute("""INSERT INTO toner_snapshots_v2 VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (i, '192.0.2.80', 'black', counter, 30 if i < 3 else 27,
                     30 if i < 3 else 27, 'poll', 1, None, '', 'part', None, counter))
            rows = _load_interval_snapshots(conn, ip='192.0.2.80', color='black')
            results.append([tuple(row) for row in rows])
        self.assertEqual(results[0], results[1])
        self.assertIn(0, [row[2] for row in results[0]])


if __name__ == '__main__':
    unittest.main()
