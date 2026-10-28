#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/find_insert_or_replace.py
# @brief:        Lists 'INSERT OR REPLACE' usages and their nested columns (step E.0.4).
# @details:      Responsibilities: Scan the source for the SQL pattern and enrich the report with
#                schema data.
#                Main API: scan_file(), scan_assigned_verbs(), build_schema_map(), enrich(),
#                decide(), print_report(), to_markdown(), main()
#                Side effects: database access, file I/O
# @dependencies: internal: core.database, core.security_audit, core.yield_engine
# @notes:        Can be run directly as a script.
# =============================================================================

"""`INSERT OR REPLACE' list and nested columns - step E.0.4.

why `INSERT OR REPLACE' in SQLite means "If there was a key conflict, delete the previous row."
Go and write a new row." This meaning is **not the same** as ``ON CONFLICT DO UPDATE'' of Postgres:
  • SQLite: rows are deleted and reinserted ⇒ columns that are not listed
    **reverts to default/NULL** (data is lost).
  • Postgres: ``ON CONFLICT DO UPDATE'' changes only the named columns and
    Other columns are left untouched ⇒ If we want SQLite behavior, we should
    Write all the columns of the table explicitly or ``DO UPDATE SET col=EXCLUDED.col''
    Let's leave it for everyone; And if we want Postgres behavior, that is SQLite today
    Clears data silently (hidden bug).

So, before migration, we should know what columns are written in each place and which ones
The columns fall into place**.

This tool is read-only (does not open any production DB; for the column map a DB
temporary in the temporary folder) and is AST-oriented:

    python tools/find_insert_or_replace.py
    python tools/find_insert_or_replace.py --md /tmp/ior.md --json /tmp/ior.json
    python tools/find_insert_or_replace.py --no-schema # No temporary DB creation

Exit codes: 0 = all locations are definite, 2 = dynamic/unknown location exists, 1 = error.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sqlite3
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEARCH_DIRS = ("core", "web", "tools", "tests")
ROOT_FILES = ("create_admin.py",)
SELF = os.path.abspath(__file__)
PHRASE = "INSERT OR REPLACE"

_SQL_RE = re.compile(
    r"INSERT\s+OR\s+REPLACE\s+INTO\s+(?P<table>\{[^}]+\}|[A-Za-z_][\w\.]*)\s*"
    r"(?:\((?P<cols>[^)]*)\))?",
    re.IGNORECASE | re.DOTALL,
)

# The tables we need for the column map (the rest will be added if found)
SCHEMA_TABLES = (
    "logs", "printer_counters", "cartridge_state", "cartridge_yield_profiles",
    "yield_samples", "toner_snapshots_v2", "toner_history", "sensor_readings",
    "security_events",
)


# Code scanning
def _iter_py_files():
    """Yield the Python files under the search directories."""
    for d in SEARCH_DIRS:
        for root, _dirs, files in os.walk(os.path.join(REPO_ROOT, d)):
            if "__pycache__" in root:
                continue
            for f in files:
                if f.endswith(".py"):
                    yield os.path.join(root, f)
    for f in ROOT_FILES:
        p = os.path.join(REPO_ROOT, f)
        if os.path.exists(p):
            yield p


def _string_of(node) -> str:
    """string value of an AST node; For f-string only constant parts."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return ""


def _call_name(node) -> str:
    """Return the function name of a call node."""
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    return getattr(f, "id", "")


def _parse_sql(sql: str, dynamic: bool) -> dict:
    """Extracts the named table and columns from the SQL statement."""
    m = _SQL_RE.search(sql)
    if not m:
        return {"table": "?", "columns": [], "dynamic": dynamic, "named": False}
    table = m.group("table").strip()
    cols_raw = (m.group("cols") or "").strip()
    cols = [c.strip().strip('`"[]') for c in cols_raw.split(",") if c.strip()]
    return {
        "table": table,
        "columns": cols,
        "named": bool(cols),
        "dynamic": dynamic or table.startswith("{"),
    }


def scan_file(path: str) -> list:
    """Find INSERT OR REPLACE statements in one source file."""

    try:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
    except OSError as exc:
        return [{"file": path, "line": 0, "error": str(exc)}]
    if PHRASE not in src:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return [{"file": path, "line": exc.lineno or 0, "error": f"SyntaxError: {exc}"}]

    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name not in ("execute", "executescript", "executemany"):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        sql = _string_of(arg)
        dynamic = isinstance(arg, ast.JoinedStr) or not sql
        if PHRASE not in sql and not dynamic:
            continue
        # Unexpected variable (like f"{verb} INTO tbl...") where the sentence is constructed elsewhere
        if PHRASE not in sql and dynamic:
            continue
        info = _parse_sql(sql, dynamic)
        info.update({
            "file": os.path.relpath(path, REPO_ROOT).replace(os.sep, "/"),
            "line": node.lineno,
            "api": name,
            "sql": " ".join(sql.split())[:200],
        })
        findings.append(info)
    return findings


def scan_assigned_verbs() -> list:
    """Finds the places where the sentence is made as a variable (like import_db)."""
    out = []
    for path in _iter_py_files():
        if os.path.abspath(path) == SELF:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
        except OSError:
            continue
        if PHRASE not in src:
            continue
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                    and node.value.value == PHRASE):
                continue
            out.append({
                "file": os.path.relpath(path, REPO_ROOT).replace(os.sep, "/"),
                "line": node.lineno,
                "table": "پویا (از متغیر)",
                "columns": [],
                "named": False,
                "dynamic": True,
                "api": "assign",
                "sql": f'{ast.unparse(node.targets[0])} = "{PHRASE}"',
            })
    return out


# Map of the columns
def build_schema_map() -> dict:
    """Creates a temporary DB and returns the columns of each table (read-only for the repository)."""
    schema: dict[str, list[str]] = {}
    tmp = tempfile.mkdtemp(prefix="pg-ior-schema-")
    old = os.getcwd()
    sys.path.insert(0, REPO_ROOT)
    try:
        os.chdir(tmp)
        import core.database as database           # noqa: PLC0415
        from core.yield_engine import ensure_yield_tables  # noqa: PLC0415
        database.init_db()
        ensure_yield_tables()
        try:
            from core.security_audit import init_security_audit  # noqa: PLC0415
            init_security_audit()
        except Exception:  # noqa: BLE001 - Optional
            pass
        conn = sqlite3.connect(os.path.join(tmp, "logs.db"))
        try:
            for t in SCHEMA_TABLES:
                try:
                    rows = conn.execute(f"PRAGMA table_info({t})").fetchall()
                    if rows:
                        schema[t] = [r[1] for r in rows]
                except sqlite3.Error:
                    continue
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - Tool must be invincible
        schema["__error__"] = [str(exc)]
    finally:
        os.chdir(old)
    return schema


def enrich(findings: list, schema: dict) -> None:
    """Add schema information to each finding."""

    for f in findings:
        cols = f.get("columns") or []
        table = f.get("table") or ""
        all_cols = schema.get(table)
        f["table_columns"] = all_cols or []
        if all_cols and cols:
            f["missing_columns"] = [c for c in all_cols if c not in cols]
        else:
            f["missing_columns"] = None      # dynamic/unknown


def decide(f: dict) -> str:
    """Explicit decision for each location (criterion E.0.4: no row is left undecided)."""
    if f.get("dynamic"):
        return "بازبینی دستی (SQL پویا)"
    all_cols = f.get("table_columns") or []
    cols = f.get("columns") or []
    if not all_cols or not cols:
        return "بازبینی دستی (ستون‌ها نامشخص)"
    missing = f.get("missing_columns") or []
    if not missing:
        return "semantics یکسان"
    return ("ON CONFLICT DO UPDATE با همهٔ ستون‌ها"
            if len(missing) == 1 else "نیاز به بازبینی")


def print_report(findings: list, schema: dict, verbose=True) -> int:
    """Print the INSERT OR REPLACE findings and return an exit code."""

    print("=" * 78)
    print("فهرست INSERT OR REPLACE — فاز E.0.4 (فقط خواندن کد + DB موقت برای نقشه)")
    print("=" * 78)
    if schema.get("__error__"):
        print(f"[WARN] نقشهٔ ستون‌ها ساخته نشد: {schema['__error__'][0]}")
        print("       ⇒ ستون‌های غایب نامعلوم‌اند؛ با --no-schema هم می‌توان اجرا کرد.")
    unknown = 0
    for f in findings:
        if "error" in f:
            print(f"[FAIL] {f['file']}: {f['error']}")
            unknown += 1
            continue
        cols = f.get("columns") or []
        all_cols = f.get("table_columns") or []
        missing = f.get("missing_columns")
        print(f"\n▸ {f['file']}:{f['line']}  ← {f['api']}()")
        print(f"   جدول: {f['table']}"
              + (f" ({len(all_cols)} ستون)" if all_cols else " (نقشه نامعلوم)"))
        if cols:
            print(f"   ستون‌های نوشته‌شده ({len(cols)}): {', '.join(cols)}")
        if missing:
            print(f"   ستون‌های غایب ({len(missing)}): {', '.join(missing)}")
        elif missing == []:
            print("   ستون‌های غایب: هیچ (همه‌ی ستون‌ها نوشته می‌شوند)")
        print(f"   تصمیم: {f['decision']}")
        if verbose:
            print(f"   SQL: {f.get('sql', '')[:170]}")
    n_review = sum(1 for f in findings if "بازبینی" in f.get("decision", ""))
    print("\n" + "-" * 78)
    print(f"جمع: {len(findings)} محل | نیاز به بازبینی دستی: {n_review}")
    print("-" * 78)
    return 2 if (n_review or unknown) else 0


def to_markdown(findings: list) -> str:
    """Render the findings as a Markdown table."""

    lines = [
        "| فایل:خط | API | جدول | ستون‌های نوشته‌شده | ستون‌های جدول | ستون‌های غایب | تصمیم |",
        "|---|---|---|---|---|---|---|",
    ]
    for f in findings:
        cols = ", ".join(f.get("columns") or []) or "—"
        allc = ", ".join(f.get("table_columns") or []) or "—"
        missing = f.get("missing_columns")
        miss = "—" if missing is None else (", ".join(missing) or "هیچ")
        lines.append(f"| `{f['file']}:{f.get('line', '?')}` | `{f.get('api', '?')}` | "
                     f"`{f.get('table', '?')}` | {cols} | {allc} | {miss} | "
                     f"{f.get('decision', '')} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    """Command-line entry point of the INSERT OR REPLACE scan."""

    ap = argparse.ArgumentParser(description="فهرست INSERT OR REPLACE — E.0.4")
    ap.add_argument("--md", default="", help="نوشتن جدول Markdown")
    ap.add_argument("--json", dest="json_path", default="", help="نوشتن خروجی ماشینی")
    ap.add_argument("--no-schema", action="store_true",
                    help="بدون ساخت DB موقت (ستون‌های جدول نامعلوم می‌مانند)")
    ap.add_argument("--quiet", action="store_true", help="بدون چاپ SQL")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    findings = []
    for path in _iter_py_files():
        if os.path.abspath(path) == SELF:
            continue
        findings += scan_file(path)
    findings += scan_assigned_verbs()
    findings.sort(key=lambda f: (f["file"], f.get("line", 0)))

    schema = {} if args.no_schema else build_schema_map()
    enrich(findings, schema)
    for f in findings:
        f["decision"] = decide(f)

    code = print_report(findings, schema, verbose=not args.quiet)

    if args.md:
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(to_markdown(findings))
        print(f"[INFO] جدول Markdown نوشته شد: {args.md}")
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump({"schema": schema, "findings": findings}, fh,
                      ensure_ascii=False, indent=2)
        print(f"[INFO] خروجی JSON نوشته شد: {args.json_path}")
    return code


if __name__ == "__main__":
    sys.exit(main())
