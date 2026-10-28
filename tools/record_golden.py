#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/record_golden.py
# @brief:        Records golden fixtures (work 1, phase C) and checks them.
# @details:      Responsibilities: Generate fixtures from the golden scenarios or verify them with
#                --check.
#                Main API: record_counters(), record_enhanced(), check_fixtures(), main()
#                Side effects: none at import time
# @dependencies: internal: core, core.enhanced_collector, tests.golden_harness,
#                tests.golden_profiles, tests.golden_scenarios
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Recording of golden fixtures (work 1 phase C).

Execution:
    python tools/record_golden.py --only counters # Record 5 step counters
    python tools/record_golden.py --only enhanced # Record 5 collect_enhanced scenarios
    python tools/record_golden.py --only all --check # record + playback and zero-difference comparison

Modes:
  • Default (hermetic): SNMP responds via FakeSnmpTransport (no socket).
  • ``--with-simulator'': the local UDP simulator (tools/mock_snmp_device.py) is loaded and
    Recording is done from the same profile; To validate "profile match with
    "Actual behavior of the protocol" is useful, but not necessary for everyday testing.

No domain logic changes: only inputs are fixed and outputs are recorded.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.golden_harness import (  # noqa: E402
    FROZEN_ISO, capture_seed, event_recorder, first_diff, frozen_time, isolated_db,
    json_safe, normalize_tree, replay_counters, replay_enhanced, snapshot_db,
    write_fixture,
)
from tests.golden_scenarios import COUNTERS_IP, build_counters_call, counters_steps  # noqa: E402
from core import store  # noqa: E402


# ─── Recording: counters (5 sequential steps, one DB)
def record_counters(verbose=True):
    """Record the counter golden fixtures."""

    results = []
    with tempfile.TemporaryDirectory(prefix="pg-golden-") as tmp:
        with isolated_db(tmp), frozen_time():
            for step in counters_steps():
                db_seed = capture_seed()
                args = step["args"]()
                prev_before = store._prev.get(COUNTERS_IP)
                with event_recorder() as rec:
                    ret = build_counters_call(args)()
                prev_after = store._prev.get(COUNTERS_IP)
                fixture = {
                    "kind": "counters_event",
                    "name": step["name"],
                    "desc": step["desc"],
                    "input": {
                        "ip": COUNTERS_IP,
                        "args": args,
                        "prev": prev_before,          # Raw input (deliberately unnormalized)
                        "db_seed": db_seed,           # DB status at that moment (raw, with id)
                    },
                    "expected": {
                        "return_value": ret,
                        "events": normalize_tree(rec),
                        "new_prev": normalize_tree(prev_after),
                    },
                    "db_state_after": snapshot_db(),
                    "snmp_call_sequence": [],
                }
                path = write_fixture(step["name"], json_safe(fixture))
                results.append((path, fixture))
                if verbose:
                    print(f"  ✔ {path.name}  events={len(fixture['expected']['events'])}")
    return results


# ─── Record: collect_enhanced (5 scenarios, each separate DB) ──────────────────────
def record_enhanced(verbose=True):
    """Record the enhanced-collector golden fixtures."""

    import copy

    from tests.golden_harness import FakeHttp, clear_module_caches
    from tests.golden_profiles import build_transport
    from tests.golden_scenarios import enhanced_scenarios
    import core.enhanced_collector as ec

    results = []
    for sc in enhanced_scenarios():
        with tempfile.TemporaryDirectory(prefix="pg-golden-") as tmp:
            transport = build_transport(sc)
            http = FakeHttp(sc.get("http", ()))
            with isolated_db(tmp), frozen_time(), transport.installed(), http.installed():
                steps = []
                for phase in sc["phases"]:
                    # Profile of each phase (Toshiba scenario: phase 1 offline, phase 2 online)
                    transport.devices = copy.deepcopy(phase.get("devices") or {})
                    clear_module_caches()
                    transport.calls.clear()
                    http.calls.clear()
                    ret = ec.collect_enhanced(dict(sc["printer"]), save_to_db=True)
                    steps.append({
                        "phase": phase["name"],
                        "return_value": json_safe(normalize_tree(ret)),
                        "db_state_after": snapshot_db(),
                        "snmp_call_sequence": transport.call_sequence(),
                        "http_calls": list(http.calls),
                    })
            fixture = {
                "kind": "collect_enhanced",
                "name": sc["name"],
                "desc": sc["desc"],
                "input": {
                    "printer": sc["printer"],
                    "phases": [{"name": ph["name"], "devices": ph.get("devices") or {}}
                               for ph in sc["phases"]],
                    "http_responses": [list(x) for x in sc.get("http", ())],
                },
                "expected": {"steps": steps},
                "frozen_now": FROZEN_ISO,   # Locked moment of harness (E.0.3)
            }
            path = write_fixture(sc["name"], json_safe(fixture))
            results.append((path, fixture))
            if verbose:
                print(f"  ✔ {path.name}  phases={len(steps)} "
                      f"snmp_calls={[len(s['snmp_call_sequence']) for s in steps]}")
    return results


# Replay and compare
def check_fixtures(fixtures, verbose=True):
    """Hermetic playback of each fixture and zero-difference comparison."""
    failures = 0
    for path, fixture in fixtures:
        kind = fixture["kind"]
        with tempfile.TemporaryDirectory(prefix="pg-golden-chk-") as tmp:
            if kind == "counters_event":
                from tests.golden_harness import seed_db
                from core import store as _store
                with isolated_db(tmp), frozen_time():
                    seed_db(fixture["input"]["db_seed"])
                    _store._prev.set(fixture["input"]["ip"], fixture["input"]["prev"])
                    observed = json_safe(replay_counters(fixture))
                expected = {
                    "return_value": json_safe(fixture["expected"]["return_value"]),
                    "expected_events": json_safe(fixture["expected"]["events"]),
                    "new_prev": json_safe(fixture["expected"]["new_prev"]),
                    "db_state_after": json_safe(fixture["db_state_after"]),
                    "snmp_call_sequence": json_safe(fixture["snmp_call_sequence"]),
                }
            else:
                with isolated_db(tmp):          # Temporary DB — Never run on repository logs.db
                    observed = replay_enhanced(fixture)
                expected = json_safe({"steps": fixture["expected"]["steps"]})

        diff = first_diff(observed, expected)
        if diff:
            failures += 1
            print(f"  ✘ {path.name}: {diff}")
        elif verbose:
            print(f"  ✔ {path.name}: تفاضل صفر")
    return failures


def main():
    """Command-line entry point: record or check the golden fixtures."""

    ap = argparse.ArgumentParser(description="ضبط/بررسی fixtureهای طلایی PrintGuard")
    ap.add_argument("--only", choices=["counters", "enhanced", "all"], default="all")
    ap.add_argument("--check", action="store_true", help="پس از ضبط، بازپخش و مقایسه کن")
    ap.add_argument("--with-simulator", action="store_true",
                    help="شبیه‌ساز UDP محلی را هم بالا بیاور (اعتبارسنجی پروفایل)")
    args = ap.parse_args()

    os.chdir(ROOT)
    fixtures = []
    if args.only in ("counters", "all"):
        print("ضبط گام‌های _counters_event ...")
        fixtures += record_counters()
    if args.only in ("enhanced", "all"):
        print("ضبط سناریوهای collect_enhanced ...")
        fixtures += record_enhanced()

    print(f"\n{len(fixtures)} fixture نوشته شد در tests/fixtures/golden/")
    if args.check:
        print("\nبازپخش و مقایسه:")
        failures = check_fixtures(fixtures)
        print(f"\nنتیجه: {'همه تفاضل-صفر ✔' if failures == 0 else f'{failures} عدم‌تطابق ✘'}")
        return 1 if failures else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
