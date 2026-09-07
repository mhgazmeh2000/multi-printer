# -*- coding: utf-8 -*-
"""کشف خودکار OID شناسه‌ی یکتای کارتریج (Chip ID / Supply Serial).

هدف: بدون هیچ تنظیم دستی، برای همه‌ی پرینترهای HP / Canon / Brother / Toshiba
شناسه‌ی یکتای کارتریج پیدا و «تأیید» شود تا موتور موجود CARTRIDGE_CHANGED
(core/collectors/base.py) بتواند تعویض کارتریج را قطعی تشخیص دهد.

سه مرحله:

۱) **کشف (walk):** زیرشاخه‌ی استاندارد Printer-MIB
   (prtMarkerSuppliesDescription — فقط برای نگاشت شاخص→رنگ) و زیرشاخه‌های
   vendor هر برند با GETNEXT محدود (max_vars) پیمایش می‌شود؛ هر مقدار رشته‌ای
   که فیلتر سخت‌گیرانه‌ی ``normalize_id()`` (از cartridge_id.py) را پاس کند و
   «شبیه سریال» باشد، کاندیدای Chip ID آن رنگ ثبت می‌شود.

۲) **دروازه‌ی ثبات (Stability Gate):** مقدار کاندیدا فقط وقتی تأیید می‌شود که
   در ``STABILITY_HITS`` پروب متوالی دقیقاً یکسان بماند. مقادیر پویا (سطح تونر،
   وضعیت، شمارنده‌ها) بین پروب‌ها تغییر می‌کنند و حذف می‌شوند؛ شناسه‌ی تراشه
   ثابت است.

۳) **ذخیره‌سازی:** OID تأییدشده به‌صورت اتمیک (temp + os.replace) در
   ``config/cartridge_id_map.json`` زیر کلید ``learned`` نوشته می‌شود.
   ورودی‌های دستی کاربر (کلیدهای brands/printers) هرگز بازنویسی نمی‌شوند و
   اولویت دارند. بعد از تأیید، خواننده‌ی runtime (cartridge_id.py) به‌طور
   خودکار همان OIDها را GET می‌کند.

زمان‌بندی: ``queue_discovery`` از مسیر poll صدا زده می‌شود (فقط وقتی برای IP
هنوز شناسه‌ای پیدا نشده) و worker پس‌زمینه‌ی تک‌نخ، پرینترها را یکی‌یکی با
فاصله‌ی زمانی پردازش می‌کند تا ناوگان زیر بار SNMP نرود.
"""
import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime

from core.collectors.cartridge_id import (
    normalize_id,
    load_id_config,
    CONFIG_PATH,
    clear_cache as clear_id_cache,
)

log = logging.getLogger("PrinterMonitor")

# ─────────────────────────────────────────────────────────────────────────────
# تنظیمات کشف
# ─────────────────────────────────────────────────────────────────────────────
STABILITY_HITS = 3          # تعداد پروب متوالی یکسان برای تأیید
PROBE_GAP_SECONDS = 20.0    # فاصله‌ی پروب‌های یک پرینتر
MAX_PASSES = 6              # سقف پروب هر پرینتر در هر جلسه‌ی کشف
COOLDOWN_SECONDS = 6 * 3600 # بعد از شکست، تا ۶ ساعت دوباره تلاش نشود
QUEUE_RECHECK_SECONDS = 3600  # حداقل فاصله‌ی صف برای یک IP
WALK_TIMEOUT = 1.5
WALK_MAX_VARS = 48

# زیرشاخه‌های کاندید. «standard» برای نگاشت رنگ است؛ بقیه برای یافتن سریال.
CANDIDATE_SUBTREES = {
    "standard": (
        "1.3.6.1.2.1.43.11.1.1.6",   # prtMarkerSuppliesDescription
    ),
    "hp": (
        "1.3.6.1.4.1.11.2.3.9.4.2.1.1.6",
        "1.3.6.1.4.1.11.2.3.9.4.2.1.4",
    ),
    "brother": (
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.1.6",
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5",
    ),
    "canon": (
        # 1602.1.11.1.2.0 = شناسه مدل فریمور (07b2010100000000) — یکسان
        # برای همه Canon MF ها، غیرقابل‌تغییر، و فاقد ارزش تشخیصی.
        # Canon سریال کارتریج اختصاصی از طریق SNMP منتشر نمی‌کند.
    ),
    "toshiba": (
        # Toshiba سریال کارتریج اختصاصی از طریق SNMP منتشر نمی‌کند.
        # OIDهای .1.2 و .1.3 فقط نسخه فریمور و وضعیت supply برمی‌گردانند
        # (غیرقابل‌تغییر و غیرunique). تشخیص تعویض از طریق fallback
        # (جهش سطح تونر / ریست صفحات) انجام می‌شود.
    ),
}

_COLOR_KEYWORDS = (
    ("black", ("black", " k)", " bk", "k toner", "مشکی")),
    ("cyan", ("cyan", " c)", "مشکی‌نما", "فیروزه")),
    ("magenta", ("magenta", " m)", "ارغوان")),
    ("yellow", ("yellow", " y)", "زرد")),
)

# ─────────────────────────────────────────────────────────────────────────────
# وضعیت درون-حافظه
# ─────────────────────────────────────────────────────────────────────────────
_stability_lock = threading.Lock()
_stability = {}          # ip -> {oid: {"value": str, "hits": int, "color": str}}
_queue = deque()
_queue_set = set()
_queue_lock = threading.Lock()
_last_attempt = {}       # ip -> monotonic ts (برای cooldown)
_worker_started = False


# واژه‌های نامرتبط با سریال: نام مدل/برند، پیام‌های وضعیت و نگهداری.
# داده‌ی واقعی ناوگان نشان داد برخی دستگاه‌ها رشته‌هایی مثل
# "TOSHIBA e-STUDIO3015AC" یا "The Time for Periodic Maintenance" برمی‌گردانند
# که ثابت‌اند (دروازه‌ی ثبات ردشان نمی‌کند) ولی شناسه‌ی کارتریج نیستند.
_SERIAL_STOPWORDS = (
    "toshiba", "e-studio", "estudio", "hewlett", "laserjet", "jetdirect",
    "canon", "brother", "ricoh", "kyocera", "samsung", "epson", "xerox",
    "lexmark", "develop", "ineo", "sinium",
    "maintenance", "periodic", "error", "ready", "please", "call",
    "service", "replace", "paper", "jam", "door", "cover", "open",
    "online", "offline", "idle", "printing", "warming", "status",
    "message", "display",    "version", "firmware", "model", "serial no",
    "total", "count", "bw", "large", "small", "color",
    "print", "copy", "scan", "fax",
)



def _looks_like_serial(value) -> bool:
    """فیلتر سریال‌مانند: رشته، دارای حرف، خالص‌عددی نبودن، تراکم حرف/رقم،
    حداکثر ۳ واژه، و نبود واژه‌های نامرتبط (مدل/برند/پیام وضعیت)."""
    s = normalize_id(value)
    if not s:
        return False
    if s.isdigit():
        return False
    letters = sum(1 for ch in s if ch.isalpha())
    digits = sum(1 for ch in s if ch.isdigit())
    if letters == 0:
        return False
    # رد نسخه فریمور (V6.0.0.477.10, V13.0.0.520.905, …)
    if re.match(r'^[Vv]\d+(?:\.\d+)+$', s):
        return False
    # سریال واقعی معمولاً ترکیب متراکم حرف/رقم است؛ متن‌های توصیفی فاصله‌ی زیاد دارند
    if (letters + digits) < max(4, int(len(s) * 0.5)):
        return False
    # رد متن‌های رایج توصیفی که فیلتر normalize از آن‌ها می‌گذرد
    lowered = s.lower()
    if any(tok in lowered for tok in ("toner", "cartridge", "drum", "supply", "waste", "staple", "fuser", "kit")):
        return False
    # حداکثر ۳ واژه — سریال واقعی به‌ندرت چندکلمه‌ای است
    if len(lowered.split()) > 3:
        return False
    # واژه‌های نامرتبط (برند/مدل/پیام وضعیت و نگهداری)
    for stop in _SERIAL_STOPWORDS:
        if stop in lowered:
            return False
    return True


def _color_from_text(text: str):
    if not text:
        return None
    low = str(text).lower()
    for color, keywords in _COLOR_KEYWORDS:
        for kw in keywords:
            if kw in low:
                return color
    return None


def _color_from_description_index(desc_map: dict, oid: str):
    """نگاشت شاخص OID به رنگ سطر description همان شاخص در جدول استاندارد.

    جدول استاندارد: ``.6.<row>`` — شاخص آخرین جزء است. ولی OIDهای vendor
    اغلب با ``.<index>.0`` تمام می‌شوند؛ پس آخرین جزء و یکی مانده به انتها
    هر دو امتحان می‌شوند.
    """
    parts = oid.rsplit(".", 2)
    for idx in reversed(parts[1:]):
        desc = desc_map.get(idx)
        if desc:
            return _color_from_text(desc)
    return None


def _probe_pass(ip: str, brand: str, community: str, snmp_version=None) -> dict:
    """یک گذر کشف: walk شاخه‌ها و استخراج کاندیداهای {color: (oid, value)}.

    خروجی: {"candidates": {color: {"oid":…, "value":…}}, "desc_map": {index: text}}
    تزریق‌پذیر برای تست از طریق پارامتر walker (پیش‌فرض snmp_walk واقعی).
    """
    from core.snmp.protocol import snmp_walk

    brand_key = (brand or "").lower()
    subtrees = list(CANDIDATE_SUBTREES["standard"])
    subtrees += list(CANDIDATE_SUBTREES.get(brand_key, ()))

    desc_map = {}
    candidates = {}
    for prefix in subtrees:
        try:
            rows = snmp_walk(ip, prefix, community, timeout=WALK_TIMEOUT,
                             max_vars=WALK_MAX_VARS, version=snmp_version)
        except Exception as exc:
            log.debug("cartridge-discovery walk %s %s failed: %s", ip, prefix, exc)
            continue
        if not rows:
            continue
        is_desc_table = prefix in CANDIDATE_SUBTREES["standard"]
        for oid, value in rows:
            if is_desc_table:
                idx = oid.rsplit(".", 1)[-1]
                if isinstance(value, str) and value.strip():
                    desc_map[idx] = value.strip()
                continue
            if not _looks_like_serial(value):
                continue
            color = _color_from_text(str(value)) or _color_from_description_index(desc_map, oid)
            if not color:
                # Canon MF/LBP مونو هستند — اگر فقط یک کاندیدا باشد → black
                if brand == "canon" and len(candidates) == 0:
                    color = "black"
                else:
                    continue
            prev = candidates.get(color)
            if prev is None or len(str(value)) > len(str(prev["value"])):
                candidates[color] = {"oid": oid, "value": str(value)}
    return {"candidates": candidates, "desc_map": desc_map}


# ─────────────────────────────────────────────────────────────────────────────
# دروازه‌ی ثبات
# ─────────────────────────────────────────────────────────────────────────────
def _update_stability(ip: str, candidates: dict) -> dict:
    """به‌روزرسانی شمارنده‌ی ثبات؛ رنگ‌های رسیده به آستانه برمی‌گردد."""
    confirmed = {}
    with _stability_lock:
        state = _stability.setdefault(ip, {})
        for color, cand in candidates.items():
            oid, value = cand["oid"], cand["value"]
            entry = state.get(oid)
            if entry and entry.get("value") == value:
                entry["hits"] += 1
            else:
                entry = {"value": value, "hits": 1, "color": color}
                state[oid] = entry
            if entry["hits"] >= STABILITY_HITS:
                confirmed[color] = {"oid": oid, "value": value}
        # پاکسازی OIDهایی که دیگر کاندیدا نیستند (تغییر مقدار → ریست شده‌اند)
        live_oids = {cand["oid"] for cand in candidates.values()}
        for oid in list(state):
            if oid not in live_oids:
                state.pop(oid)
    return confirmed


def reset_stability(ip: str = None):
    with _stability_lock:
        if ip is None:
            _stability.clear()
        else:
            _stability.pop(ip, None)


# ─────────────────────────────────────────────────────────────────────────────
# ذخیره‌سازی اتمیک در cartridge_id_map.json (بخش learned)
# ─────────────────────────────────────────────────────────────────────────────
def _save_learned(ip: str, confirmed: dict, brand: str = "", model: str = "") -> bool:
    """نوشتن OIDهای تأییدشده زیر learned.printers.<ip>.snmp — بدون دست‌زدن به
    ورودی‌های دستی (brands/printers). اتمیک با os.replace."""
    try:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except FileNotFoundError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        learned = data.get("learned")
        if not isinstance(learned, dict):
            learned = {}
        printers = learned.get("printers")
        if not isinstance(printers, dict):
            printers = {}
        entry = printers.get(ip)
        if not isinstance(entry, dict):
            entry = {}
        snmp_map = entry.get("snmp")
        if not isinstance(snmp_map, dict):
            snmp_map = {}
        meta = entry.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        for color, cand in confirmed.items():
            snmp_map[color] = cand["oid"]
            meta[color] = {
                "value_at_confirm": cand["value"],
                "confirmed_at": datetime.now().isoformat(),
                "brand": brand or None,
                "model": model or None,
            }
        entry["snmp"] = snmp_map
        entry["meta"] = meta
        printers[ip] = entry
        learned["printers"] = printers
        data["learned"] = learned

        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp_path = CONFIG_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, CONFIG_PATH)
        log.info("  [%s] OID شناسه‌ی کارتریج تأیید و ذخیره شد: %s", ip,
                 {c: c_["oid"] for c, c_ in confirmed.items()})
        return True
    except Exception as exc:
        log.warning("  [%s] ذخیره‌ی learned cartridge-id map ناموفق: %s", ip, exc)
        return False


def load_learned_oids(ip: str) -> dict:
    """OIDهای تأییدشده‌ی یک IP از فایل تنظیمات ({color: oid}) — برای UI/diagnose."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
        entry = ((data.get("learned") or {}).get("printers") or {}).get(ip) or {}
        return {str(k).lower(): str(v) for k, v in (entry.get("snmp") or {}).items() if v}
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# جلسه‌ی کشف یک پرینتر (پروب‌های فاصله‌دار تا تأیید)
# ─────────────────────────────────────────────────────────────────────────────
def discover_for_printer(ip: str, brand: str, community: str = "public",
                         snmp_version=None, passes: int = STABILITY_HITS,
                         probe_gap: float = PROBE_GAP_SECONDS,
                         probe_fn=_probe_pass) -> dict:
    """کشف کامل یک پرینتر: حداکثر ``passes`` گذر با فاصله؛ تأیید → ذخیره.

    خروجی: {"confirmed": {color: oid}, "candidates": {color: value}}
    تزریق probe_fn برای تست (بدون sleep واقعی).
    """
    if not ip or brand not in ("hp", "canon", "brother", "toshiba"):
        return {"confirmed": {}, "candidates": {}}
    confirmed = {}
    last_candidates = {}
    effective_passes = max(1, passes) if probe_fn is not _probe_pass else MAX_PASSES
    for i in range(effective_passes):
        if i:
            time.sleep(probe_gap)
        try:
            res = probe_fn(ip, brand, community, snmp_version) or {}
        except Exception as exc:
            log.debug("cartridge-discovery probe %s failed: %s", ip, exc)
            continue
        candidates = res.get("candidates") or {}
        last_candidates = {c: cand["value"] for c, cand in candidates.items()}
        if not candidates:
            continue
        confirmed = _update_stability(ip, candidates)
        if confirmed:
            break
    if confirmed:
        if _save_learned(ip, confirmed, brand=brand):
            clear_id_cache()
            try:
                load_id_config(force=True)
            except Exception:
                pass
    else:
        with _stability_lock:
            _stability.pop(ip, None)
    return {"confirmed": {c: cand["oid"] for c, cand in confirmed.items()},
            "candidates": last_candidates}


# ─────────────────────────────────────────────────────────────────────────────
# صف پس‌زمینه (throttle شده)
# ─────────────────────────────────────────────────────────────────────────────
def queue_discovery(ip: str, brand: str, community: str = "public", snmp_version=None):
    """افزودن پرینتر به صف کشف؛ dedup با IP و cooldown."""
    global _worker_started
    if not ip or brand not in ("hp", "canon", "brother", "toshiba"):
        return False
    now = time.monotonic()
    with _queue_lock:
        if ip in _queue_set:
            return False
        last = _last_attempt.get(ip)
        if last is not None and (now - last) < COOLDOWN_SECONDS:
            return False
        _queue_set.add(ip)
        _queue.append((ip, brand, community, snmp_version))
        _last_attempt[ip] = now
        if not _worker_started:
            _worker_started = True
            t = threading.Thread(target=_worker, daemon=True, name="cartridge-discovery")
            t.start()
    return True


def _worker():
    while True:
        with _queue_lock:
            item = _queue.popleft() if _queue else None
            if item is None:
                _queue_set.clear()
        if item is None:
            time.sleep(5.0)
            continue
        ip, brand, community, snmp_version = item
        try:
            log.info("🔍 کشف OID شناسه‌ی کارتریج برای %s (%s) …", ip, brand)
            result = discover_for_printer(ip, brand, community, snmp_version)
            if result.get("confirmed"):
                log.info("  [%s] ✅ کشف موفق: %s", ip, result["confirmed"])
            else:
                log.info("  [%s] کاندیدای پایدار پیدا نشد (candidates=%s) — بعداً دوباره", ip,
                         result.get("candidates") or {})
        except Exception as exc:
            log.debug("cartridge-discovery worker error for %s: %s", ip, exc)
        # فاصله بین پرینترها تا SNMP ناوگان زیر بار نرود
        time.sleep(3.0)


def queue_size() -> int:
    with _queue_lock:
        return len(_queue)
