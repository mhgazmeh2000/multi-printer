# -*- coding: utf-8 -*-
"""Regression tests for instant startup card hydration and fast 40s dashboard pull."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import config.settings as settings
from core import database, poller, store, yield_engine
from core.db import ENV_DB_BACKEND
from models import User
from web import create_app


class StartupAndFastPullTests(unittest.TestCase):
    """Verify cold-start card hydration, batch PrevStore warmup, incremental poll_all, and slot cache exactness."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_fast_pull.db")
        self._db_patch = patch.object(settings, "DB_PATH", self.db_path)
        self._env_patch = patch.dict(
            os.environ,
            {
                ENV_DB_BACKEND: "sqlite",
                "DB_PATH": self.db_path,
            },
            clear=False,
        )
        self._db_patch.start()
        self._env_patch.start()

        database.init_db()
        yield_engine.ensure_yield_tables()
        with yield_engine._YIELD_STATUS_CACHE_LOCK:
            yield_engine._YIELD_STATUS_SLOT_CACHE.clear()

        self._orig_printers = list(store.PRINTERS)
        self._orig_printer_data = dict(store.printer_data)
        self._orig_poll_stats = dict(store.poll_stats)
        store._prev._cache.clear()
        with store.data_lock:
            store.printer_data.clear()
        store.poll_stats.update({"count": 0, "last": None, "duration_ms": 0})

    def tearDown(self):
        with store.printers_lock:
            store.PRINTERS[:] = self._orig_printers
        with store.data_lock:
            store.printer_data.clear()
            store.printer_data.update(self._orig_printer_data)
        store.poll_stats.update(self._orig_poll_stats)
        store._prev._cache.clear()
        with yield_engine._YIELD_STATUS_CACHE_LOCK:
            yield_engine._YIELD_STATUS_SLOT_CACHE.clear()
        self._env_patch.stop()
        self._db_patch.stop()
        self._tmp.cleanup()

    def test_load_printer_counters_many_and_warm_many_batch_single_query(self):
        """PrevStore.warm_many loads multiple IPs in one batch call and caches None sentinels."""
        database.save_printer_counters(
            "10.10.0.1",
            {"device_type": "mono", "print_total": 12345, "black_white": 12345, "toner_level": 72},
        )
        database.save_printer_counters(
            "10.10.0.2",
            {"device_type": "color", "print_total": 54321, "full_color": 4321, "black_white": 50000, "toner_level": 88},
        )
        store._prev._cache.clear()

        with patch("core.store.load_printer_counters_many", wraps=database.load_printer_counters_many) as spy_many, \
             patch("core.store.load_printer_counters", wraps=database.load_printer_counters) as spy_single:
            store._prev.warm_many(["10.10.0.1", "10.10.0.2", "10.10.0.99"])
            self.assertEqual(spy_many.call_count, 1)
            self.assertEqual(spy_single.call_count, 0)

            # Subsequent gets hit cache (including missing 10.10.0.99 sentinel) without touching DB
            c1 = store._prev.get("10.10.0.1")
            c2 = store._prev.get("10.10.0.2")
            c_missing = store._prev.get("10.10.0.99")
            self.assertEqual(spy_single.call_count, 0)

        self.assertEqual(c1["print_total"], 12345)
        self.assertEqual(c2["full_color"], 4321)
        self.assertIsNone(c_missing)

    def test_api_printers_hydrates_unpolled_startup_cards_while_keeping_online_none(self):
        """Cold-start /api/printers hydrates cached counters/toners/model without mutating store.printer_data."""
        ip_cached = "10.20.0.10"
        ip_new = "10.20.0.11"
        with store.printers_lock:
            store.PRINTERS[:] = [
                {"ip": ip_cached, "name": "Office Toshiba", "device_type": "color", "brand": "toshiba", "group": "imamat"},
                {"ip": ip_new, "name": "Brand New Printer", "device_type": "mono", "brand": "hp", "group": "soroush"},
            ]

        database.save_printer_counters(
            ip_cached,
            {
                "device_type": "color",
                "print_total": 40500,
                "full_color": 10500,
                "black_white": 30000,
                "toner_level": 64,
                "cartridge_ids": {"black": "SN-BLK-9001"},
                "cartridge_state": {"black": {"signal_quality": "genuine"}},
            },
        )
        yield_engine.process_printer_yield_snapshot(
            ip=ip_cached,
            printer_model="TOSHIBA e-STUDIO2515AC",
            counters={"total": 40500, "full_color": 10500},
            toners={
                "black": {"name": "T-FC415E-K", "level": 64, "raw_level": 64},
                "cyan": {"name": "T-FC415E-C", "level": 81, "raw_level": 81},
            },
            device_type="color",
        )
        store._prev._cache.clear()

        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        admin = User.create(
            username="fastpull_admin",
            email="fastpull@test.local",
            password="StrongPassword!123",
            role="admin",
            is_verified=True,
        )

        with patch("core.store.load_printer_counters", wraps=database.load_printer_counters) as spy_single:
            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["_user_id"] = str(admin.id)
                    sess["_fresh"] = True
                res = client.get("/api/printers")
                self.assertEqual(res.status_code, 200)
                payload = res.get_json()
            # Cold cache must warm via batch query, zero per-printer load_printer_counters calls
            self.assertEqual(spy_single.call_count, 0)

        by_ip = {p["ip"]: p for p in payload["printers"]}
        cached_card = by_ip[ip_cached]
        new_card = by_ip[ip_new]

        # Critical invariant: online MUST remain None until a real SNMP poll completes
        self.assertIsNone(cached_card["online"])
        self.assertTrue(cached_card.get("pending_initial_poll"))
        self.assertEqual(cached_card["counters"]["total"], 40500)
        self.assertEqual(cached_card["counters"]["full_color"], 10500)
        self.assertEqual(cached_card["toners"]["black"]["level"], 64)
        self.assertEqual(cached_card["toners"]["cyan"]["level"], 81)
        self.assertEqual(cached_card["device"]["model"], "TOSHIBA e-STUDIO2515AC")
        self.assertEqual(cached_card["cartridge_ids"]["black"], "SN-BLK-9001")
        self.assertEqual(cached_card["cartridge_signal_quality"]["black"], "genuine")

        # Meta online/offline counts must both be 0 while online is None
        self.assertEqual(payload["meta"]["online"], 0)
        self.assertEqual(payload["meta"]["offline"], 0)

        # Brand-new printer still renders cleanly with online: None
        self.assertIsNone(new_card["online"])

        # Critical invariant: in-memory store.printer_data was NOT pre-populated with fake SNMP state
        with store.data_lock:
            self.assertNotIn(ip_cached, store.printer_data)
            self.assertNotIn(ip_new, store.printer_data)

    def test_poll_all_publishes_each_printer_incrementally_before_slow_peer_finishes(self):
        """Fast printers appear in store.printer_data immediately without waiting for slow/offline peers."""
        fast_ip = "10.30.0.1"
        slow_ip = "10.30.0.2"
        with store.printers_lock:
            store.PRINTERS[:] = [
                {"ip": fast_ip, "name": "Fast Printer", "device_type": "mono"},
                {"ip": slow_ip, "name": "Slow Printer", "device_type": "mono"},
            ]

        fast_published = threading.Event()
        release_slow = threading.Event()

        def fake_collect(p):
            ip = p["ip"]
            if ip == fast_ip:
                return {"ip": ip, "name": p["name"], "online": True, "counters": {"total": 100}, "toners": {}}
            # Wait until observer confirms fast_ip is already live in store.printer_data
            release_slow.wait(timeout=5.0)
            return {"ip": ip, "name": p["name"], "online": False, "error": "SNMP timeout"}

        observed_mid_cycle = {}

        def run_poll():
            with patch.object(poller, "collect", side_effect=fake_collect):
                poller.poll_all()

        worker = threading.Thread(target=run_poll)
        worker.start()
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                with store.data_lock:
                    if fast_ip in store.printer_data:
                        observed_mid_cycle = dict(store.printer_data)
                        fast_published.set()
                        break
                time.sleep(0.01)
            self.assertTrue(fast_published.is_set(), "fast_ip was not published incrementally during poll_all()")
            self.assertTrue(observed_mid_cycle[fast_ip]["online"])
            self.assertNotIn(slow_ip, observed_mid_cycle)
        finally:
            release_slow.set()
            worker.join(timeout=5.0)

        with store.data_lock:
            self.assertIn(fast_ip, store.printer_data)
            self.assertIn(slow_ip, store.printer_data)
            self.assertFalse(store.printer_data[slow_ip]["online"])
        self.assertEqual(store.poll_stats["count"], 1)

    def test_get_yield_status_windowed_query_and_exact_cache_invalidation(self):
        """get_yield_status bounds snapshots to 400 per slot, caches unchanged slots, and invalidates immediately on mutation."""
        ip = "10.40.0.5"
        yield_engine.process_printer_yield_snapshot(
            ip=ip,
            printer_model="HP LaserJet M404dn",
            counters={"total": 1000},
            toners={"black": {"name": "CF259A", "level": 90, "raw_level": 90}},
            device_type="mono",
        )
        # Insert 450 historical snapshots to exercise the ROW_NUMBER() <= 400 window query
        with database.db_connection(commit=True) as conn:
            for idx in range(450):
                conn.execute(
                    """
                    INSERT INTO toner_snapshots_v2
                    (printer_ip, color, usage_counter, toner_level, raw_level, source, valid, timestamp, cartridge_key, print_total)
                    VALUES (?, 'black', ?, ?, ?, 'poll', 1, ?, 'hp laserjet m404dn|cf259a|black', ?)
                    """,
                    (
                        ip,
                        1000 + idx * 5,
                        max(10, 90 - (idx // 6)),
                        max(10, 90 - (idx // 6)),
                        f"2026-10-01T00:{idx // 60:02d}:{idx % 60:02d}Z",
                        1000 + idx * 5,
                    ),
                )

        with yield_engine._YIELD_STATUS_CACHE_LOCK:
            yield_engine._YIELD_STATUS_SLOT_CACHE.clear()

        with patch.object(
            yield_engine,
            "estimate_cartridge_interval",
            wraps=yield_engine.estimate_cartridge_interval,
        ) as spy_est:
            first = yield_engine.get_yield_status(ip=ip)
            self.assertEqual(len(first), 1)
            self.assertEqual(spy_est.call_count, 1)
            # Verify windowed query passed at most 400 snapshot rows
            passed_snaps = spy_est.call_args.kwargs["snap_rows"]
            self.assertEqual(len(passed_snaps), 400)
            # Ascending order preserved (oldest to newest within the 400-row tail)
            self.assertLess(passed_snaps[0][0], passed_snaps[-1][0])

            # Second call with identical DB state hits exact slot cache (0 estimator calls)
            second = yield_engine.get_yield_status(ip=ip)
            self.assertEqual(spy_est.call_count, 1)
            self.assertEqual(first, second)

            # Mutating returned dict does not corrupt cached entry
            second[0]["current_level"] = 1
            second[0]["capacity_ci95"][0] = 999999
            third = yield_engine.get_yield_status(ip=ip)
            self.assertEqual(third[0]["current_level"], first[0]["current_level"])
            self.assertEqual(third[0]["capacity_ci95"], first[0]["capacity_ci95"])

            # Immediate mutation (manual cartridge reset in the exact same second) invalidates cache key automatically
            yield_engine.register_manual_refill(
                ip=ip,
                color="black",
                printer_model="HP LaserJet M404dn",
                cartridge_name="CF259A",
                new_level=100,
                counters={"total": 3500},
                device_type="mono",
                replacement_reason="empty",
            )
            after_reset = yield_engine.get_yield_status(ip=ip)
            self.assertEqual(spy_est.call_count, 2)
            self.assertEqual(after_reset[0]["current_level"], 100)
            self.assertEqual(after_reset[0]["last_reset_reason"], "empty")

    def test_dashboard_js_decoupled_logs_and_lazy_panels_contract(self):
        """Verify dashboard.js preserves decoupled log loading, lazy detail panels, and immediate #cfill snap."""
        js_path = Path(__file__).resolve().parents[1] / "web" / "static" / "js" / "dashboard.js"
        src = js_path.read_text(encoding="utf-8")
        self.assertIn("async function fetchLogsInBackground()", src)
        self.assertIn("function renderSinglePrinterPanel(p)", src)
        self.assertIn("_scheduleStartupWarmupIfNeeded", src)
        self.assertIn("cfill.style.transition = 'none'", src)


if __name__ == "__main__":
    unittest.main()
