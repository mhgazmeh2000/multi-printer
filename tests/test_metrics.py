# -*- coding: utf-8 -*-
"""
تست‌های endpoint /metrics سازگار با Prometheus.

⚠ شکل داده: fixtureها با **شکل واقعی خروجی ``collect_enhanced``** ساخته شده‌اند
(همان چیزی که در golden fixtureهای ``tests/fixtures/golden/enhanced_*.json`` ثبت
شده است):
    - ``online``  بولی است (نه ``status`` رشته‌ای)
    - شمارنده‌ها زیر ``counters`` هستند: ``counters.total`` (نه ``print_total``)
    - ``toners`` یک دیکشنری ``{color: {"level": …, "name": …}}`` است (نه لیست)

پوشش (۸ تست):
  ۱) فرمت خروجی (Content-Type, HELP/TYPE) + نبود خط خطا با داده‌ی واقعی
  ۲) خالی بودن در صورت نبود رویداد
  ۳) شمارنده cartridge_changes_total با برچسب‌های صحیح
  ۴) گیج printer_online (آنلاین/آفلاین)
  ۵) گیج printer_toner_level (چند رنگ، از دیکشنری واقعی)
  ۶) scrape_timestamp_seconds
  ۷) گیج printer_total_pages (از counters.total)
  ۸) گیج printer_cartridge_chip_id_known
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
    """داده‌ی زنده به **شکل واقعی** خروجی ``collect_enhanced``."""
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


# نمونه‌ی واقعی Toner (کلید = رنگ، مقدار = دیکشنری)
_REAL_TONERS = {
    "black": {"level": 75, "status": "ok", "name": "Black Cartridge HP CF287A"},
    "cyan": {"level": 40, "status": "low", "name": "Cyan Cartridge HP CF031A"},
    "magenta": {"level": 12, "status": "critical", "name": "Magenta Cartridge HP CF033A"},
    "yellow": {"level": 0, "status": "empty", "name": "Yellow Cartridge HP CF032A"},
}


class MetricsEndpointTests(unittest.TestCase):
    """تست endpoint /metrics با Flask test client (بدون توکن: رفتار پیش‌فرض)."""

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
        # خروجی /metrics نباید به تصادفِ وجود METRICS_TOKEN در محیط گره بخورد
        os.environ.pop("METRICS_TOKEN", None)
        return app.test_client()

    # ─── ۱) فرمت خروجی ───
    def test_metrics_content_type_and_format(self):
        """Content-Type باید text/plain باشد، خطوط HELP/TYPE داشته باشد و با
        داده‌ی واقعی هیچ خط خطایی تولید نشود (باگ قبلی: 'str' has no attribute 'get')."""
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
        """پرینتر آنلاین 1 و پرینتر آفلاین (بدون پاسخ SNMP) صفر."""
        store.PRINTERS = [
            {"ip": "10.1.1.1", "name": "HP M527"},
            {"ip": "10.2.2.2", "name": "Canon MF220"},
        ]
        store.printer_data = {
            "10.1.1.1": _live_printer(online=True, total=50000),
            "10.2.2.2": _live_printer(online=False),      # شکل واقعی دستگاه خاموش
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

    # ─── ۵) گیج printer_toner_level ───
    def test_toner_level_gauge(self):
        """سطح تونر باید از دیکشنری واقعی toners خوانده شود (چند رنگ)."""
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
        """تعداد کل صفحات از counters.total خوانده شود."""
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

    # ─── ۸) printer_cartridge_chip_id_known ───
    def test_chip_id_known_gauge(self):
        """اگر chip ID موجود باشد، مقدار 1 باید نمایش داده شود."""
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
