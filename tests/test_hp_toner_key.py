# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_hp_toner_key.py
# @brief:        Tests for the HP toner-key matching rule (TD-016).
# @details:      Responsibilities: Verify word-boundary colour matching and mirror consistency.
#                Main API: 4 test functions
#                Side effects: none at import time
# @dependencies: internal: core.collectors.exporter_adapter, core.collectors.hp | external: pytest
# @notes:        None.
# =============================================================================

"""
`_hp_toner_key' test (TD-016 — Fix substring matching bug in HP SNMP path).

Why this bug matters (the G.5 benchmark released it): The cartridge color key from
`prtMarkerSuppliesDescription` with "Is kw in the name?" Door and single letter keys
``k/c/m/y'' borrowed any name. Actual numbers before fixing:

    'Imaging Drum' → magenta      'CF470A Imaging Drum' → cyan
    'Fuser Kit' → black           'Maintenance Kit' → black

and its effect in `collect_hp`: `has_color_toner=True` is false ⇒ for monochrome printer whose counter OID
no color, `full_color=None` and `black_white=None` (instead of `black_white=total`) and display
Drum/fuser as "magenta toner" in UI. The web path of the same file (`_key` in
`_scrape_hp_toners`) already had a word boundary; This test locks two paths to the same rule.

Two clear points:
  • No real device data is read here; The input is the cartridge name strings
    They appear in the actual MIB lines ("CF470A Imaging Drum", "W110A Black",...).
  • Exporter mirror (`core/collectors/exporter_adapter.py`) imports the same function,
    So the fix here automatically applies to that path as well — and the test in §2 below measures the same.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.collectors.hp import _hp_toner_key

ROOT = Path(__file__).resolve().parents[1]

# (cartridge/consumable name, waiting key)
CASES = [
    ("Black Cartridge HP CE505A", "black"),
    ("W110A Black", "black"),
    ("Black (K) Cartridge", "black"),
    ("K", "black"),
    ("Cyan Cartridge HP CF031A", "cyan"),
    ("Toner C", "cyan"),
    ("Magenta Cartridge HP CF033A", "magenta"),
    ("Yellow Cartridge HP CF032A", "yellow"),
    # Bugged items - should not take any color
    ("Imaging Drum", None),
    ("CF470A Imaging Drum", None),
    ("Fuser Kit", None),
    ("Document Feeder Kit", None),
    ("Maintenance Kit", None),
    ("Drum Unit", None),
    ("CF470A", None),
    ("", None),
    (None, None),
]


@pytest.mark.parametrize("name,expected", CASES)
def test_color_keys_use_word_boundaries(name, expected):
    """Verifies color keys use word boundaries."""
    assert _hp_toner_key(name) == expected


def test_no_false_color_on_drum_or_fuser_names():
    """Explicit regressions of TD-016 (each containing a single letter keyword)."""
    for bad in ("Imaging Drum", "CF470A Imaging Drum", "Fuser Kit", "Document Feeder Kit"):
        assert _hp_toner_key(bad) is None, bad


def test_rule_matches_the_web_helper():
    """The two HP paths have the same rule: `_key()` inside `_scrape_hp_toners` is a word boundary."""
    src = (ROOT / "core" / "collectors" / "hp.py").read_text(encoding="utf-8")
    body = src[src.index("def _scrape_hp_toners"):src.index("_HP_TONER_COLOR_MAP =")]
    assert 're.search(r"\\b%s\\b" % color, n)' in body, "قاعدهٔ مسیرِ وب عوض شده — هم‌راستا کنید"
    fixed = src[src.index("def _hp_toner_key"):src.index("def collect_hp")]
    assert "if kw in n" not in fixed, "تطبیقِ زیررشته به مسیرِ SNMP برگشته"
    assert "re.search" in fixed and "re.escape" in fixed


def test_exporter_mirror_uses_the_fixed_rule():
    """The mirror exporter no longer makes the drum magenta (the `toner_2` key is the same as collect_hp)."""
    from core.collectors.exporter_adapter import _toners

    metrics = {
        "toner_name_1": [({"toner_name_1": "W110A Black"}, 1.0)],
        "toner_max_1": [({}, 100.0)],
        "toner_remain_1": [({}, 62.0)],
        "toner_name_2": [({"toner_name_2": "CF470A Imaging Drum"}, 1.0)],
        "toner_max_2": [({}, 100.0)],
        "toner_remain_2": [({}, 50.0)],
    }
    toners = _toners("hp", metrics)
    assert set(toners) == {"black", "toner_2"}, sorted(toners)
    assert toners["toner_2"]["level"] == 50 and toners["toner_2"]["name"] == "CF470A Imaging Drum"
    # And more important: "color toner" is not counted, so the color branch of collect_hp does not remain active
    from core.collectors.exporter_adapter import _counters_hp

    counters = _counters_hp(dict(metrics, bro_total=[({}, 500.0)]), toners)
    assert counters["full_color"] is None and counters["black_white"] == 500
