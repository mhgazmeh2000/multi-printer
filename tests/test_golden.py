# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_golden.py
# @brief:        Golden tests: compare live collector output with recorded fixtures.
# @details:      Responsibilities: Replay each fixture and compare the output byte for byte.
#                Main API: 3 test functions
#                Side effects: none at import time
# @dependencies: internal: core, tests.golden_harness | external: pytest
# @notes:        None.
# =============================================================================

"""
golden tests (work 1 phase C).

Each fixture in ``tests/fixtures/golden/*.json'' is replayed once **independently** and
All four parts are asserted:

    input → complete input (args + prev + db_seed, or printer/phases)
    expected         → return_value + events + new_prev
    db_state_after → rows logs/printer_counters/cartridge_state/yield_samples/toner_snapshots_v2
    snmp_call_sequence → exact sequence of read OIDs (and http_calls)

Specifications:
  • Completely hermetic: no real socket/network/clock in the path (FakeSnmpTransport + frozen_time).
  • Fast: each test is a few hundred milliseconds (no network timeout/retry).
  • Cleanup: The temporary DB is created in ``tmp_path'' and deleted after the test.

Execution:
    python -m pytest tests/test_golden.py -q
    python -m pytest tests/ -q # The whole set (210 previous tests + these)
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

# This file is only run with pytest (CI: 21 test path).
# empty load_tests ⇒ under `python -m unittest discover` zero tests from this file
# is removed (the tests are in pytest function style) and counting does not occur again.
pytestmark = [pytest.mark.pytest_only, pytest.mark.sqlite_only]  # golden = SQLite byte byte fixture (docs/phase-e-plan.md §5.3)


def load_tests(loader, tests, pattern):  # noqa: ARG001 - unittest protocol signature
    """Unittest protocol: This file is not detected/executed by unittest."""
    return unittest.TestSuite()


FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))
COUNTERS_FIXTURES = [p for p in FIXTURES if p.stem.startswith("counters_")]
ENHANCED_FIXTURES = [p for p in FIXTURES if p.stem.startswith("enhanced_")]


def _load(path):
    """Helper: load."""
    return json.loads(path.read_text(encoding="utf-8"))


def test_fixture_inventory():
    """10 gold fixtures must be available (5 counters + 5 collect_enhanced scenarios)."""
    assert len(FIXTURES) == 10, f"expected 10 golden fixtures, found {len(FIXTURES)}"
    assert len(COUNTERS_FIXTURES) == 5
    assert len(ENHANCED_FIXTURES) == 5


@pytest.mark.parametrize("path", COUNTERS_FIXTURES, ids=[p.stem for p in COUNTERS_FIXTURES])
def test_golden_counters_event(path, tmp_path):
    """Independent playback of each ``_counters_event'' step with zero difference in all 4 sections."""
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
    """Independent replay of each ``collect_enhanced'' scenario (all phases) with zero difference."""
    fixture = _load(path)
    with isolated_db(str(tmp_path)):
        observed = replay_enhanced(fixture)

    expected = json_safe({"steps": fixture["expected"]["steps"]})
    diff = first_diff(observed, expected)
    assert diff is None, f"{path.name}: {diff}"
