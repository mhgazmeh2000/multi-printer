import os
import tempfile
import unittest
from unittest import mock

import core.snmp.protocol as proto
from core import store
from core.database import init_db
import core.poller as poller


def _build_v2c_response(community: str, rid: int, value: int) -> bytes:
    """یک پاسخ SNMP v2c معتبر با request-id دلخواه می‌سازد."""
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
    def __init__(self, payload: bytes):
        self.payload = payload

    def settimeout(self, *_):
        pass

    def sendto(self, *_):
        pass

    def recvfrom(self, *_):
        return self.payload, ("10.0.0.9", 161)

    def close(self):
        pass


class SnmpCompatTests(unittest.TestCase):
    """سازگاری با پرینت‌سرورهای قدیمی و جلوگیری از offline کاذب."""

    def setUp(self):
        with proto._version_cache_lock:
            proto._SNMP_VERSION_CACHE.clear()

    tearDown = setUp

    # ۱. پاسخ با request-id ناسازگار باید پذیرفته شود (پرینت‌سرورهای قدیمی)
    def test_mismatched_request_id_is_accepted(self):
        payload = _build_v2c_response("public", rid=111, value=123456)
        with mock.patch.object(proto.socket, "socket", return_value=_FakeSock(payload)):
            val = proto.snmp_get("10.0.0.9", "1.3.6.1.2.1.1.3.0", "public",
                                 timeout=0.5, request_id=1, version=2)
        self.assertEqual(val, 123456)

    # ۲. شکست خواندن یک OID نباید کش نسخه را negative کند (cache-poisoning)
    def test_oid_failure_does_not_poison_version_cache(self):
        ip = "10.0.0.10"
        proto._set_cached_version(ip, "public", 2, proto._POSITIVE_CACHE_TTL)
        with mock.patch.object(proto, "snmp_get", return_value=None) as m:
            res = proto.snmp_get_with_fallback(ip, "1.3.6.1.9.9.9.9", "public", timeout=0.5)
        self.assertIsNone(res)
        found, ver = proto._get_cached_version(ip, "public")
        self.assertTrue(found, "کش نسخه نباید پاک شود")
        self.assertEqual(ver, 2, "نسخهی معتبر کش شده باید حفظ شود (نه negative)")
        # هر دو نسخه امتحان شده‌اند ولی کش دست نخورده
        self.assertEqual(m.call_count, 2)

    # ۳. تشخیص نسخه دو گذر دارد: اگر پاسخ فقط در timeout بالا بیاید، پیدا شود
    def test_detect_second_pass_with_longer_timeout(self):
        ip = "10.0.0.11"
        calls = []

        def fake_snmp_get(ip_, oid, community, port, timeout, version=2):
            calls.append(timeout)
            # پاسخ فقط وقتی timeout بزرگ است (گذر دوم)
            return 42 if timeout > 2 else None

        with mock.patch.object(proto, "snmp_get", side_effect=fake_snmp_get):
            ver = proto._detect_snmp_version(ip, "public", probe_timeout=1.5)
        self.assertIsNotNone(ver, "باید در گذر دوم (timeout بزرگ‌تر) پیدا شود")
        self.assertTrue(any(t > 2 for t in calls), "گذر دوم با timeout بزرگ‌تر اجرا نشده")

    # ۴. وقتی همه‌چیز واقعاً پاسخ نمی‌دهد، negative-cache می‌ماند
    def test_detect_negative_cache_when_dead(self):
        ip = "10.0.0.12"
        with mock.patch.object(proto, "snmp_get", return_value=None):
            ver = proto._detect_snmp_version(ip, "public", probe_timeout=0.2)
        self.assertIsNone(ver)
        found, cached = proto._get_cached_version(ip, "public")
        self.assertTrue(found)
        self.assertIsNone(cached)


class PollerGraceTests(unittest.TestCase):
    """پنجره‌ی ارفاق آفلاین: یک UDP drop نباید دستگاه را آفلاین کند."""

    IP = "10.20.30.40"

    def setUp(self):
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
        self.p1.stop()
        self.p2.stop()
        with poller._health_fail_lock:
            poller._health_fail_counts.clear()
        with store.data_lock:
            store.printer_data.pop(self.IP, None)
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _printer(self):
        return {"ip": self.IP, "name": "HP X", "community": "public", "brand": "hp"}

    def test_first_failure_keeps_previous_online_state(self):
        result = poller.collect(self._printer())
        self.assertTrue(result.get("online"), "اولین شکست نباید آفلاین کند")
        self.assertTrue(result.get("degraded"), "باید degraded علامت بخورد")
        with poller._health_fail_lock:
            self.assertEqual(poller._health_fail_counts.get(self.IP), 1)

    def test_second_consecutive_failure_goes_offline(self):
        poller.collect(self._printer())           # grace
        result = poller.collect(self._printer())  # offline
        self.assertFalse(result.get("online"), "شکست دوم متوالی باید آفلاین کند")

    def test_success_resets_failure_counter(self):
        poller.collect(self._printer())  # fail 1 → grace
        # موفقیت مجدد
        with mock.patch.object(poller, "snmp_get_first", return_value=("OK", "1.3.6.1.2.1.1.1.0")), \
             mock.patch.object(poller, "collect_enhanced", return_value={"ip": self.IP, "online": True}):
            ok = poller.collect(self._printer())
        self.assertTrue(ok.get("online"))
        with poller._health_fail_lock:
            self.assertIsNone(poller._health_fail_counts.get(self.IP))
        # یک شکست جدید دوباره از grace شروع می‌کند نه آفلاین فوری
        result = poller.collect(self._printer())
        self.assertTrue(result.get("degraded"))


if __name__ == "__main__":
    unittest.main()
