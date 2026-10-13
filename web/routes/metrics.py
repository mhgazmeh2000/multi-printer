# -*- coding: utf-8 -*-
"""
Prometheus-compatible /metrics endpoint for Grafana dashboards.

Exposes:
  - cartridge_changes_total: counter of CARTRIDGE_CHANGED events (by detection, color, printer)
  - printer_toner_level: current toner level % (by printer, color)
  - printer_total_pages: total pages printed (by printer)
  - printer_online: 1 if online, 0 if offline (by printer)
  - printer_cartridge_chip_id_known: 1 if genuine chip ID exists (by printer, color)
  - printer_supply_name: current supply name (by printer, color) — informational label

حفاظت: اگر متغیر محیطی ``METRICS_TOKEN`` تنظیم شده باشد، درخواست باید هدر
``Authorization: Bearer <token>`` را بفرستد؛ در غیر این صورت ۴۰۱ برمی‌گردد.
اگر تنظیم نشده باشد، endpoint مثل قبل باز است (با هشدار در لاگ).

No external dependencies required — generates Prometheus exposition text directly.
"""

import os
import secrets
import logging
from datetime import datetime
from flask import Blueprint, Response, request
from core.db import get_connection
from core import store

bp = Blueprint("metrics", __name__)
log = logging.getLogger("PrinterMonitor")

# هشدار «بدون توکن» فقط یک‌بار در طول عمر پروسه ثبت می‌شود.
_METRICS_TOKEN_WARNED = False


def _check_metrics_token():
    """بررسی هدر Bearer در برابر ``METRICS_TOKEN``.

    خروجی: ``(ok, required)``
      • توکن تنظیم نشده  → (True, False)  → رفتار قبلی حفظ می‌شود (با هشدار یک‌باره)
      • توکن تنظیم شده   → مقایسه‌ی ثابت‌زمان هدر با مقدار محیطی
    """
    global _METRICS_TOKEN_WARNED
    expected = (os.getenv("METRICS_TOKEN") or "").strip()
    if not expected:
        if not _METRICS_TOKEN_WARNED:
            _METRICS_TOKEN_WARNED = True
            log.warning(
                "⚠ METRICS_TOKEN تنظیم نشده است؛ /metrics بدون احراز هویت در دسترس "
                "است. برای محیط تولید یک توکن تنظیم کنید."
            )
        return True, False

    header = (request.headers.get("Authorization") or "").strip()
    prefix = "Bearer "
    if not header.startswith(prefix):
        return False, True
    provided = header[len(prefix):].strip()
    return secrets.compare_digest(provided, expected), True


def _escape_label(value: str) -> str:
    """Escape a label value for Prometheus text format."""
    if value is None:
        return ""
    s = str(value)
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    s = s.replace("\n", "\\n")
    return s


def _metric_line(name: str, labels: dict, value, help_text: str = None, metric_type: str = None, seen: set = None):
    """Generate a Prometheus metric line with optional HELP/TYPE header."""
    parts = []
    label_str = ",".join(f'{k}="{_escape_label(v)}"' for k, v in labels.items() if v is not None)
    if label_str:
        parts.append(f"{name}{{{label_str}}} {value}")
    else:
        parts.append(f"{name} {value}")
    return "\n".join(parts)


@bp.route("/metrics")
def prometheus_metrics():
    """
    Prometheus exposition format endpoint.
    Grafana Prometheus datasource: http://<host>:<port>/metrics
    (در صورت تنظیم METRICS_TOKEN: با هدر Authorization: Bearer <token>)
    """
    token_ok, _token_required = _check_metrics_token()
    if not token_ok:
        return Response(
            "unauthorized\n",
            status=401,
            mimetype="text/plain; version=0.0.4; charset=utf-8",
        )

    lines = []
    help_seen = set()

    def _help(name, text):
        if name not in help_seen:
            lines.append(f"# HELP {name} {text}")
            help_seen.add(name)

    def _type(name, t):
        lines.append(f"# TYPE {name} {t}")

    try:
        # فاز D: اتصال از لایه‌ی adapter (core.db)؛ همان timeout قبلی.
        conn = get_connection(timeout=10.0)

        # ─── 1. cartridge_changes_total ─────────────────────────────────
        _help("cartridge_changes_total", "Total number of CARTRIDGE_CHANGED events detected")
        _type("cartridge_changes_total", "counter")
        # فاز E.4.6: cursor خام حذف شد ⇒ ``conn.execute`` (همان قرارداد بقیه‌ی کوئری‌ها).
        # فاز E.4.9: ``json_extract`` تابع SQLite است ⇒ قطعهٔ SQL از هِلپر adapter.
        detection_sql = conn.json_extract("details", "detection")
        # فاز E.4.9b: ``printer_name`` در فهرست ``GROUP BY`` نیست؛ SQLite این را
        # می‌پذیرد، PostgreSQL نه — خطای 42803 «column logs.printer_name must
        # appear in the GROUP BY clause or be used in an aggregate function»
        # (سنجیده‌شده، E.4.9). تصمیم مالک: ``MAX(printer_name)`` انتخاب شد تا
        # **تعداد سری امروز ثابت بماند** (افزودن ستون به GROUP BY اگر پرینتری
        # تغییر نام داده باشد سری اضافه می‌کند). مزیت جانبی: مقدار برچسب دیگر
        # به ترتیب تصادفی ردیف‌ها وابسته نیست.
        c = conn.execute(f"""
            SELECT printer_ip, MAX(printer_name) AS printer_name,
                   {detection_sql} as detection,
                   color,
                   COUNT(*) as cnt
            FROM logs
            WHERE type = 'CARTRIDGE_CHANGED'
            GROUP BY printer_ip, detection, color
            ORDER BY printer_ip
        """)
        for row in c.fetchall():
            ip, name, detection, color, cnt = row
            if not ip:
                continue
            labels = {
                "printer_ip": ip,
                "printer_name": name or "",
                "detection": detection or "unknown",
                "color": color or "unknown",
            }
            lines.append(_metric_line("cartridge_changes_total", labels, cnt))

        # ─── 2. printer_toner_level ────────────────────────────────────
        _help("printer_toner_level", "Current toner level percentage per color")
        _type("printer_toner_level", "gauge")
        with store.data_lock:
            for ip, data in store.printer_data.items():
                if not data:
                    continue
                printer_name = ""
                with store.printers_lock:
                    for p in store.PRINTERS:
                        if p.get("ip") == ip:
                            printer_name = p.get("name", "")
                            break
                # شکل واقعی collect_enhanced: {"black": {"level": …, "name": …}, …}
                # (شیء قدیمی لیستی بود؛ هر دو پشتیبانی می‌شوند.)
                toners = data.get("toners") or {}
                if isinstance(toners, dict):
                    toner_items = list(toners.items())
                else:
                    toner_items = [(None, t) for t in toners]
                for color_key, t in toner_items:
                    if not isinstance(t, dict):
                        continue
                    level = t.get("level")
                    if level is None:
                        level = t.get("remaining")
                    color_name = color_key or t.get("color") or t.get("name", "")
                    if level is not None:
                        try:
                            level = int(level)
                        except (TypeError, ValueError):
                            continue
                        labels = {
                            "printer_ip": ip,
                            "printer_name": printer_name,
                            "color": color_name,
                        }
                        lines.append(_metric_line("printer_toner_level", labels, level))

        # ─── 3. printer_total_pages ────────────────────────────────────
        _help("printer_total_pages", "Total pages printed since device tracking began")
        _type("printer_total_pages", "gauge")
        with store.data_lock:
            for ip, data in store.printer_data.items():
                if not data:
                    continue
                printer_name = ""
                with store.printers_lock:
                    for p in store.PRINTERS:
                        if p.get("ip") == ip:
                            printer_name = p.get("name", "")
                            break
                # شکل واقعی: counters.total (با fallback به print_total قدیمی)
                counters = data.get("counters")
                total = None
                if isinstance(counters, dict):
                    total = counters.get("total")
                if total is None:
                    total = data.get("print_total")
                if total is not None:
                    try:
                        total = int(total)
                    except (TypeError, ValueError):
                        continue
                    labels = {"printer_ip": ip, "printer_name": printer_name}
                    lines.append(_metric_line("printer_total_pages", labels, total))

        # ─── 4. printer_online ─────────────────────────────────────────
        _help("printer_online", "1 if printer responded to last poll, 0 otherwise")
        _type("printer_online", "gauge")
        with store.data_lock:
            for ip, data in store.printer_data.items():
                printer_name = ""
                with store.printers_lock:
                    for p in store.PRINTERS:
                        if p.get("ip") == ip:
                            printer_name = p.get("name", "")
                            break
                # شکل واقعی: کلید بولی online (fallback: status == "online")
                if data and "online" in data:
                    online = 1 if data.get("online") else 0
                else:
                    online = 1 if data and data.get("status") == "online" else 0
                labels = {"printer_ip": ip, "printer_name": printer_name}
                lines.append(_metric_line("printer_online", labels, online))

        # ─── 5. printer_cartridge_chip_id_known ────────────────────────
        _help("printer_cartridge_chip_id_known", "1 if genuine chip ID is known for this color slot")
        _type("printer_cartridge_chip_id_known", "gauge")
        with store.data_lock:
            for ip, data in store.printer_data.items():
                if not data:
                    continue
                printer_name = ""
                with store.printers_lock:
                    for p in store.PRINTERS:
                        if p.get("ip") == ip:
                            printer_name = p.get("name", "")
                            break
                cart_ids = data.get("cartridge_ids") or {}
                sq = data.get("cartridge_signal_quality") or {}
                if isinstance(cart_ids, dict):
                    for color_key, chip_id in cart_ids.items():
                        if not chip_id:
                            continue
                        quality = sq.get(color_key, "unknown")
                        labels = {
                            "printer_ip": ip,
                            "printer_name": printer_name,
                            "color": color_key,
                            "quality": quality,
                        }
                        lines.append(_metric_line("printer_cartridge_chip_id_known", labels, 1))

        conn.close()

    except Exception as e:
        log.exception("Error generating /metrics: %s", e)
        lines.append(f"# ERROR generating metrics: {e}")

    # ─── 6. Scrape metadata ────────────────────────────────────────────
    _help("scrape_timestamp_seconds", "Unix timestamp of this scrape")
    _type("scrape_timestamp_seconds", "gauge")
    lines.append(f"scrape_timestamp_seconds {datetime.now().timestamp():.0f}")

    body = "\n".join(lines) + "\n"
    return Response(body, mimetype="text/plain; version=0.0.4; charset=utf-8")
