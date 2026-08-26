# -*- coding: utf-8 -*-
"""خواندن «شناسه‌ی یکتای کارتریج» برای تشخیص قطعی تعویض کارتریج (CARTRIDGE_CHANGED).

سه منبع شواهد، به ترتیب قوت:

1) **شناسه‌ی تراشه / شماره سریال کارتریج** (Chip ID / Supply Serial) — قطعی:
   هر کارتریج، شناسه‌ی متفاوتی دارد؛ تغییرش یعنی کارتریج عوض شده.
   منابع:
     - SNMP/OID اختصاصیِ هر مدل: از `config/cartridge_id_map.json` خوانده
       می‌شود (الگوی config-driven تا بدون تغییر کد و با تأیید diagnose روی
       ناوگان واقعی فعال شود؛ نمونه: cartridge_id_map.example.json).
     - پنل وب HP (EWS): صفحه‌های Supplies/Status مدل‌های LaserJet معمولاً ردیف
       «Serial Number» کارتریج را برای کارتریج‌های اصل نمایش می‌دهند.

2) **شمارنده‌ی «صفحات چاپ‌شده با این کارتریج»** (Pages printed with this
   supply) — شبه‌قطعی: این عدد فقط صعودی است؛ افت معنی‌دارش (مثلاً ۳۲,۰۰۰ → ۱۵)
   یعنی کارتریج عوض شده. روی EWS مدل‌های HP در دسترس است و حتی برای کارتریج‌های
   سازگار (بدون سریال معتبر) کار می‌کند.

3) چیزی پیدا نشد → دیکشنری خالی؛ موتور رویداد سراغ fallback جهش سطح می‌رود
   (همان مسیر موجود، دست‌نخورده).

نکته‌ی صادقانه: بسیاری از دستگاه‌ها (مخصوصاً Canon i-SENSYS / Brother / Toshiba
e-STUDIO) شناسه‌ی تراشه را هیچ‌جای عمومی منتشر نمی‌کنند. برای آن‌ها
`tools/diagnose_printer.py` موارد کاندید را کاوش می‌کند و پس از تأیید روی
دستگاه واقعی، OID در فایل تنظیمات فعال می‌شود — بدون تغییر کد.

عملکرد: نتیجه به‌ازای هر IP با TTL کش می‌شود (موفق ۱۰ دقیقه / ناموفق ۶۰ دقیقه)
تا سربار پال‌کردن ۶۰ثانیه‌ای ناوگان نزدیک به صفر بماند.
"""
import json
import logging
import os
import re
import time

from core.snmp.protocol import snmp_get_with_fallback

log = logging.getLogger("PrinterMonitor")

_TTL_FOUND = 600.0    # شناسه پیدا شد: ۱۰ دقیقه کش
_TTL_MISS = 3600.0    # چیزی پیدا نشد: ۱ ساعت بک‌آف (دستگاهها دیر عوض می‌شوند)
_cache = {}           # ip -> (expire_ts, result_dict)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(_REPO_ROOT, "config", "cartridge_id_map.json")


# ─────────────────────────────────────────────────────────────────────────────
# ابزارهای اعتبارسنجی/نرمال‌سازی شناسه
# ─────────────────────────────────────────────────────────────────────────────
_BAD_TOKENS = {
    "", "-", "--", "0", "00", "000", "0000", "000000", "n/a", "na", "none",
    "null", "unknown", "not available", "notAvailable".lower(), "(not available)",
    "serial", "sn", "s/n", "serialnumber",
}


def normalize_id(value):
    """مقدار SNMP/وب را به شناسه‌ی قابل‌اعتماد تبدیل می‌کند؛ غیرمعتبر → None.

    قواعد: ۴ تا ۴۸ نویسه، حداقل یک حرف/رقم، نویسه‌های مجاز، و نبودن در فهرست
    توکن‌های بی‌معنا. بسیاری دستگاه‌ها در نبود سریال رشته‌هایی مثل "Unknown"
    یا "0" برمی‌گردانند که نباید به‌عنوان شناسه‌ی تراشه مصرف شوند.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("latin-1", "replace")
        except Exception:
            return None
    s = str(value).strip().strip("\x00").strip()
    if not (4 <= len(s) <= 48):
        return None
    if s.lower() in _BAD_TOKENS:
        return None
    if s.lower().startswith(("unknown", "not avail")):
        return None
    if not re.search(r"[A-Za-z0-9]", s):
        return None
    if not re.fullmatch(r"[A-Za-z0-9" + re.escape("-_/:.,#()[] ") + r"]+", s):
        return None
    return s


# ─────────────────────────────────────────────────────────────────────────────
# تنظیمات (config-driven OID map)
# ─────────────────────────────────────────────────────────────────────────────
_config_cache = {"ts": 0.0, "data": None}
_CONFIG_TTL = 300.0


def load_id_config(force: bool = False):
    """نقشه‌ی OIDهای تأییدشده را می‌خواند؛ فایل اختیاری است.

    ساختار (نمونه در cartridge_id_map.example.json):
      {
        "brands":   {"hp": {"snmp": {"black": "1.3.6.1...."}}, ...},
        "printers": {"172.16.8.52": {"snmp": {"black": "1.3.6.1...."}}},
        "hp_web": true
      }
    """
    now = time.time()
    if not force and _config_cache["data"] is not None and now - _config_cache["ts"] < _CONFIG_TTL:
        return _config_cache["data"]
    data = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except FileNotFoundError:
        data = {}
    except Exception as exc:
        log.warning("cartridge_id_map.json خوانده نشد: %s", exc)
        data = {}
    _config_cache["ts"] = now
    _config_cache["data"] = data
    return data


def _snmp_oids_for(ip: str, brand: str, cfg: dict):
    """ترکیب نقشه‌ی برند + override دستگاه؛ override اولویت دارد."""
    out = {}
    try:
        out.update(((cfg.get("brands") or {}).get(brand or "") or {}).get("snmp") or {})
        out.update(((cfg.get("printers") or {}).get(ip) or {}).get("snmp") or {})
    except Exception:
        pass
    return {str(k).lower(): str(v) for k, v in out.items() if v}


def _read_snmp_ids(ip: str, oids_by_color: dict, community: str, snmp_version=None, timeout: float = 1.2) -> dict:
    """خواندن شناسه‌ها از SNMP طبق نقشه‌ی تنظیمات؛ جاهای خالی رد می‌شوند."""
    ids = {}
    if not oids_by_color:
        return ids
    g = (lambda oid: snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=timeout))
    for color, oid in oids_by_color.items():
        try:
            sid = normalize_id(g(oid))
        except Exception:
            sid = None
        if sid:
            ids[color] = sid
            log.info("  [%s] شناسه کارتریج %s از SNMP (%s): %s", ip, color, oid, sid)
    return ids


# ─────────────────────────────────────────────────────────────────────────────
# خواندن از پنل وب HP (EWS): سریال کارتریج + «صفحات چاپ‌شده با این کارتریج»
# ─────────────────────────────────────────────────────────────────────────────
_HP_URLS = (
    "http://{ip}/hp/device/InternalPages/Index?id=SuppliesStatus",
    "https://{ip}/hp/device/InternalPages/Index?id=SuppliesStatus",
    "http://{ip}/DevMgmt/ConsumableConfigDyn.xml",
    "https://{ip}/DevMgmt/ConsumableConfigDyn.xml",
    "http://{ip}/hp/device/info_suppliesStatus.html",
    "http://{ip}/info_suppliesStatus.html",
)

# سریال: نزدیکِ برچسب Serial/Serial Number (در HTML و XML)
# نکته: مقدار ممکن است بعد از چند تگ‌بستن/بازکردن بیاید: <td>Serial:</td><td>VAL</td>
_HP_SERIAL_PATTERNS = (
    re.compile(
        r"Serial\s*(?:Number|No\.?|#)?\s*[:：]?\s*(?:</\w+>\s*)?(?:<[^>]*>\s*){0,3}"
        r"([A-Za-z0-9][A-Za-z0-9\-]{3,30})", re.IGNORECASE),
    re.compile(r"<[A-Za-z0-9:]*SerialNumber>\s*([A-Za-z0-9][A-Za-z0-9\-]{3,30})\s*<", re.IGNORECASE),
)
# «صفحات چاپ‌شده با این کارتریج» (انگلیسی EWS؛ معادل XML)
_HP_SUPPLY_PAGES_PATTERNS = (
    re.compile(r"Pages\s+printed\s+with\s+this\s+supply[^0-9]{0,40}([0-9][0-9,]{0,12})", re.IGNORECASE),
    re.compile(r"PagesWithSupply[^0-9]{0,20}([0-9][0-9,]{0,12})", re.IGNORECASE),
    re.compile(r"<[A-Za-z0-9:]*PagesPrintedWithSupply>\s*([0-9][0-9,]{0,12})\s*<", re.IGNORECASE),
)


def _first_int(text: str):
    if not text:
        return None
    try:
        return int(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _read_hp_web_ids(ip: str, timeout: float = 3.5):
    """خواندن سریال/شمارنده‌ی کارتریج HP از EWS؛ نبود → دیکشنری خالی.

    توجه: ناوگان فعلی HPها مونو است؛ نتیجه روی کلید ``black`` می‌نشیند. برای
    مدل‌های رنگی (اگر بعداً اضافه شوند) باید بخش‌بندی per-color اضافه شود.
    """
    from core.collectors.base import fetch_first_web_page

    used, html = fetch_first_web_page(ip, [u.format(ip=ip) for u in _HP_URLS], timeout)
    if not html:
        return {}
    ids, pages = {}, {}
    for pat in _HP_SERIAL_PATTERNS:
        m = pat.search(html)
        if m:
            sid = normalize_id(m.group(1))
            if sid:
                ids["black"] = sid
                break
    for pat in _HP_SUPPLY_PAGES_PATTERNS:
        m = pat.search(html)
        if m:
            val = _first_int(m.group(1))
            if val is not None:
                pages["black"] = val
                break
    if ids or pages:
        log.info("  [%s] شواهد EWS کارتریج HP: سریال=%s صفحات‌باکارتریج=%s",
                 ip, ids.get("black"), pages.get("black"))
    return {"ids": ids, "supply_pages": pages} if (ids or pages) else {}


# ─────────────────────────────────────────────────────────────────────────────
# خواندن از پنل وب Canon / Brother / Toshiba
# ─────────────────────────────────────────────────────────────────────────────
def _read_other_web_ids(ip: str, brand: str, timeout: float = 3.0):
    """Canon/Brother/Toshiba: سریال تراشه معمولاً در صفحات عمومی نیست.

    بسیاری Remote UI های i-SENSYS و صفحات status برادر فقط سطح تونر را
    نشان می‌دهند. این خواننده عمداً محافظه‌کار است: فقط الگوی صریح
    «Serial Number» را برمی‌دارد؛ در نبودش چیزی برنمی‌گرداند (fallback سطح
    همچنان پوشش می‌دهد). تأیید واقع‌ی با diagnose انجام می‌شود.
    """
    from core.collectors.base import fetch_first_web_page

    urls = []
    if brand == "canon":
        urls = [f"http://{ip}/", f"https://{ip}/"]
    elif brand == "brother":
        urls = [f"http://{ip}/general/status.html", f"http://{ip}/"]
    elif brand in ("toshiba", "toshiba_tec"):
        urls = [f"http://{ip}/?MAIN=DEVICE", f"http://{ip}/"]
    if not urls:
        return {}
    used, html = fetch_first_web_page(ip, urls, timeout)
    if not html:
        return {}
    ids = {}
    m = re.search(
        r"Serial\s*(?:Number|No\.?|#)?\s*[:：]?\s*(?:</\w+>\s*)?(?:<[^>]*>\s*){0,3}"
        r"([A-Za-z0-9][A-Za-z0-9\-]{3,30})", html, re.IGNORECASE)
    if m:
        sid = normalize_id(m.group(1))
        if sid:
            ids["black"] = sid
            log.info("  [%s] شناسه کارتریج (%s web): %s", ip, brand, sid)
    return {"ids": ids, "supply_pages": {}} if ids else {}


# ─────────────────────────────────────────────────────────────────────────────
# نقطه‌ی تجمیع (با کش TTL)
# ─────────────────────────────────────────────────────────────────────────────
def get_cartridge_identity_data(ip: str, brand: str = None, community: str = "public",
                                snmp_version=None, force_refresh: bool = False) -> dict:
    """شناسه‌ها و شمارنده‌های کارتریج یک دستگاه را برمی‌گرداند.

    خروجی: {"ids": {color: serial}, "supply_pages": {color: pages}, "source": str}
    در نبود شواهد: {"ids": {}, "supply_pages": {}, "source": "none"} (کش‌شده).
    """
    if not ip:
        return {"ids": {}, "supply_pages": {}, "source": "none"}
    now = time.time()
    cached = _cache.get(ip)
    if cached and not force_refresh and cached[0] > now:
        return cached[1]

    cfg = load_id_config()
    brand_key = (brand or "").lower()
    ids, pages, sources = {}, {}, []

    # ۱) SNMP بر اساس نقشه‌ی تأییدشده (پرینتر-خاص یا برند-عمومی)
    oids = _snmp_oids_for(ip, brand_key, cfg)
    if oids:
        try:
            snmp_ids = _read_snmp_ids(ip, oids, community, snmp_version)
        except Exception as exc:
            log.debug("cartridge-id snmp read failed for %s: %s", ip, exc)
            snmp_ids = {}
        if snmp_ids:
            ids.update(snmp_ids)
            sources.append("snmp")

    # ۲) پنل وب (HP همیشه‌تلاش؛ بقیه اگر web=true در تنظیمات یا پیش‌فرض محدود)
    web = {}
    try:
        if brand_key == "hp" and cfg.get("hp_web", True):
            web = _read_hp_web_ids(ip)
        elif cfg.get("other_web", True):
            web = _read_other_web_ids(ip, brand_key)
    except Exception as exc:
        log.debug("cartridge-id web read failed for %s: %s", ip, exc)
        web = {}
    if web.get("ids"):
        for ck, sid in web["ids"].items():
            ids.setdefault(ck, sid)
        sources.append(f"{brand_key}_web")
    if web.get("supply_pages"):
        pages.update(web["supply_pages"])

    result = {"ids": ids, "supply_pages": pages,
              "source": "+".join(sources) if sources else "none"}
    ttl = _TTL_FOUND if (ids or pages) else _TTL_MISS
    _cache[ip] = (now + ttl, result)
    return result


def clear_cache():
    _cache.clear()
