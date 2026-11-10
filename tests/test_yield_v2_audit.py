"""v2 audit: independent evidence, rejected observations, and telemetry boundaries.

Pure tests plus a temporary SQLite replay; never use an operational DSN.
"""
import dataclasses
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.yield_estimator import (
    SupplyReading, combine_with_prior, compress_plateaus, derive_ci_confidence,
    estimate_supply_series, fit_segment, reading_to_interval, segment_readings,
)


class V2ObservationAuditTests(unittest.TestCase):
    def test_invalid_counters_are_ignored_without_throwing_or_truncation(self):
        for counter in (True, False, -1, 1.5, float('nan'), float('inf'), 'bad', None):
            with self.subTest(counter=counter):
                self.assertIsNone(reading_to_interval(SupplyReading(counter, 50)))

    def test_invalid_levels_quality_capacity_and_step_are_not_evidence(self):
        for kwargs in (
            {'raw_level': True}, {'raw_level': float('nan')}, {'raw_level': float('inf')},
            {'counter_quality': 0}, {'counter_quality': float('nan')},
            {'step_hint_pct': float('inf')}, {'max_capacity': float('inf')},
            {'level_kind': 'capacity_units', 'max_capacity': None},
            {'level_kind': 'capacity_units', 'max_capacity': -3},
            {'level_kind': 'unrecognized'},
        ):
            with self.subTest(kwargs=kwargs):
                args = {'usage_counter': 1000, 'raw_level': 50, **kwargs}
                self.assertIsNone(reading_to_interval(SupplyReading(**args)))

    def test_near_end_is_critical_not_empty(self):
        ob = reading_to_interval(SupplyReading(1000, -3, status_text='Toner near end', vendor='canon'))
        self.assertGreater(ob.level_lo, 0)
        self.assertGreater(ob.level_nominal, 0)
        self.assertLess(ob.level_nominal, 25)
        for text in ('Replace Toner', 'Toner exhausted', 'Out of toner'):
            self.assertEqual(reading_to_interval(SupplyReading(1000, -3, status_text=text)).level_nominal, 0)

    def test_status_substrings_do_not_invent_empty_toner(self):
        for text in ('pending', 'without measurement', 'weekend'):
            with self.subTest(text=text):
                self.assertIsNone(reading_to_interval(SupplyReading(1000, None, status_text=text)))

    def test_blip_filter_cannot_remove_chip_swap(self):
        series = [SupplyReading(1000, 80, chip_id='old-chip'),
                  SupplyReading(1001, 8, chip_id='new-chip'),
                  SupplyReading(1002, 80, chip_id='new-chip')]
        segs = segment_readings(series)
        self.assertTrue(any(s.break_reason == 'chip_swap' for s in segs))
        self.assertEqual(sum(len(s.observations) for s in segs), 3)

    def test_blip_filter_cannot_bridge_counter_reset(self):
        segs = segment_readings([SupplyReading(1000, 80), SupplyReading(1001, 8), SupplyReading(5, 80)])
        self.assertTrue(any(s.break_reason == 'counter_reset' for s in segs))
        self.assertEqual(sum(len(s.observations) for s in segs), 3)

    def test_real_blip_with_same_chip_still_filtered(self):
        segs = segment_readings([SupplyReading(1000, 80, chip_id='same'),
                                SupplyReading(1001, 8, chip_id='same'),
                                SupplyReading(1002, 80, chip_id='same')])
        self.assertEqual(len(segs), 1)
        self.assertEqual(len(segs[0].observations), 2)

    def test_percent_precision_is_not_rounded_into_one_plateau(self):
        obs = [reading_to_interval(SupplyReading(1000, 80.004)),
               reading_to_interval(SupplyReading(1100, 80.001))]
        self.assertEqual(obs[0].level_nominal, 80.004)
        self.assertEqual(len(compress_plateaus(obs)), 2)

    def test_idle_duplicates_do_not_change_posterior_or_confidence(self):
        series = [SupplyReading(1000 + i * 100, 80 - i, vendor='hp') for i in range(8)]
        a = estimate_supply_series(series, vendor_or_model='hp')
        b = estimate_supply_series([r for r in series for _ in range(40)], vendor_or_model='hp')
        self.assertEqual(dataclasses.asdict(a), dataclasses.asdict(b))

    def test_prior_only_confidence_agrees_with_ci_and_is_not_converged(self):
        for source in ('catalog', 'shared_profile', 'device_capacity'):
            with self.subTest(source=source):
                dec = combine_with_prior(None, prior_yield=10000, prior_source=source)
                self.assertEqual(dec.confidence, derive_ci_confidence(dec.rel_ci95))
                self.assertNotEqual(dec.confidence, 'high')
                self.assertEqual(dec.learning_state, 'prior')
                self.assertEqual(dec.n_segments, 0)

    def test_nan_ci_is_low_confidence(self):
        self.assertEqual(derive_ci_confidence(float('nan')), 'low')
        self.assertEqual(derive_ci_confidence(-0.1), 'low')

    def test_empirical_current_estimate_is_not_an_independent_prior(self):
        series = [SupplyReading(1000 + i * 50, 80 - i) for i in range(4)]
        plain = estimate_supply_series(series)
        for src in ('auto_learn', 'cycle_learn', 'learned'):
            dec = estimate_supply_series(series, prior_yield=10000, prior_source=src)
            self.assertEqual(dec.capacity_pages, plain.capacity_pages)
            self.assertEqual(dec.capacity_ci95, plain.capacity_ci95)
            self.assertEqual(dec.confidence, plain.confidence)
            self.assertEqual(estimate_supply_series([], prior_yield=10000, prior_source=src),
                             estimate_supply_series([]))


class V2ReplayAuditTests(unittest.TestCase):
    def setUp(self):
        import config.settings as settings
        from core.database import init_db
        from core.yield_engine import ensure_yield_tables
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, old_cwd)
        self.start_patch(patch.dict(os.environ, {
            'DB_BACKEND': 'sqlite', 'PG_DSN': '', 'REPORTING_PG_DSN': '',
            'YIELD_ESTIMATOR': 'v2', 'CARTRIDGE_YIELD_CATALOG': str(Path(self.tmp.name) / 'catalog.json'),
        }))
        Path('catalog.json').write_text('{"version":1,"entries":[]}', encoding='utf8')
        self.start_patch(patch.object(settings, 'DB_PATH', str(Path(self.tmp.name) / 'audit.db')))
        init_db()
        ensure_yield_tables()
        self.state = {'printer_ip': '192.0.2.80', 'color': 'black', 'printer_model': 'Audit Model',
                      'cartridge_name': 'Audit Part', 'cartridge_key': 'audit model|audit part|black',
                      'current_level': 70, 'yield_per_page': 2000, 'yield_source': 'default'}

    def start_patch(self, patcher):
        obj = patcher.start()
        self.addCleanup(patcher.stop)
        return obj

    def rows(self):
        return [(1000 + i * 100, 80 - i, 80 - i, 'poll', 1, None,
                 f'2026-01-01T00:{i:02}:00Z', self.state['cartridge_key'], None, 1000 + i * 100)
                for i in range(8)]

    def estimate(self, rows):
        from core.database import db_connection
        from core.yield_engine import estimate_cartridge_interval
        with db_connection() as conn:
            return estimate_cartridge_interval(conn, ip=self.state['printer_ip'], color='black',
                                               state=self.state, snap_rows=rows)

    def test_rejected_sensor_noise_does_not_change_interval_decision(self):
        rows = self.rows()
        good = self.estimate(rows)
        bad = (1401, 2, 2, 'poll', 0, 'level_read_noise', '2026-01-01T00:04:01Z',
               self.state['cartridge_key'], None, 1401)
        mixed = self.estimate(rows[:5] + [bad] + rows[5:])
        self.assertEqual(dataclasses.asdict(good), dataclasses.asdict(mixed))

    def test_unknown_invalid_reason_is_not_reintroduced(self):
        rows = self.rows()
        bad = (1401, 2, 2, 'poll', 0, 'future_invalid_sensor', '', self.state['cartridge_key'], None, 1401)
        self.assertEqual(self.estimate(rows), self.estimate(rows[:5] + [bad] + rows[5:]))

    def test_entire_invalid_series_cannot_become_learned_evidence(self):
        for reason in ('level_read_noise', 'future_invalid_sensor'):
            with self.subTest(reason=reason):
                rejected = [(r[0], r[1], r[2], r[3], 0, reason, *r[6:]) for r in self.rows()]
                self.assertEqual(self.estimate(rejected).learning_state, 'prior')

    def test_valid_flag_does_not_rehabilitate_synthetic_observation(self):
        rows = self.rows()
        bad = (1401, 2, 2, 'poll', 1, 'status_coded_level', '', self.state['cartridge_key'], None, 1401)
        self.assertEqual(self.estimate(rows), self.estimate(rows[:5] + [bad] + rows[5:]))

    def test_ratio_rejections_remain_usable_measurements_for_v2(self):
        rows = self.rows()
        for reason in ('quantization_1pct_noise', 'not_enough_pages_yet', 'estimated_yield_out_of_range'):
            rejected = [(r[0], r[1], r[2], r[3], 0, reason, *r[6:]) for r in rows]
            self.assertEqual(self.estimate(rows), self.estimate(rejected))

    def test_malformed_replay_row_does_not_abort_other_observations(self):
        rows = self.rows()
        for counter, raw in (('bad', 50), (1401, 'bad'), (float('inf'), 50)):
            with self.subTest(counter=counter, raw=raw):
                bad = (counter, 50, raw, 'poll', 1, None, '', self.state['cartridge_key'], None, counter)
                self.assertEqual(self.estimate(rows), self.estimate(rows[:5] + [bad] + rows[5:]))

    def test_current_slot_learning_is_not_reused_as_prior(self):
        from core.yield_engine import _resolve_interval_prior
        self.state.update(yield_source='auto_learn', yield_per_page=10000)
        with patch('core.yield_engine.estimate_yield_fallback', return_value=-1):
            prior, source, _ = _resolve_interval_prior(self.state)
            self.assertEqual(source, 'default')
            self.assertEqual(prior, 2000)

    def test_own_profile_does_not_count_as_independent_fleet_prior(self):
        from core.yield_engine import _resolve_interval_prior
        profile = {'yield_per_page': 10000, 'confidence': 'high', 'sample_count': 50,
                   'total_weight': 100, 'source_printer_ip': self.state['printer_ip']}
        with patch('core.yield_engine._profile_is_trusted', return_value=True), \
             patch('core.yield_engine.estimate_yield_fallback', return_value=-1):
            _, source, _ = _resolve_interval_prior(self.state, profile=profile)
            self.assertEqual(source, 'default')
            profile['source_printer_ip'] = '192.0.2.81'
            self.assertEqual(_resolve_interval_prior(self.state, profile=profile)[1], 'shared_profile')

    def test_offline_replay_matches_live_observation_admission(self):
        import sqlite3
        from tools.yield_backtest import run_db_replay

        db = Path(self.tmp.name) / 'offline.db'
        with sqlite3.connect(db) as conn:
            conn.execute("""CREATE TABLE toner_snapshots_v2 (
                id INTEGER PRIMARY KEY, printer_ip TEXT, color TEXT, timestamp TEXT,
                usage_counter, toner_level, raw_level, source TEXT, valid INTEGER,
                reject_reason TEXT, cartridge_key TEXT)""")

            def replace_rows(rows):
                conn.execute('DELETE FROM toner_snapshots_v2')
                conn.executemany(
                    'INSERT INTO toner_snapshots_v2 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    [(i, self.state['printer_ip'], 'black', r[6], r[0], r[1], r[2], r[3], r[4], r[5], r[7])
                     for i, r in enumerate(rows)])
                conn.commit()

            replace_rows(self.rows())
            baseline = run_db_replay(str(db))['series'][0]
            self.state['current_level'] = self.rows()[-1][2]
            live = self.estimate(self.rows()).to_api_dict()
            for key in live:
                self.assertEqual(baseline[key], live[key], key)
            for reason in ('level_read_noise', 'unknown_sensor_error', 'synthetic_level',
                           'status_coded_level', 'quarantined_chip', 'frozen_chip'):
                with self.subTest(reason=reason):
                    bad = (1401, 1, 1, 'poll', 0, reason, '', self.state['cartridge_key'], None, 1401)
                    replace_rows(self.rows()[:5] + [bad] + self.rows()[5:])
                    self.assertEqual(run_db_replay(str(db))['series'][0], baseline)
            for counter, raw in (('bad', 50), (1401, 'bad')):
                replace_rows(self.rows() + [(counter, 50, raw, 'poll', 1, None, '',
                                            self.state['cartridge_key'], None, counter)])
                self.assertEqual(run_db_replay(str(db))['series'][0], baseline)
            for reason in ('not_enough_pages_yet', 'quantization_1pct_noise', 'estimated_yield_out_of_range'):
                replace_rows([(r[0], r[1], r[2], r[3], 0, reason, *r[6:]) for r in self.rows()])
                self.assertEqual(run_db_replay(str(db))['series'][0], baseline)



if __name__ == '__main__':
    unittest.main()
