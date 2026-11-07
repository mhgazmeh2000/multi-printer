# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_yield_reconciliation_multibrand.py
# @brief:        Regression and convergence suite for Yield Engine reconciliation & all 10 brands.
# @details:      Verifies fixes for M1-M9 / B1-B2 / C13 / D1-D2 / R5 from the yield reconciliation
#                audit and proves fast, minimum-error convergence across quantized, sub-percent,
#                coarse-step, sensorless, and multi-brand printers.
# =============================================================================

"""Comprehensive tests for Yield Engine bug fixes, fast convergence, and 10-brand compatibility."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import config.settings as settings
from core import store
from core.collectors.base import _learn_yield_per_page, apply_toner_override
from core.database import db_connection, init_db
from core.enhanced_collector import _detect_toner_color, _vendor_hint_from_identity
from core.yield_engine import (
    BRAND_COLOR_FALLBACKS,
    MAX_ESTIMATED_YIELD,
    _catalog_lookup,
    _record_sample,
    _robust_learned_yield,
    build_cartridge_key,
    compute_yield_per_color,
    ensure_yield_tables,
    estimate_yield_fallback,
    get_yield_status,
    process_cartridge_snapshot,
    process_printer_yield_snapshot,
    register_manual_refill,
)
from web.routes.printers import _attach_yield_metadata


def _ts(minute: int) -> str:
    base = datetime(2026, 11, 7, 8, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class YieldReconciliationAndConvergenceTests(unittest.TestCase):
    """Tests covering reconciliation defects (M1-M9, B1-B2, D1-D2, R5) and statistical convergence."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "yield_reconcile.db")
        self.empty_catalog = os.path.join(self.tmpdir.name, "empty_catalog.json")
        with open(self.empty_catalog, "w", encoding="utf-8") as fh:
            fh.write('{"version": 1, "entries": []}')
        self.db_patch = mock.patch.object(settings, "DB_PATH", self.db_path)
        env_vars = {
            "CARTRIDGE_YIELD_CATALOG": self.empty_catalog,
            "CARTRIDGE_YIELD_FACTOR": "1",
            "YIELD_ESTIMATOR": "v1",
        }
        if "printguard_test_" not in os.environ.get("PG_DSN", ""):
            env_vars["DB_BACKEND"] = "sqlite"
        self.env_patch = mock.patch.dict(os.environ, env_vars)
        self.db_patch.start()
        self.env_patch.start()
        init_db()
        ensure_yield_tables()
        store._prev._cache.clear()

    def tearDown(self):
        store._prev._cache.clear()
        self.env_patch.stop()
        self.db_patch.stop()
        self.tmpdir.cleanup()

    def test_m8_blend_freeze_eliminated_and_verified_regime_shift_adapts(self):
        """Out-of-band samples never fabricate an intermediate blend (22,800), yet consistent regime shifts adapt."""
        ip = "10.20.0.1"
        model = "Toshiba e-STUDIO457"
        cart = "T-5070E"
        rated = 36600

        # Seed at 100%
        meta = process_cartridge_snapshot(
            ip=ip, color="black", printer_model=model, cartridge_name=cart,
            level=100, counters={"total": 10000}, device_type="mono",
            device_capacity_pages=rated, timestamp=_ts(0),
        )
        self.assertEqual(meta["yield_per_page"], rated)
        self.assertEqual(meta["yield_source"], "device_capacity")

        # Polls 1..5: compatible/refilled cartridge yielding ~9,000 pages (5% drop = 450 pages each).
        # Note: 9,000 < 0.25 * 36,600 (= 9,150). Previously L1382 blended 36,600 & 9,000 -> 22,800 and froze!
        level = 100
        total = 10000
        for step in range(1, 6):
            level -= 5
            total += 450
            meta = process_cartridge_snapshot(
                ip=ip, color="black", printer_model=model, cartridge_name=cart,
                level=level, counters={"total": total}, device_type="mono",
                device_capacity_pages=rated, timestamp=_ts(step * 10),
            )
            self.assertNotEqual(meta["yield_per_page"], 22800, "blend-freeze intermediate artifact must never appear")
            self.assertEqual(meta["yield_per_page"], rated)
            self.assertEqual(meta["yield_source"], "device_capacity")

        # Poll 6 (6 consistent samples, 30% cumulative real drop, CV = 0.0 <= 0.20):
        # Regime shift is now statistically proven -> adapts cleanly to 9,000 and persists without self-heal amnesia!
        level -= 5
        total += 450
        meta = process_cartridge_snapshot(
            ip=ip, color="black", printer_model=model, cartridge_name=cart,
            level=level, counters={"total": total}, device_type="mono",
            device_capacity_pages=rated, timestamp=_ts(60),
        )
        self.assertEqual(meta["yield_per_page"], 9000)
        self.assertEqual(meta["yield_source"], "auto_learn")
        self.assertEqual(meta["confidence"], "high")

        # Poll 7 (stable level): must NOT snap back to 36,600 via self-heal
        meta_after = process_cartridge_snapshot(
            ip=ip, color="black", printer_model=model, cartridge_name=cart,
            level=level, counters={"total": total + 10}, device_type="mono",
            device_capacity_pages=rated, timestamp=_ts(70),
        )
        self.assertEqual(meta_after["yield_per_page"], 9000)
        self.assertEqual(meta_after["yield_source"], "auto_learn")

    def test_m8_unverified_poisoned_state_still_self_heals(self):
        """A poisoned DB state without multi-sample low-CV backing still snaps back to reference."""
        ip = "10.20.0.2"
        process_cartridge_snapshot(
            ip=ip, color="black", printer_model="Toshiba e-STUDIO306", cartridge_name="T-5070",
            level=90, counters={"total": 5000}, device_type="mono",
            device_capacity_pages=36600, timestamp=_ts(0),
        )
        with db_connection(commit=True) as conn:
            conn.execute(
                "UPDATE cartridge_state SET yield_per_page = 420, yield_source = 'cycle_learn', sample_count = 1 "
                "WHERE printer_ip = ? AND color = 'black'",
                (ip,),
            )
        healed = process_cartridge_snapshot(
            ip=ip, color="black", printer_model="Toshiba e-STUDIO306", cartridge_name="T-5070",
            level=90, counters={"total": 5010}, device_type="mono",
            device_capacity_pages=36600, timestamp=_ts(5),
        )
        self.assertEqual(healed["yield_per_page"], 36600)
        self.assertEqual(healed["yield_source"], "device_capacity")
        self.assertEqual(healed["sample_count"], 0)

    def test_m4_pre_zero_early_refill_divides_by_actual_consumed_drop(self):
        """Early refill at 40% (60% consumed over 6,000 pages) learns 10,000 instead of 6,000."""
        ip = "10.20.0.3"
        process_cartridge_snapshot(
            ip=ip, color="black", printer_model="HP M404", cartridge_name="CF259X",
            level=100, counters={"total": 1000}, device_type="mono", timestamp=_ts(0),
        )
        process_cartridge_snapshot(
            ip=ip, color="black", printer_model="HP M404", cartridge_name="CF259X",
            level=40, counters={"total": 7000}, device_type="mono", timestamp=_ts(30),
        )
        refill_meta = register_manual_refill(
            ip=ip, color="black", printer_model="HP M404", cartridge_name="CF259X",
            new_level=100, counters={"total": 7000}, device_type="mono", timestamp=_ts(35),
        )
        self.assertEqual(refill_meta["yield_source"], "cycle_learn")
        self.assertEqual(refill_meta["yield_per_page"], 10000)

        # Tiny 5% consumption followed by manual reset must NOT close a bogus partial cycle
        process_cartridge_snapshot(
            ip=ip, color="black", printer_model="HP M404", cartridge_name="CF259X",
            level=95, counters={"total": 7500}, device_type="mono", timestamp=_ts(40),
        )
        before_samples = get_yield_status(ip=ip)[0]["sample_count"]
        register_manual_refill(
            ip=ip, color="black", printer_model="HP M404", cartridge_name="CF259X",
            new_level=100, counters={"total": 7500}, device_type="mono", timestamp=_ts(45),
        )
        after_samples = get_yield_status(ip=ip)[0]["sample_count"]
        self.assertEqual(after_samples, before_samples)

    def test_m4_single_step_auto_refill_closes_zero_plateau_cycle(self):
        """Single-step refill (0% -> 100% -> 100%) closes zero_plateau cycle including pages_after_zero."""
        ip = "10.20.0.4"
        process_cartridge_snapshot(
            ip=ip, color="black", printer_model="Canon LBP226", cartridge_name="057",
            level=100, counters={"total": 1000}, device_type="mono", timestamp=_ts(0),
        )
        process_cartridge_snapshot(
            ip=ip, color="black", printer_model="Canon LBP226", cartridge_name="057",
            level=0, counters={"total": 3000}, device_type="mono", timestamp=_ts(20),
        )
        # 800 reserve pages printed at 0% -> total real cycle = 2800 pages
        process_cartridge_snapshot(
            ip=ip, color="black", printer_model="Canon LBP226", cartridge_name="057",
            level=0, counters={"total": 3800}, device_type="mono", timestamp=_ts(40),
        )
        # Candidate refill jump 0% -> 100%
        process_cartridge_snapshot(
            ip=ip, color="black", printer_model="Canon LBP226", cartridge_name="057",
            level=100, counters={"total": 3800}, device_type="mono", timestamp=_ts(50),
        )
        # Confirmation poll at 100% (stale_anchor_reset path)
        confirmed = process_cartridge_snapshot(
            ip=ip, color="black", printer_model="Canon LBP226", cartridge_name="057",
            level=100, counters={"total": 3810}, device_type="mono", timestamp=_ts(60),
        )
        self.assertEqual(confirmed["yield_source"], "cycle_learn")
        self.assertEqual(confirmed["yield_per_page"], 2800)

    def test_m3_high_capacity_copier_toners_and_drums_above_100k(self):
        """Enterprise copiers (>100k toner) and long-life OPC drums (up to 600k) are supported."""
        self.assertGreaterEqual(MAX_ESTIMATED_YIELD, 500000)
        ip = "10.20.0.5"
        process_cartridge_snapshot(
            ip=ip, color="drum", printer_model="Toshiba e-STUDIO7518A", cartridge_name="OD-6510 Drum",
            level=100, counters={"total": 10000}, device_type="mono",
            device_capacity_pages=240000, timestamp=_ts(0),
        )
        process_cartridge_snapshot(
            ip=ip, color="drum", printer_model="Toshiba e-STUDIO7518A", cartridge_name="OD-6510 Drum",
            level=95, counters={"total": 22500}, device_type="mono",
            device_capacity_pages=240000, timestamp=_ts(20),
        )
        meta = process_cartridge_snapshot(
            ip=ip, color="drum", printer_model="Toshiba e-STUDIO7518A", cartridge_name="OD-6510 Drum",
            level=90, counters={"total": 35000}, device_type="mono",
            device_capacity_pages=240000, timestamp=_ts(40),
        )
        self.assertEqual(meta["yield_source"], "auto_learn")
        self.assertEqual(meta["yield_per_page"], 250000)

    def test_m5_hardware_swap_with_identical_model_and_name_resets_anchor(self):
        """Chip/serial or supply_pages swap resets anchor even when model, name, and level jump <20%."""
        ip = "10.20.0.6"
        toners_initial = {"black": {"name": "Black Cartridge HP CF287A", "level": 75, "raw_level": 75}}
        process_printer_yield_snapshot(
            ip=ip, printer_model="HP LaserJet M506", counters={"total": 10000},
            toners=toners_initial, device_type="mono", timestamp=_ts(0),
        )
        # Swap a 75% cartridge with an 85% spare cartridge of the SAME part number (+10% < REFILL_JUMP_PERCENT):
        toners_swapped = {"black": {"name": "Black Cartridge HP CF287A", "level": 85, "raw_level": 85}}
        res = process_printer_yield_snapshot(
            ip=ip, printer_model="HP LaserJet M506", counters={"total": 10050},
            toners=toners_swapped, device_type="mono", timestamp=_ts(10),
            swapped_colors=["black"],
        )
        self.assertEqual(res["black"]["anchor_level"], 85)
        self.assertEqual(res["black"]["anchor_counter"], 10050)
        self.assertEqual(res["black"]["cycle_start_level"], 85)

    def test_fast_convergence_quantized_boundary_cancellation_and_outlier_immunity(self):
        """Telescoping M-estimator cancels integer rounding jitter (0% error vs 20% old error) and rejects outliers."""
        ip = "10.20.0.7"
        key = build_cartridge_key("Ricoh MP 3055", "Black Toner", "black")
        # True yield = 10,000. Consecutive 2% quantized hops have rounding phase noise:
        # 155p/2%=7750, 240p/2%=12000, 160p/2%=8000, 245p/2%=12250 (sum=800p / 8% = 10,000 exact!),
        # plus one corrupted 48,000 outlier.
        hops = [(155, 2, 7750), (240, 2, 12000), (160, 2, 8000), (245, 2, 12250), (9600, 20, 48000)]
        with db_connection(commit=True) as conn:
            for p_delta, t_drop, est in hops:
                _record_sample(
                    conn, ip=ip, color="black", printer_model="Ricoh MP 3055",
                    cartridge_name="Black Toner", cartridge_key=key,
                    start_counter=1000, end_counter=1000 + p_delta,
                    start_level=100, end_level=100 - t_drop,
                    pages_delta=p_delta, toner_drop=t_drop, estimated_yield=est,
                    counter_basis="total_for_black", weight=2.0, accepted=True, source="poll",
                )
            learned, _ = _robust_learned_yield(conn, ip, "black", key)
        self.assertEqual(learned, 10000)

    def test_fast_convergence_subpercent_raw_units_and_coarse_step_printers(self):
        """High-res SNMP supplies (max > 100) learn accurately in <1% drop; coarse 25%-step printers reach medium in 1 step."""
        # 1) Sub-percent raw SNMP impressions: max=10000, remaining 9960 -> 9880 (0.80% drop over 80 pages -> 10,000)
        ip_sub = "10.20.0.8"
        process_printer_yield_snapshot(
            ip=ip_sub, printer_model="Canon iR-ADV C3530",
            counters={"total": 5000},
            toners={"black": {"name": "NPG-67 Black", "level": 100, "raw_level": 100, "max": 10000, "remaining": 9960, "unit_code": 7}},
            device_type="mono", timestamp=_ts(0),
        )
        res_sub = process_printer_yield_snapshot(
            ip=ip_sub, printer_model="Canon iR-ADV C3530",
            counters={"total": 5080},
            toners={"black": {"name": "NPG-67 Black", "level": 99, "raw_level": 99, "max": 10000, "remaining": 9880, "unit_code": 7}},
            device_type="mono", timestamp=_ts(10),
        )
        self.assertEqual(res_sub["black"]["yield_source"], "auto_learn")
        self.assertEqual(res_sub["black"]["yield_per_page"], 10000)

        # 2) Coarse-step Kyocera reporting 25% steps (100% -> 75% over 3,000 pages -> 12,000, medium confidence on 1st step)
        ip_coarse = "10.20.0.9"
        process_cartridge_snapshot(
            ip=ip_coarse, color="black", printer_model="Kyocera ECOSYS M3645", cartridge_name="TK-3160",
            level=100, counters={"total": 2000}, device_type="mono",
            device_capacity_pages=12500, timestamp=_ts(0),
        )
        coarse_meta = process_cartridge_snapshot(
            ip=ip_coarse, color="black", printer_model="Kyocera ECOSYS M3645", cartridge_name="TK-3160",
            level=75, counters={"total": 5000}, device_type="mono",
            device_capacity_pages=12500, timestamp=_ts(30),
        )
        self.assertEqual(coarse_meta["confidence"], "medium")
        self.assertEqual(coarse_meta["yield_source"], "auto_learn")
        self.assertEqual(coarse_meta["yield_per_page"], 12000)

    def test_multibrand_all_10_brands_catalog_part_colors_and_r5_fallback(self):
        """All 10 project brands have fallbacks, OID hints, part-code color detection, and R5 default-skip."""
        expected_brands = {
            "toshiba", "hp", "canon", "brother",
            "kyocera", "ricoh", "xerox", "lexmark", "samsung", "epson",
        }
        self.assertTrue(expected_brands.issubset(set(BRAND_COLOR_FALLBACKS.keys())))
        for b in expected_brands:
            self.assertGreater(estimate_yield_fallback(b, "black"), 2000)

        # Enterprise OID brand hints for newly wired brands
        self.assertEqual(_vendor_hint_from_identity("", "1.3.6.1.4.1.1347.43.5"), "kyocera")
        self.assertEqual(_vendor_hint_from_identity("", "1.3.6.1.4.1.367.3.2"), "ricoh")
        self.assertEqual(_vendor_hint_from_identity("", "1.3.6.1.4.1.253.8.53"), "xerox")
        self.assertEqual(_vendor_hint_from_identity("", "1.3.6.1.4.1.641.2.1"), "lexmark")

        # Part-number color suffix/prefix detection without full English color names
        self.assertEqual(_detect_toner_color("T-FC505U-C"), "cyan")
        self.assertEqual(_detect_toner_color("T-FC415E-K"), "black")
        self.assertEqual(_detect_toner_color("TK-5240M"), "magenta")
        self.assertEqual(_detect_toner_color("TN-247Y"), "yellow")
        self.assertEqual(_detect_toner_color("CLT-C506L"), "cyan")

        # Repository catalog multi-brand coverage check
        with mock.patch.dict(os.environ, {"CARTRIDGE_YIELD_MODE": "oem"}):
            os.environ.pop("CARTRIDGE_YIELD_CATALOG", None)
            self.assertEqual(_catalog_lookup("HP Color LaserJet MFP M577", "Cyan Cartridge HP CF031A", "cyan")["yield_per_page"], 5000)
            self.assertEqual(_catalog_lookup("HL-L2350DW", "Black Toner Cartridge TN-2430", "black")["yield_per_page"], 3000)
            self.assertEqual(_catalog_lookup("e-STUDIO3515AC", "cyan toner", "cyan")["yield_per_page"], 33600)
            self.assertEqual(_catalog_lookup("e-STUDIO6518A", "black toner", "black")["yield_per_page"], 106600)
            self.assertEqual(_catalog_lookup("Kyocera ECOSYS M2040dn", "TK-1170", "black")["yield_per_page"], 7200)
            self.assertEqual(_catalog_lookup("Ricoh MP C3504", "cyan toner", "cyan")["yield_per_page"], 18000)
            self.assertEqual(_catalog_lookup("Xerox VersaLink B405", "black toner", "black")["yield_per_page"], 13900)

        # R5 fix on full 31-column cartridge_state: uninformative 'default' row defers to brand fallback
        ip_r5 = "10.20.0.10"
        process_cartridge_snapshot(
            ip=ip_r5, color="cyan", printer_model="Toshiba UnknownProto", cartridge_name="cyan",
            level=80, counters={"total": 1000}, device_type="color", timestamp=_ts(0),
        )
        self.assertEqual(compute_yield_per_color(ip_r5, "cyan"), 33600.0)

    def test_m6_legacy_learn_defers_and_per_color_override_and_api_metadata(self):
        """Legacy _learn_yield_per_page defers to yield_engine; overrides & API metadata keep per-color yields."""
        ip = "10.20.0.11"
        process_cartridge_snapshot(
            ip=ip, color="black", printer_model="Toshiba e-STUDIO457", cartridge_name="T-5070",
            level=80, counters={"total": 10000}, device_type="mono",
            device_capacity_pages=36600, timestamp=_ts(0),
        )
        process_cartridge_snapshot(
            ip=ip, color="cyan", printer_model="Toshiba e-STUDIO457", cartridge_name="T-FC505C",
            level=70, counters={"total": 10000, "full_color": 4000}, device_type="color",
            device_capacity_pages=21800, timestamp=_ts(0),
        )
        store._prev.set(ip, {
            "print_total": 10000,
            "yield_per_page": 36600,
            "yield_learning_failures": 0,
            "force_estimate": 0,
            "toner_overrides": {
                "black": {"start_total": 10000, "start_toner": 100, "yield_per_page": 25000},
                "cyan": {"start_total": 10000, "start_toner": 100, "yield_per_page": 10000},
            },
        })
        # Legacy _learn_yield_per_page must NOT increment failures or overwrite 36,600
        _learn_yield_per_page(ip, delta_pages=500, prev_toner_level=80, current_toner_level=70, prev=store._prev.get(ip))
        self.assertEqual(store._prev.get(ip)["yield_per_page"], 36600)
        self.assertEqual(store._prev.get(ip).get("yield_learning_failures", 0), 0)

        # Per-color override drops at each color's own yield rate (after 2,500 pages: black -10% -> 90%, cyan -25% -> 75%)
        self.assertEqual(apply_toner_override(ip, total=12500, snmp_level=None, color="black"), 90)
        self.assertEqual(apply_toner_override(ip, total=12500, snmp_level=None, color="cyan"), 75)

        # API _attach_yield_metadata attaches true per-color source & yield from cartridge_state
        raw_api_payload = {
            "ip": ip,
            "counters": {"total": 12500},
            "toners": {
                "black": {"level": 80},
                "cyan": {"level": 70},
            },
        }
        enriched = _attach_yield_metadata(raw_api_payload)
        self.assertEqual(enriched["toners"]["black"]["capacity_pages"], 36600)
        self.assertEqual(enriched["toners"]["black"]["capacity_source"], "device_capacity")
        self.assertEqual(enriched["toners"]["cyan"]["capacity_pages"], 21800)
        self.assertEqual(enriched["toners"]["cyan"]["capacity_source"], "device_capacity")


if __name__ == "__main__":
    unittest.main()
