#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/verify_server_timezone.py
# @brief:        Read-only check of the server time zone (step E.0.1).
# @details:      Responsibilities: Compare OS and tzdata time zone rules and report offsets and DST.
#                Main API: detect_windows_tz(), detect_posix_tz(), check_offset_and_dst(),
#                check_tzdata(), inspect_db(), main()
#                Side effects: database access, runs external processes, file I/O
# @dependencies: stdlib only
# @notes:        Can be run directly as a script.
# =============================================================================

"""Verify server time zone — step E.0.1 (read only, no changes).

Why this tool? All program historical data (except for the cartridge motor path that follows D.6
writes UTC+Z) with ``datetime.now().isoformat()'' ie **naive local** save
have been The meaning of these strings can be clarified only by knowing the TZ of the same server; If the TZ server
If someone else has it (or someone changes it later), interpretation of historical data 3:30 is moved
will be This tool runs where the program runs and gathers evidence
to interpret the migration (document ``docs/timezone-contract.md'') based on reality
Be, not guess.

How to run (on the server itself, the same user with which the service/program is run):

    python tools/verify_server_timezone.py
    python tools/verify_server_timezone.py --db C:\\printguard\\logs.db
    python tools/verify_server_timezone.py --json report.json

Read only: does not INSERT/UPDATE/DELETE any tables. DB connection with URI
``mode=ro'' opens.

Exit codes:
    0 = all clear and compatible with Asia/Tehran agreement
    2 = need for manual confirmation/decision (non-Iranian TZ, tzdata not installed,...)
    1 = execution error (e.g. DB failed to read)
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

# -------------------------------------------------------------- Basic tools

OK, WARN, INFO, FAIL = "[OK]", "[WARN]", "[INFO]", "[FAIL]"
_results: list[dict] = []


def _emit(level: str, key: str, value: str, hint: str = "") -> None:
    """Record a finding (both in the console and in the report)."""
    _results.append({"level": level, "key": key, "value": value, "hint": hint})
    line = f"{level} {key}: {value}"
    if hint:
        line += f"\n       ↳ {hint}"
    print(line)


def _run(cmd: list[str], timeout: int = 10) -> str:
    """execute a system command; Any error ⇒ empty string (the tool must not explode)."""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            shell=False, errors="replace",
        )
        return (out.stdout or out.stderr or "").strip()
    except Exception:  # noqa: BLE001 - Diagnostic tool must be indestructible
        return ""


# --------------------------------------------------- TZ detection in Windows/Linux

# Mapping common Windows names to IANA (only names related to this project).
_WIN_TO_IANA = {
    "iran standard time": "Asia/Tehran",
    "utc": "UTC",
    "coordinated universal time": "UTC",
    "turkey standard time": "Europe/Istanbul",
    "arabian standard time": "Asia/Dubai",
    "pacific standard time": "America/Los_Angeles",
    "central europe standard time": "Europe/Budapest",
}


def detect_windows_tz() -> dict:
    """Effective Windows TZ reading with tzutil and PowerShell (both breakable)."""
    info = {"raw": "", "source": "", "iana": "", "dst_supported": None,
            "base_offset": "", "ntp": ""}

    raw = _run(["tzutil", "/g"])
    if raw:
        info["raw"] = raw.splitlines()[0].strip()
        info["source"] = "tzutil /g"
        info["iana"] = _WIN_TO_IANA.get(info["raw"].lower(), "")

    ps = _run([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Get-TimeZone | Select-Object -Property Id,DisplayName,BaseUtcOffset,"
        "SupportsDaylightSavingTime | Format-List",
    ])
    if ps:
        for ln in ps.splitlines():
            if ":" not in ln:
                continue
            k, _, v = ln.partition(":")
            k, v = k.strip().lower(), v.strip()
            if k == "id" and not info["raw"]:
                info["raw"] = v
                info["source"] = "Get-TimeZone"
                info["iana"] = _WIN_TO_IANA.get(v.lower(), "")
            elif k == "baseutcoffset":
                info["base_offset"] = v
            elif k == "supportsdaylightsavingtime":
                info["dst_supported"] = v

    ntp = _run(["w32tm", "/query", "/status"])
    if ntp:
        for ln in ntp.splitlines():
            if ":" in ln and ("last successful sync" in ln.lower()
                              or "source" in ln.lower()):
                info["ntp"] += ln.strip() + " | "
        info["ntp"] = info["ntp"].rstrip(" |")
    return info


def detect_posix_tz() -> dict:
    """Read TZ on Linux/Mac (for testing tools and non-target servers)."""
    info = {"raw": "", "source": "", "iana": ""}
    for path in ("/etc/timezone",):
        try:
            with open(path, encoding="utf-8") as fh:
                info["raw"] = fh.read().strip()
                info["source"] = path
                info["iana"] = info["raw"]
                break
        except OSError:
            continue
    if not info["raw"]:
        try:
            info["raw"] = os.path.realpath("/etc/localtime")
            info["source"] = "/etc/localtime (symlink)"
            marker = "zoneinfo/"
            if marker in info["raw"]:
                info["iana"] = info["raw"].split(marker, 1)[1]
        except OSError:
            pass
    return info


def check_offset_and_dst() -> dict:
    """The actual "right now" offset from the difference between local time and UTC."""
    now_local = datetime.now()
    now_utc = datetime.now(timezone.utc)
    delta = now_local - now_utc.replace(tzinfo=None)
    total_minutes = int(round(delta.total_seconds() / 60.0))
    sign = "+" if total_minutes >= 0 else "-"
    hh, mm = divmod(abs(total_minutes), 60)
    return {
        "offset": f"UTC{sign}{hh:02d}:{mm:02d}",
        "offset_minutes": total_minutes,
        "tzname": time.tzname,
        "env_tz": os.environ.get("TZ", ""),
        "tm_isdst": time.localtime().tm_isdst,
        "daylight_flag": bool(time.daylight),
        "altzone_minutes": int(-time.altzone / 60) if time.altzone else 0,
    }


def check_tzdata() -> dict:
    """Does ``zoneinfo.ZoneInfo("Asia/Tehran")`` work on this machine?"""
    res = {"python": sys.version.split()[0], "zoneinfo_ok": False,
           "tzdata_pkg": False, "tehran_offset": ""}
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415 - measure optionality
        tz = ZoneInfo("Asia/Tehran")
        now = datetime.now(tz)
        res["zoneinfo_ok"] = True
        res["tehran_offset"] = now.strftime("%z")
    except Exception as exc:  # noqa: BLE001
        res["error"] = f"{type(exc).__name__}: {exc}"
    try:
        import importlib.util
        res["tzdata_pkg"] = importlib.util.find_spec("tzdata") is not None
    except Exception:  # noqa: BLE001
        res["tzdata_pkg"] = False
    return res


# --------------------------------------------------------------- Data check

def inspect_db(path: str, limit: int = 15) -> dict:
    """A read-only sampling of ``logs'' to test the naive-local assertion."""
    res = {"path": os.path.abspath(path), "exists": os.path.exists(path),
           "rows": [], "fresh": 0, "local_like": 0, "utc_like": 0, "z_rows": 0,
           "error": ""}
    if not res["exists"]:
        res["error"] = "فایل پیدا نشد"
        return res

    try:
        conn = sqlite3.connect(
            f"file:{res['path']}?mode=ro", uri=True, timeout=10.0
        )
    except sqlite3.Error as exc:
        res["error"] = f"اتصال فقط‌خواندنی ناموفق: {exc}"
        return res

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT timestamp, type FROM logs "
            "ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = cur.fetchall()
    except sqlite3.Error as exc:
        res["error"] = f"خواندن logs ناموفق: {exc}"
        rows = []
    finally:
        conn.close()

    now_local = datetime.now()
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    for ts, etype in rows:
        ts = (ts or "").strip()
        row = {"timestamp": ts, "type": etype, "tag": "old/unknown"}
        if ts.endswith("Z") or "+" in ts[10:]:
            res["z_rows"] += 1
            row["tag"] = "UTC-aware (Z/offset)"
        else:
            try:
                dt = datetime.fromisoformat(ts)
                dl = abs((now_local - dt).total_seconds())
                du = abs((now_utc - dt).total_seconds())
                if dl < 3 * 3600 or du < 3 * 3600:
                    res["fresh"] += 1
                    if dl <= du:
                        res["local_like"] += 1
                        row["tag"] = "naive ⇒ به ساعت محلی نزدیک‌تر"
                    else:
                        res["utc_like"] += 1
                        row["tag"] = "naive ⇒ به UTC نزدیک‌تر"
            except ValueError:
                row["tag"] = "غیرقابل تجزیه"
        res["rows"].append(row)
    return res


# --------------------------------------------------------------------------- Rulings

def _verdict_iran(off_min: int, dst: bool, tzname: str) -> None:
    """Print the verdict of the Iran time zone check."""
    if off_min == 210 and not dst:
        _emit(OK, "حکم نهایی",
              "TZ سرور = ایران (UTC+03:30، بدون DST) ⇒ داده‌های naive local "
              "با «Asia/Tehran» قابل تفسیر است",
              "همین فرض در بخش ۲ سند contracts ثبت شده؛ migration (فاز F) "
              "می‌تواند بی‌تبدیل از آن استفاده کند.")
    elif off_min == 0:
        _emit(WARN, "حکم نهایی",
              "TZ سرور = UTC ⇒ رشته‌های naive local در واقع UTCاند، نه محلی",
              "تفسیر آن‌ها با «Asia/Tehran» ۳:۳۰ خطا می‌دهد. باید در نشانهٔ "
              "cutover (E.0.13) و migration لحاظ شود؛ همچنین بند «تغییر TZ "
              "سرور» در سند را بخوانید.")
    else:
        _emit(WARN, "حکم نهایی",
              f"TZ سرور ناشناخته/غیرایران است (آفست {off_min} دقیقه، "
              f"tzname={tzname})",
              "پیش از migration باید یا TZ سرور تثبیت شود یا تفسیر داده‌های "
              "naive local برای همین TZ نوشته شود. نیاز به تأیید دستی.")


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point of the server time-zone check."""

    ap = argparse.ArgumentParser(
        description="تأیید منطقهٔ زمانی سرور (فقط خواندن) — گام E.0.1"
    )
    ap.add_argument("--db", default="logs.db",
                    help="مسیر logs.db (پیش‌فرض: logs.db در پوشهٔ جاری)")
    ap.add_argument("--json", dest="json_path", default="",
                    help="نوشتن گزارش ماشینی در این مسیر")
    ap.add_argument("--limit", type=int, default=15,
                    help="تعداد ردیف نمونه از logs (پیش‌فرض ۱۵)")
    args = ap.parse_args(argv)

    try:  # Windows console may be cp1256; Farsi output must remain intact
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 72)
    print("تأیید منطقهٔ زمانی سرور — فاز E.0.1 (فقط خواندن، بدون تغییر)")
    print("=" * 72)

    # 1) Platform
    osname = f"{platform.system()} {platform.release()} ({platform.machine()})"
    _emit(INFO, "پلتفرم", osname, f"Python {sys.version.split()[0]}")
    is_win = platform.system().lower().startswith("win")

    # 2) Effective system TZ
    sysinfo = detect_windows_tz() if is_win else detect_posix_tz()
    if sysinfo["raw"]:
        _emit(INFO, "TZ سیستم", sysinfo["raw"],
              f"منبع: {sysinfo['source']}"
              + (f" | IANA≈{sysinfo['iana']}" if sysinfo["iana"] else ""))
    else:
        _emit(WARN, "TZ سیستم", "نامشخص (دستور تشخیصی جواب نداد)",
              "در ویندوز دستور «tzutil /g» را دستی اجرا و نتیجه را ثبت کنید.")

    if sysinfo.get("dst_supported") not in (None, ""):
        _emit(INFO, "DST پشتیبانی‌شده (ویندوز)", str(sysinfo["dst_supported"]),
              "ایران از ۲۰۲۲ DST ندارد؛ مقدار True یعنی TZ چیز دیگری است.")
    if sysinfo.get("ntp"):
        _emit(INFO, "هم‌گام‌سازی ساعت (NTP)", sysinfo["ntp"][:160],
              "برای داده‌های naive local، ساعت درست به‌اندازهٔ خود TZ مهم است.")

    # 3) Actual offset and DST
    off = check_offset_and_dst()
    _emit(INFO, "آفست پروسهٔ فعلی", off["offset"],
          f"tzname={off['tzname']} | tm_isdst={off['tm_isdst']} | "
          f"time.daylight={off['daylight_flag']}")
    if off["env_tz"]:
        _etz = off["env_tz"].strip().lower()
        if _etz in ("asia/tehran", "iran standard time", "+0330", "iran"):
            _emit(OK, "متغیر محیطی TZ", off["env_tz"],
                  "معادل ایران است و همان چیزی را می‌دهد که TZ سیستم ایران "
                  "می‌داد؛ آفست پروسه بر همین پایه سنجیده شده است.")
        else:
            _emit(WARN, "متغیر محیطی TZ", off["env_tz"],
                  "TZ محیطی TZ سیستم را برای این پروسه بازنویسی می‌کند و آفست "
                  "پروسهٔ فعلی بر همین پایه سنجیده شده؛ سرویس ویندوز معمولاً "
                  "این متغیر را ست نمی‌کند، ولی اینجا ست است. برای سنجش TZ "
                  "واقعی سرویس، این ابزار را در محیط خود سرویس اجرا کنید.")
    else:
        _emit(OK, "متغیر محیطی TZ", "ست نشده",
              "پس پایتون از TZ سیستم ویندوز پیروی می‌کند.")

    # 4) zoneinfo feature
    tzd = check_tzdata()
    if tzd["zoneinfo_ok"]:
        _emit(OK, "ZoneInfo(\"Asia/Tehran\")", f"کار می‌کند ({tzd['tehran_offset']})",
              "پیش‌نیاز migration فاز F روی همین ماشین برقرار است."
              + ("" if tzd["tzdata_pkg"] else
                 " (از دیتابیس TZ سیستم می‌خواند؛ روی سرورِ بدون آیکون‌های"
                 " ویندوزی، بستهٔ tzdata لازم می‌شود.)"))
    else:
        _emit(WARN, "ZoneInfo(\"Asia/Tehran\")", "کار نمی‌کند",
              f"{tzd.get('error', '')} ⇒ افزودن «tzdata» به requirements.txt "
              "(بند ۳ بخش ۶ سند قرارداد) ضروری می‌شود.")

    # 5) Data
    db = inspect_db(args.db, limit=args.limit) if args.db else {"exists": False}
    if db.get("exists"):
        _emit(INFO, "DB نمونه", f"{db['path']}",
              f"{len(db['rows'])} ردیف آخر logs | Z-aware={db['z_rows']} | "
              f"تازه={db['fresh']} (local_like={db['local_like']}, "
              f"utc_like={db['utc_like']})")
        for row in db["rows"][:5]:
            print(f"       · {row['timestamp']}  [{row['type']}]  → {row['tag']}")
        if db["fresh"] == 0:
            _emit(WARN, "شواهد تازهٔ DB",
                  "هیچ ردیف تازه‌ای (در محدودهٔ ۳ ساعت) برای سنجش وجود ندارد",
                  "این سنجش فقط روی سرورِ فعال معنا دارد؛ چند ساعت پس از "
                  "راه‌اندازی دوباره اجرا کنید.")
        elif db["local_like"] >= db["utc_like"]:
            _emit(OK, "شواهد تازهٔ DB",
                  "ردیف‌های naive به ساعت محلی نزدیک‌ترند",
                  "تأیید تجربی فرض «naive = ساعت محلی سرور».")
        else:
            _emit(WARN, "شواهد تازهٔ DB",
                  "ردیف‌های naive به UTC نزدیک‌ترند",
                  "⇒ TZ سرور احتمالاً UTC است؛ به حکم نهایی نگاه کنید.")
    else:
        _emit(WARN, "DB نمونه", f"خوانده نشد ({db.get('error', '')})",
              "با --db مسیر درست را بدهید؛ این بخش اختیاری است.")

    # 6) Judgment
    _verdict_iran(off["offset_minutes"], bool(off["tm_isdst"]), off["tzname"][0])

    print("-" * 72)
    print("یادآوری‌ها:")
    print(" · TZ سرور را در حین migration عوض نکنید: رشته‌های naive local معنی‌شان")
    print("   با TZ همان لحظهٔ نوشتن گره خورده است؛ تغییر TZ، داده‌های قدیمی را")
    print("   بی‌صدا جابه‌جا می‌کند.")
    print(" · این ابزار هیچ چیزی تغییر نمی‌دهد. گزارش را برای گام‌های E.0.13/F نگه دارید.")
    print("-" * 72)

    if args.json_path:
        payload = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "platform": osname,
            "system_tz": sysinfo,
            "offset": off,
            "zoneinfo": tzd,
            "db": db,
            "findings": _results,
        }
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"گزارش JSON نوشته شد: {args.json_path}")

    has_warn = any(r["level"] == WARN for r in _results)
    has_fail = any(r["level"] == FAIL for r in _results)
    if has_fail:
        return 1
    return 2 if has_warn else 0


if __name__ == "__main__":
    sys.exit(main())
