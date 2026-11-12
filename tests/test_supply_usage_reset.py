# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_supply_usage_reset.py
# @brief:        End-to-end checks for system-counted cartridge usage: poll tracking, manual
#                cartridge reset, replacement by chip identity, and persistence.
# @notes:        Temporary SQLite database, same setup style as test_frozen_chip_and_reset_reason.py.
# =============================================================================

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import config.settings as settings
import core.database as core_database
from core import store
from core.database import init_db, load_printer_counters
from core.enhanced_collector import _track_supply_usage
from core.yield_engine import ensure_yield_tables
from models import User
from web import create_app
from web.routes.printers import _attach_yield_metadata


def _today():
    return datetime.now(timezone.utc).date().isoformat()


class SupplyUsageEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "supply_usage.db")
        catalog_path = os.path.join(self.tmpdir.name, "catalog.json")
        with open(catalog_path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "entries": []}, fh)
        self.patches = [
            mock.patch.object(settings, "DB_PATH", self.db_path),
            mock.patch.object(core_database, "MISSING_YIELD_FILE",
                              os.path.join(self.tmpdir.name, "missing.txt")),
            mock.patch.dict(os.environ, {
                "CARTRIDGE_YIELD_CATALOG": catalog_path,
                "CARTRIDGE_YIELD_FACTOR": "1",
                "CARTRIDGE_YIELD_MODE": "oem",
                "YIELD_ESTIMATOR": "v1",
                "DB_BACKEND": "sqlite",
            }),
        ]
        for p in self.patches:
            p.start()
        init_db()
        ensure_yield_tables()
        store._prev._cache.clear()
        with store.data_lock:
            store.printer_data.clear()

    def tearDown(self):
        with store.data_lock:
            store.printer_data.clear()
        store._prev._cache.clear()
        for p in reversed(self.patches):
            p.stop()
        self.tmpdir.cleanup()

    def _poll(self, ip, total, cartridge_id="CHIP-A"):
        """One poll: the collector's tracking step with the validated counters of that poll."""
        counters = {"total": total, "full_color": None, "black_white": total}
        toners = {"black": {"name": "HP CF259A", "level": 60}}
        _track_supply_usage(ip, store._prev.get(ip) or {}, toners, counters,
                            {"black": cartridge_id}, {"black": "chip_id"})

    def _view(self, ip):
        with store.data_lock:
            raw = store.printer_data[ip]
            copy = dict(raw)
            copy["toners"] = {k: dict(v) for k, v in raw["toners"].items()}
            copy["counters"] = dict(raw["counters"])
        return _attach_yield_metadata(copy)["toners"]["black"]

    def test_poll_tracking_reset_and_persistence(self):
        ip = "10.40.0.7"
        with store.data_lock:
            store.printer_data[ip] = {
                "ip": ip, "name": "HP Office", "device_type": "mono",
                "device": {"model": "HP LaserJet"},
                "counters": {"total": 1000, "black_white": 1000},
                "toners": {"black": {"name": "HP CF259A", "level": 60, "status": "ok"}},
                "alerts": [],
            }
        app = create_app()
        app.config["WTF_CSRF_ENABLED"] = False
        client = app.test_client()
        admin = User.create(username="usage_admin", email="usage_admin@test.local",
                            password="StrongPassword!123", role="admin", is_verified=True)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(admin.id)
            sess["_fresh"] = True

        # First poll: the system starts counting from the current counter.
        self._poll(ip, 1000)
        view = self._view(ip)
        self.assertEqual(view["supply_pages"], 0)
        self.assertEqual(view["first_install_date"], _today())
        self.assertIsNone(view["last_use_date"])

        # Printing 120 pages: pages since install, last use = today.
        self._poll(ip, 1120)
        view = self._view(ip)
        self.assertEqual(view["supply_pages"], 120)
        self.assertEqual(view["last_use_date"], _today())

        # Manual cartridge reset: counting restarts from the current counter.
        with store.data_lock:
            store.printer_data[ip]["counters"]["total"] = 1120
            store.printer_data[ip]["counters"]["black_white"] = 1120
        r = client.post(f"/api/printer/{ip}/toner_reset",
                        json={"color": "black", "new_level": 100, "reason": "empty"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        view = self._view(ip)
        self.assertEqual(view["supply_pages"], 0)
        self.assertIsNone(view["last_use_date"])

        self._poll(ip, 1170)
        self.assertEqual(self._view(ip)["supply_pages"], 50)

        # The record survives a restart (cache cleared, reloaded from the database).
        store._prev._cache.clear()
        loaded = load_printer_counters(ip) or {}
        self.assertEqual(loaded.get("supply_usage", {}).get("black", {}).get("start_pages"), 1120)

        # Chip identity changes: the system detects a replaced cartridge and restarts counting.
        _track_supply_usage(ip, store._prev.get(ip) or {}, {"black": {}},
                            {"total": 1200, "full_color": None, "black_white": 1200},
                            {"black": "CHIP-B"}, {"black": "chip_id"})
        rec = (store._prev.get(ip) or {}).get("supply_usage", {}).get("black", {})
        self.assertEqual(rec.get("start_pages"), 1200)
        self.assertEqual(rec.get("cartridge_id"), "CHIP-B")

    def test_fallback_identity_does_not_restart_counting(self):
        ip = "10.40.0.8"
        self._poll(ip, 500, cartridge_id="CHIP-A")
        _track_supply_usage(ip, store._prev.get(ip) or {}, {"black": {}},
                            {"total": 560, "full_color": None, "black_white": 560},
                            {"black": "model:CF259A"}, {"black": "chip_id"})
        rec = (store._prev.get(ip) or {}).get("supply_usage", {}).get("black", {})
        self.assertEqual(rec.get("start_pages"), 500)
        self.assertEqual(rec.get("cartridge_id"), "CHIP-A")

    def test_missing_counters_do_not_start_tracking_or_touch_other_supplies(self):
        ip = '192.0.2.80'
        _track_supply_usage(ip, {}, {'black': {}, 'drum': {}}, {'total': None}, {}, {})
        self.assertFalse((store._prev.get(ip) or {}).get('supply_usage'))

    def test_colour_usage_does_not_advance_with_monochrome_only_pages(self):
        ip = '192.0.2.81'
        toners = {colour: {} for colour in ('black', 'cyan', 'magenta', 'yellow')}
        def poll(total, full_color, ids=None, types=None):
            _track_supply_usage(ip, store._prev.get(ip) or {}, toners,
                                {'total': total, 'full_color': full_color}, ids or {}, types or {})
        poll(1000, 200)
        poll(1050, 200)
        from core.supply_usage import usage_view
        usage = store._prev.get(ip)['supply_usage']
        self.assertEqual(usage_view(usage['black'])['pages'], 50)
        for colour in ('cyan', 'magenta', 'yellow'):
            self.assertEqual(usage_view(usage[colour])['pages'], 0)
            self.assertIsNone(usage_view(usage[colour])['last_use_date'])
        poll(1080, 220, {'black': 'MODEL:changed'}, {'black': 'chip_id'})
        usage = store._prev.get(ip)['supply_usage']
        self.assertEqual(usage_view(usage['black'])['pages'], 80)
        self.assertEqual(usage_view(usage['cyan'])['pages'], 20)
        store._prev._cache.clear()
        reloaded = core_database.load_printer_counters_many([ip, 'missing'])[ip]
        self.assertEqual(reloaded['supply_usage'], usage)

    def test_stale_poll_state_cannot_overwrite_newer_reset_record(self):
        from core.supply_usage import new_record, usage_view
        ip = '192.0.2.82'
        self._poll(ip, 1000)
        stale = store._prev.get(ip)
        current = {'black': new_record(1100, 'CHIP-A', '2026-11-02'),
                   'cyan': new_record(200, None, '2026-11-01')}
        store._prev.set(ip, {'supply_usage': current})
        _track_supply_usage(ip, stale, {'black': {}}, {'total': 1150},
                            {'black': 'CHIP-A'}, {'black': 'chip_id'})
        result = store._prev.get(ip)['supply_usage']
        self.assertEqual(usage_view(result['black'])['pages'], 50)
        self.assertEqual(result['black']['installed_on'], '2026-11-02')
        self.assertEqual(result['cyan'], current['cyan'])

    def test_real_collector_masks_rejected_total_for_every_supply(self):
        import copy
        from tests.golden_harness import FIXTURE_DIR, replay_enhanced
        from core.supply_usage import usage_view
        fixture = json.loads((FIXTURE_DIR / 'enhanced_01_hp_color_toner_full.json').read_text())
        ip = fixture['input']['printer']['ip']
        phase = fixture['input']['phases'][0]
        phases = []
        for total, fc in ((84210, 21480), (0, 21500)):
            current = copy.deepcopy(phase)
            oids = current['devices'][ip]['oids']
            oids['1.3.6.1.2.1.43.10.2.1.4.1.1'] = total
            oids['1.3.6.1.2.1.43.10.2.1.4.1.2'] = fc
            phases.append(current)
        fixture['input']['phases'] = phases
        fixture['expected']['steps'] = [{}, {}]
        replay_enhanced(fixture)
        state = store._prev.get(ip)
        self.assertEqual(state['print_total'], 84210)
        for color in ('black', 'cyan', 'magenta', 'yellow'):
            self.assertEqual(usage_view(state['supply_usage'][color])['pages'], 0)
            self.assertIsNone(usage_view(state['supply_usage'][color])['last_use_date'])



if __name__ == "__main__":
    unittest.main()
