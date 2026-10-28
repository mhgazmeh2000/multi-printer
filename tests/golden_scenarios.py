# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/golden_scenarios.py
# @brief:        Golden scenarios for the counter and enhanced collection tests.
# @details:      Responsibilities: Define step sequences and the calls each golden test replays.
#                Main API: none (no public functions or classes)
#                Side effects: none at import time
# @dependencies: internal: core, core.collectors.base, tests.golden_profiles
# @notes:        None.
# =============================================================================

"""
golden scenarios (work 1 phase C).

Two families of scenarios:
  A) ``counters_steps()'' — 5 sequential steps on *one* device; The output of each step is a fixture.
     Order: baseline → print 17 pages → counter reset → toner jump +20% → counter return after reboot.
     Each fixture is ``complete and independent'': ``input.prev'' (whole prev), ``input.db_seed''
     (state of the DB at that moment) and ``input.args'' are stored in it; So replay
     Each step on a new DB gives the same output.

  B) ``enhanced_scenarios()'' — 5 ``collect_enhanced'' scenarios on virtual machines
     (OID profile in ``tests/golden_profiles.py'').

Warning: none of these functions change domain logic; They only make input.
"""
from __future__ import annotations

import core.collectors.base as base_mod
from core import store

COUNTERS_IP = "192.0.2.10"

# Basic toner profile (common between steps until the actual toner level change is visible)
_BLACK_SUPPLY = "Black Cartridge HP CF287A"


def _toner(level: int) -> dict:
    """Helper: toner."""
    return {"black": {"level": level, "name": _BLACK_SUPPLY}}


def counters_steps():
    """5 sequential steps. ``args'' is the function to call ``prev'' at runtime."""
    return [
        {
            "name": "counters_01_baseline",
            "desc": "اولین poll (prev=None) — فقط پایه‌ریزی baseline",
            "args": lambda: dict(
                total=10_000, full_color=0, black_white=10_000,
                uptime=500_000_000,
                a3_total=1_000, a4_total=9_000,
                paper_size="Small (A4/A5)",
                paper_split={"a3": 250, "a4": 9750},
                toner_levels=_toner(60),
                supply_names={"black": _BLACK_SUPPLY},
                current_toner_level=60,
            ),
        },
        {
            "name": "counters_02_print_17_pages",
            "desc": "چاپ ۱۷ صفحه (۵ صفحه A3 + ۱۲ صفحه A4) بین دو poll",
            "args": lambda: dict(
                total=10_017, full_color=0, black_white=10_017,
                uptime=500_100_000,
                a3_total=1_005, a4_total=9_012,
                paper_size="Mixed",
                paper_split={"a3": 5, "a4": 12},
                username="print-room",
                toner_levels=_toner(58),
                supply_names={"black": _BLACK_SUPPLY},
                current_toner_level=58, prev_toner_level=60,
            ),
        },
        {
            "name": "counters_03_counter_reset",
            "desc": "ریبوت/ریست واقعی: شمارنده به ۳۰۰ افتاده و uptime کم است",
            "args": lambda: dict(
                total=300, full_color=0, black_white=300,
                uptime=1_000,
                a3_total=10, a4_total=290,
                paper_split={"a3": 10, "a4": 290},
                toner_levels=_toner(58),
                supply_names={"black": _BLACK_SUPPLY},
                current_toner_level=58, prev_toner_level=58,
            ),
        },
        {
            "name": "counters_04_toner_jump_refill",
            "desc": "جهش تونر +۲۰٪ (۵۸٪ → ۷۸٪) = نشانه‌ی ریفیل/تعویض کارتریج",
            "args": lambda: dict(
                total=350, full_color=0, black_white=350,
                uptime=61_000,
                a3_total=10, a4_total=340,
                paper_split={"a3": 0, "a4": 50},
                toner_levels=_toner(78),
                supply_names={"black": _BLACK_SUPPLY},
                current_toner_level=78, prev_toner_level=58,
            ),
        },
        {
            "name": "counters_05_counter_restore_after_reboot",
            "desc": "پس از ریبوت، دستگاه عدد قدیمی (۱۰٬۰۱۷) را برمی‌گرداند — "
                    "نباید PRINT تقلبی بسازد",
            "args": lambda: dict(
                total=10_017, full_color=0, black_white=10_017,
                uptime=2_000,
                a3_total=1_005, a4_total=9_012,
                paper_split={"a3": 0, "a4": 0},
                toner_levels=_toner(78),
                supply_names={"black": _BLACK_SUPPLY},
                current_toner_level=78, prev_toner_level=78,
            ),
        },
    ]


def build_counters_call(args: dict):
    """Call function ``_counters_event'' according to the actual signature (22 parameters)."""
    kw = dict(args)
    total = kw.pop("total")
    ip = kw.pop("ip", COUNTERS_IP)

    def _call():
        """Helper: call."""
        return base_mod._counters_event(ip, total, store._prev.get(ip), [], [], **kw)

    return _call


# ─── collect_enhanced scenarios (profiles in golden_profiles.py) ──────────
def enhanced_scenarios():
    """5 collect_enhanced scenarios; Lazy ``import'' to keep profiles separate."""
    from tests.golden_profiles import (
        hp_color_full, hp_color_empty, brother_unknown_toner,
        toshiba_offline_then_online, ecs100g_sensor,
    )
    return [
        hp_color_full(),
        hp_color_empty(),
        brother_unknown_toner(),
        toshiba_offline_then_online(),
        ecs100g_sensor(),
    ]
