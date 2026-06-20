"""
توابع مشترک همه collectorها:
- si, ss: تبدیل ایمن مقادیر SNMP
- _g: wrapper ساده snmp_get با fallback خودکار
- _counters_event: ثبت رویدادهای چاپ و هشدار (با جلوگیری از ثبت اولیه و دلتاهای بزرگ غیرمنطقی)
- validate_counter_consistency: بررسی سازگاری شمارنده‌ها
- detect_brand: تشخیص خودکار برند
"""

import logging
from datetime import datetime

from config.settings import POLL_INTERVAL, TONER_ALERT_THRESHOLDS
from core.snmp.protocol import snmp_get_with_fallback
from core import store
from core.database import add_event

log = logging.getLogger("PrinterMonitor")
DEFAULT_YIELD_PER_PAGE = 2000

# ─── تنظیمات اعتبارسنجی ─────────────────────────────────────────
# حداکثر دلتا منطقی: مقدار پایه برای بازهٔ 30s، سپس متناسب با POLL_INTERVAL تنظیم می‌شود
_BASE_MAX_PER_30S = 200
MAX_REASONABLE_DELTA = max(100, int(_BASE_MAX_PER_30S * (POLL_INTERVAL / 30.0)))
MIN_DELTA_FOR_FALLBACK = 1        # حداقل دلتا برای fallback مبتنی بر total
MIN_VALID_TOTAL_FOR_FIRST_POLL = 50  # اگر total < 50 باشد، شاید پرینتر جدید است
MAX_TOTAL_AFTER_RESET = 5000      # اگر مقدار جدید کمتر از این باشد و قبلی بزرگ بود، ریست شده


def _elapsed_since_prev(prev: dict) -> float:
    """مدت‌زمان گذشته از آخرین snapshot ذخیره‌شده را برحسب ثانیه برمی‌گرداند."""
    updated_at = (prev or {}).get("updated_at")
    if not updated_at:
        return float(POLL_INTERVAL)
    try:
        elapsed = (datetime.now() - datetime.fromisoformat(updated_at)).total_seconds()
        return max(1.0, elapsed)
    except Exception:
        return float(POLL_INTERVAL)


def _dynamic_max_reasonable_delta(prev: dict) -> tuple[int, float]:
    """حد بالای منطقی دلتا را بر اساس فاصله واقعی بین دو poll محاسبه می‌کند."""
    elapsed_seconds = max(float(POLL_INTERVAL), _elapsed_since_prev(prev))
    dynamic_limit = max(100, int(_BASE_MAX_PER_30S * (elapsed_seconds / 30.0)))
    return dynamic_limit, elapsed_seconds


# ─── helpers ────────────────────────────────────────────────────
def si(v, d: int = 0) -> int:
    # ✅ باگ #5: لاگ خطاها به جای مخفی کردن + فقط exception های مرتبط
    if v is None:
        return d
    try:
        return int(v)
    except (ValueError, TypeError) as e:
        log.warning(f"si() conversion failed: value={v!r} → default={d} ({e})")
        return d
    except Exception as e:
        log.error(f"si() unexpected error: value={v!r} → default={d} ({e})")
        return d


def ss(v, d: str = "N/A") -> str:
    return str(v).strip() if v is not None and str(v).strip() else d


def _g(ip: str, oid: str, community: str, timeout: float = 2.5):
    """
    Wrapper برای snmp_get_with_fallback که سعی می‌کند ابتدا v2c و در صورت شکست v1 را امتحان کند.
    """
    return snmp_get_with_fallback(ip, oid, community, timeout=timeout)


def _bootstrap_yield_from_history(ip: str, prev: dict):
    """اگر yield هنوز پیش‌فرض است، از snapshotهای تاریخی برای تخمین اولیه استفاده کن."""
    if (prev or {}).get("yield_per_page", DEFAULT_YIELD_PER_PAGE) != DEFAULT_YIELD_PER_PAGE:
        return None
    try:
        from core.database import estimate_yield_from_history
        result = estimate_yield_from_history(ip, days=7, min_points=3, min_pages=500)
        if not result:
            return None
        estimated_yield = result["yield_per_page"]
        store._prev.set(ip, {
            "yield_per_page": estimated_yield,
            "yield_learning_failures": 0,
        })
        log.info(
            f"  [{ip}] historical yield bootstrap -> {estimated_yield} "
            f"(pages={result['total_pages']}, toner_drop={result['total_drop']}, samples={result['sample_points']})"
        )
        return estimated_yield
    except Exception as exc:
        log.exception("Historical yield bootstrap failed for %s: %s", ip, exc)
        return None



def _learn_yield_per_page(ip: str, delta_pages: int, prev_toner_level: int, current_toner_level: int, prev: dict):
    """یادگیری خودکار yield_per_page بر اساس مصرف تونر و صفحات چاپ شده."""
    prev = prev or {}
    if delta_pages <= 0 or prev_toner_level is None or current_toner_level is None:
        return
    # از داده‌های مشکوک/درحال تایید برای یادگیری yield استفاده نکن.
    if prev.get("last_counter_error") or prev.get("pending_overflow_total") is not None or prev.get("pending_refill_new_toner") is not None:
        log.debug("  [%s] yield learning skipped due to pending/anomalous counter state", ip)
        return

    current_yield = int(prev.get("yield_per_page", DEFAULT_YIELD_PER_PAGE) or DEFAULT_YIELD_PER_PAGE)
    if current_yield == DEFAULT_YIELD_PER_PAGE:
        bootstrapped = _bootstrap_yield_from_history(ip, prev)
        if bootstrapped:
            current_yield = bootstrapped

    toner_drop = prev_toner_level - current_toner_level
    if toner_drop <= 0:
        return
    # افت‌های خیلی کوچک تونر نویز زیادی دارند؛ حداقل ۵۰ صفحه برای تخمین قابل قبول لازم است.
    if toner_drop < 1 or delta_pages < 50:
        return

    try:
        estimated_yield = int(round(delta_pages * 100.0 / toner_drop))
    except ZeroDivisionError:
        log.warning("  [%s] yield learning skipped بسبب تقسیم بر صفر", ip)
        return
    except Exception as exc:
        log.exception("  [%s] yield learning error: %s", ip, exc)
        return

    if estimated_yield < 300 or estimated_yield > 20000:
        log.info(f"  [{ip}] yield learning ignored خارج از بازه: {estimated_yield}")
        return

    diff_ratio = abs(estimated_yield - current_yield) / max(current_yield, 1)
    failures = int(prev.get("yield_learning_failures", 0) or 0)
    force_estimate = int(prev.get("force_estimate", 0) or 0)

    if current_yield != DEFAULT_YIELD_PER_PAGE and diff_ratio > 0.30:
        failures += 1
        log.info(
            f"  [{ip}] yield discrepancy detected: current={current_yield}, estimated={estimated_yield}, "
            f"ratio={diff_ratio:.2f}, failures={failures}/10"
        )
        if failures >= 10 and not force_estimate:
            store._prev.set(ip, {
                "force_estimate": 1,
                "yield_learning_failures": failures,
            })
            log.info(f"  [{ip}] force_estimate enabled after repeated yield discrepancies")
        else:
            store._prev.set(ip, {"yield_learning_failures": failures})
        return

    failures = 0
    if current_yield != DEFAULT_YIELD_PER_PAGE:
        if diff_ratio < 0.05:
            return
        estimated_yield = int(round((current_yield * 0.6) + (estimated_yield * 0.4)))

    if estimated_yield == current_yield:
        return

    source = "auto_learn"
    log.info(
        f"  [{ip}] yield_per_page updated: {current_yield} -> {estimated_yield} "
        f"(source={source}, pages={delta_pages}, toner_drop={toner_drop})"
    )
    store._prev.set(ip, {
        "yield_per_page": estimated_yield,
        "yield_learning_failures": failures,
    })


def get_pages_since_last_reset(prev: dict, total: int):
    """محاسبه تعداد صفحات چاپ‌شده از زمان آخرین تنظیم مجدد کارتریج."""
    if not prev or not prev.get("manual_override"):
        return None
    override_start_total = prev.get("override_start_total")
    if override_start_total is None:
        return None
    try:
        pages_since_override = int(total) - int(override_start_total)
    except Exception:
        return None
    if pages_since_override < 0:
        return None
    return pages_since_override



def apply_toner_override(ip: str, total: int, snmp_level: int = None, color: str = None):
    """محاسبه مجدد سطح تونر بر اساس override دستی و میزان صفحات چاپ‌شده."""
    prev = store._prev.get(ip) or {}
    if not prev.get("manual_override") or color is None:
        return None

    if prev.get("override_color") != color:
        return None

    override_start_total = prev.get("override_start_total")
    override_start_toner = prev.get("override_start_toner")
    yield_per_page = prev.get("yield_per_page", DEFAULT_YIELD_PER_PAGE)

    if override_start_total is None or override_start_toner is None:
        return None

    pages_since_override = get_pages_since_last_reset(prev, total)

    # 🔥 اصلاح: اگر total کمتر از override_start_total باشد، یعنی دستگاه
    # ریست شده و override دیگر معتبر نیست → برگرداندن مقدار خام سنسور
    if pages_since_override is None:
        log.debug(f"  [{ip}] Toner override invalidated: total({total}) < start({override_start_total}). "
                  f"Returning raw SNMP level: {snmp_level}")
        return snmp_level

    if pages_since_override == 0:
        return override_start_toner

    if not isinstance(yield_per_page, int) or yield_per_page <= 0:
        yield_per_page = DEFAULT_YIELD_PER_PAGE

    estimated_drop = int(round(pages_since_override * 100.0 / yield_per_page))
    final_level = max(0, min(100, override_start_toner - estimated_drop))

    log.debug(f"  [{ip}] apply_toner_override: override_color={color}, total={total}, "
              f"start_total={override_start_total}, start_toner={override_start_toner}, "
              f"yield_per_page={yield_per_page}, pages_since_reset={pages_since_override}, final={final_level}")
    return final_level


# ─── رویدادها ─────────────────────────────────────────────────
def _counters_event(ip: str, total: int, prev: dict, alerts: list, curr_codes: list,
                    full_color: int = None, black_white: int = None,
                    paper_size: str = None, username: str = None,
                    current_toner_level: int = None, prev_toner_level: int = None,
                    uptime: int = None,
                    a3_total: int = None, a4_total: int = None,
                    poll_timestamp: str = None):
    """
    ثبت رویدادهای چاپ/هشدار با محافظت در برابر داده‌های مشکوک SNMP.

    اصل مهم: اگر counter مشکوک است، آن را به عنوان حقیقت در PrevStore ذخیره نمی‌کنیم؛
    ابتدا خطای خواندن یا anomaly ثبت می‌شود تا از زنجیره لاگ دروغین
    COUNTER_RESET → PRINT_OVERFLOW جلوگیری شود.
    """
    prev = prev or {}
    alerts = alerts or []
    curr_codes = curr_codes or []

    def _to_int(value, default=None):
        try:
            if value is None:
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    def _uptime_reset(curr, old) -> bool:
        curr_i = _to_int(curr)
        old_i = _to_int(old)
        # sysUpTime بر حسب صدم ثانیه است؛ 60 ثانیه حاشیه خطا می‌گذاریم.
        return curr_i is not None and old_i is not None and curr_i < old_i - 60 * 100

    total = _to_int(total)
    prev_total = _to_int(prev.get("print_total")) if prev else None
    prev_fc = _to_int(prev.get("full_color")) if prev.get("full_color") is not None else None
    prev_bw = _to_int(prev.get("black_white")) if prev.get("black_white") is not None else None
    prev_uptime = prev.get("uptime") if prev else None
    prev_toner_level = prev_toner_level if prev_toner_level is not None else (prev.get("toner_level") if prev else None)
    reboot_detected = _uptime_reset(uptime, prev_uptime)
    dynamic_max_delta, elapsed_seconds = _dynamic_max_reasonable_delta(prev)

    if total is None:
        add_event(ip, "SNMP_COUNTER_READ_ERROR", {
            "message": "خواندن شمارنده کل نامعتبر بود (total=None)؛ snapshot قبلی حفظ شد",
            "severity": "warning",
            "prev_total": prev_total,
            "current_total": None,
            "prev_uptime": prev_uptime,
            "current_uptime": uptime,
        })
        store._prev.set(ip, {
            "print_total": prev_total,
            "full_color": prev_fc,
            "black_white": prev_bw,
            "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
            "alert_codes": curr_codes,
            "last_alert_codes": curr_codes,
            "uptime": uptime if uptime is not None else prev_uptime,
        })
        return

    # ─── ثبت رویدادهای هشدار جدید با جلوگیری از تکرار ───────────────
    suppress_toner_alerts = False
    if prev.get("manual_override") and prev.get("override_start_toner") is not None:
        try:
            current_level_int = int(current_toner_level) if current_toner_level is not None else None
            override_start = int(prev.get("override_start_toner"))
            if current_level_int is not None and current_level_int > TONER_ALERT_THRESHOLDS.get("warning", 15) and current_level_int >= override_start - 1:
                suppress_toner_alerts = True
        except (TypeError, ValueError):
            suppress_toner_alerts = False

    if curr_codes and not suppress_toner_alerts:
        alert_codes_list = prev.get("last_alert_codes", []) if prev else []
        new_codes = [c for c in curr_codes if c not in alert_codes_list]
        for code in new_codes:
            msg = next((a.get("message") for a in alerts if a.get("code") == code), f"Error {code}")
            add_event(ip, "ALERT", {"message": msg, "code": code, "severity": "warning"})

    # ─── اولین poll: baseline بگیر، لاگ چاپ نزن ─────────────────────
    if prev_total is None:
        log.warning(f"  [{ip}] جلوگیری از ثبت رویداد PRINT در اولین poll (total={total:,})")
        store._prev.set(ip, {
            "print_total": total,
            "toner_level": current_toner_level,
            "full_color": full_color,
            "black_white": black_white,
            "alert_codes": curr_codes,
            "last_alert_codes": curr_codes,
            "uptime": uptime,
            "a3_total": a3_total,
            "a4_total": a4_total,
            "pending_overflow_total": None,
            "pending_refill_new_toner": None,
        })
        return

    # ─── محافظ اصلی: total=0 یا کاهش counter بدون reboot = خطای SNMP، نه reset ───
    suspicious_zero = total == 0 and prev_total >= 1000 and not reboot_detected
    suspicious_drop = total < prev_total and not reboot_detected
    if suspicious_zero or suspicious_drop:
        etype = "SNMP_COUNTER_READ_ERROR" if suspicious_zero else "COUNTER_ANOMALY"
        add_event(ip, etype, {
            "message": (
                f"خواندن شمارنده مشکوک بود: قبلی {prev_total:,} → جدید {total:,}. "
                "uptime ریست نشده؛ snapshot قبلی حفظ شد."
            ),
            "severity": "warning",
            "prev_total": prev_total,
            "current_total": total,
            "delta": total - prev_total,
            "prev_uptime": prev_uptime,
            "current_uptime": uptime,
            "is_reboot": False,
        })
        log.warning("  [%s] suspicious counter read ignored: prev=%s current=%s uptime %s→%s", ip, prev_total, total, prev_uptime, uptime)
        store._prev.set(ip, {
            "print_total": prev_total,
            "full_color": prev_fc,
            "black_white": prev_bw,
            "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
            "alert_codes": curr_codes,
            "last_alert_codes": curr_codes,
            "uptime": uptime if uptime is not None else prev_uptime,
            "a3_total": prev.get("a3_total"),
            "a4_total": prev.get("a4_total"),
            "last_counter_error": etype,
        })
        return

    # ─── reset واقعی فقط با شواهد قوی مثل کاهش uptime ───────────────
    if total < prev_total and reboot_detected:
        add_event(ip, "COUNTER_RESET", {
            "message": f"شمارنده از {prev_total:,} به {total:,} کاهش یافت (ریبوت دستگاه)",
            "severity": "error",
            "prev_total": prev_total,
            "current_total": total,
            "delta": total - prev_total,
            "prev_uptime": prev_uptime,
            "current_uptime": uptime,
            "is_reboot": True,
        })
        store._prev.set(ip, {
            "print_total": total,
            "toner_level": current_toner_level,
            "full_color": full_color,
            "black_white": black_white,
            "alert_codes": curr_codes,
            "last_alert_codes": curr_codes,
            "uptime": uptime,
            "a3_total": a3_total,
            "a4_total": a4_total,
            "manual_override": 0,
            "override_color": None,
            "override_base_level": None,
            "override_start_total": None,
            "override_start_toner": None,
            "yield_per_page": 2000,
        })
        return

    actual_delta = total - prev_total
    delta_pages = actual_delta if actual_delta >= 0 else 0

    # ─── REFILL خودکار دو مرحله‌ای: با یک poll قطعی ثبت نکن ─────────
    refill_confirmed = False
    pending_refill_new = prev.get("pending_refill_new_toner")
    pending_refill_prev = prev.get("pending_refill_prev_toner")
    pending_refill_total = prev.get("pending_refill_total", prev_total)
    if current_toner_level is not None and pending_refill_new is not None and not prev.get("manual_override"):
        try:
            pages_since_candidate = total - int(pending_refill_total or prev_total)
            if int(current_toner_level) >= int(pending_refill_new) - 1 and pages_since_candidate < 50:
                refill_confirmed = True
                add_event(ip, "REFILL", {
                    "message": f"تشخیص خودکار تاییدشده: کارتریج شارژ شد (تونر از {pending_refill_prev}% به {current_toner_level}%)",
                    "severity": "info",
                    "auto_detected": True,
                    "confirmed": True,
                    "prev_toner": pending_refill_prev,
                    "new_toner": current_toner_level,
                    "delta_pages": max(0, pages_since_candidate),
                })
        except (TypeError, ValueError):
            refill_confirmed = False

    if (not refill_confirmed and current_toner_level is not None and prev_toner_level is not None and actual_delta >= 0):
        try:
            delta_toner = int(current_toner_level) - int(prev_toner_level)
        except (TypeError, ValueError):
            delta_toner = 0
        if (delta_toner > 20 and delta_pages < 50 and not prev.get("manual_override")):
            log.info("  [%s] refill candidate pending: toner %s%% → %s%%", ip, prev_toner_level, current_toner_level)

        _learn_yield_per_page(ip, delta_pages, prev_toner_level, current_toner_level, prev)

    if (delta_pages > 500 and current_toner_level is not None and prev_toner_level is not None and
        abs(int(current_toner_level) - int(prev_toner_level)) < 5):
        add_event(ip, "WARNING", {
            "message": f"هشدار: {delta_pages} صفحه چاپ شده ولی تونر تغییر نکرده ({prev_toner_level}% → {current_toner_level}%). احتمال گیر کردن چیپ.",
            "severity": "warning",
            "auto_detected": True,
        })

    # ─── محاسبه و اعتبارسنجی دلتاهای رنگی/سیاه‌وسفید ───────────────
    delta_fc = (full_color - prev_fc) if (full_color is not None and prev_fc is not None) else 0
    delta_bw = (black_white - prev_bw) if (black_white is not None and prev_bw is not None) else 0
    split_delta = delta_fc + delta_bw
    total_delta = split_delta
    color_unknown = False
    counter_mismatch = False

    if actual_delta > 0:
        partial_negative = delta_fc < 0 or delta_bw < 0
        mismatch = split_delta > 0 and abs(split_delta - actual_delta) > max(2, int(actual_delta * 0.10))
        if partial_negative or mismatch or split_delta <= 0:
            if actual_delta <= dynamic_max_delta:
                total_delta = actual_delta
                split_unavailable = full_color is None and black_white is None
                color_unknown = partial_negative or mismatch or split_unavailable
                counter_mismatch = partial_negative or mismatch
                delta_fc = 0
                delta_bw = actual_delta
                if counter_mismatch:
                    log.warning(
                        "  [%s] split counter mismatch; using total delta. actual=%s split=%s fc=%s bw=%s",
                        ip, actual_delta, split_delta, delta_fc, delta_bw,
                    )
            else:
                total_delta = actual_delta
    else:
        total_delta = 0

    log.debug(f"  [{ip}] PRINT: prev_total={prev_total:,} curr_total={total:,} "
              f"delta_fc={delta_fc}, delta_bw={delta_bw}, total_delta={total_delta}, actual_delta={actual_delta}")

    # ─── Overflow: بار اول فقط pending، بار دوم تایید و ثبت تخمینی ───
    estimated = False
    if total_delta > dynamic_max_delta:
        pending_total = prev.get("pending_overflow_total")
        pending_delta = prev.get("pending_overflow_delta")
        if pending_total == total and pending_delta == total_delta:
            estimated = True
            color_unknown = True
            log.warning("  [%s] overflow confirmed on second poll; recording estimated PRINT delta=%s", ip, total_delta)
        else:
            add_event(ip, "PRINT_OVERFLOW", {
                "message": f"افزایش غیرمنتظره صفحات: {total_delta} صفحه در یک بازه؛ برای جلوگیری از لاگ دروغین در حالت pending نگه داشته شد",
                "severity": "warning",
                "delta": total_delta,
                "dynamic_limit": dynamic_max_delta,
                "elapsed_seconds": round(elapsed_seconds, 1),
                "prev_total": prev_total,
                "current_total": total,
                "pending_confirmation": True,
            })
            store._prev.set(ip, {
                "print_total": prev_total,
                "full_color": prev_fc,
                "black_white": prev_bw,
                "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
                "alert_codes": curr_codes,
                "last_alert_codes": curr_codes,
                "uptime": uptime if uptime is not None else prev_uptime,
                "a3_total": prev.get("a3_total"),
                "a4_total": prev.get("a4_total"),
                "pending_overflow_total": total,
                "pending_overflow_delta": total_delta,
            })
            return

    # ─── ثبت PRINT ────────────────────────────────────────────────
    if total_delta > 0:
        if color_unknown:
            msg = f"{total_delta} صفحه چاپ شد (تفکیک رنگ نامطمئن)"
            color = "نامشخص"
        elif delta_fc > 0 and delta_bw > 0:
            msg = f"{delta_fc} صفحه رنگی + {delta_bw} صفحه سیاه‌سفید = {total_delta} صفحه چاپ شد"
            color = "مختلط"
        elif delta_fc > 0:
            msg = f"{delta_fc} صفحه رنگی چاپ شد"
            color = "رنگی"
        else:
            msg = f"{total_delta} صفحه سیاه‌سفید چاپ شد"
            color = "سیاه‌سفید"
        if estimated:
            msg = "ثبت تخمینی پس از تایید overflow: " + msg

        event_data = {
            "message": msg,
            "pages": total_delta,
            "color": color,
            "paper_size": paper_size,
            "severity": "warning" if estimated or counter_mismatch else "info",
        }
        if estimated:
            event_data["estimated"] = True
            event_data["overflow_confirmed"] = True
        if counter_mismatch:
            event_data["counter_mismatch"] = True
            event_data["actual_delta"] = actual_delta
            event_data["split_delta"] = split_delta
        if username:
            event_data["username"] = username
        if poll_timestamp:
            event_data["poll_timestamp"] = poll_timestamp

        add_event(ip, "PRINT", event_data)
        log.info(f"  [{ip}] ✓ ثبت چاپ: {total_delta} صفحه ({color})")

    # ─── ذخیره snapshot جدید؛ counterهای جزئی عقب‌گرد نکنند ─────────
    safe_fc = full_color if (full_color is not None and (prev_fc is None or full_color >= prev_fc)) else prev_fc
    safe_bw = black_white if (black_white is not None and (prev_bw is None or black_white >= prev_bw)) else prev_bw

    new_prev = {
        "print_total": total,
        "full_color": safe_fc,
        "black_white": safe_bw,
        "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
        "alert_codes": curr_codes,
        "last_alert_codes": curr_codes,
        "uptime": uptime if uptime is not None else prev_uptime,
        "pending_overflow_total": None,
        "pending_overflow_delta": None,
        "last_counter_error": None,
    }
    if current_toner_level is not None and prev_toner_level is not None:
        try:
            if int(current_toner_level) - int(prev_toner_level) > 20 and delta_pages < 50 and not refill_confirmed:
                new_prev["pending_refill_prev_toner"] = prev_toner_level
                new_prev["pending_refill_new_toner"] = current_toner_level
                new_prev["pending_refill_total"] = total
            else:
                new_prev["pending_refill_prev_toner"] = None
                new_prev["pending_refill_new_toner"] = None
                new_prev["pending_refill_total"] = None
        except (TypeError, ValueError):
            pass
    if a3_total is not None:
        new_prev["a3_total"] = a3_total
    if a4_total is not None:
        new_prev["a4_total"] = a4_total
    store._prev.set(ip, new_prev)

# ─── سازگاری شمارنده‌ها ─────────────────────────────────────────
def validate_counter_consistency(counters: dict, brand: str) -> list:
    warnings = []
    total   = counters.get("total",       0) or 0
    color   = counters.get("full_color",  0) or 0
    bw      = counters.get("black_white", 0) or 0
    copy_   = counters.get("copy",        0) or 0
    printer = counters.get("printer",     0) or 0

    if brand == "toshiba" and total > 0:
        twin_ = counters.get("twin", 0) or 0
        if color + bw > 0:
            diff = abs(total - (color + bw + twin_))
            if diff > max(100, total * 0.01):
                warnings.append(
                    f"⚠ Toshiba: fc({color:,})+bw({bw:,})+twin({twin_:,})={color+bw+twin_:,} ≠ total({total:,}) diff={diff:,}"
                )
        copy_fc = counters.get("copy_fc",   0) or 0
        ptr_fc  = counters.get("printer_fc",0) or 0
        if color > 0 and (copy_fc + ptr_fc) > color + 1000:
            warnings.append(
                f"⚠ Toshiba FC: copy_fc({copy_fc:,})+ptr_fc({ptr_fc:,})={copy_fc+ptr_fc:,} > fc_total({color:,})"
            )

    if brand == "canon" and total > 0 and copy_ > 0 and printer > 0:
        diff = abs(total - (copy_ + printer))
        if diff > max(300, total * 0.01):
            warnings.append(
                f"⚠ Canon: copy({copy_:,})+print({printer:,})={copy_+printer:,} ≠ total({total:,}) diff={diff:,}"
            )

    return warnings


# ─── تشخیص برند ─────────────────────────────────────────────────
def detect_brand(ip: str, community: str) -> str:
    sys_oid  = snmp_get_with_fallback(ip, "1.3.6.1.2.1.1.2.0", community, timeout=2.0)
    sys_desc = str(snmp_get_with_fallback(ip, "1.3.6.1.2.1.1.1.0", community, timeout=2.0) or "").lower()
    oid_str  = str(sys_oid) if sys_oid else ""

    if "ecs100g" in sys_desc or "1.3.6.1.4.1.47206" in oid_str:
        return "sensor"
    # برخی ECS100Gها sysDescr استاندارد نمی‌دهند؛ با OIDهای اختصاصی هم probe می‌کنیم.
    for sensor_oid in (
        "1.3.6.1.4.1.47206.1.0",
        "1.3.6.1.4.1.47206.110.1.2.0",
        "1.3.6.1.4.1.47206.111.1.2.0",
    ):
        if snmp_get_with_fallback(ip, sensor_oid, community, timeout=1.5) is not None:
            return "sensor"
    if "1.3.6.1.4.1.1129" in oid_str or "toshiba"   in sys_desc: return "toshiba"
    if "1.3.6.1.4.1.1602" in oid_str or "canon"     in sys_desc: return "canon"
    if "1.3.6.1.4.1.2435" in oid_str or "brother"   in sys_desc: return "brother"
    if ("1.3.6.1.4.1.11"   in oid_str or
            "jetdirect" in sys_desc or
            "hp " in sys_desc or
            "hewlett" in sys_desc or
            "laserjet" in sys_desc or
            "officejet" in sys_desc or
            "pagewide" in sys_desc):
        return "hp"
    return "unknown"