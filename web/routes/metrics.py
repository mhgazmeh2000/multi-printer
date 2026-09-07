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

No external dependencies required — generates Prometheus exposition text directly.
"""

import sqlite3
import logging
from datetime import datetime
from flask import Blueprint, Response
from config.settings import DB_PATH
from core import store

bp = Blueprint("metrics", __name__)
log = logging.getLogger("PrinterMonitor")


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
    """
    lines = []
    help_seen = set()

    def _help(name, text):
        if name not in help_seen:
            lines.append(f"# HELP {name} {text}")
            help_seen.add(name)

    def _type(name, t):
        lines.append(f"# TYPE {name} {t}")

    try:
        conn = sqlite3.connect(DB_PATH, timeout=10.0)
        c = conn.cursor()

        # ─── 1. cartridge_changes_total ─────────────────────────────────
        _help("cartridge_changes_total", "Total number of CARTRIDGE_CHANGED events detected")
        _type("cartridge_changes_total", "counter")
        c.execute("""
            SELECT printer_ip, printer_name,
                   json_extract(details, '$.detection') as detection,
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
                toners = data.get("toners") or []
                for t in toners:
                    level = t.get("level")
                    color_name = t.get("color") or t.get("name", "")
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
