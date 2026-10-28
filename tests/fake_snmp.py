# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/fake_snmp.py
# @brief:        Fake SNMP transport used by golden tests.
# @details:      Responsibilities: Return deterministic SNMP values so tests run without a network.
#                Main API: FakeSnmpTransport
#                Side effects: network I/O
# @dependencies: internal: core.snmp
# @notes:        None.
# =============================================================================

"""
FakeSnmpTransport — fake and *definitive* SNMP layer for golden tests.

Design (according to the decision of Phase C):
  • Only two functions of the lowest layer in ``core.snmp.protocol'' are patched:
    ``snmp_get'' and ``snmp_getnext''.
  • No higher-level functions are patched; So the whole actual route
    (``snmp_get_with_fallback'' → version cache/detection, ``snmp_get_first'',
    ``snmp_get_bulk'', ``snmp_walk'', and the collectors logic) run intact.
  • Definitiveness: no sockets are opened, no timeouts/networks are involved;
    Answers are read from the device profile.

Device profile::

    {
      "127.0.0.1": {
          "version": 2, # The version that the device responds to
          "oids": {"1.3.6.1.2.1.1.1.0": "HP LaserJet M527", ...},
          "slow_oids": {...}, # optional: only respond on the second pass
      }
    }

Any OID not in the profile ⇒ ``None'' (ie ``device did not respond''), which behavior
True reproduces multi-copy/protected devices.
"""
from __future__ import annotations

import contextlib
import copy
from unittest import mock

from core.snmp import protocol as _protocol


def oid_key(oid: str):
    """Numeric (not string) comparison key for OIDs."""
    return tuple(int(p) for p in oid.strip(".").split(".") if p != "")


class FakeSnmpTransport:
    """Helper class: fake snmp transport."""
    def __init__(self, devices: dict, record_calls: bool = True):
        """Initialize the instance."""
        self.devices = copy.deepcopy(devices)
        self.record_calls = record_calls
        self.calls = []   # [{"fn": "get"|"getnext", "ip":…, "oid":…, "version":…}]

    # Internal
    def _device(self, ip):
        """Helper: device."""
        return self.devices.get(ip)

    def _lookup(self, ip, oid, version, second_pass=False):
        """Helper: lookup."""
        dev = self._device(ip)
        if not dev:
            return None                      # Anonymous IP ⇒ Offline
        want = dev.get("version", 2)
        if want is not None and version != want:
            return None                      # Incorrect version ⇒ unanswered (like reality)
        pool = dev.get("oids", {})
        if oid in pool:
            return pool[oid]
        if second_pass:
            return dev.get("slow_oids", {}).get(oid)
        return None

    # ── API equivalent to core.snmp.protocol ───────────────────────────────────────
    def snmp_get(self, ip, oid, community="public", port=161, timeout=3.0,
                 request_id=1, version=2):
        """Helper: snmp get."""
        if self.record_calls:
            self.calls.append({"fn": "get", "ip": ip, "oid": oid, "version": version})
        return self._lookup(ip, oid, version)

    def snmp_getnext(self, ip, oid, community="public", port=161, timeout=3.0,
                     request_id=1, version=2):
        """Helper: snmp getnext."""
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

    # ── Install/uninstall patch ──────────────────────────────────────────────────
    @contextlib.contextmanager
    def installed(self):
        """``snmp_get''/``snmp_getnext'' replace the protocol module temporarily."""
        with mock.patch.object(_protocol, "snmp_get", side_effect=self.snmp_get), \
             mock.patch.object(_protocol, "snmp_getnext", side_effect=self.snmp_getnext):
            yield self

    def call_sequence(self):
        """The sequence of calls read (for the snmp_call_sequence section of the fixture)."""
        return [f'{c["fn"]}:{c["oid"]}@v{c["version"]}' for c in self.calls]


def clear_version_cache():
    """Clears the SNMP version cache so that each test starts clean."""
    cache = getattr(_protocol, "_SNMP_VERSION_CACHE", None)
    if isinstance(cache, dict):
        cache.clear()
