#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/find_sqlite_excepts.py
# @brief:        Lists 'except sqlite3.*' handlers and their PostgreSQL equivalents (step E.0.5).
# @details:      Responsibilities: Scan the source and print a decision report.
#                Main API: scan_file(), decide(), print_report(), to_markdown(), main()
#                Side effects: file I/O
# @dependencies: stdlib only
# @notes:        Can be run directly as a script.
# =============================================================================

"""list `except sqlite3.*` and the Postgres equivalent of each item — step E.0.5.

why After migration, `sqlite3.*` exceptions are no longer thrown (or if they are,
is a sign of error). If the code instead of the common error type of the data layer to `sqlite3.Error`
remains dependent, a Postgres error will be thrown from the same `except' and the user will get 500
arrives This tool replaces any ``sqlite3.*'' containment with the ``operations inside try.''
can be” and lists “postgres equivalent” so that the migration is aware.

Policy of this project (Phase D): Data layer errors with common types
(`core.db.DatabaseError` / `DatabaseConnectionError`) raise; Translation of
The type of error is **only** adapter work. So:
  • Caught exceptions in the adapter itself (``core/db/connection.py'') and locale
    ``sqlite3.connect'' are outside the scope of this directory (they are marked with a sign).
  • A place that executes raw SQL and throws `sqlite3.OperationalError` for ``table/column
    does not exist', in Postgres it takes `psycopg.errors.UndefinedTable/Column'
    is mapped; For duplicate `ALTER TABLE` to `DuplicateColumn`.

Execution:

    python tools/find_sqlite_excepts.py
    python tools/find_sqlite_excepts.py --md /tmp/exc.md --json /tmp/exc.json
    python tools/find_sqlite_excepts.py --include-adapter # Register adapter rows

Exit codes: 0 = all rows have a final decision, 2 = "needs revision", 1 = error.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEARCH_DIRS = ("core", "web", "tools", "tests")
ROOT_FILES = ("create_admin.py",)
SELF = os.path.abspath(__file__)
ADAPTER_FILE = "core/db/connection.py"

# Suggested mapping: sqlite3 → psycopg (psycopg 3)
PSYCOPG_MAP = {
    "OperationalError": "psycopg.OperationalError",
    "IntegrityError": "psycopg.errors.UniqueViolation",
    "DatabaseError": "psycopg.DatabaseError",
    "Error": "psycopg.Error",
    "ProgrammingError": "psycopg.errors.ProgrammingError",
    "InterfaceError": "psycopg.InterfaceError",
    "NotSupportedError": "psycopg.errors.FeatureNotSupported",
}

_ALTER_RE = re.compile(r"ALTER\s+TABLE\s+\S+\s+ADD\s+COLUMN\s+(?P<col>\S+)", re.I)
_PRAGMA_RE = re.compile(r"PRAGMA\s+(?P<name>\w+)", re.I)
_SELECT_RE = re.compile(r"SELECT\b", re.I)


# AST Helpers AST Helpers
def _dotted(node) -> str:
    """The full name of a node (``sqlite3.OperationalError'')."""
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Tuple):
        return ", ".join(_dotted(e) for e in node.elts)
    return ast.unparse(node)


def _is_sqlite_exc(node) -> bool:
    """Is this name/node a sqlite3 exception? (Both AST node accepts string)"""
    name = node if isinstance(node, str) else _dotted(node)
    return name.startswith("sqlite3.") or name in {
        "OperationalError", "IntegrityError", "DatabaseError", "ProgrammingError",
        "InterfaceError", "NotSupportedError", "Error",
    }


def _sql_literal(node) -> str:
    """SQL string of a node (fixed or f-string with fixed parts)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                out.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                out.append("{" + ast.unparse(v.value) + "}")
        return "".join(out)
    return ""


def _call_label(node: ast.Call) -> str:
    """Short description of a call: name + SQL/first text."""
    f = node.func
    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
    sql = _sql_literal(node.args[0]) if node.args else ""
    sql = " ".join(sql.split())
    if name == "connect" and sql == "":
        return "sqlite3.connect(...)"
    if name == "execute" and sql:
        return f"execute: {sql[:90]}"
    if name == "executescript" and sql:
        return f"executescript: {sql[:70]}"
    mod = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
    return f"{mod + '.' if mod else ''}{name}(...)"


def _describe_ops(body: list, raw: str = "", loop_raw: str = "",
                  helper_cols: list | None = None) -> dict:
    """Operations inside try: Calls, SQLs and frequently used features.

    ``raw'' = raw text of the same range (for dynamic SQLs such as f-string or variable;
    AST scan alone does not see dynamic ALTER/SELECT).
    """
    ops, sqls, alters, pragmas = [], [], [], []
    calls = 0
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                calls += 1
                ops.append(_call_label(node))
                sql = _sql_literal(node.args[0]) if node.args else ""
                flat = " ".join(sql.split())
                if flat:
                    sqls.append(flat[:120])
                m = _ALTER_RE.search(flat)
                if m:
                    alters.append(m.group("col").strip())
                for pm in _PRAGMA_RE.finditer(flat):
                    pragmas.append(pm.group("name"))
    # Hidden ALTERs: inside loop (loop text) or in helper function (single source columns)
    hidden_alters = _alter_columns_in_source(loop_raw or "")
    for col in (helper_cols or []):
        if col not in hidden_alters:
            hidden_alters.append(col)
    dynamic_sql_vars = []
    for stmt in body:
        for node in ast.walk(stmt):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            f = node.func
            if (isinstance(f, ast.Attribute) and f.attr in ("execute", "executemany")
                    and isinstance(node.args[0], ast.Name)):
                dynamic_sql_vars.append(ast.unparse(node.args[0]))
    raw_all = ((raw or "") + "\n" + (loop_raw or "")).upper()
    return {
        "calls": calls,
        "ops": ops[:6],
        "sql": sqls[:4],
        "alter_columns": sorted(set(alters)),
        "hidden_alter_columns": sorted(set(hidden_alters)),
        "pragmas": sorted(set(pragmas)),
        "dynamic_sql_vars": sorted(set(dynamic_sql_vars)),
        "has_sqlite_connect": any(o.startswith("sqlite3.connect") for o in ops),
        "has_alter_dynamic": ("ALTER TABLE" in raw_all and not alters and not hidden_alters),
        "has_select_dynamic": ("SELECT" in raw_all and not any(_SELECT_RE.search(x) for x in sqls)),
    }


def _describe_handler(handler: ast.ExceptHandler, raw: str = "") -> dict:
    """Handler behavior: pass / log / raise / return / etc + error message checking.

    The "message check" detection comes only from string constants *inside the handler*
    (not a text scan) so that, for example, the key ``res["exists"]'' is not confused in the neighbor.
    """
    kinds = set()
    for node in ast.walk(handler):
        if isinstance(node, ast.Pass):
            kinds.add("pass")
        elif isinstance(node, ast.Raise):
            kinds.add("raise")
        elif isinstance(node, ast.Return):
            kinds.add("return")
        elif isinstance(node, ast.Call):
            f = node.func
            nm = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if nm in ("warning", "error", "exception", "info", "debug", "critical"):
                kinds.add("log")
            if nm in ("append", "setdefault", "jsonify", "fail", "assertEqual",
                      "assertNotIn", "assertIn", "skipTest"):
                kinds.add(nm)
    if any(isinstance(n, ast.Continue) for n in ast.walk(handler)):
        kinds.add("continue")
    # Error message checking (eg: if "locked" not in str(exc)) — SQLite specific
    def _involves_exc(node, alias: str) -> bool:
        """Return True when the exception alias is used inside a node."""
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id == alias:
                return True
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id == "str"):
                return True
        return False

    alias = handler.name or ""
    msg_checks = []
    for node in ast.walk(handler):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left] + list(node.comparators)
        if not any(_involves_exc(o, alias) for o in operands):
            continue
        for o in operands:
            for sub in ast.walk(o):
                if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                        and sub.value.strip() and len(sub.value) < 40):
                    msg_checks.append(sub.value.strip().lower())
    return {"kinds": sorted(kinds), "lines": handler.body[0].lineno,
            "message_checks": sorted(set(msg_checks))}


def _enclosing_map(tree: ast.AST) -> dict:
    """Map "node → name of enclosing function/class"."""
    out = {}

    def walk(node, stack):
        """Walk the AST and record exception handlers together with their enclosing scope."""
        for child in ast.iter_child_nodes(node):
            name = stack
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = stack + [child.name]
            out[child] = ".".join(stack) or "<module>"
            walk(child, name)

    out[tree] = "<module>"
    walk(tree, [])
    return out


def _parents(tree: ast.AST) -> dict:
    """"Child → Parent" map for climbing the tree."""
    out = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[child] = node
    return out


def _enclosing_for(parents: dict, node: ast.AST):
    """The closest enclosing ``for'' loop (if inside the same function)."""
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.For, ast.AsyncFor)):
            return cur
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None
        cur = parents.get(cur)
    return None


def _alter_columns_in_source(text: str) -> list:
    """ALTER TABLE ADD COLUMN columns from a Python script."""
    return sorted({m.group("col").strip().strip('"\'') for m in _ALTER_RE.finditer(text)})


def _resolve_names(tree, source: str, name: str, depth: int = 0) -> list:
    """Pulls column names from a module constant.

    Two common forms: ``X = (("col", "TEXT"),...)'' and ``X = A + B''.
    """
    if depth > 4:
        return []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        tgt = node.targets[0]
        if not (isinstance(tgt, ast.Name) and tgt.id == name):
            continue
        value = node.value
        if isinstance(value, ast.Tuple):
            out = []
            for elt in value.elts:
                if isinstance(elt, ast.Tuple) and elt.elts:
                    first = elt.elts[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        out.append(first.value)
                elif isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    out.append(elt.value)
            return out
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            out = []
            for side in (value.left, value.right):
                if isinstance(side, ast.Name):
                    out += _resolve_names(tree, source, side.id, depth + 1)
            return out
    return []


def _helper_alter_columns(tree, source: str, name: str) -> list:
    """If the loop spins on the output of a function like ``_counter_column_alters()'',
    ALTER pulls columns from the body of the same function (single-source columns).

    Two-step pattern: ``for name, decl in _COUNTER_COLUMNS'' inside helper function
    ⇒ ``_COUNTER_COLUMNS'' itself (and its ``A + B's'') expands to column names.
    """
    for node in ast.walk(tree):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            continue
        cols = _alter_columns_in_source(ast.get_source_segment(source, node) or "")
        cols = [c for c in cols if "{" not in c]          # Reject f-string patterns
        # Unwrap the names from the loop variable inside the helper function
        for sub in ast.walk(node):
            if isinstance(sub, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
                for gen in sub.generators:
                    if isinstance(gen.iter, ast.Name):
                        cols += _resolve_names(tree, source, gen.iter.id)
            if isinstance(sub, ast.For) and isinstance(sub.iter, ast.Name):
                cols += _resolve_names(tree, source, sub.iter.id)
        if cols:
            return sorted(set(cols))
    return []


# - Scan files - Scan files
def _iter_py_files():
    """Yield the Python files under the search directories."""
    for d in SEARCH_DIRS:
        for root, _dirs, files in os.walk(os.path.join(REPO_ROOT, d)):
            if "__pycache__" in root:
                continue
            for f in sorted(files):
                if f.endswith(".py"):
                    yield os.path.join(root, f)
    for f in ROOT_FILES:
        p = os.path.join(REPO_ROOT, f)
        if os.path.exists(p):
            yield p


def scan_file(path: str) -> list:
    """Find sqlite3 exception handlers in one source file."""

    rel = os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")
    try:
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        lines = source.splitlines()
        tree = ast.parse(source)
    except (OSError, SyntaxError) as exc:
        return [{"file": rel, "line": 0, "error": str(exc)}]

    owners = _enclosing_map(tree)
    parents = _parents(tree)
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        loop = _enclosing_for(parents, node)
        loop_raw = ""
        helper_cols = []
        if loop is not None:
            loop_raw = ast.get_source_segment(source, loop) or ""
            iterable = loop.iter
            if isinstance(iterable, ast.Call) and isinstance(iterable.func, ast.Name):
                helper_cols = _helper_alter_columns(tree, source, iterable.func.id)
        for handler in node.handlers:
            if handler.type is None:
                continue
            names = [n.strip() for n in _dotted(handler.type).split(",")]
            sqlite_names = [n for n in names if _is_sqlite_exc(n) or n.startswith("sqlite3.")]
            if not sqlite_names:
                continue
            body_src = "\n".join(lines[node.lineno - 1:handler.lineno - 1])
            handler_src = "\n".join(lines[handler.lineno - 1:
                                         (handler.end_lineno or handler.lineno)])
            findings.append({
                "file": rel,
                "line": handler.lineno,
                "try_line": node.lineno,
                "owner": owners.get(node, "?"),
                "exceptions": sqlite_names,
                "ops": _describe_ops(node.body, body_src, loop_raw, helper_cols),
                "handler": _describe_handler(handler, handler_src),
            })
    return findings


# Decisions
def decide(f: dict) -> tuple:
    """(decision, postgres equivalent, note) — rule-based and reviewable."""
    if f["file"] == ADAPTER_FILE:
        return ("خارج از دامنه (adapter)",
                "— (ترجمه نوع خطا کار adapter است)", "")

    exc = f["exceptions"][0]
    ops = f["ops"]
    checks = f.get("handler", {}).get("message_checks") or []
    short = exc.split(".")[-1]
    note_parts = []
    if checks:
        note_parts.append(
            "بررسی پیام خطا (" + ", ".join(f'"{c}"' for c in checks) +
            ") اختصاصی SQLite است؛ در پستگرس باید با نوع/SQLSTATE جایگزین شود "
            "(psycopg: exc.sqlstate)")

    # 1) Iterative ALTER TABLE (fixed, inside a loop or from a single source function) ⇒ there is a column
    alter_cols = ops["alter_columns"] or ops.get("hidden_alter_columns") or []
    if alter_cols or ops["has_alter_dynamic"]:
        target = (f" روی {', '.join(alter_cols)}" if alter_cols
                  else " (ALTER پویا، از متغیر)")
        note = " | ".join(note_parts)
        if ops.get("hidden_alter_columns"):
            note = (" | ".join([note, "ALTER داخل حلقه/تابع تک‌منبع ستون‌ها"]).strip(" |"))
        return ("معادل دقیق psycopg", "psycopg.errors.DuplicateColumn" + target, note)

    # 2) PRAGMA ⇒ has no equivalent; work adapter
    if ops["pragmas"]:
        prag = ", ".join(ops["pragmas"])
        return ("معادل دقیق psycopg",
                f"PRAGMA({prag}) در پستگرس معادل ندارد؛ adapter باید به no-op "
                "تبدیل کند", " | ".join(note_parts))

    # 3) IntegrityError ⇒ uniqueness
    if short == "IntegrityError":
        return ("معادل دقیق psycopg", "psycopg.errors.UniqueViolation",
                " | ".join(note_parts))

    # 4) Reading from a table/column that may not exist (or dynamic SQL)
    if short == "OperationalError" and (
            any(_SELECT_RE.search(x) for x in ops["sql"]) or ops["has_select_dynamic"]
            or ops.get("dynamic_sql_vars")):
        note = " | ".join(note_parts)
        if ops.get("dynamic_sql_vars"):
            note = (" | ".join([note, "SQL پویا: "
                                + ", ".join(ops["dynamic_sql_vars"])]).strip(" |"))
        return ("معادل دقیق psycopg",
                "psycopg.errors.UndefinedTable / UndefinedColumn", note)

    # 5) General error
    if short in ("DatabaseError", "Error"):
        return ("تبدیل به DatabaseError مشترک", "core.db.DatabaseError (مشترک)",
                " | ".join(note_parts))

    # 6) OperationalError without a clear sign (such as creating/accessing the DB)
    if short == "OperationalError":
        return ("معادل دقیق psycopg", "psycopg.OperationalError",
                " | ".join(note_parts))

    why = f"نوع {exc} قاعدهٔ قطعی ندارد"
    return ("نیاز به بازبینی", PSYCOPG_MAP.get(short, "؟"),
            " | ".join(note_parts + [why]))


# Report
def print_report(findings: list, verbose=True) -> int:
    """Print the sqlite3 exception findings and return an exit code."""

    print("=" * 80)
    print("فهرست except sqlite3.* — فاز E.0.5 (اسکن AST، بدون تغییر هیچ فایلی)")
    print("=" * 80)
    dev = [f for f in findings if f["file"] != ADAPTER_FILE]
    adapter = [f for f in findings if f["file"] == ADAPTER_FILE]
    review = 0
    for f in dev + adapter:
        if "error" in f:
            print(f"[FAIL] {f['file']}: {f['error']}")
            review += 1
            continue
        exc = ", ".join(f["exceptions"])
        if f.get("decision") and "بازبینی" in f["decision"]:
            review += 1
        print(f"\n▸ {f['file']}:{f['line']}   except {exc}"
              + (f"   [در تابع {f['owner']}]" if f["owner"] != "<module>" else ""))
        print(f"   try خط {f['try_line']} | {f['ops']['calls']} فراخوانی"
              + (f" | PRAGMA: {', '.join(f['ops']['pragmas'])}" if f["ops"]["pragmas"] else "")
              + (f" | ALTERهای تکراری: {', '.join(f['ops']['alter_columns'])}"
                 if f["ops"]["alter_columns"] else ""))
        if verbose:
            for op in f["ops"]["ops"][:4]:
                print(f"     · {op}")
        print(f"   رفتار handler: {', '.join(f['handler']['kinds']) or '—'}")
        print(f"   معادل PostgreSQL: {f['pg']}")
        print(f"   تصمیم فاز E: {f['decision']}")
        if f.get("note"):
            print(f"   یادداشت: {f['note']}")
    print("\n" + "-" * 80)
    print(f"جمع: {len(findings)} محل (کد برنامه/ابزار/تست: {len(dev)}، "
          f"adapter: {len(adapter)}) | نیاز به بازبینی: {review}")
    print("-" * 80)
    return 2 if review else 0


def to_markdown(findings: list) -> str:
    """Render the sqlite3 exception findings as a Markdown table."""

    lines = ["| فایل:خط | exception | عملیات داخل try | معادل PostgreSQL | تصمیم فاز E |",
             "|---|---|---|---|---|"]
    for f in findings:
        ops = f.get("ops", {})
        parts = []
        if ops.get("alter_columns"):
            parts.append(f"ALTER TABLE: {', '.join(ops['alter_columns'])}")
        if ops.get("pragmas"):
            parts.append(f"PRAGMA {', '.join(ops['pragmas'])}")
        parts += [o.replace("|", "/") for o in ops.get("ops", [])[:2]]
        lines.append(f"| `{f['file']}:{f.get('line', '?')}` | "
                     f"`{', '.join(f.get('exceptions', []))}` | "
                     f"{'; '.join(parts) or '—'} | {f.get('pg', '—')} | "
                     f"{f.get('decision', f.get('error', '—'))} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    """Command-line entry point of the sqlite3 exception scan."""

    ap = argparse.ArgumentParser(description="فهرست except sqlite3.* — E.0.5")
    ap.add_argument("--md", default="", help="نوشتن جدول Markdown")
    ap.add_argument("--json", dest="json_path", default="", help="خروجی ماشینی")
    ap.add_argument("--include-adapter", action="store_true",
                    help="نمایش ردیف‌های adapter در فهرست (پیش‌فرض: فقط علامت‌گذاری)")
    ap.add_argument("--quiet", action="store_true", help="بدون جزئیات عملیات")
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
    findings.sort(key=lambda f: (f["file"], f.get("line", 0)))
    for f in findings:
        if "error" in f:
            f["decision"], f["pg"], f["note"] = "—", "—", ""
        else:
            f["decision"], f["pg"], f["note"] = decide(f)

    shown = findings if args.include_adapter else findings
    code = print_report(shown, verbose=not args.quiet)

    if args.md:
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(to_markdown(shown))
        print(f"[INFO] جدول Markdown نوشته شد: {args.md}")
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(findings, fh, ensure_ascii=False, indent=2)
        print(f"[INFO] خروجی JSON نوشته شد: {args.json_path}")
    return code


if __name__ == "__main__":
    sys.exit(main())
