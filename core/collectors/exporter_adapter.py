"""تبدیلِ خروجیِ `snmp_exporter` به شکلِ collectorها — فاز G، گام G.5.3 (+ گام H.1).

    shape = exporter_to_collector_shape("172.16.0.5", "hp", metrics)   # metrics = dict
    shape = collect_via_exporter(client, ip, name, brand, start)       # یا None

خروجی همان dictی است که `core/collectors/hp.py` و `canon.py` برمی‌گردانند (همان کلیدها و
همان مقدار‌های پیش‌فرض)، چون مسیرِ fallbackِ poller از قبل همین شکل را مصرف می‌کند و
UI/DB با آن کار می‌کنند.

**چه چیزی در این مسیر انجام نمی‌شود (و چرا پرچم پیش‌فرض خاموش است):** ثبتِ رویداد.
`_counters_event`/YieldEngine/cartridge-change در کدِ collectorها زندگی می‌کنند و طبقِ
تصمیمِ فاز `collectors/` دست‌نخورده می‌ماند؛ پس شکلِ ساخته‌شده «snapshot قابل‌نمایش» است،
نه «چرخهٔ کاملِ poll». به همین دلیل `USE_SNMP_EXPORTER` پیش‌فرض ۰ است و تا گام G.6
روشن‌کردنش توصیه نمی‌شود (§۹ `docs/snmp-exporter-setup.md`).

چهار تصمیم، با دلیل و جایگزینِ ردشده:

1. **آینه‌سازیِ فرمول‌ها، نه باز‌نویسیِ مسیرِ داده.** آستانه‌های درصدِ تونر، وضعیت
   سینی، زنجیرهٔ جایگزینِ model/serial و شرطِ رنگ، سطر‌به‌سطر از `collect_hp`/
   `collect_canon` گرفته شده‌اند و `si`/`ss`/`_hp_toner_key` import می‌شوند تا drift
   ممکن نباشد. جایگزینِ ردشده: فرمولِ مستقلِ exporter — در G.6 عددِ «تفاضل» الکی می‌داد.
   دو موردِ عمدی آینه نمی‌شوند چون از روی یک scrape شدنی نیستند: retryِ «total=0 مشکوک»
   (نیاز به خواندنِ دوبارهٔ SNMP دارد) و شاخهٔ «کارتریج شارژی/`_used`» + سریال/صفحاتِ EWS
   در `collect_enhanced` (کلیدهای `cartridge_*` را آینه نمی‌سازیم — بندِ «موکول» در
   `docs/phase-h-decisions.md`). **گام H.1:** وب‌اسکرپِ تونر در این مسیر فعال است، ولی با
   import از `core.collectors.{hp,canon}` و **صفر کپیِ اسکرپر** (تصمیمِ مالک) — اسکرپر همان‌جا
   می‌ماند و آینه فقط صدا می‌زند. merge از تک‌درِ `_merge_scraped` می‌آید که *فقط deleg* است و
   triggerِ هر دو برند یکی است: «SNMP عددی برای هیچ تونرِ واقعی ندارد»؛ واگراییِ آینه با
   `collect_canon:325` (که همیشه اسکریپ می‌زند) تستِ صریح دارد: §H.1.
2. **`ignore_values` یعنی «دستگاه این را گزارش نمی‌کند»، نه «صفر».** sentinel‌های
   `-2`/`-3` فقط در همان متریک‌هایی دور ریخته می‌شوند که `monitoring/snmp.yml` برایششان
   `# ignore_values:` دارد و جایشان `defaultِ` خودِ collector می‌نشیند؛ مثلاً
   `toner_remain_2 = -2` → `level=None/status=unknown`، دقیقاً مثلِ مسیرِ مستقیم.
   متریکِ بدونِ کامنت حتی با مقدارِ `-2` دست‌نخورده می‌ماند. فهرستِ ۳۱ متریک در
   `IGNORED_METRICS` است و `tests/test_exporter_adapter.py` آن را با خودِ `snmp.yml`
   مقایسه می‌کند. واگراییِ آگاهانهٔ تکمیلی: `prtInputCurrent = -2` در مسیرِ HP
   `no_sensor` است؛ اینجا sentinel دور ریخته می‌شود و `empty` می‌شود — تستِ صریح دارد.
3. **برندهای آینه‌سازی‌نشده → `None`.** `SUPPORTED_BRANDS = ("hp", "canon")`؛ برای
   `toshiba`/`brother`/`sensor` دادهٔ exporter شکلِ کاملِ collector را نمی‌دهد
   (`printer_toshiba` ۶۳ متریک دارد با پیام‌های هشدارِ متنی و سینی ۳/۶ که در
   `printer_std` نیست) و حدس‌زدنِ بقیه یعنی دادهٔ جعلی. poller در این حالت به مسیرِ
   مستقیم برمی‌گردد.

4. **از گام H.2، `ExporterCircuitOpen` در سطحِ *debug** (بقیهٔ خطاها warning): با پرچمِ روشن
   یک پرینترِ خاموش وگرنه هر ۶۰ ثانیه یک هشدارِ تازه می‌ساخت، چون شمارشِ شکست و «چقدر
   صبر کنم» در خودِ کلاینت است (`core/exporter_client.py`) و `query_brand` پیش ازِ هر
   درخواست می‌گوید «نوبت نیست». این فایل state نمی‌شمارد؛ فقط همان «صفر» را معنی می‌کند:
   `return None` تا poller مسیرِ مستقیمِ کامل را بنویسد.

هیچ عددی اینجا از سنجهٔ واقعی نیامده: fixtureها دست‌نویس‌اند و exporter در این sandbox
اجرا نشده (`load_test_sandbox: not_attempted` در `monitoring/snmp-manifest.json`).
"""
from __future__ import annotations

import logging
import re
import time

from core.collectors.base import si, ss
from core.collectors.canon import _scrape_canon_toners
from core.collectors.hp import _hp_toner_key, _scrape_hp_toners
from core.exporter_client import (DEFAULT_AUTH, ExporterCircuitOpen, ExporterClient, ExporterError,
                             label_value, metric_value)
from core.time_utils import utc_now_iso

log = logging.getLogger("PrinterMonitor")

#: برندهایی که شکلِ خروجی‌شان آینه‌سازی شده (بقیه → None تا مسیرِ مستقیم برود).
SUPPORTED_BRANDS = ("hp", "canon")

#: متریک‌هایی که در `monitoring/snmp.yml` کامنتِ `# ignore_values: ["-2", "-3"]` دارند.
#: ۸ (printer_std) + ۳ (printer_hp) + ۲ (printer_canon) + ۲ (printer_brother)
#: + ۱۶ (printer_toshiba) = ۳۱ — تستِ همین فایل با فایل مقایسه می‌کند.
IGNORED_METRICS = frozenset({
    "toner_max_1", "toner_max_2", "toner_remain_1", "toner_remain_2",
    "tray1_cap", "tray2_cap", "tray1_level", "tray2_level",
    "general_oids", "general_oids_2", "hp_npcl_pct_oid",
    "canon_alternate_oids", "canon_alternate_oids_2",
    "brother_black", "brother_drum_oid",
    "toner_cyan_usage", "toner_magenta_usage", "toner_yellow_usage", "toner_black_usage",
    "toner_magenta_status", "toner_cyan_status", "toner_yellow_status", "toner_black_status",
    "tsh_t1_level", "tsh_t2_level", "tray3_level", "tray6_level",
    "tsh_cyan_st", "tsh_mag_st", "tsh_yel_st", "tsh_blk_st",
})
IGNORED_SENTINELS = (-2.0, -3.0)


# ── خواندنِ یک متریک از dictِ پارس‌شده ─────────────────────────────────────
def _num(metrics: dict, name: str, default):
    """عددِ متریک؛ `default` اگر نبود، غیرمحدود بود، یا sentinelِ ignore داشت."""
    raw = metric_value(metrics, name)
    if raw is None:
        return default
    if name in IGNORED_METRICS and raw in IGNORED_SENTINELS:
        return default
    return raw


def _int(metrics: dict, name: str, default):
    """عددِ صحیح؛ `si()` خودِ collector هم همین تبدیل را می‌کند (و لاگِ خطا)."""
    raw = _num(metrics, name, None)
    if raw is None:
        return default
    return si(raw, default)


def _text(metrics: dict, name: str, default: str = "") -> str:
    """متریکِ متنی: مقدارِ لیبلِ هم‌نام (رفتارِ collector.go)، با `ss()` نرمال شده."""
    return ss(label_value(metrics, name, None), default)


def _uptime_str(ticks) -> str:
    """رشتهٔ uptimeِ collectorها: sysUptime در ۱/۱۰۰ ثانیه → «1d 01:40»."""
    us = int(ticks or 0) // 100
    return f"{us // 86400}d {(us % 86400) // 3600:02d}:{(us % 3600) // 60:02d}"


# ── اجزای شکلِ خروجی ──────────────────────────────────────────────────────
def _device(brand: str, metrics: dict) -> dict:
    """زنجیرهٔ model/serial/firmware، آینهٔ `collect_hp`/`collect_canon`."""
    if brand == "hp":
        model = _text(metrics, "hp_model", "")
        if not model:
            model = _text(metrics, "model_mib", "")
        if not model:
            match = re.search(r"PID:([^,]+)", _text(metrics, "sys_descr", ""))
            model = match.group(1).strip() if match else "Unknown"
        serial = _text(metrics, "hp_serial", "") or _text(metrics, "serial_mib", "N/A")
        firmware = _text(metrics, "hp_firmware", "N/A")
    else:  # canon
        model = _text(metrics, "canon_model", "")
        if not model:
            model = _text(metrics, "model_mib", "")
        if not model:
            model = _text(metrics, "sys_descr", "N/A")
        serial = _text(metrics, "canon_serial", "") or _text(metrics, "serial_mib", "N/A")
        firmware = _text(metrics, "canon_firmware", "N/A")
    return {
        "model": ss(model, "Unknown"),
        "serial": ss(serial, "N/A"),
        "firmware": ss(firmware, "N/A"),
        "uptime_str": _uptime_str(_int(metrics, "sys_uptime", 0)),
    }


def _toners(brand: str, metrics: dict) -> dict:
    """تونرها از `prtMarkerSuppliesTable`؛ شاخهٔ هر برند عیناً کدِ خودش است.

    `printer_canon` در spec هیچ متریکِ تونری ندارد، پس برای canon هم تنها منبع همان
    MIB استاندارد است؛ HTTP-scrapeهای `canon.py` اینجا انجام نمی‌شود (§۲ بالا).
    """
    toners: dict = {}
    if brand == "hp":
        for idx in (1, 2, 3, 4):
            t_name = _text(metrics, f"toner_name_{idx}", "")
            t_max = _int(metrics, f"toner_max_{idx}", -1)
            t_rem = _int(metrics, f"toner_remain_{idx}", -2)
            if t_max == -1 and t_rem == -2:
                continue
            if not t_name:
                t_name = "Black Toner" if idx == 1 else f"Toner {idx}"
            key = _hp_toner_key(t_name) or ("black" if idx == 1 else f"toner_{idx}")
            if t_rem == -2 or t_max <= 0:
                pct, status = None, "unknown"
            elif t_rem <= 0:
                pct, status = 0, "empty"
            else:
                pct = round(t_rem / t_max * 100)
                status = "ok" if pct > 25 else ("low" if pct > 10 else "critical")
            toners[key] = {"level": pct, "status": status, "name": t_name,
                           "remaining": t_rem, "max": t_max}
        if not toners:
            toners["black"] = {"level": None, "status": "unknown",
                               "name": "Black Toner", "remaining": -1, "max": -1}
        return toners

    for idx, key, default_name in ((1, "black", "Black Toner"), (2, "cyan", "Cyan Toner"),
                                   (3, "magenta", "Magenta Toner"), (4, "yellow", "Yellow Toner")):
        t_name = _text(metrics, f"toner_name_{idx}", default_name)
        t_max = _int(metrics, f"toner_max_{idx}", -1)
        t_rem = _int(metrics, f"toner_remain_{idx}", -1)
        if t_max == -1 and t_rem == -1:
            toners[key] = {"level": None, "status": "unknown", "name": default_name,
                           "remaining": -1, "max": -1, "source": "snmp_unavailable"}
            continue
        if t_rem < 0 or t_max <= 0:
            pct, status = None, "unknown"
        else:
            pct = round(t_rem / t_max * 100)
            status = ("empty" if pct == 0 else "critical" if pct <= 10
                      else "low" if pct <= 25 else "ok")
        toners[key] = {"level": pct, "status": status, "name": t_name or default_name,
                       "remaining": t_rem, "max": t_max, "source": "snmp"}
    return toners


# ── H.1 — وب‌اسکرپِ تونر در مسیرِ exporter (fallback/اولویت، عیناً مثلِ collectorها) ─────
#: timeoutهایِ legacy: `collect_canon` صریح ۳.۰ می‌دهد؛ `collect_enhanced` برای HP
#: `_scrape_hp_toners(ip)` را با پیش‌فرضِ ۴.۰ صدا می‌زند. همین دو عدد اینجا.
CANON_HTTP_TIMEOUT = 3.0
HP_HTTP_TIMEOUT = 4.0

#: چهار اسلاتِ CMYKِ canon — همان `toner_map`ِ `collect_canon` (برایِ اولویتِ HTTP).
_CANON_TONER_MAP = ((1, "black", "Black Toner"), (2, "cyan", "Cyan Toner"),
                    (3, "magenta", "Magenta Toner"), (4, "yellow", "Yellow Toner"))


def _toner_level_missing(toners: dict) -> bool:
    """«هیچ عددی برای هیچ تونرِ واقعی نداریم؟» — قاعدهٔ `core/enhanced_collector.py:134`.

    یک تطبیقِ لازم و عمداً: آن‌جا عددِ خام در `raw_level` است (شکلِ `collect_enhanced`) و
    این‌جا در `level` (شکلِ `collect_hp` هیچ `raw_level` ندارد — افزودنش یعنی کلیدِ اضافه
    در snapshot و یک سطرِ `only_in_exporter` ساختگی در مقایسهٔ G.6). «drum/opc شمرده
    نمی‌شوند» عیناً از همان‌جا آمده: درامِ سالم نباید مانعِ fallback شود.
    """
    toner_keys = [k for k in toners.keys() if k not in ("drum", "opc")]
    if not toner_keys:
        return True
    return not any(toners[k].get("level") is not None for k in toner_keys)


def _merge_scraped_toners(toners: dict, scraped: dict, source: str) -> int:
    """ادغامِ نتیجهٔ وب‌اسکرپ در `toners` — قاعدهٔ `core/enhanced_collector.py:145`.

    دو خطِ `raw_level` عمداً نیامده (توضیحِ بالا). باقیِ قاعده عیناً: کلیدهایِ «_» فراداده‌اند
    و رد می‌شوند، bool شمارنده نیست، و شاخهٔ «تونرِ تازه» همان name/remaining/max و
    آستانه‌هایِ empty/critical/low/ok را می‌سازد. شمارشِ برگشتی = تعدادِ تونرهایی که
    تازه شدند (همان چیزی که legacy لاگ می‌کند).
    """
    merged = 0
    for color_key, pct in scraped.items():
        if color_key.startswith("_") or not isinstance(pct, (int, float)) or isinstance(pct, bool):
            continue
        pct = int(pct)
        t = toners.get(color_key)
        if not t:
            toners[color_key] = {
                "level": pct, "status": "unknown",
                "name": f"{color_key} toner", "remaining": -1, "max": -1,
                "source": source,
            }
            merged += 1
            continue
        if t.get("level") is None:
            t["level"] = pct
            t["source"] = source
            if pct == 0:
                t["status"] = "empty"
            elif pct <= 10:
                t["status"] = "critical"
            elif pct <= 25:
                t["status"] = "low"
            else:
                t["status"] = "ok"
            merged += 1
    return merged


def _prefer_scraped_canon_toners(toners: dict, scraped: dict) -> int:
    """اولویتِ HTTP بر SNMP، سطر‌به‌سطر از `collect_canon:329-347`.

    در کد دو قاعدهٔ canon داریم: `collect_canon:325` («همیشه اسکریپ کن، HTTP برنده است»،
    `source="http_scrape"`) و `enhanced_collector:1199` («فقط وقتی هیچ `raw_level` نداریم»،
    `source="canon_remote_ui"`). آینه اولی را می‌گیرد، چون شکلِ این فایل از
    `collect_canon` برگرفته است و `source` هم باید همان چیزی باشد که UI/DB برای آن شکل
    می‌شناسند.

    canon «fallback» نیست: `collect_canon` اسکریپ را **همیشه** صدا می‌زند و اگر برای کلیدی
    عدد داشت، همان کلید را با `remaining=None`/`max=100`/`source="http_scrape"` و
    نامِ پیش‌فرضِ برند بازنویسی می‌کند. آینه هم همین کار را می‌کند؛ قاعدهٔ «فقط وقتی SNMP
    ناقص است» برای canon واگرایی می‌ساخت — ولی برنامهٔ گام H.1 همان قاعدهٔ یکنواخت را خواست،
    پس آینه اول `_toner_level_missing` را می‌پرسد و *بعد* این بازنویسی را می‌کند؛ تفاوتِ
    باقی‌مانده (کی اسکریپ می‌زنیم) در `apply_toner_http_fallback` و `docs/phase-h-decisions.md`
    §H.1 ثبت و با تست قفل شده است.
    """
    merged = 0
    for _idx, key, default_name in _CANON_TONER_MAP:
        if key not in scraped:
            continue
        level = scraped[key]
        if level is None:
            continue
        if level == 0:
            status = "empty"
        elif level <= 10:
            status = "critical"
        elif level <= 25:
            status = "low"
        else:
            status = "ok"
        toners[key] = {"level": level, "status": status, "name": default_name,
                       "remaining": None, "max": 100, "source": "http_scrape"}
        merged += 1
    return merged


def _merge_scraped(toners: dict, brand: str, scraped: dict) -> int:
    """تکِ درِ merge در این فایل — **فقط deleg می‌کند** (تصمیمِ گام H.1، بندِ ۱-۳).

    `collect_via_exporter`/`apply_toner_http_fallback` هیچ‌وقت مستقیمِ `_merge_scraped_toners`
    یا `_prefer_scraped_canon_toners` را صدا نمی‌زنند؛ انتخابِ قاعده این‌جا و فقط با نگاه‌کردن
    به `brand` انجام می‌شود. دو قاعدهٔ زیر همچنان **کپیِ وفادار**‌اند (چرا کپی و نه import:
    `docs/phase-h-decisions.md` §H.1) و متنشان با
    `tests/test_exporter_adapter.py::test_merge_functions_are_faithful_copies` قفل شده.

    HP: پرکردنِ فیلدهایِ خالی + نردبانِ `0/10/25` (`source="hp_web"`).
    canon: جاگذاریِ رکوردِ کاملِ HTTP (`remaining=None`/`max=100`/`source="http_scrape"`)،
    که سطر‌به‌سطر `collect_canon:329-347` است.
    """
    if brand == "canon":
        return _prefer_scraped_canon_toners(toners, scraped)
    return _merge_scraped_toners(toners, scraped, "hp_web")


def apply_toner_http_fallback(shape: dict, brand: str, ip: str) -> int:
    """تکمیلِ `shape["toners"]` با وب‌اسکرپِ خودِ دستگاه. تعدادِ تازه‌شده‌ها را برمی‌گرداند.

    - **کجا صدا زده می‌شود:** فقط `collect_via_exporter` (مسیرِ runtime). خودِ
      `exporter_to_collector_shape` خالص می‌ماند — وگرنه `tools/compare_snmp_sources.py`
      که همان تابع را برای مقایسهٔ آفلاین مصرف می‌کند، ناگهانی شبکه می‌زد.
    - **هیچ استثنایی بیرون نمی‌رود:** legacy هم همین‌جا `try/except` دارد (خطای وب فقط
      debug است و اعدادِ SNMP سرِ جایشان می‌مانند).
    - **merge:** فقط از راهِ `_merge_scraped(...)`؛ این تابع چیزی را بازنویسی نمی‌کند.
    - **trigger، هر دو برند:** فقط وقتی `_toner_level_missing(toners)` — قاعدهٔ یکنواختِ
      برنامهٔ H.1. `collect_canon` اما اسکریپ را **همیشه** می‌زند (`canon.py:325`)، پس روی
      canonِ سالم دو مسیر فرق می‌کنند؛ واگرایی پین‌شده:
      `test_canon_divergence_from_the_collector_is_pinned`.
    - `_used`/سریالِ EWS آینه نمی‌شود (کلیدهایش در قراردادِ ۱۲تاییِ G.5 نیست — TD-017) و
      mirrorِ toshiba/brother هم TD-018 است. چیزی که بینِ دو برند فرق می‌کند *شکلِ
      بازنویسی* است: canon رکوردِ HTTP را جاگذاری می‌کند، HP فیلدهایِ خالی را پر می‌کند.
    """
    toners = shape.get("toners")
    if not isinstance(toners, dict):
        return 0
    if not _toner_level_missing(toners):    # قاعدهٔ یکنواختِ H.1: هر دو برند فقط وقتی SNMP
        return 0                            # عددی برای هیچ تونرِ واقعی ندارد (برنامهٔ مالک)
    try:
        if brand == "canon":
            scraped = _scrape_canon_toners(ip, timeout=CANON_HTTP_TIMEOUT)
        else:
            scraped = _scrape_hp_toners(ip, timeout=HP_HTTP_TIMEOUT)
        if not scraped:
            return 0
        return _merge_scraped(toners, brand, scraped)
    except Exception as exc:            # همان وسواسِ legacy: وب هر چه خواست، snapshot نمی‌میرد
        log.debug("exporter: وب‌اسکرپِ تونرِ %s انجام نشد (%s) — اعدادِ SNMP دست‌نخورده", ip, exc)
        return 0


def _trays(brand: str, metrics: dict) -> list[dict]:
    """سینی‌ها از `prtInputTable`؛ برچسبِ پیش‌فرضِ هر برند از کدِ خودش."""
    slots = ((1, "Tray 1"), (2, "Tray 2")) if brand == "hp" else ((1, "Bypass"), (2, "Tray 1"))
    trays = []
    for idx, label in slots:
        cap = _int(metrics, f"tray{idx}_cap", 0)
        lvl = _int(metrics, f"tray{idx}_level", -9)
        nm = _text(metrics, f"tray{idx}_name", label) or label
        if cap == 0 and lvl == -9:
            continue
        if lvl == -2:
            status = "no_sensor"
        elif brand == "canon":
            status = "empty" if lvl <= 0 else None
        elif lvl == -3 or lvl <= 0:
            status = "empty"
        else:
            status = None
        if status is None:
            if cap > 0:
                pct = round(lvl / cap * 100)
                status = "low" if pct <= 25 else ("medium" if pct <= 75 else "ok")
            else:
                status = "unknown"
        trays.append({"name": nm, "level": lvl, "capacity": cap, "status": status})
    return trays


def _alerts(metrics: dict) -> list[dict]:
    """تنها هشداری که spec فعلی می‌دهد: درِ باز (`prtCoverStatus …1.1`).

    `printer_canon` هم متریکِ در ندارد، پس همین قاعدهٔ مشترک برای هر دو برند.
    """
    cover = _int(metrics, "cover_status", 4)
    return [] if cover == 4 else [{"message": "درب پرینتر باز است", "code": cover}]


def _counters(brand: str, metrics: dict, toners: dict) -> dict:
    if brand == "hp":
        return _counters_hp(metrics, toners)
    return _counters_canon(metrics)


def _counters_hp(metrics: dict, toners: dict) -> dict:
    total = _int(metrics, "bro_total", 0)
    color_print = _int(metrics, "hp_print_color", -1)
    copy_mono = _int(metrics, "hp_copy_mono", -1)
    copy_color = _int(metrics, "hp_copy_color", -1)
    fax = _int(metrics, "hp_fax", -1)
    copy_total = max(copy_mono, 0) + max(copy_color, 0)
    has_color_toner = any(key in ("cyan", "magenta", "yellow") for key in toners)
    if color_print >= 0:
        full_color, black_white = color_print, max(0, total - color_print)
    elif has_color_toner:
        full_color = black_white = None
    else:
        full_color, black_white = None, total
    return {
        "total": total,
        "full_color": full_color,
        "black_white": black_white,
        "printer": total,
        "copy": copy_total if copy_total > 0 else None,
        "fax": fax if fax >= 0 else None,
    }


def _counters_canon(metrics: dict) -> dict:
    total_std = _int(metrics, "bro_total", -1)
    color_std = _int(metrics, "bro_color", -1)
    bw_std = _int(metrics, "bw_std", -1)
    total_mf = _int(metrics, "canon_total", -1)
    copy_mf = _int(metrics, "canon_copy", -1)
    printer_mf = _int(metrics, "canon_print", -1)
    total_lbp = _int(metrics, "canon_lbp_total", -1)
    color_lbp = _int(metrics, "canon_lbp_color", -1)
    alt_total = _int(metrics, "alt_total", -1)
    alt_copy = _int(metrics, "alt_copy", -1)
    alt_print = _int(metrics, "alt_print", -1)

    if color_std >= 0 and total_std >= 0 and color_std >= total_std:
        color_std = -1  # canon.py: رنگ ≥ کل ⇒ دادهٔ رنگ بی‌اعتبار
    is_mf = total_mf >= 0 or alt_total >= 0
    if not is_mf and "MF" in _text(metrics, "canon_model", "").upper():
        is_mf = True  # canon.py: تشخیصِ MF از نامِ مدل وقتی شمارندهٔ MF ندارد

    if is_mf:
        if total_mf < 0 and alt_total >= 0:
            total_mf, copy_mf, printer_mf = alt_total, alt_copy, alt_print
        total = total_mf if total_mf >= 0 else (total_std if total_std >= 0 else 0)
        copy_ = copy_mf if copy_mf >= 0 else -1
        printer = printer_mf if printer_mf >= 0 else -1
        if copy_ < 0 and printer < 0 and total > 0:
            printer, copy_ = total, 0
    else:
        total = total_lbp if total_lbp >= 0 else (total_std if total_std >= 0 else 0)
        if total == 0:
            alt_lbp = _int(metrics, "alt_lbp_total", -1)
            if alt_lbp >= 0:
                total = alt_lbp
        copy_, printer = -1, total
    if total < 0:
        total = 0

    full_color = black_white = None
    if is_mf:
        if 0 <= color_std < total:
            full_color = color_std
        if 0 <= bw_std <= total:
            black_white = bw_std
    else:
        if 0 <= color_lbp < total:
            full_color = color_lbp
        elif 0 <= color_std < total:
            full_color = color_std
        if 0 <= bw_std <= total:
            black_white = bw_std
    if black_white is None and full_color is not None and total > 0:
        black_white = max(0, total - full_color)
    if full_color is None and black_white is None and total > 0:
        black_white = total

    return {
        "total": total,
        "full_color": full_color,
        "black_white": black_white,
        "printer": printer if printer >= 0 else None,
        "copy": copy_ if (is_mf and copy_ >= 0) else None,
        "fax": None,
    }


# ── دروازهٔ «دادهٔ معنادار هست؟» ──────────────────────────────────────────
_ESSENTIAL = ("bro_total", "sys_uptime", "sys_descr", "model_mib")
_VENDOR_MODEL = {"hp": ("hp_model", "hp_print_color"), "canon": ("canon_model", "canon_total")}


def _has_signal(brand: str, metrics: dict) -> bool:
    """اگر نه متریکِ استاندارد باشد و نه vendor، پاسخِ exporter بی‌فایده است."""
    if not metrics:
        return False
    names = set(metrics)
    return bool(names & set(_ESSENTIAL)) or bool(names & set(_VENDOR_MODEL.get(brand, ())))


def exporter_to_collector_shape(ip: str, brand: str, metrics: dict, *,
                                name: str = "", start: float | None = None) -> dict | None:
    """`dict` با شکلِ `collect_{hp,canon}`، یا `None` اگر قابل‌ساخت نباشد.

    کلیدهای خروجی دقیقاً کلیدهایِ returnِ `collect_hp` است (`ip`, `name`, `brand`,
    `online`, `last_poll`, `poll_ms`, `device`, `counters`, `paper_sizes`, `trays`,
    `toners`, `alerts`) — بدونِ کلیدِ جدید، تا مصرف‌کننده‌ها چیزی برای «حالتِ exporter»
    لازم نداشته باشند.
    """
    brand = (brand or "").strip().lower()
    if brand not in SUPPORTED_BRANDS or not _has_signal(brand, metrics):
        return None
    toners = _toners(brand, metrics)
    elapsed = int((time.time() - start) * 1000) if start is not None else 0
    return {
        "ip": ip,
        "name": name or ip,
        "brand": brand,
        "online": True,
        "last_poll": utc_now_iso(),
        "poll_ms": elapsed,
        "device": _device(brand, metrics),
        "counters": _counters(brand, metrics, toners),
        "paper_sizes": {},
        "trays": _trays(brand, metrics),
        "toners": toners,
        "alerts": _alerts(metrics),
    }


#: برندهایِ آینه‌سازی‌نشده‌ای که یک‌بار درباره‌شان هشدار داده‌ایم — برایِ هر چرخهٔ poll یک
#: هشدارِ تکراری خبرِ تازه‌ای ندارد (مسیرِ legacy برای این برندها سالم است) و سیلِ لاگ هم
#: می‌سازد؛ H.2 همین‌جا stateِ per-device را می‌گیرد.
_WARNED_UNSUPPORTED: set = set()


def collect_via_exporter(client: ExporterClient, ip: str, name: str, brand: str,
                         start: float | None = None, auth: str | None = None) -> dict | None:
    """ماژول‌های برند را می‌خواند و شکلِ collector را می‌سازد. `None` ⇒ «به مسیرِ مستقیم برگرد».

    هیچ استثنایی بیرون نمی‌رود: `ExporterError` (و هر خطای غیرمنتظرهٔ دیگر) به `None`
    تبدیل و لاگ می‌شود، چون exporter در این فاز یک **امکان** است نه یک وابستگیِ poll.
    """
    norm = (brand or "").strip().lower()
    if norm not in SUPPORTED_BRANDS:
        key = (norm or "?", ip)
        if key not in _WARNED_UNSUPPORTED:   # یک هشدار به ازای (brand, ip) — نه هر ۶۰ ثانیه
            _WARNED_UNSUPPORTED.add(key)
            log.warning("exporter: برندِ «%s» آینه‌سازی نشده ⇒ هیچ کوئری‌ای به exporter "
                        "نمی‌رود و مسیرِ مستقیم می‌نویسد (محدودهٔ H.1 = hp/canon؛ "
                        "mirrorِ toshiba/brother = TD-018)", key[0])
        return None
    try:
        metrics = client.query_brand(ip, brand, auth or DEFAULT_AUTH)
    except ExporterCircuitOpen as exc:      # گام H.2 — «نوبتِ ما نیست» نه «خطا»: بی‌سروصدا
        log.debug("exporter: %s در backoff است (%s) — این چرخه مسیرِ مستقیم می‌نویسد", ip, exc)
        return None
    except ExporterError as exc:
        log.warning("exporter: خواندنِ %s نشد (%s) — مسیرِ مستقیم", ip, exc)
        return None
    except Exception as exc:  # هر چیزِ غیرمنتظره‌ای؛ poller نباید بمیرد
        log.warning("exporter: خطای غیرمنتظره برای %s: %s — مسیرِ مستقیم", ip, exc)
        return None
    shape = exporter_to_collector_shape(ip, brand, metrics, name=name, start=start)
    if shape is None:
        log.warning("exporter: پاسخِ %s برای شکلِ «%s» کافی نبود — مسیرِ مستقیم", ip, brand)
        return None
    merged = apply_toner_http_fallback(shape, shape["brand"], ip)
    if merged:
        log.info("exporter: %s تونرِ %s از وب‌اسکرپِ دستگاه تکمیل/تصحیح شد (brand=%s)",
                 merged, ip, shape["brand"])
    return shape
