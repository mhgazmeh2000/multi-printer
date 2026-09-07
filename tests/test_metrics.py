# -*- coding: utf-8 -*-
"""
تست‌های endpoint /metrics سازگار با Prometheus.

پوشش:
  ۱) فرمت خروجی (Content-Type, خطوط HELP/TYPE)
  ۲) شمارنده cartridge_changes_total با برچسب‌های صحیح
  ۳) گیج printer_toner_level
  ۴) گیج printer_online
  ۵) scrape_timestamp_seconds
  ۶) خالی بودن در صورت نبود داده
  ۷) نمایش printer_total_pages
  ۸) نمایش printer_cartridge_chip_id_known
"""
import json
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


class MetricsEndpointTests(unittest.TestCase):
    """تست endpoint /metrics با Flask test client."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        store._prev._cache.clear()
        store.printer_data = {}
        store.PRINTERS = []

    def tearDown(self):
        store._prev._cache.clear()
        store.printer_data = {}
        store.PRINTERS = []
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _client(self):
        from web import create_app
        app = create_app()
        app.config["TESTING"] = True
        app.config["LOGIN_DISABLED"] = True
        return app.test_client()

    # ─── ۱) فرمت خروجی ───
    def test_metrics_content_type_and_format(self):
        """Content-Type باید text/plain باشد و خطوط HELP/TYPE داشته باشد."""
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

    # ─── ۲) خالی بودن بدون داده ───
    def test_metrics_empty_when_no_events(self):
        """بدون رویداد CARTRIDGE_CHANGED، شمارنده وجود ندارد."""
        client = self._client()
        resp = client.get("/metrics")
        body = resp.data.decode("utf-8")
        self.assertNotIn("cartridge_changes_total{", body)

    # ─── ۳) شمارنده cartridge_changes_total ───
    def test_cartridge_changes_total_counter(self):
        """رویداد CARTRIDGE_CHANGED باید شمارنده را افزایش دهد."""
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

        # چک کن: 10.1.1.1 + chip_id + black = 2
        found = False
        for labels, value in cc:
            if (labels.get("printer_ip") == "10.1.1.1"
                    and labels.get("detection") == "chip_id"
                    and labels.get("color") == "black"):
                self.assertEqual(value, "2")
                found = True
        self.assertTrue(found, "شمارنده برای 10.1.1.1/chip_id/black باید 2 باشد")

        # چک کن: 10.2.2.2 + supply_name_change + cyan = 1
        found2 = False
        for labels, value in cc:
            if (labels.get("printer_ip") == "10.2.2.2"
                    and labels.get("detection") == "supply_name_change"
                    and labels.get("color") == "cyan"):
                self.assertEqual(value, "1")
                found2 = True
        self.assertTrue(found2, "شمارنده برای 10.2.2.2/supply_name_change/cyan باید 1 باشد")

    # ─── ۴) گیج printer_online ───
    def test_printer_online_gauge(self):
        """پرینتر آنلاین باید 1 و آفلاین باید 0 باشد."""
        store.PRINTERS = [
            {"ip": "10.1.1.1", "name": "HP M527"},
            {"ip": "10.2.2.2", "name": "Canon MF220"},
        ]
        store.printer_data = {
            "10.1.1.1": {"status": "online", "print_total": 50000},
            "10.2.2.2": {},  # offline — no data
        }

        client = self._client()
        resp = client.get("/metrics")
        body = resp.data.decode("utf-8")
        metrics = _parse_metrics(body)
        online = metrics.get("printer_online", [])

        for labels, value in online:
            if labels.get("printer_ip") == "10.1.1.1":
                self.assertEqual(value, "1")
            elif labels.get("printer_ip") == "10.2.2.2":
                self.assertEqual(value, "0")

    # ─── ۵) گیج printer_toner_level ───
    def test_toner_level_gauge(self):
        """سطح تونر باید از داده‌ی زنده خوانده شود."""
        store.PRINTERS = [{"ip": "10.1.1.1", "name": "HP M527"}]
        store.printer_data = {
            "10.1.1.1": {
                "status": "online",
                "toners": [
                    {"color": "black", "level": 75, "name": "Black Toner"},
                ],
            },
        }

        client = self._client()
        resp = client.get("/metrics")
        body = resp.data.decode("utf-8")
        metrics = _parse_metrics(body)
        levels = metrics.get("printer_toner_level", [])

        found = False
        for labels, value in levels:
            if labels.get("printer_ip") == "10.1.1.1" and labels.get("color") == "black":
                self.assertEqual(value, "75")
                found = True
        self.assertTrue(found, "سطح تونر ۷۵٪ برای black باید وجود داشته باشد")

    # ─── ۶) scrape_timestamp_seconds ───
    def test_scrape_timestamp(self):
        """scrape_timestamp_seconds باید عدد Unix timestamp باشد."""
        client = self._client()
        resp = client.get("/metrics")
        body = resp.data.decode("utf-8")
        metrics = _parse_metrics(body)
        ts = metrics.get("scrape_timestamp_seconds", [])
        self.assertEqual(len(ts), 1)
        value = int(ts[0][1])
        self.assertGreater(value, 1_700_000_000)  # after 2023

    # ─── ۷) printer_total_pages ───
    def test_total_pages_gauge(self):
        """تعداد کل صفحات باید از داده‌ی زنده خوانده شود."""
        store.PRINTERS = [{"ip": "10.1.1.1", "name": "HP M527"}]
        store.printer_data = {
            "10.1.1.1": {"status": "online", "print_total": 123456},
        }

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

    # ─── ۸) printer_cartridge_chip_id_known ───
    def test_chip_id_known_gauge(self):
        """اگر chip ID موجود باشد، مقدار 1 باید نمایش داده شود."""
        store.PRINTERS = [{"ip": "10.1.1.1", "name": "HP M527"}]
        store.printer_data = {
            "10.1.1.1": {
                "status": "online",
                "cartridge_ids": {"black": "fde86d"},
                "cartridge_signal_quality": {"black": "genuine"},
            },
        }

        client = self._client()
        resp = client.get("/metrics")
        body = resp.data.decode("utf-8")
        metrics = _parse_metrics(body)
        known = metrics.get("printer_cartridge_chip_id_known", [])

        found = False
        for labels, value in known:
            if labels.get("printer_ip") == "10.1.1.1" and labels.get("color") == "black":
                self.assertEqual(value, "1")
                self.assertEqual(labels.get("quality"), "genuine")
                found = True
        self.assertTrue(found, "chip_id_known برای black باید 1 باشد")


if __name__ == "__main__":
    unittest.main()
