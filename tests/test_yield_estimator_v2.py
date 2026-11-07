# -*- coding: utf-8 -*-
"""Comprehensive unit, statistical calibration, and integration tests for v2 Yield Estimator."""

from __future__ import annotations

import os
import random
import tempfile
import unittest
from unittest.mock import patch

import config.settings as settings
import core.store as store
from core.database import init_db
from core.yield_engine import (
    ensure_yield_tables,
    get_yield_status,
    process_cartridge_snapshot,
)
from core.yield_estimator import (
    BRAND_SPECS,
    SupplyReading,
    compress_plateaus,
    estimate_supply_series,
    fit_segment,
    get_brand_spec,
    reading_to_interval,
    segment_readings,
)
from tools.yield_backtest import run_db_replay, run_synthetic_benchmark
from web.routes.printers import _attach_yield_metadata


class TestYieldEstimatorV2Pure(unittest.TestCase):
    """Pure Layer 1-5 mathematical & behavioral tests (zero DB/Flask dependencies)."""

    def test_all_10_brand_specs_resolved(self):
        expected = {
            "hp", "toshiba", "canon", "brother", "kyocera",
            "ricoh", "xerox", "lexmark", "samsung", "epson",
        }
        self.assertEqual(set(BRAND_SPECS.keys()), expected)
        self.assertEqual(get_brand_spec("HP LaserJet Enterprise M608").vendor, "hp")
        self.assertEqual(get_brand_spec("TOSHIBA e-STUDIO4515AC").vendor, "toshiba")
        self.assertEqual(get_brand_spec("KYOCERA TASKalfa 3253ci").vendor, "kyocera")
        self.assertEqual(get_brand_spec("Xerox VersaLink C405").vendor, "xerox")
        self.assertEqual(get_brand_spec("Ricoh IM C3000").vendor, "ricoh")

    def test_idle_poll_compression_exact_invariance(self):
        sparse = [
            SupplyReading(usage_counter=1000, raw_level=85, vendor="hp"),
            SupplyReading(usage_counter=1500, raw_level=80, vendor="hp"),
            SupplyReading(usage_counter=2000, raw_level=75, vendor="hp"),
            SupplyReading(usage_counter=2500, raw_level=70, vendor="hp"),
            SupplyReading(usage_counter=3000, raw_level=65, vendor="hp"),
        ]
        dense = []
        for u, lv in [(1000, 85), (1500, 80), (2000, 75), (2500, 70), (3000, 65)]:
            for _ in range(160):
                dense.append(SupplyReading(usage_counter=u, raw_level=lv, vendor="hp"))

        est_sparse = fit_segment(sparse, spec=get_brand_spec("hp"))
        est_dense = fit_segment(dense, spec=get_brand_spec("hp"))
        self.assertEqual(est_sparse.n_plateaus, 5)
        self.assertEqual(est_dense.n_plateaus, 5)
        self.assertEqual(est_dense.n_polls, 800)
        self.assertEqual(est_sparse.yield_pages, est_dense.yield_pages)
        self.assertEqual(est_sparse.ci95_lo, est_dense.ci95_lo)
        self.assertEqual(est_sparse.ci95_hi, est_dense.ci95_hi)
        self.assertAlmostEqual(est_sparse.yield_pages, 10000.0, delta=50.0)

    def test_coarse_step_quantization_5_10_20_pct(self):
        for vendor, step, true_y in [("brother", 5.0, 3000), ("kyocera", 10.0, 20000), ("epson", 20.0, 6000)]:
            readings = []
            for k in range(5):
                lv = 100.0 - k * step
                p = 5000 + int(round((k * step / 100.0) * true_y))
                readings.append(SupplyReading(usage_counter=p, raw_level=lv, vendor=vendor))
            obs = [reading_to_interval(r, get_brand_spec(vendor)) for r in readings]
            pls = compress_plateaus(obs)
            # Coarse grid widens interior intervals to +/- step/2
            self.assertAlmostEqual(pls[1].level_hi - pls[1].level_lo, step, places=3)
            dec = estimate_supply_series(readings, vendor_or_model=vendor, prior_yield=true_y, prior_source="catalog")
            self.assertAlmostEqual(dec.capacity_pages, true_y, delta=true_y * 0.08)
            self.assertLessEqual(dec.capacity_ci95[0], true_y)
            self.assertGreaterEqual(dec.capacity_ci95[1], true_y)

    def test_toshiba_transient_downward_blip_filtered(self):
        readings = [
            SupplyReading(usage_counter=10000, raw_level=90, vendor="toshiba"),
            SupplyReading(usage_counter=12800, raw_level=80, vendor="toshiba"),
            # Transient SNMP glitch: drops 72% with only 3 pages printed, then recovers!
            SupplyReading(usage_counter=12803, raw_level=8, vendor="toshiba"),
            SupplyReading(usage_counter=12810, raw_level=80, vendor="toshiba"),
            SupplyReading(usage_counter=15600, raw_level=70, vendor="toshiba"),
            SupplyReading(usage_counter=18400, raw_level=60, vendor="toshiba"),
        ]
        segs = segment_readings(readings, spec=get_brand_spec("toshiba"))
        self.assertEqual(len(segs), 1)
        est = fit_segment(segs[0].observations, spec=get_brand_spec("toshiba"))
        self.assertAlmostEqual(est.yield_pages, 28000.0, delta=400.0)
        self.assertFalse(est.suspect)

    def test_unannounced_midlevel_swap_without_chip_id_and_sku_isolation(self):
        readings = [
            # Old 2000-page Standard cartridge dropping from 80% -> 55%
            SupplyReading(usage_counter=1000, raw_level=80, vendor="brother"),
            SupplyReading(usage_counter=1200, raw_level=70, vendor="brother"),
            SupplyReading(usage_counter=1400, raw_level=60, vendor="brother"),
            SupplyReading(usage_counter=1500, raw_level=55, vendor="brother"),
            # Swapped mid-level to a 5000-page XL cartridge starting at 85%!
            SupplyReading(usage_counter=1510, raw_level=85, vendor="brother"),
            SupplyReading(usage_counter=2010, raw_level=75, vendor="brother"),
            SupplyReading(usage_counter=2510, raw_level=65, vendor="brother"),
            SupplyReading(usage_counter=3010, raw_level=55, vendor="brother"),
            SupplyReading(usage_counter=3510, raw_level=45, vendor="brother"),
        ]
        segs = segment_readings(readings, spec=get_brand_spec("brother"))
        self.assertEqual(len(segs), 2)
        dec = estimate_supply_series(readings, vendor_or_model="brother")
        # Must reflect the new 5000-page XL SKU without contamination from the old 2000-page SKU
        self.assertAlmostEqual(dec.capacity_pages, 5000, delta=250)

    def test_post_zero_reserve_clipping_and_full_cycle_recovery(self):
        # 4000-page 1%-granular Samsung slope (30% -> 0% = 1200 pages), then 400 pages printed at 0% reserve
        pre_zero = [
            SupplyReading(usage_counter=1000, raw_level=30, vendor="samsung"),
            SupplyReading(usage_counter=1360, raw_level=21, vendor="samsung"),
            SupplyReading(usage_counter=1760, raw_level=11, vendor="samsung"),
            SupplyReading(usage_counter=2200, raw_level=0, vendor="samsung"),
        ]
        with_reserve = pre_zero + [
            SupplyReading(usage_counter=2350, raw_level=0, vendor="samsung"),
            SupplyReading(usage_counter=2500, raw_level=0, vendor="samsung"),
            SupplyReading(usage_counter=2600, raw_level=0, vendor="samsung"),
        ]
        est_at_first_zero = fit_segment(pre_zero, spec=get_brand_spec("samsung"))
        est_with_reserve = fit_segment(with_reserve, spec=get_brand_spec("samsung"))
        # Post-zero 0% polls are stripped identically so pre-zero linear slope never inflates
        self.assertEqual(est_at_first_zero.yield_pages, est_with_reserve.yield_pages)
        self.assertAlmostEqual(est_with_reserve.yield_pages, 4000.0, delta=80.0)

        # Now refill happens at 2600 -> 100%: closed cycle incorporates reserve pages (1600 / 0.30 = 5333)
        refilled = with_reserve + [
            SupplyReading(usage_counter=2610, raw_level=100, vendor="samsung"),
            SupplyReading(usage_counter=2620, raw_level=100, vendor="samsung"),
        ]
        dec_after_refill = estimate_supply_series(refilled, vendor_or_model="samsung")
        self.assertGreater(dec_after_refill.capacity_pages, 4800)

    def test_canon_status_coded_transitions(self):
        readings = [
            SupplyReading(usage_counter=10000, raw_level=-3, status_text="OK", vendor="canon"),
            SupplyReading(usage_counter=25000, raw_level=-3, status_text="OK", vendor="canon"),
            SupplyReading(usage_counter=26000, raw_level=-3, status_text="Toner Low", vendor="canon"),
            SupplyReading(usage_counter=30500, raw_level=-3, status_text="Toner Low", vendor="canon"),
            SupplyReading(usage_counter=31000, raw_level=0, status_text="Replace Toner", vendor="canon"),
        ]
        dec = estimate_supply_series(
            readings, vendor_or_model="Canon iR-ADV C3530", prior_yield=24000, prior_source="catalog", current_level=0
        )
        self.assertEqual(dec.eta_pages, 0)
        self.assertIn(dec.learning_state, ("learning", "converged"))
        self.assertTrue(16000 <= dec.capacity_pages <= 28000)

    def test_broken_erratic_gauge_flagged_suspect(self):
        # Gauge jumps erratically across tight 1% intervals with massive residuals
        readings = [
            SupplyReading(usage_counter=1000, raw_level=95, vendor="hp"),
            SupplyReading(usage_counter=1200, raw_level=30, vendor="hp"),
            SupplyReading(usage_counter=1400, raw_level=29, vendor="hp"),
            SupplyReading(usage_counter=1600, raw_level=28, vendor="hp"),
            SupplyReading(usage_counter=1800, raw_level=27, vendor="hp"),
            SupplyReading(usage_counter=2000, raw_level=2, vendor="hp"),
            SupplyReading(usage_counter=2200, raw_level=1, vendor="hp"),
        ]
        dec = estimate_supply_series(readings, vendor_or_model="hp", prior_yield=9000, prior_source="catalog")
        self.assertEqual(dec.learning_state, "suspect")
        self.assertEqual(dec.confidence, "low")
        self.assertEqual(dec.capacity_pages, 9000)

    def test_empirical_bayes_prior_shrinkage_and_regime_shift_adaptation(self):
        # Early 2% drop on a 10,000-page catalog cartridge where noisy early step looks like 6,000
        early = [
            SupplyReading(usage_counter=1000, raw_level=98, vendor="hp"),
            SupplyReading(usage_counter=1120, raw_level=96, vendor="hp"),
        ]
        dec_early = estimate_supply_series(
            early, vendor_or_model="hp", prior_yield=10000, prior_source="catalog", nominal_capacity=10000, current_level=96
        )
        # Shrinks strongly toward 10,000 prior instead of jumping wildly to 6,000
        self.assertGreater(dec_early.capacity_pages, 7500)
        self.assertEqual(dec_early.capacity_method, "interval_eb")
        self.assertEqual(dec_early.learning_state, "learning")

        # Deep 40% drop proving an XL 20,000-page cartridge in a 10,000-page catalog slot
        deep_xl = [
            SupplyReading(usage_counter=1000 + k * 1000, raw_level=100 - k * 5, vendor="hp")
            for k in range(9)
        ]
        dec_xl = estimate_supply_series(
            deep_xl, vendor_or_model="hp", prior_yield=10000, prior_source="catalog", nominal_capacity=10000, current_level=60
        )
        self.assertAlmostEqual(dec_xl.capacity_pages, 20000, delta=600)
        self.assertEqual(dec_xl.learning_state, "converged")
        self.assertEqual(dec_xl.confidence, "high")
        self.assertAlmostEqual(dec_xl.coverage_index, 0.5, delta=0.05)
        self.assertAlmostEqual(dec_xl.eta_pages, 12000, delta=400)

    def test_monte_carlo_ci95_empirical_coverage_calibration(self):
        rng = random.Random(1337)
        hits = 0
        total = 0
        apes = []
        for _ in range(200):
            true_y = rng.choice([3000, 8000, 15000, 30000])
            step = rng.choice([1.0, 5.0, 10.0, 20.0])
            p = 5000
            cur = rng.uniform(75.0, 100.0)
            start = cur
            target = rng.uniform(max(step + 3.0, 16.0), 50.0)
            series = []
            while start - cur < target:
                dp = 0 if rng.random() < 0.35 else rng.randint(4, max(12, int(true_y * 0.01)))
                p += dp
                cur -= (dp / true_y) * 100.0 * max(0.35, rng.gauss(1.0, 0.15))
                if cur <= 1.0:
                    break
                q = max(0.0, min(100.0, round(cur / step) * step))
                series.append(SupplyReading(usage_counter=p, raw_level=q, vendor="hp"))
            est = fit_segment(series, spec=get_brand_spec("hp"))
            if est.yield_pages is not None:
                total += 1
                apes.append(abs(est.yield_pages - true_y) / true_y)
                if est.ci95_lo <= true_y <= est.ci95_hi:
                    hits += 1

        coverage = hits / max(1, total)
        apes.sort()
        med_ape = apes[len(apes) // 2]
        self.assertGreaterEqual(coverage, 0.90)
        self.assertLess(med_ape, 0.04)

    def test_synthetic_fleet_backtest_gates(self):
        bench = run_synthetic_benchmark(trials_per_brand=25, seed=20261107)
        fs = bench["fleet_summary"]
        self.assertGreaterEqual(fs["mape_relative_reduction_pct"], 30.0)
        self.assertLessEqual(fs["v2_median_ape_pct"], 5.0)
        self.assertGreaterEqual(fs["v2_mle_ci95_coverage_pct"], 90.0)
        self.assertEqual(fs["idle_poll_invariance_diff"], 0.0)

    def test_sqlite_file_backtest_replay(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmpdir:
            sq_path = os.path.join(tmpdir, "standalone_replay.db")
            conn = sqlite3.connect(sq_path)
            try:
                conn.execute(
                    "CREATE TABLE toner_snapshots_v2 ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, printer_ip TEXT, color TEXT, "
                    "printer_model TEXT, cartridge_name TEXT, cartridge_key TEXT, "
                    "timestamp TEXT, usage_counter INTEGER, toner_level INTEGER, "
                    "raw_level INTEGER, source TEXT, valid INTEGER, reject_reason TEXT)"
                )
                for i in range(11):
                    conn.execute(
                        "INSERT INTO toner_snapshots_v2 "
                        "(printer_ip, color, printer_model, cartridge_name, cartridge_key, "
                        "timestamp, usage_counter, toner_level, raw_level, source, valid, reject_reason) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            "10.88.0.1", "black", "HP LaserJet M608", "CF237A", "hp m608|cf237a|black",
                            f"2026-11-07T10:{i:02d}:00Z", 10000 + i * 500, 100 - i * 5, 100 - i * 5,
                            "poll", 1, None,
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

            rep = run_db_replay(sq_path)
            self.assertEqual(rep["series_count"], 1)
            self.assertEqual(rep["closed_or_deep_segments_evaluated"], 1)
            self.assertLessEqual(rep["early_v2_mape_pct"], 5.0)


class TestYieldEstimatorV2Integration(unittest.TestCase):
    """Database (PostgreSQL & SQLite), YIELD_ESTIMATOR=v2 env mode, API enrichment, and live DB replay."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_v2_yield.db")
        self._db_patch = patch.object(settings, "DB_PATH", self.db_path)
        env_vars = {}
        if "printguard_test_" not in os.environ.get("PG_DSN", ""):
            env_vars["DB_BACKEND"] = "sqlite"
        self._env_patch = patch.dict(os.environ, env_vars)
        self._db_patch.start()
        self._env_patch.start()
        init_db()
        ensure_yield_tables()
        store._prev._cache.clear()

    def tearDown(self):
        store._prev._cache.clear()
        self._env_patch.stop()
        self._db_patch.stop()
        self._tmp.cleanup()

    def test_yield_engine_v2_env_mode_and_db_backtest_replay(self):
        ip = "10.99.0.15"
        env_overrides = {
            "YIELD_ESTIMATOR": "v2",
            "CARTRIDGE_YIELD_MODE": "oem",
            "CARTRIDGE_YIELD_FACTOR": "1",
        }
        with patch.dict(os.environ, env_overrides):
            # Feed a 12,000-page HP LaserJet trajectory down 50% (100% -> 50%)
            last_res = None
            for step_i in range(11):
                lvl = 100 - step_i * 5
                pages = 20000 + step_i * 600
                last_res = process_cartridge_snapshot(
                    ip=ip,
                    color="black",
                    printer_model="HP LaserJet Enterprise M608",
                    cartridge_name="HP 37A CF237A",
                    level=lvl,
                    raw_level=lvl,
                    counters={"total": pages, "black_white": pages},
                    device_type="mono",
                    timestamp=f"2026-11-07T08:{step_i:02d}:00Z",
                )

            self.assertIsNotNone(last_res)
            self.assertAlmostEqual(last_res["yield_per_page"], 12000, delta=450)
            self.assertEqual(last_res["confidence"], "high")
            self.assertEqual(last_res["learning_state"], "converged")
            self.assertEqual(last_res["capacity_method"], "interval_eb")
            self.assertEqual(len(last_res["capacity_ci95"]), 2)
            self.assertLess(last_res["capacity_ci95"][0], last_res["capacity_ci95"][1])

            status_rows = get_yield_status(ip=ip)
            self.assertEqual(len(status_rows), 1)
            row = status_rows[0]
            self.assertEqual(row["learning_state"], "converged")
            self.assertIsNotNone(row["eta_pages"])
            self.assertAlmostEqual(row["eta_pages"], 6000, delta=250)

            # Verify /api/printers _attach_yield_metadata exposes Layer 5 fields cleanly
            payload = {
                "ip": ip,
                "toners": {"black": {"level": 50, "name": "HP 37A CF237A"}},
                "counters": {"total": 26000},
            }
            enriched = _attach_yield_metadata(payload)
            blk = enriched["toners"]["black"]
            self.assertEqual(blk["learning_state"], "converged")
            self.assertEqual(blk["capacity_method"], "interval_eb")
            self.assertEqual(len(blk["capacity_ci95"]), 2)
            self.assertIsNotNone(blk["eta_pages"])

            # Verify B-3 synthetic status-coded levels remain ignored in v2 mode too
            canon_ip = "10.99.0.16"
            for idx, (syn_lv, cnt) in enumerate([(70, 5000), (15, 5800)]):
                process_cartridge_snapshot(
                    ip=canon_ip,
                    color="black",
                    printer_model="Canon iR-ADV C3530",
                    cartridge_name="GPR-53 Black",
                    level=syn_lv,
                    raw_level=None,
                    level_is_synthetic=True,
                    counters={"total": cnt, "black_white": cnt},
                    device_type="color",
                    timestamp=f"2026-11-07T09:{idx:02d}:00Z",
                )
            canon_row = get_yield_status(ip=canon_ip)[0]
            self.assertEqual(canon_row["learning_state"], "prior")
            self.assertNotEqual(canon_row["yield_source"], "auto_learn")

            # Verify live DB replay works across both PostgreSQL and SQLite via db_connection()
            replay = run_db_replay()
            self.assertEqual(replay["series_count"], 1)
            self.assertEqual(replay["closed_or_deep_segments_evaluated"], 1)
            self.assertLessEqual(replay["early_v2_mape_pct"], 5.0)


if __name__ == "__main__":
    unittest.main()
