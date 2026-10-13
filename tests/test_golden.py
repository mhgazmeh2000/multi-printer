# -*- coding: utf-8 -*-
"""
تست‌های golden (کار ۱ فاز C).

هر fixture در ``tests/fixtures/golden/*.json`` یک بار و **مستقل** بازپخش می‌شود و
هر چهار بخش آن assert می‌شود:

    input            → ورودی کامل (args + prev + db_seed، یا printer/phases)
    expected         → return_value + events + new_prev
    db_state_after   → ردیف‌های logs/printer_counters/cartridge_state/yield_samples/toner_snapshots_v2
    snmp_call_sequence → توالی دقیق OIDهای خوانده‌شده (و http_calls)

مشخصات:
  • کاملاً hermetic: هیچ سوکت/شبکه/ساعت واقعی در مسیر نیست (FakeSnmpTransport + frozen_time).
  • سریع: هر تست چند صد میلی‌ثانیه (بدون timeout/retry شبکه).
  • پاک‌سازی: DB موقت در ``tmp_path`` ساخته و بعد از تست حذف می‌شود.

اجرا:
    python -m pytest tests/test_golden.py -q
    python -m pytest tests/ -q            # کل مجموعه (۲۱۰ تست قبلی + این‌ها)
"""
from __future__ import annotations

import json
import unittest

import pytest

from core import store
from tests.golden_harness import (
    FIXTURE_DIR, first_diff, frozen_time, isolated_db, json_safe, replay_counters,
    replay_enhanced, seed_db,
)

# این فایل فقط با pytest اجرا میشود (CI: مسیر ۲۱ تستی).
# load_tests خالی ⇒ تحت `python -m unittest discover` صفر تست از این فایل
# برداشته میشود (تستها به سبک تابعی pytest هستند) و شمارش دوباره رخ نمیدهد.
pytestmark = [pytest.mark.pytest_only, pytest.mark.sqlite_only]  # golden = fixture بایت‌به‌بایتِ SQLite (docs/phase-e-plan.md §۵.۳)


def load_tests(loader, tests, pattern):  # noqa: ARG001 - امضای پروتکل unittest
    """پروتکل unittest: این فایل با unittest کشف/اجرا نمیشود."""
    return unittest.TestSuite()


FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))
COUNTERS_FIXTURES = [p for p in FIXTURES if p.stem.startswith("counters_")]
ENHANCED_FIXTURES = [p for p in FIXTURES if p.stem.startswith("enhanced_")]


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_fixture_inventory():
    """۱۰ fixture طلایی باید موجود باشند (۵ شمارنده + ۵ سناریوی collect_enhanced)."""
    assert len(FIXTURES) == 10, f"expected 10 golden fixtures, found {len(FIXTURES)}"
    assert len(COUNTERS_FIXTURES) == 5
    assert len(ENHANCED_FIXTURES) == 5


@pytest.mark.parametrize("path", COUNTERS_FIXTURES, ids=[p.stem for p in COUNTERS_FIXTURES])
def test_golden_counters_event(path, tmp_path):
    """بازپخش مستقل هر گام ``_counters_event`` با تفاضل صفر در هر ۴ بخش."""
    fixture = _load(path)
    with isolated_db(str(tmp_path)), frozen_time():
        seed_db(fixture["input"]["db_seed"])
        store._prev.set(fixture["input"]["ip"], fixture["input"]["prev"])
        observed = json_safe(replay_counters(fixture))

    expected = {
        "return_value": json_safe(fixture["expected"]["return_value"]),
        "expected_events": json_safe(fixture["expected"]["events"]),
        "new_prev": json_safe(fixture["expected"]["new_prev"]),
        "db_state_after": json_safe(fixture["db_state_after"]),
        "snmp_call_sequence": json_safe(fixture["snmp_call_sequence"]),
    }
    diff = first_diff(observed, expected)
    assert diff is None, f"{path.name}: {diff}"


@pytest.mark.parametrize("path", ENHANCED_FIXTURES, ids=[p.stem for p in ENHANCED_FIXTURES])
def test_golden_collect_enhanced(path, tmp_path):
    """بازپخش مستقل هر سناریوی ``collect_enhanced`` (همه‌ی فازها) با تفاضل صفر."""
    fixture = _load(path)
    with isolated_db(str(tmp_path)):
        observed = replay_enhanced(fixture)

    expected = json_safe({"steps": fixture["expected"]["steps"]})
    diff = first_diff(observed, expected)
    assert diff is None, f"{path.name}: {diff}"
