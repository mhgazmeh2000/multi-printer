#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/diagnose_printer.py
# @brief:        Diagnostic tool that reports toner and cartridge problems for one printer model.
# @details:      Responsibilities: Probe the printer and render a Markdown report.
#                Main API: g(), probe(), render_md(), main()
#                Side effects: network I/O, file I/O
# @dependencies: internal: core.collectors.base, core.collectors.cartridge_id, core.snmp.protocol
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Printer Diagnostic Tool — to report toner/cartridge problems for a specific model.

This script reads all raw data related to consumption from the device and a
Creates a Markdown report that, when submitted, adds a detailed mapping of that model to the project.

Execution (from the root of the project):
    python tools/diagnose_printer.py 172.16.25.10
    python tools/diagnose_printer.py 172.16.25.10 --community private --timeout 3

Output: diagnose_<ip>.md file in the current folder.
"""
import argparse
import datetime
import os
import re
import sys

# Ability to run from any folder: add the root of the project to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.snmp.protocol import snmp_get_with_fallback  # noqa: E402

MIB = "1.3.6.1.2.1"
PRT_SUPPLIES = f"{MIB}.43.11.1.1"


def g(ip, community, oid, timeout, version=None):
    """Read one SNMP OID from a printer with the fallback logic."""

    return snmp_get_with_fallback(ip, oid, community, version=version, timeout=timeout)


# Paths to each brand's web page for toner/cartridge content dump (fallback with.get).
PAGE_PATHS = {
    "hp": ["/hp/device/InternalPages/Index?id=SuppliesStatus",
           "/hp/info/suppliesStatus.html",
           "/DevMgmt/ConsumableConfigDyn.xml", "/"],
    "brother": ["/general/information.html", "/general/status.html", "/"],
    "canon": ["/", "/Status.html", "/status.html", "/supply.html"],
    "toshiba": ["/", "/MAIN/TopAccess/index.html", "/status.html", "/supply.html"],
    None: ["/", "/status.html", "/supply.html"],
}


def probe(ip, community, timeout, snmp_version=None):
    """Raw collection of all important fields."""
    data = {"ip": ip, "time": datetime.datetime.now().isoformat(timespec="seconds")}

    data["sysDescr"] = g(ip, community, f"{MIB}.1.1.0", timeout, snmp_version)
    data["sysObjectID"] = g(ip, community, f"{MIB}.1.2.0", timeout, snmp_version)
    data["sysUpTime"] = g(ip, community, f"{MIB}.1.3.0", timeout, snmp_version)
    data["model_prtMIB"] = g(ip, community, f"{MIB}.43.5.1.1.16.1", timeout, snmp_version)
    data["serial_prtMIB"] = g(ip, community, f"{MIB}.43.5.1.1.17.1", timeout, snmp_version)
    data["life_count"] = g(ip, community, f"{MIB}.43.10.2.1.4.1.1", timeout, snmp_version)

    # Consumables table — first 10 rows, all key columns
    # .3=class .4=colorantIndex .5=type .6=description .7=unit .8=max .9=level
    rows = []
    for idx in range(1, 11):
        name = g(ip, community, f"{PRT_SUPPLIES}.6.1.{idx}", timeout, snmp_version)
        if name is None and idx > 4:
            break
        row = {
            "idx": idx,
            "name": name,
            "class": g(ip, community, f"{PRT_SUPPLIES}.3.1.{idx}", timeout, snmp_version),
            "type": g(ip, community, f"{PRT_SUPPLIES}.5.1.{idx}", timeout, snmp_version),
            "unit": g(ip, community, f"{PRT_SUPPLIES}.7.1.{idx}", timeout, snmp_version),
            "max": g(ip, community, f"{PRT_SUPPLIES}.8.1.{idx}", timeout, snmp_version),
            "level": g(ip, community, f"{PRT_SUPPLIES}.9.1.{idx}", timeout, snmp_version),
        }
        if all(v is None for k, v in row.items() if k != "idx"):
            if idx > 2:
                break
            continue
        rows.append(row)
    data["supplies"] = rows

    # colors (prtMarkerColorantValue)
    colors = []
    for idx in range(1, 7):
        v = g(ip, community, f"{MIB}.43.12.1.1.4.1.{idx}", timeout, snmp_version)
        if v is not None:
            colors.append((idx, v))
    data["colorants"] = colors

    # Private OIDs of HP (JetDirect/NPCL) and Canon (NETEYE 1602)
    hp_oids = [
        "1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.4.1.2.1.5.5.1.1",
        "1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.1.5.5.1.1",
        "1.3.6.1.4.1.11.2.3.9.1.1.7.0",
        "1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.2.0",   # model
        "1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.3.0",   # serial
    ]
    canon_oids = [
        "1.3.6.1.4.1.1602.1.1.1.1.0",               # model
        "1.3.6.1.4.1.1602.1.2.1.4.0",               # serial
        "1.3.6.1.4.1.1602.1.2.1.1.1.1.1",
        "1.3.6.1.4.1.1602.1.2.1.1.1.2.1",
    ]
    data["hp_private"] = {o: g(ip, community, o, timeout, snmp_version) for o in hp_oids}
    data["canon_private"] = {o: g(ip, community, o, timeout, snmp_version) for o in canon_oids}

    # Brother-specific OIDs - NC-8xxx/MFC-85xx toner family in the table
    # The standard hides with level=-3 (there is some value). These candidates from MIBs
    # General Brother (brInfo/brToner) have been removed; Everyone should answer and write
    # We will put it in the collector.
    brother_oids = [
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.8.0",     # toner remaining (some models)
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.10.0",    # drum/counter candidate
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.10.0",  # brInfo status candidate
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.11.0",
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.12.0",
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.13.0",
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.14.0",
        "1.3.6.1.4.1.2435.2.4.3.99.3.1.6.1.2.1",     # model (many NC-boards)
        "1.3.6.1.4.1.2435.2.4.3.99.3.1.6.1.2.3",     # serial
    ]
    desc_l = str(data.get("sysDescr") or "").lower()
    if "brother" in desc_l:
        data["brother_private"] = {o: g(ip, community, o, timeout, snmp_version) for o in brother_oids}
    else:
        data["brother_private"] = {}

    # PJL on port 9100 — reply @PJL INFO STATUS Sometimes the status of users is textual
    # gives If the port is closed, we are rejected silently.
    data["pjl_status"] = _probe_pjl(ip, timeout=min(timeout, 2.5))

    # Device web page — HTTP and HTTPS access + toner/cartridge related content dump
    # (For script-based fixes: HP with SuppliesStatus/DevMgmt, Brother with
    # EWS pages, Canon with Remote UI). Output "toner_contexts" surrounding HTML fragments
    # keywords to write the exact regex pattern for each model.
    from core.collectors.base import fetch_first_web_page

    web = {}
    desc_l = str(data.get("sysDescr") or "").lower()
    hint = next((b for tok, b in (
        ("brother", "brother"), ("canon", "canon"),
        ("hp ", "hp"), ("laserjet", "hp"), ("jetdirect", "hp"),
        ("toshiba", "toshiba"),
    ) if tok in desc_l), None)
    page_paths = PAGE_PATHS.get(hint, PAGE_PATHS[None])
    data["_web_brand_hint"] = hint

    kw_re = re.compile(r"toner|drum|cartridge|suppl|percent", re.IGNORECASE)
    pct_re = re.compile(r"\d{1,3}\s*%")

    for p in page_paths:
        body = None
        tried = []
        for scheme in ("http", "https"):
            url = f"{scheme}://{ip}{p}"
            used, body = fetch_first_web_page(ip, [url], timeout=min(timeout, 5.0))
            if body:
                break
            tried.append(type(None).__name__ if used is None else "error")
        if not body:
            web[f"http(s)://{ip}{p}"] = {"error": "unreachable"}
            continue
        entry = {
            "status": 200,
            "final_url": used,
            "chars": len(body),
            "snippet": " ".join(body.split())[:400],
        }
        # Full dump of small pages (XMLs and classic Brother/HP pages) — to finalize
        # Strictly script patterns, incomplete contexts are not enough.
        if len(body) <= 12000:
            entry["full"] = body
        contexts = []
        for m in kw_re.finditer(body):
            start = max(0, m.start() - 90)
            ctx = " ".join(body[start:m.end() + 110].split())
            if ctx not in contexts:
                contexts.append(ctx)
            if len(contexts) >= 25:
                break
        for m in pct_re.finditer(body):
            start = max(0, m.start() - 90)
            ctx = " ".join(body[start:m.end() + 60].split())
            if ctx not in contexts:
                contexts.append(ctx)
            if len(contexts) >= 35:
                break
        entry["toner_contexts"] = contexts
        web[used] = entry
    data["web"] = web
    return data


def _probe_pjl(ip: str, timeout: float = 2.5) -> str:
    """Send the PJL command to port 9100 and read the response (if open)."""
    import socket
    try:
        with socket.create_connection((ip, 9100), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"\x1b%-12345X@PJL\r\n@PJL INFO STATUS\r\n\x1b%-12345X")
            chunks = []
            try:
                while sum(len(c) for c in chunks) < 2048:
                    data = s.recv(1024)
                    if not data:
                        break
                    chunks.append(data)
            except socket.timeout:
                pass
            raw = b"".join(chunks)
            return raw.decode("latin-1", errors="replace").strip() or "(پاسخ خالی)"
    except Exception as exc:
        return f"(در دسترس نیست: {exc.__class__.__name__})"


def render_md(d):
    """Render the printer diagnostic result as Markdown."""

    L = []
    L.append(f"# گزارش تشخیص پرینتر {d['ip']}")
    L.append(f"- زمان: {d['time']}")
    for k in ("sysDescr", "sysObjectID", "sysUpTime", "model_prtMIB", "serial_prtMIB", "life_count"):
        L.append(f"- **{k}**: `{d.get(k)}`")
    L.append("")
    L.append("## جدول prtMarkerSuppliesTable (خام)")
    L.append("")
    L.append("| idx | description | class | type | unit | max | level |")
    L.append("|---|---|---|---|---|---|---|")
    for r in d["supplies"]:
        L.append(f"| {r['idx']} | {r['name']} | {r['class']} | {r['type']} | {r['unit']} | {r['max']} | {r['level']} |")
    L.append("")
    L.append("### عناصر کلیدی تفسیر")
    L.append("- unit: `19` یعنی درصد؛ `8` sheets ؛ `7` impressions")
    L.append("- level/max: `-1` بدون محدودیت، `-2` نامشخص، `-3` «مقداری هست»")
    L.append("- Canon i-SENSYS ممکن است کد گسسته `0=Empty / 5=Low / 7=OK` بدهد")
    L.append("")
    if d["colorants"]:
        L.append("## prtMarkerColorantValue")
        for idx, v in d["colorants"]:
            L.append(f"- idx {idx}: `{v}`")
        L.append("")
    for sect, title in (("hp_private", "OIDهای خصوصی HP"), ("canon_private", "OIDهای خصوصی Canon"),
                        ("brother_private", "OIDهای خصوصی Brother")):
        if not d.get(sect):
            continue
        L.append(f"## {title}")
        for o, v in d[sect].items():
            L.append(f"- `{o}` → `{v}`")
        L.append("")
    if d.get("pjl_status"):
        L.append("## پاسخ PJL (پورت 9100)")
        L.append("```")
        L.append(str(d["pjl_status"]))
        L.append("```")
        L.append("")
    if d.get("web"):
        L.append("## پنل وب دستگاه")
        if d.get("_web_brand_hint"):
            L.append(f"- حدس برند از sysDescr: `{d['_web_brand_hint']}`")
        for url, info in d["web"].items():
            if "error" in info:
                L.append(f"- {url} → `{info}`")
            else:
                L.append(f"- {url} → status={info['status']} chars={info['chars']} final=`{info['final_url']}`")
                L.append(f"  - snippet: `{info['snippet']}`")
                if info.get("toner_contexts"):
                    L.append("  - 🔍 تکه‌های مرتبط با تونر/کارتریج (برای پترن‌های اسکریپ):")
                    for c in info["toner_contexts"]:
                        L.append(f"    - `{c}`")
                if info.get("full") is not None:
                    L.append("  - 📄 دامپ کامل صفحه:")
                    L.append("")
                    L.append("```html")
                    L.append(info["full"])
                    L.append("```")
                    L.append("")
        L.append("")
    return "\n".join(L)


def _scan_toshiba_paper_map(ip, community, timeout, snmp_version):
    """Scan the branches of Toshiba's private counter table to discover exact size mappings.

    Known mapping (proof on 2050C/3015AC):
      207=large whole, 208=small whole, 209=print(large), 210=print(small), 227=unclear
    If other branches (e.g. instant A3 or copy-per-size) respond on the machine,
    By running this probe, they are determined immediately after an A3 print.
    """
    base = "1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1"
    known = {2: "print_total", 3: "printer", 4: "copy", 7: "fax", 8: "scan_net",
             9: "scan", 11: "list", 207: "ALL large", 208: "ALL small",
             209: "PRINT large", 210: "PRINT small", 227: "unknown-227"}
    cols = {1: "fc", 2: "twin?", 3: "bw", 4: "total"}
    lines = ["", "## Toshiba paper counter map (--paper-map)", "",
             "| branch | fc(1) | twin?(2) | bw(3) | total(4) | نگاشت |",
             "|---|---|---|---|---|---|"]
    found = 0
    print(f"[..] scanning Toshiba counter branches 200-240 on {ip} ...")
    for branch in range(200, 241):
        vals = {}
        for c in cols:
            v = g(ip, community, f"{base}.{branch}.1.{c}", timeout, snmp_version)
            if v not in (None, "", "N/A"):
                vals[c] = v
        if not vals:
            continue
        found += 1
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            branch,
            vals.get(1, "—"), vals.get(2, "—"), vals.get(3, "—"), vals.get(4, "—"),
            known.get(branch, "❓ جدید — کاندید نگاشت سایز")))
    if not found:
        lines.append("| — | — | — | — | — | هیچ شاخه‌ای پاسخ نداد |")
    print(f"[OK] paper-map scan done: {found} active branch(es)")
    return "\n".join(lines) + "\n"


def _scan_cartridge_identity(ip, community, timeout, snmp_version, brand_hint=None):
    """Exploring evidence of "unique cartridge ID" (Chip ID / serial / cartridge pages).

    It uses the same logic as the actual runtime (core/collectors/cartridge_id.py):
    The first priority is the verified OIDs in config/cartridge_id_map.json and then the web panel.
    In the absence of evidence, the activation guide is printed.
    """
    from core.collectors.cartridge_id import (
        get_cartridge_identity_data, load_id_config, _snmp_oids_for)

    cfg = load_id_config(force=True)
    brand = brand_hint or ""
    lines = ["\n---\n\n## 🔍 کاوش شناسه‌ی یکتای کارتریج (Chip ID / Serial / Supply Counter)\n"]
    cfg_exists = os.path.exists(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "config", "cartridge_id_map.json"))
    lines.append(f"- فایل نقشه‌ی OID تأییدشده (config/cartridge_id_map.json): "
                 f"{'✅ یافت شد' if cfg_exists else '— موجود نیست (نمونه: cartridge_id_map.example.json)'}")
    oids = _snmp_oids_for(ip, (brand or "").lower(), cfg)
    if oids:
        lines.append(f"- OIDهای تنظیم‌شده برای این دستگاه/برند: {oids}")

    result = get_cartridge_identity_data(
        ip=ip, brand=brand, community=community, snmp_version=snmp_version,
        force_refresh=True) or {}
    ids = result.get("ids") or {}
    pages = result.get("supply_pages") or {}
    lines.append(f"- منبع شواهد یافت‌شده: `{result.get('source', 'none')}`")
    if ids or pages:
        lines.append("\n| رنگ | سریال/ChipID | صفحات با این کارتریج |")
        lines.append("|-----|--------------|----------------------|")
        for ck in sorted(set(list(ids) + list(pages))):
            lines.append(f"| {ck} | `{ids.get(ck, '—')}` | {pages.get(ck, '—')} |")
    else:
        lines.append(
            "\n> ⚠️ برای این مدل شناسه‌ی عمومی پیدا نشد. اگر در پنل وب دستگاه یا برچسب کارتریج،\n"
            "> شماره‌ی سریال/Chip ID می‌بینید: مقدار دقیق + آدرس صفحه (URL) + مدل دستگاه را ارسال کنید\n"
            "> تا OID/الگوی اختصاصی در cartridge_id_map.json یا کد اضافه شود. تا آن زمان، تشخیص تعویض\n"
            "> با fallback جهش سطح تونر انجام می‌شود.")
    return "\n".join(lines) + "\n"


def main():
    """Command-line entry point that prints the supply diagnostics for one printer."""

    ap = argparse.ArgumentParser(description="Printer supply diagnostic dumper")
    ap.add_argument("ip")
    ap.add_argument("--community", default="public")
    ap.add_argument("--timeout", type=float, default=2.5)
    ap.add_argument("--snmp-version", type=int, default=None, choices=[1, 2])
    ap.add_argument(
        "--paper-map", action="store_true",
        help="Toshiba: اسکن شاخه‌های شمارنده ۲۰۰ تا ۲۴۰ (ستون‌های ۱..۴) برای کشف "
             "نگاشت دقیق سایز کاغذ (A3/B4/A5 لحظه‌ای). بهتر است بلافاصله پس از "
             "چاپ یک صفحه A3 اجرا شود تا شاخه‌ی فقط‌A3 مشخص شود.")
    args = ap.parse_args()

    print(f"[..] probing {args.ip} (community={args.community!r}) ...")
    data = probe(args.ip, args.community, args.timeout, args.snmp_version)
    md = render_md(data)
    try:
        md += _scan_cartridge_identity(
            args.ip, args.community, args.timeout, args.snmp_version,
            brand_hint=(data.get("brand_hint") or None))
    except Exception as exc:
        md += f"\n## 🔍 کاوش شناسه‌ی کارتریج\n\nخطا در اجرای کاوش: {exc}\n"
    if args.paper_map:
        md += _scan_toshiba_paper_map(args.ip, args.community, args.timeout, args.snmp_version)
    out = f"diagnose_{args.ip}.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"[OK] report written: {out}")
    print("     این فایل را ارسال کنید تا نگاشت دقیق مدل به پروژه اضافه شود.")


if __name__ == "__main__":
    main()
