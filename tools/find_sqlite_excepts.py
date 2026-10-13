#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""فهرست `except sqlite3.*` و معادل پستگرس هر مورد — گام E.0.5.

چرا؟ پس از مهاجرت، استثناهای `sqlite3.*` دیگر پرتاب نمی‌شوند (یا اگر شوند،
نشانه‌ی اشتباه است). اگر کد به‌جای نوع خطای مشترکِ لایه‌ی داده به `sqlite3.Error`
وابسته بماند، یک خطای پستگرس از دستِ همان `except` رد می‌شود و به کاربر ۵۰۰
می‌رسد. این ابزار هر محلِ مهارِ `sqlite3.*` را با «عملیاتی که داخل try انجام
می‌شود» و «معادل پستگرس» فهرست می‌کند تا مهاجرت آگاهانه باشد.

سیاست این پروژه (فاز D): خطاهای لایه‌ی داده با انواع مشترک
(`core.db.DatabaseError` / `DatabaseConnectionError`) بالا می‌آیند؛ ترجمه‌ی
نوعِ خطا **فقط** کار adapter است. پس:
  • استثناهای مهارشده در خودِ adapter (``core/db/connection.py``) و محل
    ``sqlite3.connect`` خارج از دامنه‌ی این فهرست‌اند (با علامت ثبت می‌شوند).
  • محلی که SQL خام اجرا می‌کند و `sqlite3.OperationalError` را برای «جدول/ستون
    وجود ندارد» می‌گیرد، در پستگرس به `psycopg.errors.UndefinedTable/Column`
    نگاشت می‌شود؛ برای `ALTER TABLE` تکراری به `DuplicateColumn`.

اجرا:

    python tools/find_sqlite_excepts.py
    python tools/find_sqlite_excepts.py --md /tmp/exc.md --json /tmp/exc.json
    python tools/find_sqlite_excepts.py --include-adapter    # ثبت ردیف‌های adapter

کدهای خروج: 0 = همه‌ی ردیف‌ها تصمیم قطعی دارند، 2 = «نیاز به بازبینی» هست، 1 = خطا.
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

# نگاشت پیشنهادی: sqlite3 → psycopg (psycopg 3)
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


# ────────────────────────────── کمکی‌های AST ────────────────────────────────
def _dotted(node) -> str:
    """نام کامل یک گره (``sqlite3.OperationalError``)."""
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Tuple):
        return ", ".join(_dotted(e) for e in node.elts)
    return ast.unparse(node)


def _is_sqlite_exc(node) -> bool:
    """آیا این نام/گره، استثنای sqlite3 است؟ (هم گره AST هم رشته را می‌پذیرد)"""
    name = node if isinstance(node, str) else _dotted(node)
    return name.startswith("sqlite3.") or name in {
        "OperationalError", "IntegrityError", "DatabaseError", "ProgrammingError",
        "InterfaceError", "NotSupportedError", "Error",
    }


def _sql_literal(node) -> str:
    """رشته‌ی SQL یک گره (ثابت یا f-string با بخش‌های ثابت)."""
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
    """توصیف کوتاه یک فراخوانی: نام + SQL/متن اول."""
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
    """عملیات داخل try: فراخوانی‌ها، SQLها و ویژگی‌های پرکاربرد.

    ``raw`` = متن خام همان محدوده (برای SQLهای پویا مثل f-string یا متغیر؛
    اسکن AST به‌تنهایی ALTER/SELECT پویا را نمی‌بیند).
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
    # ALTERهای پنهان: داخل حلقه (متن حلقه) یا در تابع کمکی (تک‌منبع ستون‌ها)
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
    """رفتار handler: pass / log / raise / return / غیره + بررسی پیام خطا.

    تشخیص «بررسی پیام» فقط از ثابت‌های رشته‌ایِ *داخل خود handler* می‌آید
    (نه اسکن متنی) تا مثلاً کلید ``res["exists"]`` در همسایه اشتباه گرفته نشود.
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
    # بررسی پیام خطا (مثل: if "locked" not in str(exc)) — اختصاصی SQLite
    def _involves_exc(node, alias: str) -> bool:
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
    """نقشه‌ی «نود ← نام تابع/کلاس دربرگیرنده»."""
    out = {}

    def walk(node, stack):
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
    """نقشه‌ی «فرزند ← والد» برای بالا رفتن در درخت."""
    out = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[child] = node
    return out


def _enclosing_for(parents: dict, node: ast.AST):
    """نزدیک‌ترین حلقهٔ ``for`` دربرگیرنده (اگر داخل همان تابع باشد)."""
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.For, ast.AsyncFor)):
            return cur
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None
        cur = parents.get(cur)
    return None


def _alter_columns_in_source(text: str) -> list:
    """ستون‌های ALTER TABLE ADD COLUMN از یک متن پایتون."""
    return sorted({m.group("col").strip().strip('"\'') for m in _ALTER_RE.finditer(text)})


def _resolve_names(tree, source: str, name: str, depth: int = 0) -> list:
    """نام ستون‌ها را از یک ثابت ماژول بیرون می‌کشد.

    دو شکل رایج: ``X = (("col", "TEXT"), ...)`` و ``X = A + B``.
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
    """اگر حلقه روی خروجی تابعی مثل ``_counter_column_alters()`` بچرخد،
    ستون‌های ALTER را از بدنهٔ همان تابع بیرون می‌کشد (تک‌منبع ستون‌ها).

    الگوی دو‌مرحله‌ای: ``for name, decl in _COUNTER_COLUMNS`` داخل تابع کمکی
    ⇒ خودِ ``_COUNTER_COLUMNS`` (و ``A + B``هایش) تا نام ستون‌ها باز می‌شود.
    """
    for node in ast.walk(tree):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            continue
        cols = _alter_columns_in_source(ast.get_source_segment(source, node) or "")
        cols = [c for c in cols if "{" not in c]          # الگوهای f-string را رد کن
        # نام‌ها را از متغیر حلقه‌ی درون تابع کمکی باز کن
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


# ─────────────────────────────── اسکن فایل‌ها ────────────────────────────────
def _iter_py_files():
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


# ───────────────────────────────── تصمیم‌ها ──────────────────────────────────
def decide(f: dict) -> tuple:
    """(تصمیم, معادل پستگرس, یادداشت) — قاعده‌محور و قابل بازبینی."""
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

    # ۱) ALTER TABLE تکراری (ثابت، داخل حلقه یا از تابع تک‌منبع) ⇒ ستون هست
    alter_cols = ops["alter_columns"] or ops.get("hidden_alter_columns") or []
    if alter_cols or ops["has_alter_dynamic"]:
        target = (f" روی {', '.join(alter_cols)}" if alter_cols
                  else " (ALTER پویا، از متغیر)")
        note = " | ".join(note_parts)
        if ops.get("hidden_alter_columns"):
            note = (" | ".join([note, "ALTER داخل حلقه/تابع تک‌منبع ستون‌ها"]).strip(" |"))
        return ("معادل دقیق psycopg", "psycopg.errors.DuplicateColumn" + target, note)

    # ۲) PRAGMA ⇒ معادل ندارد؛ کار adapter
    if ops["pragmas"]:
        prag = ", ".join(ops["pragmas"])
        return ("معادل دقیق psycopg",
                f"PRAGMA({prag}) در پستگرس معادل ندارد؛ adapter باید به no-op "
                "تبدیل کند", " | ".join(note_parts))

    # ۳) IntegrityError ⇒ یکتایی
    if short == "IntegrityError":
        return ("معادل دقیق psycopg", "psycopg.errors.UniqueViolation",
                " | ".join(note_parts))

    # ۴) خواندن از جدول/ستونی که ممکن است نباشد (یا SQL پویا)
    if short == "OperationalError" and (
            any(_SELECT_RE.search(x) for x in ops["sql"]) or ops["has_select_dynamic"]
            or ops.get("dynamic_sql_vars")):
        note = " | ".join(note_parts)
        if ops.get("dynamic_sql_vars"):
            note = (" | ".join([note, "SQL پویا: "
                                + ", ".join(ops["dynamic_sql_vars"])]).strip(" |"))
        return ("معادل دقیق psycopg",
                "psycopg.errors.UndefinedTable / UndefinedColumn", note)

    # ۵) خطای عمومی
    if short in ("DatabaseError", "Error"):
        return ("تبدیل به DatabaseError مشترک", "core.db.DatabaseError (مشترک)",
                " | ".join(note_parts))

    # ۶) OperationalError بدون نشانه‌ی روشن (مثل ساخت/دسترسی به DB)
    if short == "OperationalError":
        return ("معادل دقیق psycopg", "psycopg.OperationalError",
                " | ".join(note_parts))

    why = f"نوع {exc} قاعدهٔ قطعی ندارد"
    return ("نیاز به بازبینی", PSYCOPG_MAP.get(short, "؟"),
            " | ".join(note_parts + [why]))


# ──────────────────────────────── گزارش ─────────────────────────────────────
def print_report(findings: list, verbose=True) -> int:
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
