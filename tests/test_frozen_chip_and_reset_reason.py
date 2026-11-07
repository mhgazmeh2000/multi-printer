# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_frozen_chip_and_reset_reason.py
# @brief:        End-to-end tests for frozen-chip detection, chip quarantine, overrun alerts,
#                and cartridge reset replacement reasons ('empty' vs 'dirty').
# =============================================================================

"""Tests for the 4-part chip-less / frozen-chip workflow and 'empty' vs 'dirty' reset reasons."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import config.settings as settings
import core.database as core_database
from core import store
from core.collectors.base import evaluate_override_quarantine
from core.database import db_connection, init_db
from core.yield_engine import (
    ensure_yield_tables,
    get_yield_status,
    process_cartridge_snapshot,
    process_printer_yield_snapshot,
    register_manual_refill,
)
from models import User
from web import create_app
from web.routes.printers import _attach_yield_metadata

REPO_ROOT = Path(__file__).resolve().parents[1]


def _ts(minute: int) -> str:
    base = datetime(2026, 11, 7, 9, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class FrozenChipAndResetReasonTests(unittest.TestCase):
    """Covers frozen-chip detection, quarantine, overrun alerts, and empty/dirty reset reasons."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "frozen_chip_test.db")
        self.catalog_path = os.path.join(self.tmpdir.name, "test_catalog.json")
        catalog_data = {
            "version": 1,
            "entries": [
                {
                    "id": "canon-057",
                    "match": {
                        "model_contains": ["canon lbp226"],
                        "cartridge_contains": ["057"],
                        "color": "black",
                    },
                    "yield_per_page": 3100,
                    "confidence": "medium",
                },
                {
                    "id": "hp-cf259a",
                    "match": {
                        "model_contains": ["m404"],
                        "cartridge_contains": ["cf259a"],
                        "color": "black",
                    },
                    "yield_per_page": 3000,
                    "confidence": "medium",
                },
                {
                    "id": "brother-tn2320",
                    "match": {
                        "model_contains": ["hl-l2360"],
                        "cartridge_contains": ["tn-2320"],
                        "color": "black",
                    },
                    "yield_per_page": 2600,
                    "confidence": "medium",
                },
            ],
        }
        with open(self.catalog_path, "w", encoding="utf-8") as fh:
            json.dump(catalog_data, fh)

        self.db_patch = mock.patch.object(settings, "DB_PATH", self.db_path)
        self.missing_patch = mock.patch.object(
            core_database,
            "MISSING_YIELD_FILE",
            os.path.join(self.tmpdir.name, "missing_yield_printers.txt"),
        )
        env_vars = {
            "CARTRIDGE_YIELD_CATALOG": self.catalog_path,
            "CARTRIDGE_YIELD_FACTOR": "1",
            "CARTRIDGE_YIELD_MODE": "oem",
            "YIELD_ESTIMATOR": "v1",
        }
        if "printguard_test_" not in os.environ.get("PG_DSN", ""):
            env_vars["DB_BACKEND"] = "sqlite"
        self.env_patch = mock.patch.dict(os.environ, env_vars)
        self.db_patch.start()
        self.missing_patch.start()
        self.env_patch.start()
        init_db()
        ensure_yield_tables()
        store._prev._cache.clear()
        with store.data_lock:
            store.printer_data.clear()

    def tearDown(self):
        with store.data_lock:
            store.printer_data.clear()
        store._prev._cache.clear()
        self.env_patch.stop()
        self.missing_patch.stop()
        self.db_patch.stop()
        self.tmpdir.cleanup()

    def test_1_frozen_chip_auto_detected_and_self_heals_if_chip_moves(self):
        """A chip stuck at 90% over >=20% of capacity switches to dead-reckoning, yet self-heals if it drops."""
        ip = "10.30.0.1"
        # Initial poll: chip reports 90% at page 1,000 (HP CF259A catalog = 3,000 -> freeze threshold = 600 pages)
        m0 = process_cartridge_snapshot(
            ip=ip, color="black", printer_model="HP LaserJet Pro M404", cartridge_name="CF259A",
            level=90, raw_level=90, counters={"total": 1000}, device_type="mono", timestamp=_ts(0),
        )
        self.assertEqual(m0["sensor_state"], "healthy")
        self.assertEqual(m0["current_level"], 90)

        # 750 pages later (25% of 3,000 capacity) chip is STILL stuck at 90% -> auto frozen-chip!
        m1 = process_cartridge_snapshot(
            ip=ip, color="black", printer_model="HP LaserJet Pro M404", cartridge_name="CF259A",
            level=90, raw_level=90, counters={"total": 1750}, device_type="mono", timestamp=_ts(30),
        )
        self.assertEqual(m1["sensor_state"], "frozen_chip")
        self.assertEqual(m1["frozen_chip_level"], 90)
        # Dead-reckoned drop = 750 / 3000 * 100 = 25% -> 90 - 25 = 65%
        self.assertEqual(m1["current_level"], 65)

        # Suppose it was actually ultra-low coverage printing and at page 1,850 the real chip drops 90% -> 89%
        m2 = process_cartridge_snapshot(
            ip=ip, color="black", printer_model="HP LaserJet Pro M404", cartridge_name="CF259A",
            level=89, raw_level=89, counters={"total": 1850}, device_type="mono", timestamp=_ts(45),
        )
        self.assertEqual(m2["sensor_state"], "healthy")
        self.assertEqual(m2["cycle_status"], "active")
        self.assertEqual(m2["current_level"], 89)

    def test_2_chip_quarantine_blocks_90pct_false_drop_even_across_virtual_90_crossing(self):
        """After manual reset to 100% without a new chip, raw_level=90% never creates a fake 100->90 sample."""
        ip = "10.30.0.2"
        register_manual_refill(
            ip=ip, color="black", printer_model="Canon LBP226", cartridge_name="057",
            new_level=100, counters={"total": 5000}, device_type="mono",
            timestamp=_ts(0), replacement_reason="empty", quarantine_chip=True,
        )
        ov_entry = {
            "start_total": 5000,
            "start_toner": 100,
            "yield_per_page": 3100,
            "mode": "manual_reset",
            "reset_reason": "empty",
            "chip_level_at_reset": 90,
            "chip_aligned_at_reset": False,
            "chip_quarantined": True,
        }

        # Poll across 50 -> 310 (virtual=90%!) -> 1,550 (virtual=50%) pages while chip stays stuck at 90%
        for idx, tot in enumerate((5050, 5310, 5620, 6550), start=1):
            ev = evaluate_override_quarantine(ov_entry, tot, snmp_level=90, swapped=False, fallback_yield=3100)
            self.assertFalse(ev["release"], f"Quarantine must NOT auto-release at total={tot} when chip was stuck at 90%")
            self.assertTrue(ev["chip_quarantined"])
            ov_entry = ev["updated_entry"]
            res = process_printer_yield_snapshot(
                ip=ip,
                printer_model="Canon LBP226",
                counters={"total": tot},
                toners={"black": {"name": "057", "level": ev["virtual_level"], "raw_level": 90}},
                device_type="mono",
                timestamp=_ts(idx * 10),
                quarantined_colors={"black"},
                virtual_levels={"black": ev["virtual_level"]},
            )
            self.assertEqual(res["black"]["anchor_level"], 100)
            self.assertEqual(res["black"]["sensor_state"], "chip_quarantined")

        with db_connection() as conn:
            accepted_cnt = conn.execute(
                "SELECT COUNT(*) FROM yield_samples WHERE printer_ip = ? AND accepted = ?",
                (ip, True),
            ).fetchone()[0]
        self.assertEqual(accepted_cnt, 0)

    def test_3_working_chip_auto_releases_from_quarantine_after_real_step_down(self):
        """If a reset/new chip starts at 100% and genuinely steps down to 98% after 65 pages, quarantine releases."""
        ov_entry = {
            "start_total": 2000,
            "start_toner": 100,
            "yield_per_page": 3000,
            "mode": "manual_reset",
            "reset_reason": "empty",
            "chip_level_at_reset": 100,
            "chip_aligned_at_reset": True,
            "chip_quarantined": True,
        }
        ev_initial = evaluate_override_quarantine(ov_entry, 2010, snmp_level=100, swapped=False, fallback_yield=3000)
        self.assertFalse(ev_initial["release"])

        ev_step = evaluate_override_quarantine(
            ev_initial["updated_entry"], 2065, snmp_level=98, swapped=False, fallback_yield=3000
        )
        self.assertTrue(ev_step["release"])
        self.assertFalse(ev_step["chip_quarantined"])

    def test_4_empty_reason_learns_exact_refill_cycle_on_stuck_90pct_chip_v1_and_v2(self):
        """Confirming reason='empty' after 2,100 pages with a chip stuck at 90% learns 2,100 in both v1 and v2."""
        ip = "10.30.0.4"
        # Start cycle 1 at 100% (chip stuck at 90%)
        register_manual_refill(
            ip=ip, color="black", printer_model="Canon LBP226", cartridge_name="057",
            new_level=100, counters={"total": 10000}, device_type="mono",
            timestamp=_ts(0), replacement_reason="empty", quarantine_chip=True,
        )
        # Intermediate quarantined polls with chip stuck at 90%
        for minute, tot, virt in ((20, 10700, 77), (40, 11400, 55), (60, 12100, 32)):
            process_cartridge_snapshot(
                ip=ip, color="black", printer_model="Canon LBP226", cartridge_name="057",
                level=90, raw_level=90, counters={"total": tot}, device_type="mono",
                timestamp=_ts(minute), chip_quarantined=True, virtual_display_level=virt,
            )

        # Physical toner runs empty at 12,100 (2,100 pages printed, inside CYCLE_RATED_BAND of 3,100: [930, 7750])
        refill_meta = register_manual_refill(
            ip=ip, color="black", printer_model="Canon LBP226", cartridge_name="057",
            new_level=100, counters={"total": 12100}, device_type="mono",
            timestamp=_ts(80), replacement_reason="empty", quarantine_chip=True,
        )
        self.assertEqual(refill_meta["yield_source"], "cycle_learn")
        self.assertEqual(refill_meta["yield_per_page"], 2100)
        self.assertEqual(refill_meta["last_reset_reason"], "empty")

        # Next quarantined poll must NOT snap 2,100 back to 3,100 catalog!
        next_poll = process_cartridge_snapshot(
            ip=ip, color="black", printer_model="Canon LBP226", cartridge_name="057",
            level=90, raw_level=90, counters={"total": 12200}, device_type="mono",
            timestamp=_ts(90), chip_quarantined=True, virtual_display_level=95,
        )
        self.assertEqual(next_poll["yield_per_page"], 2100)

        # Verify Layer 1-5 v2 interval estimator also sees the verified 100% -> 0% 2,100-page interval
        st_rows = get_yield_status(ip=ip)
        self.assertEqual(len(st_rows), 1)
        self.assertEqual(st_rows[0]["last_reset_reason"], "empty")
        # Empirical Bayes shrinkage between catalog prior (3,100) and observed cycle (2,100) pulls < 3,000
        self.assertLess(st_rows[0]["interval_capacity_pages"], 2900)
        self.assertGreater(st_rows[0]["interval_capacity_pages"], 1900)

    def test_5_dirty_reason_on_canon_hp_brother_never_corrupts_yield_downward_and_ratchets_underestimates(self):
        """Early swap for dirty prints ('dirty') protects Canon/HP/Brother capacity and ratchets up underestimates."""
        # Case A: Brother HL-L2360DN (TN-2320 = 2,600) swapped dirty at 950 pages (sensorless / quarantined)
        ip_bro = "10.30.0.5"
        register_manual_refill(
            ip=ip_bro, color="black", printer_model="Brother HL-L2360DN", cartridge_name="TN-2320",
            new_level=100, counters={"total": 1000}, device_type="mono",
            timestamp=_ts(0), replacement_reason="empty", quarantine_chip=True,
        )
        dirty_bro = register_manual_refill(
            ip=ip_bro, color="black", printer_model="Brother HL-L2360DN", cartridge_name="TN-2320",
            new_level=100, counters={"total": 1950}, device_type="mono",
            timestamp=_ts(30), replacement_reason="dirty", quarantine_chip=True,
        )
        # Without reason='dirty', 950 >= 0.30 * 2600 (780) would have closed a 950-page full cycle!
        # With reason='dirty', 2,600 is 100% preserved!
        self.assertEqual(dirty_bro["yield_per_page"], 2600)
        self.assertEqual(dirty_bro["last_reset_reason"], "dirty")

        # Case B: Uncataloged printer with underestimated default 2,000 prints 3,200 pages and STILL isn't empty
        # when swapped due to dirty drum -> right-censored lower bound ratchets yield UP above 2,000!
        ip_ratchet = "10.30.0.6"
        register_manual_refill(
            ip=ip_ratchet, color="black", printer_model="Canon i-SENSYS Custom", cartridge_name="CRG-CUSTOM",
            new_level=100, counters={"total": 500}, device_type="mono",
            timestamp=_ts(0), replacement_reason="empty", quarantine_chip=True,
        )
        ratchet_meta = register_manual_refill(
            ip=ip_ratchet, color="black", printer_model="Canon i-SENSYS Custom", cartridge_name="CRG-CUSTOM",
            new_level=100, counters={"total": 3700}, device_type="mono",
            timestamp=_ts(60), replacement_reason="dirty", quarantine_chip=True,
        )
        self.assertGreater(ratchet_meta["yield_per_page"], 2500)
        self.assertEqual(ratchet_meta["yield_source"], "cycle_learn")

        # Case C: HP LaserJet Pro M404 with a healthy non-quarantined numeric chip (100% -> 50% over 1,600 pages)
        # swapped early because prints got streaky -> partial sensor slope learns ~3,200 pages cleanly
        ip_hp = "10.30.0.7"
        process_cartridge_snapshot(
            ip=ip_hp, color="black", printer_model="HP LaserJet Pro M404", cartridge_name="CF259A",
            level=100, raw_level=100, counters={"total": 4000}, device_type="mono", timestamp=_ts(0),
        )
        process_cartridge_snapshot(
            ip=ip_hp, color="black", printer_model="HP LaserJet Pro M404", cartridge_name="CF259A",
            level=50, raw_level=50, counters={"total": 5600}, device_type="mono", timestamp=_ts(25),
        )
        hp_dirty = register_manual_refill(
            ip=ip_hp, color="black", printer_model="HP LaserJet Pro M404", cartridge_name="CF259A",
            new_level=100, counters={"total": 5600}, device_type="mono",
            timestamp=_ts(30), replacement_reason="dirty", quarantine_chip=False,
        )
        self.assertEqual(hp_dirty["yield_per_page"], 3200)

    def test_6_overrun_alert_flags_suspected_unregistered_refill(self):
        """Printing >=15% past 0% dead-reckoned level with a stuck 90% chip triggers overrun_alert."""
        ip = "10.30.0.8"
        register_manual_refill(
            ip=ip, color="black", printer_model="HP LaserJet Pro M404", cartridge_name="CF259A",
            new_level=100, counters={"total": 1000}, device_type="mono",
            timestamp=_ts(0), replacement_reason="empty", quarantine_chip=True,
        )
        # Expected empty at 1000 + 3000 = 4000. At 4650 (650 overrun pages >= 450 threshold), overrun_alert fires!
        m_over = process_cartridge_snapshot(
            ip=ip, color="black", printer_model="HP LaserJet Pro M404", cartridge_name="CF259A",
            level=90, raw_level=90, counters={"total": 4650}, device_type="mono",
            timestamp=_ts(90), chip_quarantined=True, virtual_display_level=0,
        )
        self.assertEqual(m_over["sensor_state"], "overrun_alert")
        self.assertTrue(m_over["unregistered_refill_suspected"])
        self.assertEqual(m_over["overrun_pages"], 650)
        self.assertEqual(m_over["frozen_chip_level"], 90)

        enriched = _attach_yield_metadata({
            "ip": ip,
            "counters": {"total": 4650},
            "toners": {"black": {"name": "CF259A", "level": 0, "raw_level": 90}},
        })
        blk = enriched["toners"]["black"]
        self.assertEqual(blk["sensor_state"], "overrun_alert")
        self.assertTrue(blk["unregistered_refill_suspected"])
        self.assertEqual(blk["overrun_pages"], 650)

    def test_7_toner_reset_api_route_and_ui_modal_support_empty_and_dirty_reasons(self):
        """POST /api/printer/<ip>/toner_reset validates reason ('empty'|'dirty'), quarantines chip, and UI exposes it."""
        app = create_app()
        app.config["WTF_CSRF_ENABLED"] = False
        client = app.test_client()
        admin = User.create(
            username="reset_admin",
            email="reset_admin@test.local",
            password="StrongPassword!123",
            role="admin",
            is_verified=True,
        )
        with client.session_transaction() as sess:
            sess["_user_id"] = str(admin.id)
            sess["_fresh"] = True

        ip = "10.30.0.9"
        with store.data_lock:
            store.printer_data[ip] = {
                "ip": ip,
                "name": "Canon Office",
                "device_type": "mono",
                "device": {"model": "Canon LBP226"},
                "counters": {"total": 2000, "black_white": 2000},
                "toners": {"black": {"name": "057", "level": 90, "raw_level": 90, "status": "ok"}},
                "alerts": [],
            }

        # 1. Invalid reason -> 400
        r_bad = client.post(f"/api/printer/{ip}/toner_reset", json={"color": "black", "new_level": 100, "reason": "invalid_xyz"})
        self.assertEqual(r_bad.status_code, 400)

        # 2. Initial reset with reason='empty' at total=2000
        r_ok1 = client.post(f"/api/printer/{ip}/toner_reset", json={"color": "black", "new_level": 100, "reason": "empty"})
        self.assertEqual(r_ok1.status_code, 200)
        body1 = r_ok1.get_json()
        self.assertEqual(body1["status"], "ok")
        self.assertEqual(body1["reason"], "empty")
        self.assertTrue(body1["chip_quarantined"])

        # Advance total by 800 pages (dirty print swap) -> must preserve 3,100 catalog yield!
        with store.data_lock:
            store.printer_data[ip]["counters"]["total"] = 2800
            store.printer_data[ip]["counters"]["black_white"] = 2800
            store.printer_data[ip]["toners"]["black"]["raw_level"] = 90

        r_dirty = client.post(f"/api/printer/{ip}/toner_reset", json={"color": "black", "new_level": 100, "reason": "dirty"})
        self.assertEqual(r_dirty.status_code, 200)
        body_dirty = r_dirty.get_json()
        self.assertEqual(body_dirty["reason"], "dirty")
        self.assertEqual(body_dirty["yield_per_page"], 3100)

        prev_saved = store._prev.get(ip) or {}
        ov_blk = (prev_saved.get("toner_overrides") or {}).get("black") or {}
        self.assertEqual(ov_blk.get("reset_reason"), "dirty")
        self.assertEqual(ov_blk.get("chip_level_at_reset"), 90)
        self.assertFalse(ov_blk.get("chip_aligned_at_reset"))
        self.assertTrue(ov_blk.get("chip_quarantined"))

        # 3. Verify dashboard HTML & JS include the replacement reason selector
        html_txt = (REPO_ROOT / "web" / "templates" / "dashboard.html").read_text(encoding="utf-8")
        js_txt = (REPO_ROOT / "web" / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn('id="toner-reset-reason"', html_txt)
        self.assertIn('value="empty"', html_txt)
        self.assertIn('value="dirty"', html_txt)
        self.assertIn("JSON.stringify({ color, new_level, reason })", js_txt)


if __name__ == "__main__":
    unittest.main()
