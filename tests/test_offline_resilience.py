# =============================================================================
# @file:         tests/test_offline_resilience.py
# @brief:        Tests for SNMP compatibility and the poller's offline grace window.
# @details:      Responsibilities: Verify that a single lost cycle does not mark a printer offline.
#                Main API: _FakeSock, SnmpCompatTests, PollerGraceTests
#                Side effects: network I/O
# @dependencies: internal: core, core.database, core.poller, core.snmp.protocol
# @notes:        Can be run directly as a script.
# =============================================================================

"""Tests for SNMP compatibility and the poller's offline grace window (one lost cycle must not mark a printer offline)."""

import os
import tempfile
import unittest
from unittest import mock

import core.snmp.protocol as proto
from core import store
from core.database import init_db
import core.poller as poller


def _build_v2c_response(community: str, rid: int, value: int) -> bytes:
    """Creates a valid SNMP v2c response with an arbitrary request-id."""
    enc_len = proto.encode_length
    oid_bytes = proto.encode_oid("1.3.6.1.2.1.1.3.0")
    oid_tlv = b"\x06" + enc_len(len(oid_bytes)) + oid_bytes
    val_tlv = b"\x43" + enc_len(4) + value.to_bytes(4, "big")  # TimeTicks
    varbind = b"\x30" + enc_len(len(oid_tlv) + len(val_tlv)) + oid_tlv + val_tlv
    vbl = b"\x30" + enc_len(len(varbind)) + varbind
    pdu_body = (
        b"\x02" + enc_len(1) + bytes([rid])
        + b"\x02\x01\x00\x02\x01\x00"
        + vbl
    )
    pdu = b"\xa2" + enc_len(len(pdu_body)) + pdu_body
    cb = community.encode()
    comm_tlv = b"\x04" + enc_len(len(cb)) + cb
    body = b"\x02\x01\x01" + comm_tlv + pdu
    return b"\x30" + enc_len(len(body)) + body


class _FakeSock:
    """Helper class: fake sock."""
    def __init__(self, payload: bytes):
        """Initialize the instance."""
        self.payload = payload

    def settimeout(self, *_):
        """Helper: settimeout."""
        pass

    def sendto(self, *_):
        """Helper: sendto."""
        pass

    def recvfrom(self, *_):
        """Helper: recvfrom."""
        return self.payload, ("10.0.0.9", 161)

    def close(self):
        """Helper: close."""
        pass


class SnmpCompatTests(unittest.TestCase):
    """Compatible with old print servers and prevent false offline."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        with proto._version_cache_lock:
            proto._SNMP_VERSION_CACHE.clear()

    tearDown = setUp

    # 1. Responses with inconsistent request-id must be accepted (old print servers)
    def test_mismatched_request_id_is_accepted(self):
        """Verifies mismatched request id is accepted."""
        payload = _build_v2c_response("public", rid=111, value=123456)
        with mock.patch.object(proto.socket, "socket", return_value=_FakeSock(payload)):
            val = proto.snmp_get("10.0.0.9", "1.3.6.1.2.1.1.3.0", "public",
                                 timeout=0.5, request_id=1, version=2)
        self.assertEqual(val, 123456)

    # 2. Failure to read an OID should not make the version cache negative (cache-poisoning).
    def test_oid_failure_does_not_poison_version_cache(self):
        """Verifies oid failure does not poison version cache."""
        ip = "10.0.0.10"
        proto._set_cached_version(ip, "public", 2, proto._POSITIVE_CACHE_TTL)
        with mock.patch.object(proto, "snmp_get", return_value=None) as m:
            res = proto.snmp_get_with_fallback(ip, "1.3.6.1.9.9.9.9", "public", timeout=0.5)
        self.assertIsNone(res)
        found, ver = proto._get_cached_version(ip, "public")
        self.assertTrue(found, "کش نسخه نباید پاک شود")
        self.assertEqual(ver, 2, "نسخهی معتبر کش شده باید حفظ شود (نه negative)")
        # Both versions have been tried, but the cache is intact
        self.assertEqual(m.call_count, 2)

    # 3. Version detection is two-pass: found if the response only raises the timeout
    def test_detect_second_pass_with_longer_timeout(self):
        """Verifies detect second pass with longer timeout."""
        ip = "10.0.0.11"
        calls = []

        def fake_snmp_get(ip_, oid, community, port, timeout, version=2):
            """Helper: fake snmp get."""
            calls.append(timeout)
            # Answer only when timeout is big (second pass)
            return 42 if timeout > 2 else None

        with mock.patch.object(proto, "snmp_get", side_effect=fake_snmp_get):
            ver = proto._detect_snmp_version(ip, "public", probe_timeout=1.5)
        self.assertIsNotNone(ver, "باید در گذر دوم (timeout بزرگ‌تر) پیدا شود")
        self.assertTrue(any(t > 2 for t in calls), "گذر دوم با timeout بزرگ‌تر اجرا نشده")

    # 4. When things don't really respond, negative-cache stays
    def test_detect_negative_cache_when_dead(self):
        """Verifies detect negative cache when dead."""
        ip = "10.0.0.12"
        with mock.patch.object(proto, "snmp_get", return_value=None):
            ver = proto._detect_snmp_version(ip, "public", probe_timeout=0.2)
        self.assertIsNone(ver)
        found, cached = proto._get_cached_version(ip, "public")
        self.assertTrue(found)
        self.assertIsNone(cached)


class PollerGraceTests(unittest.TestCase):
    """Offline friendship window: A UDP drop should not take the device offline."""

    IP = "10.20.30.40"

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        with poller._health_fail_lock:
            poller._health_fail_counts.clear()
        with store.data_lock:
            store.printer_data[self.IP] = {
                "ip": self.IP, "name": "HP X", "brand": "hp",
                "online": True, "counters": {"total": 1000}, "toners": {},
            }
        self.p1 = mock.patch.object(poller, "snmp_get_first", return_value=(None, None))
        self.p2 = mock.patch.object(poller, "_probe_snmp_agent_reachable", return_value=None)
        self.p1.start()
        self.p2.start()

    def tearDown(self):
        """Release the resources that each test in this class created."""
        self.p1.stop()
        self.p2.stop()
        with poller._health_fail_lock:
            poller._health_fail_counts.clear()
        with store.data_lock:
            store.printer_data.pop(self.IP, None)
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _printer(self):
        """Helper: printer."""
        return {"ip": self.IP, "name": "HP X", "community": "public", "brand": "hp"}

    def test_first_failure_keeps_previous_online_state(self):
        """Verifies first failure keeps previous online state."""
        result = poller.collect(self._printer())
        self.assertTrue(result.get("online"), "اولین شکست نباید آفلاین کند")
        self.assertTrue(result.get("degraded"), "باید degraded علامت بخورد")
        with poller._health_fail_lock:
            self.assertEqual(poller._health_fail_counts.get(self.IP), 1)

    def test_second_consecutive_failure_goes_offline(self):
        """Verifies second consecutive failure goes offline."""
        poller.collect(self._printer())           # grace
        result = poller.collect(self._printer())  # offline
        self.assertFalse(result.get("online"), "شکست دوم متوالی باید آفلاین کند")

    def test_success_resets_failure_counter(self):
        """Verifies success resets failure counter."""
        poller.collect(self._printer())  # fail 1 → grace
        # Success again
        with mock.patch.object(poller, "snmp_get_first", return_value=("OK", "1.3.6.1.2.1.1.1.0")), \
             mock.patch.object(poller, "collect_enhanced", return_value={"ip": self.IP, "online": True}):
            ok = poller.collect(self._printer())
        self.assertTrue(ok.get("online"))
        with poller._health_fail_lock:
            self.assertIsNone(poller._health_fail_counts.get(self.IP))
        # A new failure starts again from grace, not instant offline
        result = poller.collect(self._printer())
        self.assertTrue(result.get("degraded"))


if __name__ == "__main__":
    unittest.main()
