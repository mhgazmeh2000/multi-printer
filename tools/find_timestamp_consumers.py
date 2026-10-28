#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/find_timestamp_consumers.py
# @brief:        Indexes the consumers of timestamp values (E.0 diagnostic).
# @details:      Responsibilities: Scan the code and write a Markdown index.
#                Main API: scan(), markdown(), main()
#                Side effects: file I/O
# @dependencies: stdlib only
# @notes:        Can be run directly as a script.
# =============================================================================

"""
timestamp consumer indexer — E.0 phase diagnostic tool (E.0.0 job).

why After work D.6, column `logs.timestamp` and field `details["timestamp"]` **two types
They have ** value: UTC with `Z' extension (cartridge motor path) and naive local without extension
(other authors). Before migrating to PostgreSQL, we need to know the code of this column
Reads, compares, sorts, groups or displays — and whatever
What does the consumer do with each of these two formats?

This tool **only reads source code**; It does not connect to any database or anything
does not write

Categories
-----------
  W1 INSERT INTO logs (author; direct)
  W2 UPDATE/DELETE FROM logs (author; modify/delete)
  W3 add_event(...) (author; Python layer + timestamp key check)
  R1 FROM logs (reader)
  R2 comparative timestamp (WHERE timestamp >=?,...)
  R3 ORDER BY timestamp
  R4 substr(timestamp, 1, 10) (grouping by day)
  R5 datetime(...) on value (parsing in Python)
  R6 consumption in UI (JS/HTML) (display)
  R7 json_extract(details,...) (built-in JSON reader)

use
-------
    python tools/find_timestamp_consumers.py # Readable summary
    python tools/find_timestamp_consumers.py --md out.md # Markdown table
    python tools/find_timestamp_consumers.py --json out.json # Machine output
    python tools/find_timestamp_consumers.py --only W3,R2 # Batch filter
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

# (code, description, regex pattern)
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

ADD_EVENT_RE = re.compile(r"add_event\s*\(")
DATETIME_RE = re.compile(r"datetime\.(?:now|utcnow|fromisoformat)\s*\(")


SELF = os.path.abspath(__file__)


def _iter_files(root: str, dirs: Iterable[str], exts: Tuple[str, ...]) -> Iterable[str]:
    """Yield the file paths under the given directories with the given extensions."""
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
    """Return a path relative to the root, using forward slashes."""
    return os.path.relpath(path, root).replace(os.sep, "/")


def _add_event_calls(path: str, src: str) -> List[dict]:
    """
    add_event calls by specifying whether they have an "explicit timestamp" or not.

    Three modes are checked:
      • Direct dictionary in arguments with timestamp key;
      • Dictionary in the variable that has a timestamp (such as event_payload in base.py:813);
      • Without timestamp ⇒ add_event writes the default naive local.
    """
    out: List[dict] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out

    # Variables that take timestamps in each domain
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
    """Scan a source tree for code that consumes timestamps."""

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

        # W3 enrichment: timestamp explicitness
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
    """Render the timestamp-consumer findings as Markdown."""

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
    """Command-line entry point of the timestamp-consumer scan."""

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
