#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/probe_cartridge_id.py
# @brief:        Probe for the unique cartridge ID, to detect cartridge replacement on a real
#                machine.
# @details:      Responsibilities: Query the candidate OIDs and print what each returns.
#                Main API: g(), fmt(), main()
#                Side effects: network I/O, file I/O
# @dependencies: internal: core.collectors, core.collectors.base, core.snmp.protocol
# @notes:        Can be run directly as a script.
# =============================================================================

"""
"Unique Cartridge ID" detection probe — to enable definitive detection of cartridge replacement
On a real machine, with a run.

What it does:
  1) basic device information (sysDescr/sysObjectID/printer model/serial).
  2) Verbatim reading of Printer-MIB consumption table (some devices include serial in
     description — we read the raw text).
  3) The actual execution of the runtime reader (core/collectors/cartridge_id.py with
     force_refresh — no cache) to see exactly what the system sees.
  4) Dump the brand web pages as an HTML file next to the report (to make the regexs more accurate)
     + Automatic evidence extraction (Serial / Pages printed with this supply).
  5) Suggestion ready to paste in config/cartridge_id_map.json.

Run (in the root folder of the project on your computer):
    venv\\Scripts\\python tools\\probe_cartridge_id.py 172.16.25.34 --brand hp
    venv\\Scripts\\python tools\\probe_cartridge_id.py 172.16.32.10 --brand brother
    # If you have a specific OID from the model's panel/docs:
    venv\\Scripts\\python tools\\probe_cartridge_id.py <ip> --oid 1.3.6.1.4.1.x.y.z

Output:
    probe_identity_<ip>.md → Send this file + probe_web_<ip>_*.html files.
"""
import argparse
import datetime
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.snmp.protocol import snmp_get_with_fallback  # noqa: E402
from core.collectors import cartridge_id as cid  # noqa: E402
from core.collectors.base import fetch_first_web_page  # noqa: E402

MIB = "1.3.6.1.2.1"

WEB_URLS = {
    "hp": [
        "http://{ip}/hp/device/InternalPages/Index?id=SuppliesStatus",
        "https://{ip}/hp/device/InternalPages/Index?id=SuppliesStatus",
        "http://{ip}/DevMgmt/ConsumableConfigDyn.xml",
        "http://{ip}/hp/device/info_suppliesStatus.html",
    ],
    "canon": ["http://{ip}/", "http://{ip}/Status.html"],
    "brother": ["http://{ip}/general/status.html", "http://{ip}/general/information.html"],
    "toshiba": ["http://{ip}/?MAIN=DEVICE"],
    None: [],
}

SERIAL_HINT = re.compile(
    r"Serial\s*(?:Number|No\.?|#)?\s*[:：]?\s*(?:</\w+>\s*)?(?:<[^>]*>\s*){0,3}"
    r"([A-Za-z0-9][A-Za-z0-9\-]{3,30})", re.IGNORECASE)
SUPPLY_PAGES_HINT = re.compile(
    r"Pages\s+printed\s+with\s+this\s+supply[^0-9]{0,40}([0-9][0-9,]{0,12})", re.IGNORECASE)


def g(ip, community, oid, timeout, version=None):
    """Read one SNMP OID from a printer with the fallback logic."""

    return snmp_get_with_fallback(ip, oid, community, version=version, timeout=timeout)


def fmt(v):
    """Format an SNMP value for display."""

    if v is None:
        return "—"
    s = str(v)
    return s if len(s) <= 120 else s[:117] + "..."


def main():
    """Command-line entry point that probes the cartridge ID OIDs of one printer."""

    ap = argparse.ArgumentParser(description="Cartridge unique-ID probe")
    ap.add_argument("ip")
    ap.add_argument("--brand", default=None, choices=["hp", "canon", "brother", "toshiba"])
    ap.add_argument("--community", default="public")
    ap.add_argument("--timeout", type=float, default=2.5)
    ap.add_argument("--snmp-version", type=int, default=None, choices=[1, 2])
    ap.add_argument("--oid", action="append", default=[],
                    help="OID اضافه برای خواندن مستقیم (قابل تکرار) — برای تست کاندیدهای شناسه")
    args = ap.parse_args()

    ip, comm, to = args.ip, args.community, args.timeout
    out = []
    web_paths = []

    def w(line=""):
        """Write one line to the output list and to stdout."""
        out.append(line)
        print(line)

    w(f"# 🔬 پروب شناسه‌ی یکتای کارتریج — {ip}")
    w(f"- زمان اجرا: {datetime.datetime.now().isoformat(timespec='seconds')}")
    w(f"- برند گفته‌شده: {args.brand or 'auto'} | community={comm!r} | snmp-version={args.snmp_version or 'auto'}")

    # ── 1) Basic information
    w("\n## ۱) اطلاعات پایه‌ی دستگاه")
    base_info = {
        "sysDescr": f"{MIB}.1.1.0",
        "sysObjectID": f"{MIB}.1.2.0",
        "مدل (prtMIB)": f"{MIB}.43.5.1.1.16.1",
        "سریال پرینتر (prtMIB)": f"{MIB}.43.5.1.1.17.1",
    }
    for label, oid in base_info.items():
        w(f"- **{label}** (`{oid}`): `{fmt(g(ip, comm, oid, to, args.snmp_version))}`")

    # ── 2) Printer-MIB consumption table (raw text) ─────────────────────
    w("\n## ۲) ردیف‌های مصرفی Printer-MIB (متن verbatim — سریال گاهی در description است)")
    w("| idx | description (.6) | type (.5) | unit (.7) | max (.8) | level (.9) |")
    w("|-----|------------------|-----------|-----------|----------|------------|")
    for idx in range(1, 11):
        desc = g(ip, comm, f"{MIB}.43.11.1.1.6.1.{idx}", to, args.snmp_version)
        styp = g(ip, comm, f"{MIB}.43.11.1.1.5.1.{idx}", to, args.snmp_version)
        unit = g(ip, comm, f"{MIB}.43.11.1.1.7.1.{idx}", to, args.snmp_version)
        mx = g(ip, comm, f"{MIB}.43.11.1.1.8.1.{idx}", to, args.snmp_version)
        lv = g(ip, comm, f"{MIB}.43.11.1.1.9.1.{idx}", to, args.snmp_version)
        if desc is None and styp is None and lv is None:
            if idx <= 2:
                continue
            break
        w(f"| {idx} | {fmt(desc)} | {fmt(styp)} | {fmt(unit)} | {fmt(mx)} | {fmt(lv)} |")

    # ── 3) --oid OIDs (user candidates)
    if args.oid:
        w("\n## ۳) OIDهای کاندید (--oid)")
        for oid in args.oid:
            val = g(ip, comm, oid, to, args.snmp_version)
            nid = cid.normalize_id(val)
            mark = "✅ شبیه شناسه" if nid else "—"
            w(f"- `{oid}` → `{fmt(val)}`  {mark}{' → ' + nid if nid else ''}")

    # ── 4) Real runtime reader (no cache) ─────────────────────────
    w("\n## ۴) نتیجه‌ی خواننده‌ی واقعی سیستم (همان چیزی که poll می‌بیند)")
    cfg = cid.load_id_config(force=True)
    oids_cfg = cid._snmp_oids_for(ip, (args.brand or "").lower(), cfg)
    w(f"- OIDهای تنظیم‌شده در config/cartridge_id_map.json برای این دستگاه: `{oids_cfg or '—'}`")
    t0 = time.time()
    result = cid.get_cartridge_identity_data(
        ip=ip, brand=args.brand, community=comm, snmp_version=args.snmp_version,
        force_refresh=True)
    w(f"- نتیجه (در {time.time()-t0:.1f} ثانیه): ```json\n{json.dumps(result, ensure_ascii=False, indent=2)}\n```")

    # 5) Dump web pages + evidence extraction
    w("\n## ۵) صفحات وب (دامپ HTML کنار فایل ذخیره می‌شود)")
    for n, url_tpl in enumerate(WEB_URLS.get(args.brand, [])):
        used, html = fetch_first_web_page(ip, [url_tpl.format(ip=ip)], min(to, 4.0))
        if not html:
            w(f"- `{url_tpl.format(ip=ip)}` → ❌ در دسترس نیست")
            continue
        fname = f"probe_web_{ip}_{n}.html"
        web_paths.append(fname)
        with open(fname, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(html)
        serials = sorted(set(SERIAL_HINT.findall(html)))
        scr = SUPPLY_PAGES_HINT.search(html)
        w(f"- ✅ `{used}` → ذخیره در `{fname}` ({len(html):,} نویسه)")
        if serials:
            w(f"  - کاندیدهای سریال یافت‌شده: {serials}")
        if scr:
            w(f"  - «صفحات با این کارتریج»: {scr.group(1)}")
        loginish = re.search(r"(password|sign[ -]?in|log[ -]?in)", html[:8000], re.IGNORECASE)
        if loginish and not serials:
            w("  - ⚠️ صفحه شبیه فرم ورود (Login) است؛ اگر EWS رمز دارد، خواندن سریال ممکن نیست.")

    # 6) config suggestion
    w("\n## ۶) پیشنهاد آماده برای config/cartridge_id_map.json")
    suggestion = {"brands": {}, "printers": {ip: {"snmp": {}}}}
    have = False
    for oid in args.oid:
        nid = cid.normalize_id(g(ip, comm, oid, to, args.snmp_version))
        if nid:
            suggestion["printers"][ip]["snmp"]["black"] = oid
            have = True
            break
    if not have:
        suggestion["printers"].pop(ip)
        w("> فعلاً OID قابل‌استفاده‌ای تأیید نشد؛ HTMLهای دامپ را بفرستید تا الگو/OID دقیق ساخته شود.")
    else:
        w("```json\n" + json.dumps(suggestion, ensure_ascii=False, indent=2) + "\n```")

    report = f"probe_identity_{ip}.md"
    with open(report, "w", encoding="utf-8") as fh:
        fh.write("\n\n".join(out) + "\n")
    print(f"\n[OK] گزارش: {report}")
    if web_paths:
        print(f"[OK] دامپ‌های وب: {', '.join(web_paths)}")
    print("     این فایل‌ها را بفرستید تا نگاشت دقیق فعال شود.")


if __name__ == "__main__":
    main()
