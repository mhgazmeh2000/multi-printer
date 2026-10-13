#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""اسکن PII در fixtureهای طلایی (و منابعشان) — گام E.0.7.

چرا؟ مخزن عمومی است و `tests/fixtures/golden/*.json` عیناً داده‌ی دستگاه‌های
واقعیِ زمان توسعه را نگه می‌دارد: IP، سریال چاپگر، نام/نامک، نام کاربری. اگر
یکی از آن‌ها واقعی باشد، در GitHub منتشر شده است. این ابزار همان‌ها را فهرست
می‌کند و برای هر مورد حکم می‌دهد: «مصنوعی/مستند (بی‌خطر)» یا «باید mock شود».

منابع اسکن (به ترتیب اهمیت):
  ۱. ``tests/fixtures/golden/*.json`` — خروجی ضبط‌شده (چیزی که منتشر می‌شود).
  ۲. ``tests/golden_profiles.py`` و ``tests/golden_scenarios.py`` — **سرچشمه**ی
     همان مقادیر (اصلاح باید این‌جا انجام و سپس fixture بازضبط شود).

قواعد تشخیص:
  • IP: با حذف قطعه‌های OID («1.3.6.1.4.1.47206.110.1.2.0» و مانند آن)، بقیه
    با محدوده‌های RFC 5737 (192.0.2.0/24، 198.51.100.0/24، 203.0.113.0/24)،
    RFC 1918 (10/8، 172.16/12، 192.168/16)، loopback/link-local طبقه‌بندی
    می‌شوند؛ هر IP دیگر ⇒ «احتمال واقعی».
  • سریال: الگوهای [A-Z]{2,4}[A-Z0-9]{6,}؛ اگر نشانه‌ی مصنوعی داشته باشد
    (MOCK/TEST/SAMPLE یا رشتهٔ 123456/0000) ⇒ بی‌خطر، وگرنه ⇒ «قالب واقعی».
  • MAC، ایمیل، FQDN: مطابق الگوی متعارف؛ همه باید در محدودهٔ نمونه باشند.
  • نام: فیلدهای هویتی (name/nickname/username/technician/user_identifier/…)
    فقط اگر از allowlist «برچسب توصیفی/محصول» بیرون باشند بازبینی می‌شوند.

اجرا:

    python tools/scan_golden_for_pii.py
    python tools/scan_golden_for_pii.py --md /tmp/pii.md --json /tmp/pii.json
    python tools/scan_golden_for_pii.py --all-ips        # نمایش IPهای بی‌خطر هم

کدهای خروج: 0 = بی‌خطر، 2 = مورد نیازمند mock/بازبینی هست، 1 = خطای اجرایی.
"""

from __future__ import annotations

import argparse
import ast
import ipaddress
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures", "golden")
SOURCES = (
    os.path.join(REPO_ROOT, "tests", "golden_profiles.py"),
    os.path.join(REPO_ROOT, "tests", "golden_scenarios.py"),
)

IP_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


def key_of(path: str) -> str:
    """نام آخرین کلید یک مسیر JSON (``$.a.b[0].name`` ⇒ ``name``)."""
    return path.rsplit(".", 1)[-1].split("[")[0].lower()
OID_HINT_RE = re.compile(r"(1\.3\.6\.1|\bget:|\bset:|\bgetnext:|\boctetstring:|\bint:|"
                         r"\bipaddress:|\bgauge:|\btimeticks:)")
LONG_DOTTED_RE = re.compile(r"\d+(?:\.\d+){5,}")     # ۶+ جزء ⇒ OID، نه IP
# سریال‌ها: یا ۲ تا ۴ حرف + ۶+ کاراکتر (مثل «CNXXXXXXXX») یا یک حرف + ۸+
# کاراکتر (مثل «UXXXXXXXXXXXXX»). مثال‌ها عمداً ساختگی‌اند تا خودِ این فایل
# حامل مقدار واقعی نباشد. الگو فقط در «بافتِ سریال» سنجیده می‌شود.
SERIAL_RE = re.compile(r"\b(?:[A-Z]{2,4}[-]?[A-Z0-9]{6,}|[A-Z][A-Z0-9]{8,})\b")
MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
FQDN_RE = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.(?:local|lan|com|net|"
                     r"org|io|ir|internal|corp)\b", re.I)

MOCK_MARKERS = ("MOCK", "TEST", "DUMMY", "SAMPLE", "FAKE", "123456", "0000")

# OIDهایی که مقدارشان *سریال* است (Printer-MIB 43.5.1.1.17.1 و معادل‌های برندها)
SERIAL_OIDS = ("1.3.6.1.2.1.43.5.1.1.17.1", "1.3.6.1.4.1.2435.2.4.3.99.3.1.6.1.2.3",
               "1.3.6.1.4.1.1129.2.3.50.1.2.4.1.8.1.1")
SYSNAME_OIDS = ("1.3.6.1.2.1.1.5.0",)
# الگوهای توصیفی که «نام» تلقی می‌شوند ولی مصنوعی‌اند
DESCRIPTIVE_RE = (
    re.compile(r"^tray\s*\d+$", re.I),                 # سینی‌ها
    re.compile(r"^[a-z]+ toner$", re.I),                  # «black toner»
    re.compile(r"^(counters|enhanced)_\d\d_", re.I),     # نام fixture
)

# فیلدهای هویتی که مقدارشان باید «برچسب توصیفی/محصول» باشد
IDENTITY_KEYS = ("name", "nickname", "username", "technician", "user_identifier",
                 "printer_name", "hostname", "user", "owner", "contact", "email")
# برچسب‌های توصیفیِ مصنوعی که عمداً در سناریوها به‌کار رفته‌اند
NAME_ALLOWLIST = (
    "hp m577 print room", "hp m577 empty", "brother hl-l2350 office",
    "toshiba e-studio3515ac", "ecs100g rack", "print-room", "smoke_admin",
    "hp-رنگی", "hp-خالی", "توشیبا", "سیستم", "اسکن",
)
# مدل/محصول‌نمای ASCII: حرف+رقم مثل CF287A، M577، HL-L2350، e-STUDIO3515AC
PRODUCT_LIKE_RE = re.compile(
    r"\b(?:[a-z]{1,3}[- ]?\d{2,}[a-z0-9-]*|\d{3,}[a-z]{1,3})\b", re.I)


def _ip_verdict(value: str, ctx: str) -> tuple:
    """(حکم, توضیح) برای یک IP نامزد."""
    if LONG_DOTTED_RE.search(ctx) or OID_HINT_RE.search(ctx):
        return ("ok_oid", "قطعه‌ی OID (نه IP)")
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return ("review", "قالب نامعتبر")
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return ("ok_local", "loopback/link-local")
    if ip in ipaddress.ip_network("192.0.2.0/24") or \
       ip in ipaddress.ip_network("198.51.100.0/24") or \
       ip in ipaddress.ip_network("203.0.113.0/24"):
        return ("ok_doc", "محدوده‌ی مستندسازی RFC 5737")
    if ip.is_private:
        return ("ok_private", "محدوده‌ی خصوصی RFC 1918")
    return ("action", "IP عمومی — اگر واقعی است باید mock شود")


def _serial_verdict(value: str) -> tuple:
    up = value.upper()
    if any(m in up for m in MOCK_MARKERS):
        return ("ok_mock", "نشانه‌ی مصنوعی دارد")
    return ("action", "قالب سریال واقعی (بدون نشانه‌ی مصنوعی)")


def _name_verdict(value: str) -> tuple:
    v = value.strip()
    if not v:
        return ("ok_empty", "خالی")
    if v.lower() in NAME_ALLOWLIST:
        return ("ok_label", "برچسب توصیفی سناریو")
    if PRODUCT_LIKE_RE.search(v):          # نام محصول/مدل یا برچسب دستگاه
        return ("ok_product", "نام محصول/مدل کالا")
    if v.startswith("<") and v.endswith(">"):
        return ("ok_norm", "برچسب نرمال‌سازی‌شده")
    if any(rx.search(v) for rx in DESCRIPTIVE_RE):
        return ("ok_label", "برچسب ساختاری/توصیفی (نه نام شخص)")
    return ("review", "مقدار هویتی نامشخص — بازبینی دستی")


# ─────────────────────────────── اسکن JSON ──────────────────────────────────
def _walk(obj, path="$"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def scan_fixtures(fixtures_dir: str, show_all_ips=False) -> list:
    findings = []
    for name in sorted(os.listdir(fixtures_dir)):
        if not name.endswith(".json"):
            continue
        full = os.path.join(fixtures_dir, name)
        with open(full, encoding="utf-8") as fh:
            data = json.load(fh)
        for path, value in _walk(data):
            if not isinstance(value, str):
                continue
            findings += _scan_text(value, f"fixtures/{name}", path, show_all_ips)
    return findings


def _scan_text(value: str, where: str, path: str, show_all_ips=False,
               line_ctx: str = "") -> list:
    """اسکن یک رشته؛ ``line_ctx`` متن خط در فایل‌های منبع است (برای تشخیص سریال)."""
    out = []
    for m in IP_RE.finditer(value):
        verdict, note = _ip_verdict(m.group(0), value)
        if verdict.startswith("ok_") and not show_all_ips and verdict == "ok_oid":
            continue
        out.append({"where": where, "path": path, "kind": "ip",
                    "value": m.group(0), "verdict": verdict, "note": note})
    # سریال‌ها فقط در بافت سریال سنجیده می‌شوند (وگرنه نام مدل مثل HL-L2350DW
    # یا e-STUDIO3515AC اشتباهاً سریال شمرده می‌شود).
    serial_ctx = ("serial" in path.lower()
                  or any(o in path for o in SERIAL_OIDS)
                  or "SERIAL" in (line_ctx or "").upper())
    for m in SERIAL_RE.finditer(value):
        token = m.group(0)
        if not serial_ctx:
            continue
        verdict, note = _serial_verdict(token)
        out.append({"where": where, "path": path, "kind": "serial",
                    "value": token, "verdict": verdict, "note": note})
    for m in MAC_RE.finditer(value):
        out.append({"where": where, "path": path, "kind": "mac",
                    "value": m.group(0), "verdict": "review",
                    "note": "MAC — باید نمونه‌ی مصنوعی باشد"})
    for m in EMAIL_RE.finditer(value):
        v = m.group(0)
        dom = v.split("@")[-1].lower()
        ok = dom in ("example.com", "example.org", "test.io", "localhost") or \
            dom.endswith(".test") or dom.endswith(".invalid")
        out.append({"where": where, "path": path, "kind": "email", "value": v,
                    "verdict": "ok_dom" if ok else "action",
                    "note": "دامنه‌ی نمونه" if ok else "دامنه‌ی واقعی؟"})
    for m in FQDN_RE.finditer(value):
        v = m.group(0)
        ok = v.lower() in ("example.com", "example.org", "localhost") or \
            v.lower().endswith((".local", ".test", ".invalid"))
        out.append({"where": where, "path": path, "kind": "fqdn", "value": v,
                    "verdict": "ok_dom" if ok else "review",
                    "note": "دامنه‌ی نمونه/داخلی" if ok else "میزبان عمومی؟"})
    if ".phases[" in path and key_of(path) == "name":
        out.append({"where": where, "path": path, "kind": "phase", "value": value,
                    "verdict": "ok_label", "note": "نام فاز سناریو (ساختاری)"})
    if any(o in path for o in SYSNAME_OIDS):
        v = value.strip()
        ok = bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", v)) and not re.search(r"\d{5,}", v)
        out.append({"where": where, "path": path, "kind": "hostname", "value": v,
                    "verdict": "ok_label" if ok else "review",
                    "note": "نام میزبان توصیفی سناریو" if ok
                            else "نام میزبان با شناسه‌ی یکتا — بازبینی"})
    key = path.rsplit(".", 1)[-1].split("[")[0].lower()
    if key in IDENTITY_KEYS and value.strip() and ".phases[" not in path:
        verdict, note = _name_verdict(value)
        out.append({"where": where, "path": path, "kind": "identity",
                    "value": value[:60], "verdict": verdict, "note": note})
    return out


# ─────────────────────────────── اسکن منابع ─────────────────────────────────
def scan_sources(files=SOURCES) -> list:
    """استخراج رشته‌های ثابت از فایل‌های پایتون منبع (AST)."""
    findings = []
    for full in files:
        if not os.path.exists(full):
            continue
        with open(full, encoding="utf-8") as fh:
            source = fh.read()
        lines = source.splitlines()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                ctx = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
                findings += _scan_text(node.value,
                                       f"source/{os.path.basename(full)}",
                                       f"line {node.lineno}", show_all_ips=False,
                                       line_ctx=ctx)
    return findings


# ─────────────────────────────── گزارش ─────────────────────────────────────
def dedupe(findings: list) -> list:
    """یکتا‌سازی بر پایه (فایل، نوع، مقدار) — با حفظ اولین مسیر."""
    seen, out = set(), []
    for f in findings:
        key = (f["where"], f["kind"], f["value"])
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def print_report(findings: list) -> int:
    print("=" * 82)
    print("اسکن PII در fixtureهای طلایی — فاز E.0.7 (فقط خواندن)")
    print("=" * 82)
    order = {"action": 0, "review": 1}
    findings = sorted(findings, key=lambda f: (order.get(f["verdict"], 2),
                                               f["kind"], f["value"]))
    actions = [f for f in findings if f["verdict"] == "action"]
    reviews = [f for f in findings if f["verdict"] == "review"]

    if actions:
        print(f"\n🚨 نیازمند mock ({len(actions)} مورد):")
        for f in actions:
            print(f"   • [{f['kind']}] {f['value']!r}  ({f['note']})")
            print(f"     {f['where']} :: {f['path']}")
    if reviews:
        print(f"\n⚠ نیازمند بازبینی ({len(reviews)} مورد):")
        for f in reviews:
            print(f"   • [{f['kind']}] {f['value']!r}  ({f['note']})")
            print(f"     {f['where']} :: {f['path']}")

    by_kind = {}
    for f in findings:
        by_kind.setdefault(f["kind"], {}).setdefault(f["verdict"], set()).add(f["value"])
    print("\nخلاصه‌ی طبقه‌بندی:")
    for kind in sorted(by_kind):
        parts = [f"{v}={len(vals)}" for v, vals in sorted(by_kind[kind].items())]
        print(f"   {kind:9s} " + ", ".join(parts))

    print("\nمقادیر بی‌خطر (خلاصه):")
    for kind in ("ip", "serial", "identity"):
        vals = sorted({f["value"] for f in findings
                       if f["kind"] == kind and f["verdict"].startswith("ok")})
        print(f"   {kind:9s} {vals[:14]}{' …' if len(vals) > 14 else ''}")

    print("-" * 82)
    if actions or reviews:
        print(f"نتیجه: {len(actions)} مورد mock لازم + {len(reviews)} مورد بازبینی ⇒ کد خروج 2")
        return 2
    print("نتیجه: هیچ داده‌ی واقعی‌ای پیدا نشد ✔")
    return 0


def to_markdown(findings: list) -> str:
    lines = ["| فایل | مسیر | نوع | مقدار | حکم | توضیح |", "|---|---|---|---|---|---|"]
    for f in findings:
        lines.append(f"| `{f['where']}` | `{f['path']}` | {f['kind']} | "
                     f"`{f['value']}` | {f['verdict']} | {f['note']} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="اسکن PII در golden — E.0.7")
    ap.add_argument("--fixtures", default=DEFAULT_FIXTURES)
    ap.add_argument("--md", default="")
    ap.add_argument("--json", dest="json_path", default="")
    ap.add_argument("--all-ips", action="store_true",
                    help="نمایش قطعه‌های OID هم (پیش‌فرض: پنهان)")
    ap.add_argument("--no-sources", action="store_true",
                    help="فقط fixtureها، بدون فایل‌های منبع")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    findings = scan_fixtures(args.fixtures, show_all_ips=args.all_ips)
    if not args.no_sources:
        findings += scan_sources()
    findings = dedupe(findings)
    code = print_report(findings)

    if args.md:
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(to_markdown(findings))
        print(f"[INFO] جدول Markdown نوشته شد: {args.md}")
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(findings, fh, ensure_ascii=False, indent=2)
        print(f"[INFO] خروجی JSON نوشته شد: {args.json_path}")
    return code


if __name__ == "__main__":
    sys.exit(main())
