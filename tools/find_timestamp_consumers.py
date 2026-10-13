#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
فهرست‌گیرِ مصرف‌کنندگان timestamp — ابزار تشخیصی فاز E.0 (کار E.0.0).

چرا؟ پس از کار D.6، ستون `logs.timestamp` و فیلد `details["timestamp"]` **دو نوع
مقدار** دارند: UTC با پسوند `Z` (مسیر موتور کارتریج) و naive local بدون پسوند
(سایر نویسنده‌ها). پیش از مهاجرت به PostgreSQL باید بدانیم چه کدی این ستون را
می‌خواند، مقایسه می‌کند، مرتب می‌کند، گروه‌بندی می‌کند یا نمایش می‌دهد — و هر
مصرف‌کننده با هر یک از این دو فرمت چه می‌کند.

این ابزار **فقط منبع کد را می‌خواند**؛ به هیچ دیتابیسی وصل نمی‌شود و چیزی
نمی‌نویسد.

دسته‌بندی‌ها
-----------
  W1  INSERT INTO logs            (نویسنده؛ مستقیم)
  W2  UPDATE/DELETE FROM logs     (نویسنده؛ اصلاح/حذف)
  W3  add_event(...)              (نویسنده؛ لایه‌ی پایتون + بررسی کلید timestamp)
  R1  FROM logs                   (خواننده)
  R2  timestamp مقایسه‌ای         (WHERE timestamp >= ?, ...)
  R3  ORDER BY timestamp          (مرتب‌سازی)
  R4  substr(timestamp, 1, 10)    (گروه‌بندی روزانه)
  R5  datetime(...) روی مقدار     (تجزیه در پایتون)
  R6  مصرف در UI (JS/HTML)        (نمایش)
  R7  json_extract(details, ...)  (خواننده‌ی JSON داخلی)

استفاده
-------
    python tools/find_timestamp_consumers.py                 # خلاصه‌ی خوانا
    python tools/find_timestamp_consumers.py --md out.md     # جدول Markdown
    python tools/find_timestamp_consumers.py --json out.json # خروجی ماشینی
    python tools/find_timestamp_consumers.py --only W3,R2    # فیلتر دسته
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from typing import Dict, Iterable, List, Tuple

ROOT_DEFAULT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PY_DIRS = ("core", "web", "tools", "tests")
ASSET_DIRS = ("web/static", "web/templates")
ROOT_FILES = ("create_admin.py", "run.py", "models.py")

# (کد, شرح, الگوی regex)
PY_PATTERNS: List[Tuple[str, str, str]] = [
    ("W1", "INSERT INTO logs", r"INSERT\s+(?:OR\s+REPLACE\s+)?INTO\s+logs\b"),
    ("W2", "UPDATE/DELETE روی logs", r"(?:UPDATE\s+logs\b|DELETE\s+FROM\s+logs\b)"),
    ("R1", "SELECT ... FROM logs", r"FROM\s+logs\b"),
    ("R2", "مقایسه‌ی timestamp در WHERE", r"timestamp\s*(?:>=|<=|>|<|=)\s*\?"),
    ("R3", "ORDER BY timestamp", r"ORDER\s+BY\s+timestamp\b"),
    ("R4", "گروه‌بندی روزانه‌ی timestamp", r"substr\s*\(\s*timestamp\s*,\s*1\s*,\s*10\s*\)"),
    ("R5", "تجزیه‌ی timestamp در پایتون", r"(?:datetime\.)?(?:fromisoformat|strptime)\s*\([^)]*timestamp"),
    ("R7", "خواندن JSON داخلی details", r"json_extract\s*\(\s*details"),
]

ASSET_PATTERNS: List[Tuple[str, str, str]] = [
    ("R6", "نمایش/مرتب‌سازی timestamp در UI", r"timestamp"),
]

ADD_EVENT_RE = re.compile(r"add_event\s*\(")
DATETIME_RE = re.compile(r"datetime\.(?:now|utcnow|fromisoformat)\s*\(")


SELF = os.path.abspath(__file__)


def _iter_files(root: str, dirs: Iterable[str], exts: Tuple[str, ...]) -> Iterable[str]:
    for d in dirs:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, files in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in ("__pycache__", ".venv")]
            for f in files:
                p = os.path.join(dirpath, f)
                if f.endswith(exts) and os.path.abspath(p) != SELF:
                    yield p
    for f in ROOT_FILES:
        p = os.path.join(root, f)
        if os.path.exists(p):
            yield p


def _rel(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _add_event_calls(path: str, src: str) -> List[dict]:
    """
    فراخوانی‌های add_event با تعیین اینکه «timestamp صریح» دارند یا نه.

    سه حالت بررسی می‌شود:
      • دیکشنری مستقیم در آرگومان‌ها با کلید timestamp؛
      • دیکشنری در متغیری که timestamp دارد (مثل event_payload در base.py:794)؛
      • بدون timestamp ⇒ add_event پیش‌فرض naive local می‌نویسد.
    """
    out: List[dict] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out

    # متغیرهایی که در هر دامنه timestamp می‌گیرند
    ts_vars = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
            if "timestamp" in keys:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        ts_vars.add(t.id)

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "add_event"):
            continue
        etype = ""
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            etype = str(node.args[1].value)
        explicit = False
        source = "پیش‌فرض (naive local)"
        for arg in node.args[2:]:
            if isinstance(arg, ast.Dict):
                if any(isinstance(k, ast.Constant) and k.value == "timestamp" for k in arg.keys):
                    explicit, source = True, "dict literal"
            elif isinstance(arg, ast.Name) and arg.id in ts_vars:
                explicit, source = True, f"متغیر {arg.id}"
        out.append({"line": node.lineno, "type": etype, "explicit_ts": explicit, "source": source})
    return out


def scan(root: str) -> Dict[str, List[dict]]:
    findings: Dict[str, List[dict]] = {}

    for path in _iter_files(root, PY_DIRS, (".py",)):
        rel = _rel(root, path)
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            for code, label, pattern in PY_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    findings.setdefault(code, []).append(
                        {"file": rel, "line": i, "label": label, "snippet": stripped[:160]})
            if ADD_EVENT_RE.search(line):
                findings.setdefault("W3", []).append(
                    {"file": rel, "line": i, "label": "فراخوانی add_event", "snippet": stripped[:160]})
            if DATETIME_RE.search(line):
                findings.setdefault("R5", []).append(
                    {"file": rel, "line": i, "label": "ساخت/خواندن زمان با datetime", "snippet": stripped[:160]})

        # غنی‌سازی W3: صریح بودن timestamp
        src = "\n".join(lines)
        calls = _add_event_calls(path, src)
        w3 = [f for f in findings.get("W3", []) if f["file"] == rel]
        by_line = {}
        for c in calls:
            by_line.setdefault(c["line"], []).append(c)
        for f in w3:
            info = by_line.get(f["line"])
            if info:
                c = info[0]
                f["explicit_ts"] = c["explicit_ts"]
                f["source"] = c["source"]
                f["event_type"] = c["type"]
                f["label"] = ("add_event با timestamp صریح" if c["explicit_ts"]
                              else "add_event بدون timestamp (پیش‌فرض naive local)")

    for path in _iter_files(root, ASSET_DIRS, (".js", ".html")):
        rel = _rel(root, path)
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if "timestamp" in line:
                    findings.setdefault("R6", []).append(
                        {"file": rel, "line": i, "label": "مصرف UI", "snippet": line.strip()[:160]})

    return findings


ORDER = ["W1", "W2", "W3", "R1", "R2", "R3", "R4", "R5", "R7", "R6"]
TITLES = {
    "W1": "نویسنده: INSERT INTO logs",
    "W2": "نویسنده: UPDATE/DELETE روی logs",
    "W3": "نویسنده: add_event (پایتون)",
    "R1": "خواننده: SELECT ... FROM logs",
    "R2": "مقایسه: WHERE timestamp ≥/?",
    "R3": "مرتب‌سازی: ORDER BY timestamp",
    "R4": "گروه‌بندی روزانه: substr(timestamp,1,10)",
    "R5": "تجزیه‌ی زمان در پایتون",
    "R7": "خواندن json_extract(details, ...)",
    "R6": "نمایش در UI (JS/HTML)",
}


def markdown(findings: Dict[str, List[dict]]) -> str:
    lines = ["# مصرف‌کنندگان timestamp — خروجی ابزار", ""]
    total = sum(len(v) for v in findings.values())
    lines.append(f"جمع یافته‌ها: **{total}** (تولیدشده با `tools/find_timestamp_consumers.py`)")
    lines.append("")
    for code in ORDER:
        items = findings.get(code) or []
        if not items:
            continue
        lines.append(f"## {code} — {TITLES.get(code, code)}  ({len(items)} مورد)")
        lines.append("")
        lines.append("| فایل:خط | شرح | کد |")
        lines.append("|---|---|---|")
        for it in items:
            extra = ""
            if code == "W3":
                extra = f" `[{it.get('event_type','')}]` {it.get('source','')}"
            snippet = it["snippet"].replace("|", "\\|")
            lines.append(f"| `{it['file']}:{it['line']}` | {it['label']}{extra} | `{snippet}` |")
        lines.append("")
    return "\n".join(lines)


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description="فهرست مصرف‌کنندگان timestamp (فاز E.0.0)")
    ap.add_argument("--root", default=ROOT_DEFAULT, help="ریشه‌ی مخزن")
    ap.add_argument("--md", help="نوشتن جدول Markdown در این مسیر")
    ap.add_argument("--json", dest="json_path", help="نوشتن خروجی JSON در این مسیر")
    ap.add_argument("--only", help="فقط این کدها، با کاما جدا شوند (مثل W1,R2)")
    args = ap.parse_args(argv)

    findings = scan(args.root)
    if args.only:
        keep = {c.strip().upper() for c in args.only.split(",") if c.strip()}
        findings = {k: v for k, v in findings.items() if k in keep}

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(findings, fh, ensure_ascii=False, indent=1)
        print(f"✔ JSON: {args.json_path}")

    if args.md:
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(markdown(findings))
        print(f"✔ Markdown: {args.md}")

    if not (args.md or args.json_path):
        print(markdown(findings))
        return 0

    print("\nخلاصه:")
    for code in ORDER:
        if code in findings:
            print(f"  {code:3} {TITLES.get(code, code):45} {len(findings[code]):3} مورد")
    print(f"  جمع: {sum(len(v) for v in findings.values())} مورد")
    return 0


if __name__ == "__main__":
    sys.exit(main())
