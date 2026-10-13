# -*- coding: utf-8 -*-
"""
تستِ `_hp_toner_key` (TD-016 — رفعِ باگِ تطبیقِ زیررشته در مسیرِ SNMPِ HP).

چرا این باگ اهمیت دارد (سنجهٔ G.5 آن را بیرون داد): کلیدِ رنگِ کارتریج از
`prtMarkerSuppliesDescription` با «آیا kw در نام هست؟» درمی‌آمد و کلیدهای تک‌حرفی
`k/c/m/y` هر نامی را قرض می‌گرفتند. اعدادِ واقعیِ قبل از رفع:

    'Imaging Drum' → magenta      'CF470A Imaging Drum' → cyan
    'Fuser Kit' → black           'Maintenance Kit' → black

و اثرش در `collect_hp`: `has_color_toner=True` کاذب ⇒ برای پرینتر تک‌رنگی که OIDِ شمارندهٔ
رنگ ندارد، `full_color=None` و `black_white=None` (به‌جای `black_white=total`) و نمایش
درام/فیوزر به‌عنوان «تونر magenta» در UI. مسیرِ وبِ همان فایل (`_key` در
`_scrape_hp_toners`) از قبل مرزِ کلمه داشت؛ این تست دو مسیر را به یک قاعده قفل می‌کند.

دو نکتهٔ صریح:
  • هیچ دادهٔ دستگاه واقعی اینجا خوانده نمی‌شود؛ ورودی، رشته‌های نامِ کارتریج‌اند که
    در سطرهایِ واقعیِ MIB («CF470A Imaging Drum», «W110A Black», …) دیده می‌شوند.
  • آینهٔ exporter (`core/collectors/exporter_adapter.py`) همین تابع را import می‌کند،
    پس رفعِ این‌جا خودکار در آن مسیر هم اعمال می‌شود — و تستِ §۲ پایین همین را می‌سنجد.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.collectors.hp import _hp_toner_key

ROOT = Path(__file__).resolve().parents[1]

# (نامِ کارتریج/مصرفی، کلیدِ انتظار)
CASES = [
    ("Black Cartridge HP CE505A", "black"),
    ("W110A Black", "black"),
    ("Black (K) Cartridge", "black"),
    ("K", "black"),
    ("Cyan Cartridge HP CF031A", "cyan"),
    ("Toner C", "cyan"),
    ("Magenta Cartridge HP CF033A", "magenta"),
    ("Yellow Cartridge HP CF032A", "yellow"),
    # مواردِ باگ‌زده — باید هیچ رنگی نگیرند
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
    assert _hp_toner_key(name) == expected


def test_no_false_color_on_drum_or_fuser_names():
    """رگرسیونِ صریحِ TD-016 (هرکدام یک حرفِ کلیدِ تک‌حرفی را در خود داشتند)."""
    for bad in ("Imaging Drum", "CF470A Imaging Drum", "Fuser Kit", "Document Feeder Kit"):
        assert _hp_toner_key(bad) is None, bad


def test_rule_matches_the_web_helper():
    """دو مسیرِ HP یک قاعده دارند: `_key()` داخل `_scrape_hp_toners` هم مرزِ کلمه است."""
    src = (ROOT / "core" / "collectors" / "hp.py").read_text(encoding="utf-8")
    body = src[src.index("def _scrape_hp_toners"):src.index("_HP_TONER_COLOR_MAP =")]
    assert 're.search(r"\\b%s\\b" % color, n)' in body, "قاعدهٔ مسیرِ وب عوض شده — هم‌راستا کنید"
    fixed = src[src.index("def _hp_toner_key"):src.index("def collect_hp")]
    assert "if kw in n" not in fixed, "تطبیقِ زیررشته به مسیرِ SNMP برگشته"
    assert "re.search" in fixed and "re.escape" in fixed


def test_exporter_mirror_uses_the_fixed_rule():
    """آینهٔ exporter هم دیگر درام را magenta نمی‌کند (کلیدِ `toner_2` مثلِ collect_hp)."""
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
    # و مهم‌تر: «تونر رنگی» دارد حساب نمی‌شود، پس شاخهٔ رنگِ collect_hp فعال نمی‌ماند
    from core.collectors.exporter_adapter import _counters_hp

    counters = _counters_hp(dict(metrics, bro_total=[({}, 500.0)]), toners)
    assert counters["full_color"] is None and counters["black_white"] == 500
