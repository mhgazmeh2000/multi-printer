# -*- coding: utf-8 -*-
"""
تست‌های `core/collectors/exporter_adapter.py` (گام G.5.3).

داده، fixtureِ دست‌نویس است (`tests/fixtures/exporter_response_*.txt`) و انتظارها
عددِ صریح‌اند — نه دوباره‌حسابیِ فرمولِ adapter، وگرنه تست فقط خودش را تأیید می‌کرد.
همین «عددِ صریح» بودن، آینه‌بودنِ فرمول‌ها را با `collect_hp`/`collect_canon` می‌سنجد:

  ۱) کلیدهای خروجی = کلیدهایِ returnِ خودِ collectorها (از AST، نه از حافظه)؛
  ۲) شمارنده‌ها/تونر/سینی/هشدار با همان آستانه‌ها و همان defaultها؛
  ۳) `ignore_values` دقیقاً همان ۳۱ متریکِ `monitoring/snmp.yml` و نه یکی بیشتر/کمتر؛
  ۴) مسیرهای None: برندِ آینه‌سازی‌نشده، پاسخِ خالی، خطای exporter — و اینکه هیچ
     اثرِ جانبی (رویداد/DB/وب) در adapter نیست.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.collectors import exporter_adapter as ea
from core.exporter_client import ExporterError

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SNMP_YML = ROOT / "monitoring" / "snmp.yml"


def metrics_for(*names: str) -> dict:
    from core.exporter_client import parse_prometheus_text

    merged: dict = {}
    for name in names:
        merged.update(parse_prometheus_text((FIXTURES / name).read_text(encoding="utf-8")))
    return merged


@pytest.fixture(autouse=True)
def no_web_by_default(monkeypatch):
    """وب‌اسکرپ‌ها در *سراسرِ* این فایل بی‌اثرند، مگر تستی صریحاً جایشان را بگیرد.

    بدونِ این، هر تستِ مسیرِ کامل (collect_via_exporter) به 172.16.0.5 زنگ می‌زد —
    یعنی سوئیتِ آفلاینِ repo به شبکه وابسته می‌شد.
    """
    originals = {"hp": ea._scrape_hp_toners, "canon": ea._scrape_canon_toners,
                  "key": ea._hp_toner_key}      # تستِ هویت این‌ها را می‌خواهد
    calls = []
    monkeypatch.setattr(ea, "_scrape_hp_toners", lambda ip, timeout=4.0: calls.append(("hp", ip)) or None)
    monkeypatch.setattr(ea, "_scrape_canon_toners",
                        lambda ip, timeout=3.0: calls.append(("canon", ip)) or None)
    ea._WARNED_UNSUPPORTED.clear()          # هشدارِ «یک‌بار» بینِ تست‌ها نشت نکند
    originals["calls"] = calls
    return originals


@pytest.fixture()
def hp_metrics():
    return metrics_for("exporter_response_sample.txt", "exporter_response_std.txt")


@pytest.fixture()
def canon_metrics():
    return metrics_for("exporter_response_canon.txt", "exporter_response_canon_std.txt")


def _returned_keys(collector_file: str, func_name: str) -> set[str]:
    """کلیدهای dictِ returnِ مسیرِ موفقِ یک collector — از AST، بدونِ import."""
    tree = ast.parse((ROOT / "core" / "collectors" / collector_file).read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == func_name)
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
    assert tries, f"{func_name} ساختارِ try/except ندارد"
    last = tries[0].body[-1]
    assert isinstance(last, ast.Return) and isinstance(last.value, ast.Dict)
    return {k.value for k in last.value.keys}


# ── ۱) شکل ────────────────────────────────────────────────────────────────
def test_hp_shape_has_exactly_the_collect_hp_keys(hp_metrics):
    shape = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics, name="HP-05")
    assert shape is not None
    assert set(shape) == _returned_keys("hp.py", "collect_hp")
    assert set(shape["counters"]) == {"total", "full_color", "black_white", "printer", "copy", "fax"}
    assert shape["brand"] == "hp" and shape["online"] is True
    assert shape["name"] == "HP-05" and shape["paper_sizes"] == {}


def test_canon_shape_has_exactly_the_collect_canon_keys(canon_metrics):
    shape = ea.exporter_to_collector_shape("172.16.0.9", "canon", canon_metrics, name="iR")
    assert set(shape) == _returned_keys("canon.py", "collect_canon")
    assert shape["brand"] == "canon"


def test_name_falls_back_to_ip_and_poll_ms_is_zero_without_start(hp_metrics):
    shape = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics)
    assert shape["name"] == "172.16.0.5" and shape["poll_ms"] == 0
    assert shape["last_poll"].endswith("Z")            # قراردادِ timestamp مخزن


# ── ۲) اعداد ──────────────────────────────────────────────────────────────
def test_hp_counters_are_the_fixture_arithmetic(hp_metrics):
    counters = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics)["counters"]
    # bro_total 12345 · hp_print_color 1200 → black_white = 12345 - 1200 (فرمولِ hp)
    assert counters["total"] == 12345
    assert counters["full_color"] == 1200
    assert counters["black_white"] == 11145
    assert counters["printer"] == 12345
    assert counters["copy"] == 930          # hp_copy_mono 900 + hp_copy_color 30
    assert counters["fax"] is None          # hp_fax در پاسخ نیست ⇒ defaultِ -1 ⇒ None


def test_hp_device_strings_and_uptime(hp_metrics):
    device = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics)["device"]
    assert device == {
        "model": "HP LaserJet Pro M402dn",
        "serial": "CNCVC0245A",
        "firmware": "20190418",
        "uptime_str": "1d 01:40",          # 9245678/100 = 92456 s
    }


def test_hp_toner_percent_and_status(hp_metrics):
    toners = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics)["toners"]
    assert toners["black"] == {
        "level": 42, "status": "ok", "name": "Black Toner", "remaining": 1344, "max": 3200,
    }


def test_hp_drum_slot_uses_the_generic_key(hp_metrics):
    """اسلاتِ ۲ با نامِ «Imaging Drum» هیچ رنگی نمی‌گیرد ⇒ کلیدِ `toner_2`.

    این تست قبلاً رفتارِ باگ‌زده را pin کرده بود (TD-016: تطبیقِ زیررشته، «m» در
    «iMaging» ⇒ `magenta`). با رفعِ `_hp_toner_key` در `core/collectors/hp.py`، انتظار
    عوض شد و **خاصیتِ آینه‌بودن** را سنجیده نگه می‌دارد: adapter تابعِ همان collector را
    import می‌کند، پس هر تغییرِ آن‌جا این‌جا هم بی‌دررو بازتاب دارد و این تست همان را
    قرمز می‌کند تا دو مسیر جدا از هم حرکت نکنند. `tests/test_hp_toner_key.py` قاعده را
    در خودِ collector قفل می‌کند.
    """
    toners = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics)["toners"]
    assert toners["toner_2"] == {
        "level": None, "status": "unknown", "name": "Imaging Drum", "remaining": -2, "max": 100,
    }
    assert "magenta" not in toners and "cyan" not in toners


def test_hp_trays_status_thresholds(hp_metrics):
    trays = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics)["trays"]
    assert trays == [
        {"name": "Tray 1", "level": 130, "capacity": 250, "status": "medium"},   # 52 %
        {"name": "Manual Feed Tray", "level": -9, "capacity": 150, "status": "empty"},
    ]


def test_canon_counters_and_mf_precedence(canon_metrics):
    counters = ea.exporter_to_collector_shape("172.16.0.9", "canon", canon_metrics)["counters"]
    # canon_total (MF) بر bro_total ترجیح دارد؛ copy/printer از شاخهٔ MF
    assert counters["total"] == 12000
    assert counters["printer"] == 9000            # canon_print
    assert counters["copy"] == 2500               # canon_copy
    assert counters["full_color"] == 4000         # bro_color (< total ⇒ معتبر)
    assert counters["black_white"] == 8000        # bw_std
    assert counters["fax"] is None                # canon آن را در legacy ندارد


def test_canon_toners_and_open_cover_alert(canon_metrics):
    shape = ea.exporter_to_collector_shape("172.16.0.9", "canon", canon_metrics)
    assert shape["toners"]["black"]["level"] == 62 and shape["toners"]["black"]["status"] == "ok"
    assert shape["toners"]["cyan"]["level"] == 18 and shape["toners"]["cyan"]["status"] == "low"
    assert shape["toners"]["magenta"]["source"] == "snmp_unavailable"
    assert shape["toners"]["magenta"]["level"] is None
    assert shape["trays"] == [{"name": "Cassette 1", "level": 500, "capacity": 700,
                               "status": "medium"}]           # tray2_* نبود ⇒ skip
    assert shape["alerts"] == [{"message": "درب پرینتر باز است", "code": 3}]
    assert shape["device"]["uptime_str"] == "2d 00:00"        # 17280000/100 = 172800


def test_canon_lbp_path_when_no_mf_counters(canon_metrics):
    """بدونِ canon_total/alt_total، شاخهٔ LBP می‌آید: total = canon_lbp_total."""
    del canon_metrics["canon_total"]
    del canon_metrics["canon_copy"]
    del canon_metrics["canon_print"]
    del canon_metrics["canon_scan"]
    counters = ea.exporter_to_collector_shape("172.16.0.9", "canon", canon_metrics)["counters"]
    assert counters["total"] == 9000
    assert counters["printer"] == 9000 and counters["copy"] is None
    assert counters["full_color"] == 400           # canon_lbp_color < total
    assert counters["black_white"] == 8000


# ── ۳) ignore_values ──────────────────────────────────────────────────────
def _ignored_names_in_spec() -> set[str]:
    lines = SNMP_YML.read_text(encoding="utf-8").split("\n")
    names, current = set(), None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- name:"):
            current = stripped.split("- name:")[1].strip()
        elif "ignore_values" in stripped and current:
            names.add(current)
    return names


def test_ignore_values_set_is_exactly_the_31_spec_metrics():
    from_spec = _ignored_names_in_spec()
    assert ea.IGNORED_METRICS == from_spec, (
        f"کم: {sorted(from_spec - ea.IGNORED_METRICS)} · اضافه: "
        f"{sorted(ea.IGNORED_METRICS - from_spec)}")
    assert len(from_spec) == 31


def test_ignored_sentinel_falls_back_to_the_collector_default(hp_metrics):
    # toner_remain_2 = -2 با کامنتِ ignore ⇒ همان defaultِ collect_hp (-2) ⇒ unknown
    toners = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics)["toners"]
    assert toners["toner_2"]["status"] == "unknown"
    # general_oids = -2 ⇒ هیچ‌جا در شکل ظاهر نمی‌شود (نه ۰، نه -۲)
    shape = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics)
    assert "general_oids" not in str(shape["counters"])


def test_sentinel_on_unmarked_metric_is_kept(hp_metrics):
    """a `-2` on a metric without `# ignore_values:` is a value, not a gap."""
    assert ea._int(hp_metrics, "toner_remain_2", -99) == -99    # marked: dropped ⇒ default
    marked_free = {"hp_copy_mono": [({}, -2.0)]}
    assert ea._int(marked_free, "hp_copy_mono", -1) == -2       # unmarked: kept
    assert ea._int({"bro_total": [({}, float("nan"))]}, "bro_total", 7) == 7


def test_documented_divergence_hp_no_sensor_becomes_empty(hp_metrics):
    """تنها واگراییِ آگاهانه (سند §۹): prtInputCurrent = -2.

    collect_hp از روی OIDِ زنده `no_sensor` می‌گیرد؛ اینجا sentinelِ متریکِ
    tray*_level (که در spec ignore دارد) دور ریخته می‌شود و defaultِ -9 ⇒ `empty`.
    اگر این یکی مهم شد، `tray1_level`/`tray2_level` را از IGNORED_METRICS بردارید —
    و این تست عمداً همین را pin می‌کند تا تغییرِ بی‌صدا ممکن نباشد.
    """
    metrics = dict(hp_metrics)
    metrics["tray1_level"] = [({}, -2.0)]
    tray = ea.exporter_to_collector_shape("172.16.0.5", "hp", metrics)["trays"][0]
    assert tray["status"] == "empty" and tray["level"] == -9


# ── ۴) مسیرهای None ───────────────────────────────────────────────────────
def test_unsupported_brands_and_empty_answers_yield_none():
    assert ea.exporter_to_collector_shape("1.1.1.1", "toshiba", {"tsh_total": [({}, 5.0)]}) is None
    assert ea.exporter_to_collector_shape("1.1.1.1", "brother", {}) is None
    assert ea.exporter_to_collector_shape("1.1.1.1", "sensor", {"sys_uptime": [({}, 1.0)]}) is None
    assert ea.exporter_to_collector_shape("1.1.1.1", "hp", {}) is None          # پاسخِ خالی
    assert ea.exporter_to_collector_shape("1.1.1.1", "hp", {"unrelated": [({}, 1.0)]}) is None
    assert ea.exporter_to_collector_shape("1.1.1.1", "", {"bro_total": [({}, 1.0)]}) is None
    assert ea.SUPPORTED_BRANDS == ("hp", "canon")


def test_poll_ms_is_measured_when_start_given(hp_metrics):
    import time
    shape = ea.exporter_to_collector_shape("1.1.1.1", "hp", hp_metrics, start=time.time() - 0.2)
    assert 0 < shape["poll_ms"] < 5000


class _FakeClient:
    """کلاینتِ جعلی: فقط query_brand را implements می‌کند (قراردادِ adapter)."""

    def __init__(self, metrics=None, exc=None):
        self._metrics, self._exc = metrics, exc
        self.calls: list[tuple] = []

    def query_brand(self, ip, brand, auth="public_v2"):
        self.calls.append((ip, brand, auth))
        if self._exc is not None:
            raise self._exc
        return self._metrics


@pytest.mark.parametrize("exc", [ExporterError("target unreachable"), TimeoutError("slow")])
def test_collect_via_exporter_turns_every_error_into_none(exc, hp_metrics):
    client = _FakeClient(exc=exc)
    assert ea.collect_via_exporter(client, "1.1.1.1", "x", "hp") is None
    assert len(client.calls) == 1        # یک تلاش، بدونِ حلقهٔ retry


def test_collect_via_exporter_skips_unsupported_brand_without_network(hp_metrics):
    client = _FakeClient(hp_metrics)
    assert ea.collect_via_exporter(client, "1.1.1.1", "x", "toshiba") is None
    assert client.calls == []                       # هیچ کوئری‌ای نرفته


def test_collect_via_exporter_success_and_missing_signal(hp_metrics):
    client = _FakeClient(hp_metrics)
    shape = ea.collect_via_exporter(client, "172.16.0.5", "HP-05", "HP")
    assert shape["counters"]["total"] == 12345 and client.calls == [
        ("172.16.0.5", "HP", "public_v2")]
    assert ea.collect_via_exporter(_FakeClient({}), "172.16.0.5", "HP-05", "hp") is None


# ── ۸) گام H.1 — وب‌اسکرپِ تونر در مسیرِ exporter ────────────────────────────
def _hp_without_toner_numbers(hp_metrics):
    """SNMPِ «هست ولی عدد نمی‌دهد»: `toner_remain_1 = -2` همان sentinelِ `ignore_values` است.

    عمداً رکوردِ اسلاتِ ۱ را *حذف* نمی‌کنیم: آن‌وقت `collect_hp` کلاً کلیدِ `black` را
    نمی‌سازد و شاخهٔ «ساختِ تونرِ تازه» تست می‌شود، نه شاخهٔ «پرکردنِ levelِ خالی» که
    قاعدهٔ legacy روی آن حساس‌تر است (آستانه‌هایِ status آن‌جا جاری می‌شود).
    """
    m = dict(hp_metrics)
    m["toner_remain_1"] = [({}, -2.0)]
    return m


def test_adapter_uses_the_real_scrapers_not_copies(no_web_by_default):
    """«reuse» یعنی همان تابع، نه کپیِ دوم — تصمیمِ مالک برایِ H.1: **صفر کپیِ اسکرپرها**.

    این پینِ هویت، کلِ تصمیمِ H.1 را نگه می‌دارد: تا وقتی `_scrape_hp_toners`ِ ما *خودِ*
    `core.collectors.hp` باشد، هر اصلاحِ اسکرپرِ EWS در collector به‌طورِ خودکار به مسیرِ
    exporter هم می‌رسد. (برعکسش هم درست است: اگر روزی کسی اینجا کپیِ مستقل بگذارد،
    تست می‌شکند.) قاعدهٔ merge/trigger را نمی‌شود این‌طور بست — آن‌ها در
    `core/enhanced_collector.py` هستند که importش یعنی کشیدنِ `core.store`/`core.database`
    به مسیرِ exporter؛ پس برایِ آن‌ها پینِ *متن* داریم (`test_merge_functions_are_faithful_copies`، الگوی TD-016)
    + پینِ *رفتاری* (`test_merge_and_missing_rules_match_enhanced_collector`). دو سبکِ قفل برای
    دو نوع بازتولید: قفلِ *هویت* برایِ چیزی که import شده (اسکریپرها)، قفلِ *متن* برایِ چیزی که
    کپی شده (دو قاعده). چرا اسکریپرها import‌اند و نه کپی؟ `_scrape_canon_toners` داخلِ بدنه‌اش
    `import requests` + `urllib.request.urlopen` دارد و دروازهٔ G.5 هر دو اسم را در adapter
    ممنوع کرده است؛ کپیِ «وفادار» آن یعنی شکستنِ دروازه، پس یا import (انتخابِ این گام) یا
    بازنویسیِ transport که دیگر وفادار نیست. **تصمیمِ نهاییِ مالک (گام H.1): همان import** —
    جدولِ دامنه، دلیلِ «deleg» و ردِ گزینه‌هایِ دیگر: `docs/phase-h-decisions.md` §H.1.
    """
    from core.collectors.canon import _scrape_canon_toners as real_canon
    from core.collectors.hp import _hp_toner_key, _scrape_hp_toners as real_hp

    # «اصلِ bind‌شده» قبلِ patch شدنِ fixture خوانده شده
    orig = no_web_by_default
    assert orig["hp"] is real_hp
    assert orig["canon"] is real_canon
    assert orig["key"] is _hp_toner_key       # کلیدِ رنگ‌ها هم همان، نه نسخهٔ دوم


def test_merge_functions_are_faithful_copies():
    """«کپیِ وفادار» یعنی متن — همان الگوی TD-016 (`test_rule_matches_the_web_helper`).

    دو جایگزینی که برایِ مقایسه لازم است، هر دو *شمرده* و مستندند؛ به‌جز آن‌ها متن باید
    عیناً یکی باشد — و هدفِ تست همین «شمرده‌بودنِ تفاوت» است، نه تلورانسِ کلی:

    1. `raw_level`: شکلِ `collect_hp`/`collect_canon` این کلید را ندارد (مالِ
       `collect_enhanced` است). خط‌هایی که *فقط* آن را می‌خوانند/می‌نویسند حذف می‌شوند و
       `.get("raw_level")` به `.get("level")` تبدیل — همان تطبیقی که در داکسِ
       `_toner_level_missing`/`_merge_scraped_toners` این فایل نوشته شده.
    2. annotation: آن‌جا `typing.Dict`، این‌جا `dict` — چون `from typing import Dict` یعنی
       importِ تازه و دروازهٔ importِ G.5 عمداً بسته است.

    اگر روزی کسی یکی از دو قاعده را در یک طرف عوض کند، این تست می‌شکند؛ تست‌هایِ رفتاری
    (`test_merge_and_missing_rules_match_enhanced_collector`) هم همان را از راهِ ورودی/خروجی
    می‌گیرند — یکی برای «متن دوقلو ماند»، یکی برای «رفتار دوقلو ماند».
    """
    import inspect
    import re as _re

    from core import enhanced_collector as ec

    RAW_BLOCK = 'if t.get("raw_level") is None:\n            t["raw_level"] = pct\n'

    def norm(fn):
        src = inspect.getsource(fn)
        src = src.replace(RAW_BLOCK, "")                       # بلوکِ raw_level در merge
        src = src.replace('"raw_level": pct, ', "")            # کلیدِ raw_level در دیکشنریِ تازه
        src = src.replace('t["raw_level"] = pct\n', "")       # اگر کسی جدا از if نوشته باشد
        src = src.replace('"raw_level"', '"level"')           # شرطِ «عدد داریم؟» روی فیلدِ هر شکل
        src = _re.sub(r'""".*?"""', "", src, flags=_re.S)      # docstring
        src = _re.sub(r"(?m)^\s*#.*$", "", src)                # کامنت
        src = _re.sub(r"\bDict\b", "dict", src)                # annotation
        lines = [_re.sub(r"\s+", " ", ln).rstrip() for ln in src.split("\n")]
        return "\n".join(ln for ln in lines if ln.strip())

    for fname in ("_toner_level_missing", "_merge_scraped_toners"):
        mine, theirs = norm(getattr(ea, fname)), norm(getattr(ec, fname))
        if mine != theirs:
            diff = [f"  ours={a!r}\n  legacy={b!r}" for a, b in zip(mine.split("\n"), theirs.split("\n")) if a != b]
            raise AssertionError(f"{fname} دیگر کپیِ وفادار نیست:\n" + "\n".join(diff))
    # و این‌ها دو تابعِ *جدا* هستند (نه alias) — وگرنه مقایسهٔ متن بی‌معنی است
    assert ea._toner_level_missing is not ec._toner_level_missing
    assert ea._merge_scraped_toners is not ec._merge_scraped_toners
    # merge در آینه *واقعاً* مصرف می‌شود (نه کپیِ بیکار) — و فقط از تک‌درِ `_merge_scraped`:
    # مسیرِ runtime نباید خودش شاخه‌به‌شاخه قاعده‌ها را صدا بزند (تصمیمِ بندِ ۱-۳: dispatcher).
    run = inspect.getsource(ea.apply_toner_http_fallback)
    disp = inspect.getsource(ea._merge_scraped)
    assert "_merge_scraped(toners, brand, scraped)" in run, run
    assert "_merge_scraped_toners" not in run and "_prefer_scraped_canon_toners" not in run
    assert "_prefer_scraped_canon_toners" in disp and "_merge_scraped_toners" in disp


def test_hp_http_fallback_when_snmp_incomplete(monkeypatch, hp_metrics):
    """SNMP عددی برای تونر ندارد ⇒ وب‌اسکرپ، با قاعدهٔ merge خودِ legacy."""
    seen = []

    def fake_scrape(ip, timeout=4.0):
        seen.append((ip, timeout))
        return {"black": 61, "cyan": 5, "_used": ["black"]}     # «_used» فراداده است

    monkeypatch.setattr(ea, "_scrape_hp_toners", fake_scrape)
    metrics = _hp_without_toner_numbers(hp_metrics)
    client = _FakeClient(metrics)
    shape = ea.collect_via_exporter(client, "172.16.0.5", "HP-05", "hp")

    assert seen == [("172.16.0.5", 4.0)], "timeout = پیش‌فرضِ خودِ collector (4.0)"
    assert ea.HP_HTTP_TIMEOUT == 4.0 and ea.CANON_HTTP_TIMEOUT == 3.0, "عددِ legacy، نه سلیقهٔ ما"
    t = shape["toners"]
    assert t["black"]["level"] == 61 and t["black"]["status"] == "ok"
    assert t["black"]["source"] == "hp_web"
    assert t["black"]["name"] == "Black Toner", "کلیدِ موجود نامِ خودش را نگه می‌دارد"
    assert t["black"]["remaining"] == -2 and t["black"]["max"] == 3200, "فیلدهایِ SNMP دست‌نخورده"
    # شاخهٔ «تونرِ تازه» عیناً enhanced_collector: status هنوز unknown و remaining/max پیش‌فرض
    assert t["cyan"] == {"level": 5, "status": "unknown", "name": "cyan toner",
                         "remaining": -1, "max": -1, "source": "hp_web"}
    assert "_used" not in t and all("_used" not in v for v in t.values())
    # و هیچ کلیدِ تازه‌ای به شکلِ خروجی اضافه نشده (قراردادِ «۱۲ کلیدِ collect_hp»)
    assert set(shape) == _returned_keys("hp.py", "collect_hp")
    assert not any("raw_level" in v for v in t.values()), "raw_level مالِ شکلِ enhanced است"
    assert shape["counters"]["total"] == 12345             # اعدادِ SNMP سرِ جایشان


def test_hp_no_http_call_when_snmp_has_numbers(monkeypatch, hp_metrics):
    """تونر عدد دارد ⇒ هیچ درخواستِ وبی نباید برود (هزینهٔ اضافه روی دستگاه سالم)."""
    def boom(ip, timeout=4.0):
        raise AssertionError("وب‌اسکرپ نباید صدا زده شود")

    monkeypatch.setattr(ea, "_scrape_hp_toners", boom)
    client = _FakeClient(hp_metrics)
    shape = ea.collect_via_exporter(client, "172.16.0.5", "HP-05", "hp")
    assert shape["toners"]["black"] == {"level": 42, "status": "ok", "name": "Black Toner",
                                        "remaining": 1344, "max": 3200}


def test_hp_drum_number_blocks_fallback_like_the_rule(monkeypatch, hp_metrics):
    """رگرسیونِ آگاهانه: `_toner_level_missing` فقط `drum`/`opc` را معاف می‌کند؛

    در مسیرِ HP عددِ درام روی کلیدِ `toner_2` می‌نشیند (نه `drum`)، پس «درامِ سالم» مثلِ
    خودِ قاعدهٔ legacy مانعِ fallback می‌شود. این تست همین رفتار را قفل می‌کند تا اگر
    روزی خواستیم `toner_2` را هم معاف کنیم، *تصمیم* باشد نه فراموشی — سند:
    `docs/phase-h-decisions.md` §H.1 «محدودیت‌هایِ شناخته‌شده».
    """
    def boom(ip, timeout=4.0):
        raise AssertionError("با عددِ درام (toner_2=90) قاعدهٔ فعلی fallback نمی‌کند")

    monkeypatch.setattr(ea, "_scrape_hp_toners", boom)
    m = dict(hp_metrics)                     # در fixtureِ واقعی: اسلاتِ ۲ = درام با ۹۰٪ نیست،
    m["toner_max_2"] = [({}, 100.0)]         # پس همان را عدددار می‌کنیم و اسلاتِ ۱ را
    m["toner_remain_2"] = [({}, 90.0)]       # sentinel (بی‌عدد) نگه می‌داریم
    m["toner_remain_1"] = [({}, -2.0)]
    shape = ea.collect_via_exporter(_FakeClient(m), "172.16.0.5", "x", "hp")
    assert shape["toners"]["toner_2"]["level"] == 90
    assert shape["toners"]["black"]["level"] is None      # و تونرِ واقعی همچنان unknown


def test_canon_http_fallback_when_snmp_absent(monkeypatch, canon_metrics, caplog):
    """canon با SNMPِ بی‌تونر: هر چهار کلید `snmp_unavailable` می‌مانند، مگر وب عدد بدهد."""
    import logging

    caplog.set_level(logging.DEBUG, logger="PrinterMonitor")
    seen = []

    def fake_scrape(ip, timeout=3.0):
        seen.append((ip, timeout))
        return {"black": 55, "cyan": 12, "magenta": None}   # «None» یعنی وب هم عددی ندارد

    monkeypatch.setattr(ea, "_scrape_canon_toners", fake_scrape)
    m = dict(canon_metrics)
    for idx in (1, 2, 3, 4):
        for base in ("toner_name", "toner_max", "toner_remain"):
            m.pop(f"{base}_{idx}", None)
    shape = ea.collect_via_exporter(_FakeClient(m), "10.0.20.12", "C-12", "canon")

    assert seen == [("10.0.20.12", 3.0)], "collect_canon صریح timeout=3.0 می‌دهد"
    # «موفق شد» باید بی‌صدا باشد، نه با `except`ِ فراگیر پوشانده‌شده: اگر روزی اولویتِ HTTP
    # با TypeError نصفه‌کاره تمام شود، همین لاگِ debug می‌گوید.
    import logging

    assert caplog.records == [] or not [r for r in caplog.records if "انجام نشد" in r.getMessage()]
    t = shape["toners"]
    assert t["black"] == {"level": 55, "status": "ok", "name": "Black Toner",
                          "remaining": None, "max": 100, "source": "http_scrape"}
    assert t["cyan"]["level"] == 12 and t["cyan"]["status"] == "low", "آستانه‌هایِ canon (≤25 ⇒ low)"
    assert t["magenta"]["source"] == "snmp_unavailable" and t["magenta"]["level"] is None, \
        "مقدارِ None اولویتِ HTTP را بی‌اثر می‌کند (همان `if level is not None` در collect_canon)"
    assert set(shape) == _returned_keys("canon.py", "collect_canon")


def test_canon_divergence_from_the_collector_is_pinned(monkeypatch, canon_metrics):
    """واگراییِ *انتخاب‌شده*: آینه canon را مثلِ HP گارد می‌کند، `collect_canon` نه.

    برنامهٔ گام H.1 قاعدهٔ یکنواخت خواست («فقط وقتی SNMP داده ندارد یا ناقص است»)، پس روی
    canonِ سالم — که SNMP عدد دارد و Remote UI هم دارد — آینه اصلاً وب نمی‌زند و اعدادِ SNMP
    می‌مانند؛ ولی `core/collectors/canon.py:325` همان‌جا اسکریپ را می‌زند و HTTP را ترجیح
    می‌دهد. این تفاوتِ رفتاری عمداً تست می‌شود تا «کمبود» به باگِ بی‌صدا تبدیل نشود، و تا
    برگشتنش یک‌خطی بماند (گاردِ `_toner_level_missing` از شاخهٔ canon در
    `apply_toner_http_fallback` برداشته می‌شود). شکلِ رکورد وقتی وب *اجرا می‌شود* همچنان
    عینِ `collect_canon` است: `test_canon_http_fallback_when_snmp_absent`.
    """
    import inspect

    calls = []
    monkeypatch.setattr(ea, "_scrape_canon_toners",
                        lambda ip, timeout=3.0: calls.append(ip) or {"black": 7})
    shape = ea.collect_via_exporter(_FakeClient(canon_metrics), "10.0.20.12", "C-12", "canon")
    assert calls == [], "SNMP عدد دارد ⇒ آینه وب نمی‌زند (قاعدهٔ یکنواختِ H.1)"
    assert shape["toners"]["black"]["source"] == "snmp"
    assert shape["toners"]["black"]["level"] == 62, "عددِ SNMPِ fixture دست‌نخورده می‌ماند"

    # شاهدِ collector از *متنِ* خودش (نه ادعایِ ما): collect_canon هیچ گاردی ندارد
    from core.collectors.canon import collect_canon

    src = inspect.getsource(collect_canon)
    assert "_scrape_canon_toners(ip, timeout=3.0)" in src
    assert "_toner_level_missing" not in src, "collectorِ canon گاردِ «ناقص‌بودن» ندارد"


def test_fallback_keys_off_the_normalized_brand(monkeypatch, canon_metrics):
    """رگرسیونِ باگی که *همین گام* تولید کرد و گیت‌ها نگرفتند: `shape["brand"]` نه آرگومانِ خام.

    poller برند را از ستونِ DB می‌دهد و در مخزن واقعاً `"HP-05"`/`"CANON"` دیده می‌شود؛ اگر
    fallback با همان رشته مقایسه کند، `"CANON" != "canon"` و وب‌اسکرپِ canon بی‌صدا خاموش
    می‌ماند (SNMP هم ناقص باشد، هیچ‌کس نمی‌فهمد).
    """
    hits = []
    monkeypatch.setattr(ea, "_scrape_canon_toners",
                        lambda ip, timeout=3.0: hits.append(ip) or {"black": 9})
    m = dict(canon_metrics)
    for idx in (1, 2, 3, 4):
        for base in ("toner_name", "toner_max", "toner_remain"):
            m.pop(f"{base}_{idx}", None)
    shape = ea.collect_via_exporter(_FakeClient(m), "10.0.20.12", "C-12", "CANON")
    assert hits == ["10.0.20.12"] and shape["toners"]["black"]["source"] == "http_scrape"


def test_fallback_never_raises_and_keeps_snmp_numbers(monkeypatch, hp_metrics):
    """وب هر بلایی کرد (connect refused، timeout، HTML عجیب) ⇒ snapshot نمی‌میرد."""
    def explode(ip, timeout=4.0):
        raise OSError("connection refused")

    monkeypatch.setattr(ea, "_scrape_hp_toners", explode)
    shape = ea.collect_via_exporter(_FakeClient(_hp_without_toner_numbers(hp_metrics)),
                                    "172.16.0.5", "HP-05", "hp")
    assert shape["toners"]["black"] == {"level": None, "status": "unknown",
                                         "name": "Black Toner", "remaining": -2, "max": 3200}, \
        "رکوردهایِ SNMP عیناً می‌مانند؛ فقط «از وب عدد گرفتیم» حذف می‌شود"
    assert shape["counters"]["total"] == 12345


def test_hp_sentinel_only_device_still_creates_missing_rows(monkeypatch, hp_metrics):
    """حذفِ کاملِ اسلاتِ ۱ ⇒ `black` اصلاً ساخته نمی‌شود (رفتارِ collect_hp) و merge آن را
    *می‌سازد* — با status=unknownِ خودِ قاعده، نه آستانه‌ها (کپیِ وفادارِ همان شاخه)."""
    monkeypatch.setattr(ea, "_scrape_hp_toners", lambda ip, timeout=4.0: {"black": 44})
    m = dict(hp_metrics)
    for name in ("toner_name_1", "toner_max_1", "toner_remain_1"):
        m.pop(name, None)
    shape = ea.collect_via_exporter(_FakeClient(m), "172.16.0.5", "x", "hp")
    assert shape["toners"]["black"] == {"level": 44, "status": "unknown", "name": "black toner",
                                        "remaining": -1, "max": -1, "source": "hp_web"}


def test_pure_shape_function_stays_offline(monkeypatch, hp_metrics):
    """`exporter_to_collector_shape` بی‌شبکه می‌ماند — `tools/compare_snmp_sources.py`
    همین تابع را آفلاین مصرف می‌کند؛ اگر fallback داخلش می‌رفت، مقایسهٔ G.6 شبکه می‌زد."""
    def boom(*a, **k):
        raise AssertionError("تابعِ خالص نباید وب بزند")

    monkeypatch.setattr(ea, "_scrape_hp_toners", boom)
    monkeypatch.setattr(ea, "_scrape_canon_toners", boom)
    shape = ea.exporter_to_collector_shape("172.16.0.5", "hp", _hp_without_toner_numbers(hp_metrics))
    assert shape["toners"]["black"]["level"] is None      # بدونِ وب، همان اعدادِ SNMP
    assert shape["toners"]["black"]["status"] == "unknown"


def _assert_does_not_enter_exporter_path(brand, monkeypatch, caplog):
    """بدنهٔ مشترکِ سه گارد: نه کوئری، نه وب‌اسکرپ؛ یک هشدارِ صریح (و نه سیلِ لاگ).

    این سه تست عمداً *successِ ساختگی* نیستند: دامنهٔ H.1 دو برند است و `_toners()`/`_trays()`
    برایِ برندهایِ دیگر شکلِ خروجیِ دیگری دارد، پس تنها ادعایِ صادقانه همین است که «واردِ
    مسیرِ exporter نمی‌شود». مرزِ ثبت‌شده: TD-018.
    """
    import logging

    web_calls = []
    monkeypatch.setattr(ea, "_scrape_hp_toners", lambda *a, **k: web_calls.append("hp") or None)
    monkeypatch.setattr(ea, "_scrape_canon_toners",
                        lambda *a, **k: web_calls.append("canon") or None)
    client = _FakeClient({})
    with caplog.at_level(logging.WARNING, logger="PrinterMonitor"):
        assert ea.collect_via_exporter(client, "10.0.20.9", "X-09", brand) is None
    assert client.calls == [], "برندِ آینه‌سازی‌نشده نباید از exporter بپرسد"
    assert web_calls == [], "نباید حتی وب‌اسکرپ هم اجرا شود"
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warns) == 1 and "آینه‌سازی نشده" in warns[0] and "TD-018" in warns[0]
    # چرخهٔ دوم برای همان (brand, ip): هشدارِ تکراری نه (کوئری هم نه)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="PrinterMonitor"):
        assert ea.collect_via_exporter(client, "10.0.20.9", "X-09", brand) is None
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_toshiba_does_not_enter_exporter_path(monkeypatch, caplog):
    """Toshiba: ۶۳ ردیفِ `oid:` در `monitoring/snmp.yml` آینه نشده ⇒ مسیرِ legacy."""
    _assert_does_not_enter_exporter_path("toshiba", monkeypatch, caplog)


def test_brother_does_not_enter_exporter_path(monkeypatch, caplog):
    """Brother: ۹ ردیفِ `oid:` + EWS آینه نشده ⇒ مسیرِ legacy."""
    _assert_does_not_enter_exporter_path("brother", monkeypatch, caplog)


def test_sensor_does_not_enter_exporter_path(monkeypatch, caplog):
    """`sensor` اصلاً `toners` ندارد؛ با این‌همه گارد *پیش ازِ هر کوئری* می‌بندد."""
    _assert_does_not_enter_exporter_path("sensor", monkeypatch, caplog)


def test_merge_scraped_delegates_per_brand():
    """`_merge_scraped` تنها درِ merge است و *فقط* انتخاب می‌کند (بندِ ۱-۳ برنامه).

    - `hp` → پرکردنِ فیلدهایِ خالی + برچسبِ `hp_web` (و عددِ SNMPِ بی‌اعتبار نمی‌پرد)
    - `canon` → جاگذاریِ رکوردِ HTTP با `remaining=None`/`max=100`
    - برندِ دیگر → همان قاعدهٔ HP؛ در `collect_via_exporter` هرگز به اینجا نمی‌رسد چون
      گاردِ `SUPPORTED_BRANDS` زودتر `None` می‌دهد (سه گاردِ نام‌دار همین را می‌گیرند)
    """
    blank = {"level": None, "status": "unknown", "name": "Black Toner",
             "remaining": -1, "max": -1, "source": "snmp"}

    hp = {"black": dict(blank)}
    assert ea._merge_scraped(hp, "hp", {"black": 61}) == 1
    assert hp["black"]["level"] == 61 and hp["black"]["source"] == "hp_web"
    assert hp["black"]["remaining"] == -1, "HP فیلدهایِ دیگر را دست نمی‌زند"

    cn = {"black": dict(blank)}
    assert ea._merge_scraped(cn, "canon", {"black": 4}) == 1
    assert cn["black"] == {"level": 4, "status": "critical", "name": "Black Toner",
                           "remaining": None, "max": 100, "source": "http_scrape"}, \
        "canon رکورد را *جایگذاری* می‌کند، نه پر"

    other = {"black": dict(blank)}
    assert ea._merge_scraped(other, "unknown-brand", {"black": 12}) == 1
    assert other["black"]["source"] == "hp_web"


def test_merge_and_missing_rules_match_enhanced_collector():
    """قاعدهٔ merge/trigger نباید از `core/enhanced_collector.py` جدا بیفتد.

    به‌جای مقایسهٔ متنِ سورس، *رفتار* دو نسخه روی ورودی‌هایِ یکسان سنجیده می‌شود —
    با تنها تفاوتِ مجاز: شکلِ ما `raw_level` ندارد.
    """
    from core.enhanced_collector import _merge_scraped_toners as legacy_merge
    from core.enhanced_collector import _toner_level_missing as legacy_missing

    scraped = {"black": 61, "cyan": 5, "magenta": 0, "yellow": 100, "_used": ["black"], "drum": "x"}
    for ours_keys, legacy_keys in (
        ({"black": {"level": None, "status": "unknown", "name": "Black Toner", "remaining": -1,
                    "max": -1}},
         {"black": {"level": None, "raw_level": None, "status": "unknown", "name": "Black Toner",
                    "remaining": -1, "max": -1}}),
        ({"black": {"level": 30, "status": "ok", "name": "x", "remaining": 1, "max": 2}},
         {"black": {"level": 30, "raw_level": 30, "status": "ok", "name": "x", "remaining": 1,
                    "max": 2}}),
        ({}, {}),
        ({"drum": {"level": 90, "status": "ok"}}, {"drum": {"level": 90, "status": "ok"}}),
    ):
        mine, theirs = dict(ours_keys), dict(legacy_keys)
        n_mine = ea._merge_scraped_toners(mine, scraped, "hp_web")
        n_theirs = legacy_merge(theirs, scraped, "hp_web")
        assert n_mine == n_theirs, ours_keys
        assert set(mine) == set(theirs)
        for k in mine:
            for field in ("level", "status", "name", "remaining", "max", "source"):
                assert mine[k].get(field) == theirs[k].get(field), (ours_keys, k, field)
        # شرطِ «عدد داریم؟» روی فیلدِ هر شکل
        assert ea._toner_level_missing(mine) == legacy_missing(theirs), ours_keys


def test_adapter_has_no_side_effects():
    """رویداد/DB در *کدِ* این فایل نیست؛ وب فقط از دو اسکریپرِ import‌شده.

    بررسی روی AST انجام می‌شود (نه متنِ خاموش)، چون docstring عمداً اسمِ
    `_counters_event` را می‌آورد و توضیح می‌دهد چرا صدا زده نمی‌شود.

    گام H.1 یک چیز را عوض کرد و همان را این‌جا می‌بندیم: آینه حالا *مجاز* است
    وب‌اسکرپِ خودِ دستگاه را صدا بزند، ولی فقط از راهِ دو تابعِ import‌شده از
    collectorهایِ خودش (`_scrape_hp_toners`/`_scrape_canon_toners`). یعنی نه
    `urlopen`/`requests` در این فایل، نه DB، نه رویداد — همان سه «نباید»ِ G.5 سرِ
    جایشان‌اند و لیستِ importها فقط یک ماژولِ collectorِ دیگر جا باز کرده است.
    """
    tree = ast.parse((ROOT / "core" / "collectors" / "exporter_adapter.py").read_text(encoding="utf-8"))
    called, imported, touched = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                called.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                called.add(fn.attr)
                touched.add(fn.value.id if isinstance(fn.value, ast.Name) else type(fn.value).__name__)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            touched.add(node.id)
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    banned = {"_counters_event", "add_event", "yield_engine", "urlopen", "requests",
              "save_printers", "update_prev"}
    assert not (called & banned), called & banned
    assert not (touched & (banned | {"store", "database", "sqlite3"})), sorted(touched)
    for name in called | touched:
        assert not any(b in name for b in ("INSERT", "sqlite")), name
    assert imported <= {"__future__", "logging", "re", "time", "core.collectors.base",
                        "core.collectors.hp", "core.collectors.canon",
                        "core.exporter_client", "core.time_utils"}, imported
    # H.1: دروازهٔ وب *باز* شده، ولی فقط از این دو درب؛ و هر دو باید واقعاً صدا زده شوند
    # (اگر روزی fallback بی‌صدا حذف شود، این همان چیزی است که می‌گوید).
    assert {"_scrape_hp_toners", "_scrape_canon_toners"} <= called, called
    assert not (called & {"fetch_first_web_page", "Request"}), called
