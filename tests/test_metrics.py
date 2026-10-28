# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_metrics.py
# @brief:        Tests for the Prometheus-compatible /metrics endpoint.
# @details:      Responsibilities: Check metric names and values.
#                Main API: MetricsEndpointTests
#                Side effects: none at import time
# @dependencies: internal: core, core.database, web
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Endpoint / metrics tests compatible with Prometheus.

⚠ Data shape: Fixtures are built with the actual shape of ``collect_enhanced'' output
(The same as recorded in golden fixtures ``tests/fixtures/golden/enhanced_*.json'')
has been):
    - ``online'' is boolean (not string ``status'')
    - Counters are under ``counters'': ``counters.total'' (not ``print_total'')
    - ``toners'' is a ``{color: {"level": …, "name": …}}" dictionary (not a list)

Coverage (8 tests):
  1) Output format (Content-Type, HELP/TYPE) + no error line with real data
  2) Being empty if there is no event
  3) cartridge_changes_total counter with correct labels
  4) printer_online gauge (online/offline)
  5) printer_toner_level gauge (multicolor, from real dictionary)
  6) scrape_timestamp_seconds
  7) gauge printer_total_pages (from counters.total)
  8) gauge printer_cartridge_chip_id_known
"""
import os
import tempfile
import unittest

from core import store
from core.database import init_db, add_event


def _parse_metrics(body: str) -> dict:
    """Parse Prometheus exposition text into {metric_name: [(labels_dict, value_str), ...]}."""
    metrics = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Format: metric_name{label="val",...} value  or  metric_name value
        if "{" in line:
            name_part, rest = line.split("{", 1)
            labels_part, value = rest.rsplit("}", 1)
            labels = {}
            for pair in labels_part.split(","):
                pair = pair.strip()
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    labels[k.strip()] = v.strip().strip('"')
            metrics.setdefault(name_part.strip(), []).append((labels, value.strip()))
        else:
            parts = line.split()
            if len(parts) >= 2:
                metrics.setdefault(parts[0], []).append(({}, parts[1]))
    return metrics


def _live_printer(*, online=True, total=None, toners=None, cartridge_ids=None,
                  signal_quality=None, brand="hp"):
    """Live data in **real form** output of ``collect_enhanced''."""
    data = {
        "online": bool(online),
        "brand": brand,
        "counters": {},
        "toners": toners if toners is not None else {},
        "trays": [],
        "alerts": [],
    }
    if total is not None:
        data["counters"]["total"] = total
        data["counters"]["black_white"] = total
    if cartridge_ids is not None:
        data["cartridge_ids"] = cartridge_ids
        data["cartridge_signal_quality"] = signal_quality or {}
    if not online:
        data["error"] = "No SNMP response"
    return data


# Actual example of Toner (key = color, value = dictionary)
_REAL_TONERS = {
    "black": {"level": 75, "status": "ok", "name": "Black Cartridge HP CF287A"},
    "cyan": {"level": 40, "status": "low", "name": "Cyan Cartridge HP CF031A"},
    "magenta": {"level": 12, "status": "critical", "name": "Magenta Cartridge HP CF033A"},
    "yellow": {"level": 0, "status": "empty", "name": "Yellow Cartridge HP CF032A"},
}


class MetricsEndpointTests(unittest.TestCase):
    """Testing endpoint /metrics with Flask test client (no token: default behavior)."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        store._prev._cache.clear()
        store.printer_data = {}
        store.PRINTERS = []

    def tearDown(self):
        """Release the resources that each test in this class created."""
        store._prev._cache.clear()
        store.printer_data = {}
        store.PRINTERS = []
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _client(self):
        """Helper: client."""
        from web import create_app
        app = create_app()
        app.config["TESTING"] = True
        app.config["LOGIN_DISABLED"] = True
        # The output of /metrics should not be tied to the coincidence of METRICS_TOKEN in the environment
        os.environ.pop("METRICS_TOKEN", None)
        return app.test_client()

    # ─── 1) Output format ───
    def test_metrics_content_type_and_format(self):
        """Content-Type should be text/plain, have lines HELP/TYPE and with
        Real data should not generate any error line (previous bug: 'str' has no attribute 'get')."""
        store.PRINTERS = [{"ip": "10.1.1.1", "name": "HP M527"}]
        store.printer_data = {"10.1.1.1": _live_printer(total=50000, toners=_REAL_TONERS)}

        client = self._client()
        resp = client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/plain", resp.content_type)
        body = resp.data.decode("utf-8")
        self.assertIn("# HELP cartridge_changes_total", body)
        self.assertIn("# TYPE cartridge_changes_total counter", body)
        self.assertIn("# HELP printer_toner_level", body)
        self.assertIn("# TYPE printer_toner_level gauge", body)
        self.assertIn("scrape_timestamp_seconds", body)
        self.assertNotIn("# ERROR generating metrics", body,
                         "با شکل واقعی داده نباید خطا تولید شود")

    # ─── 2) Being empty without data ───
    def test_metrics_empty_when_no_events(self):
        """Without the CARTRIDGE_CHANGED event, there is no counter."""
        client = self._client()
        resp = client.get("/metrics")
        body = resp.data.decode("utf-8")
        self.assertNotIn("cartridge_changes_total{", body)

    # ─── 3) counter cartridge_changes_total ───
    def test_cartridge_changes_total_counter(self):
        """The CARTRIDGE_CHANGED event should increment the counter."""
        add_event("10.1.1.1", "CARTRIDGE_CHANGED", {
            "message": "test change",
            "severity": "info",
            "detection": "chip_id",
            "color": "black",
            "auto_detected": True,
            "confirmed": True,
        })
        add_event("10.1.1.1", "CARTRIDGE_CHANGED", {
            "message": "test change 2",
            "severity": "info",
            "detection": "chip_id",
            "color": "black",
            "auto_detected": True,
            "confirmed": True,
        })
        add_event("10.2.2.2", "CARTRIDGE_CHANGED", {
            "message": "name change",
            "severity": "info",
            "detection": "supply_name_change",
            "color": "cyan",
            "auto_detected": True,
            "confirmed": True,
        })

        client = self._client()
        resp = client.get("/metrics")
        body = resp.data.decode("utf-8")
        metrics = _parse_metrics(body)
        cc = metrics.get("cartridge_changes_total", [])
        self.assertGreater(len(cc), 0, "cartridge_changes_total باید وجود داشته باشد")

        # Check: 10.1.1.1 + chip_id + black = 2
        found = False
        for labels, value in cc:
            if (labels.get("printer_ip") == "10.1.1.1"
                    and labels.get("detection") == "chip_id"
                    and labels.get("color") == "black"):
                self.assertEqual(value, "2")
                found = True
        self.assertTrue(found, "شمارنده برای 10.1.1.1/chip_id/black باید 2 باشد")

        # Check: 10.2.2.2 + supply_name_change + cyan = 1
        found2 = False
        for labels, value in cc:
            if (labels.get("printer_ip") == "10.2.2.2"
                    and labels.get("detection") == "supply_name_change"
                    and labels.get("color") == "cyan"):
                self.assertEqual(value, "1")
                found2 = True
        self.assertTrue(found2, "شمارنده برای 10.2.2.2/supply_name_change/cyan باید 1 باشد")

    # ─── 4) printer_online gauge ───
    def test_printer_online_gauge(self):
        """Online printer 1 and offline printer (no SNMP response) zero."""
        store.PRINTERS = [
            {"ip": "10.1.1.1", "name": "HP M527"},
            {"ip": "10.2.2.2", "name": "Canon MF220"},
        ]
        store.printer_data = {
            "10.1.1.1": _live_printer(online=True, total=50000),
            "10.2.2.2": _live_printer(online=False),      # The actual form of the device is turned off
        }

        client = self._client()
        resp = client.get("/metrics")
        body = resp.data.decode("utf-8")
        metrics = _parse_metrics(body)
        online = metrics.get("printer_online", [])
        self.assertEqual(len(online), 2, "برای هر دو پرینتر باید یک نمونه باشد")

        seen = {}
        for labels, value in online:
            seen[labels.get("printer_ip")] = value
        self.assertEqual(seen.get("10.1.1.1"), "1")
        self.assertEqual(seen.get("10.2.2.2"), "0")

    # ─── 5) printer_toner_level gauge ───
    def test_toner_level_gauge(self):
        """The toner level should be read from the actual dictionary of toners (multi-color)."""
        store.PRINTERS = [{"ip": "10.1.1.1", "name": "HP M527"}]
        store.printer_data = {"10.1.1.1": _live_printer(total=50000, toners=_REAL_TONERS)}

        client = self._client()
        resp = client.get("/metrics")
        body = resp.data.decode("utf-8")
        metrics = _parse_metrics(body)
        levels = metrics.get("printer_toner_level", [])

        by_color = {labels.get("color"): value for labels, value in levels
                    if labels.get("printer_ip") == "10.1.1.1"}
        self.assertEqual(by_color.get("black"), "75")
        self.assertEqual(by_color.get("cyan"), "40")
        self.assertEqual(by_color.get("yellow"), "0")

    # ─── 6) scrape_timestamp_seconds ───
    def test_scrape_timestamp(self):
        """scrape_timestamp_seconds must be a Unix timestamp number."""
        client = self._client()
        resp = client.get("/metrics")
        body = resp.data.decode("utf-8")
        metrics = _parse_metrics(body)
        ts = metrics.get("scrape_timestamp_seconds", [])
        self.assertEqual(len(ts), 1)
        value = int(ts[0][1])
        self.assertGreater(value, 1_700_000_000)  # after 2023

    # ─── 7) printer_total_pages ───
    def test_total_pages_gauge(self):
        """Read the total number of pages from counters.total."""
        store.PRINTERS = [{"ip": "10.1.1.1", "name": "HP M527"}]
        store.printer_data = {"10.1.1.1": _live_printer(total=123456)}

        client = self._client()
        resp = client.get("/metrics")
        body = resp.data.decode("utf-8")
        metrics = _parse_metrics(body)
        pages = metrics.get("printer_total_pages", [])

        found = False
        for labels, value in pages:
            if labels.get("printer_ip") == "10.1.1.1":
                self.assertEqual(value, "123456")
                found = True
        self.assertTrue(found, "printer_total_pages برای 10.1.1.1 باید 123456 باشد")

    # ─── 8) printer_cartridge_chip_id_known ───
    def test_chip_id_known_gauge(self):
        """If the chip ID is present, the value 1 should be displayed."""
        store.PRINTERS = [{"ip": "10.1.1.1", "name": "HP M527"}]
        store.printer_data = {"10.1.1.1": _live_printer(
            total=50000,
            cartridge_ids={"black": "part:Black Cartridge HP CF287A"},
            signal_quality={"black": "static"},
        )}

        client = self._client()
        resp = client.get("/metrics")
        body = resp.data.decode("utf-8")
        metrics = _parse_metrics(body)
        known = metrics.get("printer_cartridge_chip_id_known", [])

        found = False
        for labels, value in known:
            if labels.get("printer_ip") == "10.1.1.1" and labels.get("color") == "black":
                self.assertEqual(value, "1")
                self.assertEqual(labels.get("quality"), "static")
                found = True
        self.assertTrue(found, "chip_id_known برای black باید 1 باشد")


if __name__ == "__main__":
    unittest.main()
