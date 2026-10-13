# -*- coding: utf-8 -*-
"""
پروفایل دستگاه‌های مجازی برای سناریوهای ``collect_enhanced`` (کار ۱ فاز C).

هر پروفایل یک دیکشنری ``{oid: value}`` است که توسط ``FakeSnmpTransport`` سرو
می‌شود؛ OIDهای غایب ⇒ ``None`` (یعنی «دستگاه پاسخ نداد») — دقیقاً مثل واقعیت.
مقادیر عددی از مستندات RFC 3805 (Printer-MIB)، فایل ``core/snmp/oid_map.py``
(شمارنده‌ها/تونر Toshiba) و رفتار واقعی collectorها انتخاب شده‌اند.

سناریوها:
  1) ``hp_color_full``               — HP رنگی، هر چهار تونر سالم
  2) ``hp_color_empty``              — HP رنگی، هر چهار تونر خالی
  3) ``brother_unknown_toner``       — Brother، ظرفیت/سطح نامعلوم (۳-)
  4) ``toshiba_offline_then_online`` — Toshiba: فاز ۱ آفلاین، فاز ۲ آنلاین
  5) ``ecs100g_sensor``              — سنسور دما/رطوبت ECS100G (SNMP v1)
"""
from __future__ import annotations

import copy

# ─── OIDهای مشترک ───────────────────────────────────────────────────────────
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_LOCATION = "1.3.6.1.2.1.1.6.0"
HR_DEVICE_DESCR = "1.3.6.1.2.1.25.3.2.1.5.1"
PRT_MODEL = "1.3.6.1.2.1.43.5.1.1.16.1"
PRT_SERIAL = "1.3.6.1.2.1.43.5.1.1.17.1"
PRT_TOTAL = "1.3.6.1.2.1.43.10.2.1.4.1.1"
PRT_COLOR = "1.3.6.1.2.1.43.10.2.1.4.1.2"
PRT_MONO = "1.3.6.1.2.1.43.10.2.1.4.1.3"

SUP = "1.3.6.1.2.1.43.11.1.1"     # .5 type | .6 name | .7 unit | .8 max | .9 level
TRAY = "1.3.6.1.2.1.43.8.2.1"     # .9 max | .10 level | .13 name

HP_SUPPLIES = [
    ("Black Cartridge HP CF287A", "black"),
    ("Cyan Cartridge HP CF031A", "cyan"),
    ("Magenta Cartridge HP CF033A", "magenta"),
    ("Yellow Cartridge HP CF032A", "yellow"),
]

TOSHIBA_HTML = """
<html><body><table id="toner">
<tr><td>Yellow(Y)</td><td>61%</td></tr>
<tr><td>Magenta(M)</td><td>58%</td></tr>
<tr><td>Cyan(C)</td><td>44%</td></tr>
<tr><td>Black(K)</td><td>77%</td></tr>
</table></body></html>
"""


# ─── سازنده‌ی پروفایل HP ───────────────────────────────────────────────────
def _hp_oids(levels: dict, total: int = 84_210, color: int = 21_480,
             mono: int = 62_730, uptime: int = 1_241_567) -> dict:
    oids = {
        SYS_DESCR: "HP Color LaserJet MFP M577",
        SYS_OBJECT_ID: "1.3.6.1.4.1.11.2.3.9.1.1.1",
        SYS_UPTIME: uptime,
        SYS_NAME: "HP-M577-PRINTROOM",
        SYS_LOCATION: "Print Room",
        HR_DEVICE_DESCR: "HP Color LaserJet MFP M577",
        PRT_MODEL: "HP Color LaserJet MFP M577",
        PRT_SERIAL: "CNMOCK0001",  # E.0.7: سریال واقعی‌نما با mock جایگزین شد
        PRT_TOTAL: total,
        PRT_COLOR: color,
        PRT_MONO: mono,
    }
    for idx, (name, key) in enumerate(HP_SUPPLIES, start=1):
        oids[f"{SUP}.5.1.{idx}"] = 3        # toner (RFC 3805)
        oids[f"{SUP}.6.1.{idx}"] = name
        oids[f"{SUP}.7.1.{idx}"] = 19       # percent
        oids[f"{SUP}.8.1.{idx}"] = 100      # max capacity
        oids[f"{SUP}.9.1.{idx}"] = int(levels.get(key, 50))
    for idx, (name, cap, lvl) in enumerate(
            [("Tray 1", 550, 400), ("Tray 2", 550, 120), ("Tray 3", 550, 0)], start=1):
        oids[f"{TRAY}.13.1.{idx}"] = name
        oids[f"{TRAY}.9.1.{idx}"] = cap
        oids[f"{TRAY}.10.1.{idx}"] = lvl
    return oids


# ─── سازنده‌ی پروفایل Brother ──────────────────────────────────────────────
def _brother_oids() -> dict:
    return {
        SYS_DESCR: "Brother HL-L2350DW series",
        SYS_OBJECT_ID: "1.3.6.1.4.1.2435.2.3.9.1",
        SYS_UPTIME: 400_000,
        SYS_NAME: "BROTHER-L2350-OFFICE",
        SYS_LOCATION: "Office",
        "1.3.6.1.4.1.2435.2.4.3.99.3.1.6.1.2.1": "HL-L2350DW",
        "1.3.6.1.4.1.2435.2.4.3.99.3.1.6.1.2.3": "U63899A1N123456",
        "1.3.6.1.4.1.2435.2.4.3.1240.6.5.0": "1.44",
        PRT_TOTAL: 25_431,
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.1": 25_431,
        # کارتریج با ظرفیت/سطح نامعلوم (۳-) — سناریوی اصلی این تست
        f"{SUP}.5.1.1": 3,
        f"{SUP}.6.1.1": "Black Toner Cartridge TN-2430",
        f"{SUP}.7.1.1": 0,      # unit unknown
        f"{SUP}.8.1.1": -3,     # max unknown
        f"{SUP}.9.1.1": -3,     # level unknown
        f"{TRAY}.13.1.1": "Tray 1",
        f"{TRAY}.9.1.1": 250,
        f"{TRAY}.10.1.1": 200,
        "1.3.6.1.2.1.43.6.1.1.3.1.1": 4,   # cover closed
    }


# ─── سازنده‌ی پروفایل Toshiba (کلیدهای oid_map) ────────────────────────────
def _toshiba_oids() -> dict:
    base = "1.3.6.1.4.1.1129.2.3.50"
    oids = {
        SYS_DESCR: "TOSHIBA e-STUDIO3515AC",
        SYS_OBJECT_ID: "1.3.6.1.4.1.1129.2.3.50.1",
        SYS_UPTIME: 900_000,
        SYS_NAME: "ESTUDIO-3515-TEHRAN",
        PRT_MODEL: "e-STUDIO3515AC",
        PRT_SERIAL: "CBA1234567",
        f"{base}.1.2.3.1.3.1.1": "e-STUDIO3515AC",
        f"{base}.1.2.4.1.8.1.1": "CBA1234567",
        f"{base}.1.2.3.1.4.1.1": "T300SY0.1234",
        f"{base}.1.2.3.1.11.1.1": 900_000,
        # شمارنده‌ها (a3_total + a4_total == print_total)
        f"{base}.1.3.21.6.1.2.1.4": 152_300,
        f"{base}.1.3.21.6.1.2.1.1": 41_200,
        f"{base}.1.3.21.6.1.2.1.3": 111_100,
        f"{base}.1.3.21.6.1.2.1.2": 0,
        f"{base}.1.3.21.6.1.207.1.4": 12_000,
        f"{base}.1.3.21.6.1.207.1.1": 2_100,
        f"{base}.1.3.21.6.1.207.1.3": 9_900,
        f"{base}.1.3.21.6.1.208.1.4": 140_300,
        f"{base}.1.3.21.6.1.208.1.1": 39_100,
        f"{base}.1.3.21.6.1.208.1.3": 101_200,
        f"{base}.1.3.21.6.1.209.1.4": 45_000,
        f"{base}.1.3.21.6.1.210.1.4": 95_300,
        # تونر (کد وضعیت NE) + usage
        f"{base}.1.3.21.8.1.5.1.1": 3,      # magenta ok
        f"{base}.1.3.21.8.1.5.1.2": 3,      # cyan ok
        f"{base}.1.3.21.8.1.5.1.3": 4,      # yellow ok
        f"{base}.1.3.21.8.1.5.1.4": 5,      # black low
        f"{base}.1.3.21.8.1.2.1.1": 1_200_000_000,
        f"{base}.1.3.21.8.1.2.1.2": 1_500_000_000,
        f"{base}.1.3.21.8.1.2.1.3": 2_100_000_000,
        f"{base}.1.3.21.8.1.2.1.4": 8_400_000_000,
        # نام کارتریج‌ها از Printer-MIB استاندارد (black=1, cyan=2, magenta=3, yellow=4)
        f"{SUP}.6.1.1": "Black Toner T-FC415K",
        f"{SUP}.6.1.2": "Cyan Toner T-FC415C",
        f"{SUP}.6.1.3": "Magenta Toner T-FC415M",
        f"{SUP}.6.1.4": "Yellow Toner T-FC415Y",
    }
    # سینی‌ها: level کد است (0=خالی، <=25 low، <=75 medium، >75 ok)
    for num, (lvl, size) in {1: (95, 3), 2: (60, 3), 3: (10, 37), 6: (0, 3)}.items():
        oids[f"{base}.1.3.5.1.1.5.1.{num}"] = lvl
        oids[f"{base}.1.3.5.1.1.4.1.{num}"] = size
        oids[f"{base}.1.3.5.1.1.3.1.{num}"] = 0
    return oids


# ─── سازنده‌ی پروفایل ECS100G ───────────────────────────────────────────────
def _sensor_oids() -> dict:
    return {
        "1.3.6.1.4.1.47206.1.0": "ECS100G",
        "1.3.6.1.4.1.47206.2.0": "ECS-2201-0417",
        SYS_UPTIME: 1_500_000,
        "1.3.6.1.4.1.47206.110.1.1.0": 1,   # port1 temp status = active
        "1.3.6.1.4.1.47206.110.1.2.0": 245,  # 24.5 °C
        "1.3.6.1.4.1.47206.111.1.1.0": 1,
        "1.3.6.1.4.1.47206.111.1.2.0": 553,  # 55.3 %
        "1.3.6.1.4.1.47206.110.2.1.0": 1,
        "1.3.6.1.4.1.47206.110.2.2.0": 268,  # 26.8 °C
        "1.3.6.1.4.1.47206.111.2.1.0": 1,
        "1.3.6.1.4.1.47206.111.2.2.0": 471,  # 47.1 %
    }


# ─── سناریوها ──────────────────────────────────────────────────────────────
IP_HP_FULL = "192.0.2.11"
IP_HP_EMPTY = "192.0.2.12"
IP_BROTHER = "192.0.2.13"
IP_TOSHIBA = "192.0.2.14"
IP_SENSOR = "192.0.2.15"


def hp_color_full():
    return {
        "name": "enhanced_01_hp_color_toner_full",
        "desc": "HP Color LaserJet MFP M577 — چهار تونر سالم (۷۸/۵۸/۴۴/۲۹٪)، ۳ سینی",
        "printer": {"ip": IP_HP_FULL, "name": "HP M577 Print Room", "nickname": "HP-رنگی",
                    "community": "public", "brand": "hp", "device_type": "color"},
        "http": [],
        "phases": [{"name": "main", "devices": {
            IP_HP_FULL: {"version": 2, "oids": _hp_oids(
                {"black": 78, "cyan": 58, "magenta": 44, "yellow": 29})}}}],
    }


def hp_color_empty():
    return {
        "name": "enhanced_02_hp_color_toner_empty",
        "desc": "HP Color LaserJet MFP M577 — هر چهار تونر خالی (۰٪)",
        "printer": {"ip": IP_HP_EMPTY, "name": "HP M577 Empty", "nickname": "HP-خالی",
                    "community": "public", "brand": "hp", "device_type": "color"},
        "http": [],
        "phases": [{"name": "main", "devices": {
            IP_HP_EMPTY: {"version": 2, "oids": _hp_oids(
                {"black": 0, "cyan": 0, "magenta": 0, "yellow": 0},
                total=12_500, color=3_100, mono=9_400, uptime=95_000)}}}],
    }


def brother_unknown_toner():
    return {
        "name": "enhanced_03_brother_toner_unknown",
        "desc": "Brother HL-L2350DW — ظرفیت و سطح تونر نامعلوم (max=-3, level=-3)",
        "printer": {"ip": IP_BROTHER, "name": "Brother HL-L2350 Office", "nickname": "",
                    "community": "public", "brand": "brother", "device_type": "mono"},
        "http": [],
        "phases": [{"name": "main", "devices": {
            IP_BROTHER: {"version": 2, "oids": _brother_oids()}}}],
    }


def toshiba_offline_then_online():
    online_oids = _toshiba_oids()
    return {
        "name": "enhanced_04_toshiba_offline_then_online",
        "desc": "Toshiba e-STUDIO3515AC — فاز ۱ آفلاین (بدون پاسخ SNMP)، فاز ۲ آنلاین "
                "+ TopAccess با تونر اسکرپ‌شده",
        "printer": {"ip": IP_TOSHIBA, "name": "Toshiba e-STUDIO3515AC", "nickname": "توشیبا",
                    "community": "public", "brand": "toshiba", "device_type": "color"},
        "http": [(r"\?MAIN=DEVICE", TOSHIBA_HTML)],
        "phases": [
            {"name": "offline", "devices": {}},
            {"name": "online", "devices": {IP_TOSHIBA: {"version": 2, "oids": online_oids}}},
        ],
    }


def ecs100g_sensor():
    return {
        "name": "enhanced_05_ecs100g_sensor",
        "desc": "سنسور ECS100G — پورت ۱ و ۲: دما ۲۴٫۵/۲۶٫۸ و رطوبت ۵۵٫۳/۴۷٫۱",
        "printer": {"ip": IP_SENSOR, "name": "ECS100G Rack", "nickname": "",
                    "community": "public", "brand": "sensor", "device_type": "sensor"},
        "http": [],
        "phases": [{"name": "main", "devices": {
            IP_SENSOR: {"version": 1, "oids": _sensor_oids()}}}],
    }


# ─── ساخت transport از سناریو یا fixture ───────────────────────────────────
def build_transport(obj):
    """``obj`` می‌تواند سناریو (کلید ``phases``) یا fixture (``input.phases``) باشد."""
    from tests.fake_snmp import FakeSnmpTransport

    phases = obj.get("phases") or obj["input"]["phases"]
    first = phases[0].get("devices") or {}
    return FakeSnmpTransport(copy.deepcopy(first))
