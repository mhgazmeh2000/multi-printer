# -*- coding: utf-8 -*-
"""
FakeSnmpTransport — لایه‌ی SNMP جعلی و *قطعی* برای تست‌های golden.

طراحی (طبق تصمیم فاز C):
  • فقط دو تابع پایین‌ترین لایه در ``core.snmp.protocol`` وصله (patch) می‌شوند:
    ``snmp_get`` و ``snmp_getnext``.
  • هیچ تابع سطح‌بالاتری وصله نمی‌شود؛ بنابراین کل مسیر واقعی
    (``snmp_get_with_fallback`` → کش/تشخیص نسخه، ``snmp_get_first``،
    ``snmp_get_bulk``، ``snmp_walk``، و منطق collectorها) دست‌نخورده اجرا می‌شود.
  • قطعی بودن: هیچ سوکتی باز نمی‌شود، هیچ timeout/شبکه‌ای در کار نیست؛
    پاسخ‌ها از پروفایل دستگاه خوانده می‌شوند.

پروفایل دستگاه::

    {
      "127.0.0.1": {
          "version": 2,                 # نسخه‌ای که دستگاه به آن پاسخ می‌دهد
          "oids": {"1.3.6.1.2.1.1.1.0": "HP LaserJet M527", ...},
          "slow_oids": {...},           # اختیاری: فقط در گذر دوم پاسخ می‌دهند
      }
    }

هر OID که در پروفایل نباشد ⇒ ``None`` (یعنی «دستگاه پاسخ نداد»)، که رفتار
واقعی دستگاه‌های چند-نسخه‌ای/محافظت‌شده را بازتولید می‌کند.
"""
from __future__ import annotations

import copy
import contextlib
from unittest import mock

from core.snmp import protocol as _protocol


def oid_key(oid: str):
    """کلید مقایسه‌ی عددی (نه رشته‌ای) برای OIDها."""
    return tuple(int(p) for p in oid.strip(".").split(".") if p != "")


class FakeSnmpTransport:
    def __init__(self, devices: dict, record_calls: bool = True):
        self.devices = copy.deepcopy(devices)
        self.record_calls = record_calls
        self.calls = []   # [{"fn": "get"|"getnext", "ip":…, "oid":…, "version":…}]

    # ── داخلی ──────────────────────────────────────────────────────────────
    def _device(self, ip):
        return self.devices.get(ip)

    def _lookup(self, ip, oid, version, second_pass=False):
        dev = self._device(ip)
        if not dev:
            return None                      # IP ناشناس ⇒ آفلاین
        want = dev.get("version", 2)
        if want is not None and version != want:
            return None                      # نسخه‌ی نادرست ⇒ بی‌پاسخ (مثل واقعیت)
        pool = dev.get("oids", {})
        if oid in pool:
            return pool[oid]
        if second_pass:
            return dev.get("slow_oids", {}).get(oid)
        return None

    # ── API معادل core.snmp.protocol ───────────────────────────────────────
    def snmp_get(self, ip, oid, community="public", port=161, timeout=3.0,
                 request_id=1, version=2):
        if self.record_calls:
            self.calls.append({"fn": "get", "ip": ip, "oid": oid, "version": version})
        return self._lookup(ip, oid, version)

    def snmp_getnext(self, ip, oid, community="public", port=161, timeout=3.0,
                     request_id=1, version=2):
        if self.record_calls:
            self.calls.append({"fn": "getnext", "ip": ip, "oid": oid, "version": version})
        dev = self._device(ip)
        if not dev:
            return None
        want = dev.get("version", 2)
        if want is not None and version != want:
            return None
        keys = sorted(set(dev.get("oids", {})) | set(dev.get("slow_oids", {})))
        cur = oid_key(oid)
        for k in keys:
            if oid_key(k) > cur:
                return k, (dev.get("oids", {}).get(k) or dev.get("slow_oids", {}).get(k))
        return None

    # ── نصب/برداشتن وصله ───────────────────────────────────────────────────
    @contextlib.contextmanager
    def installed(self):
        """``snmp_get``/``snmp_getnext`` ماژول protocol را موقتاً جایگزین می‌کند."""
        with mock.patch.object(_protocol, "snmp_get", side_effect=self.snmp_get), \
             mock.patch.object(_protocol, "snmp_getnext", side_effect=self.snmp_getnext):
            yield self

    def call_sequence(self):
        """توالی فراخوانی‌های خوانده‌شده (برای بخش snmp_call_sequence در fixture)."""
        return [f'{c["fn"]}:{c["oid"]}@v{c["version"]}' for c in self.calls]


def clear_version_cache():
    """کش نسخه‌ی SNMP را پاک می‌کند تا هر تست از حالت تمیز شروع شود."""
    cache = getattr(_protocol, "_SNMP_VERSION_CACHE", None)
    if isinstance(cache, dict):
        cache.clear()
